"""Focused coverage for the site-scoped, bounded full-cycle job."""

import asyncio
import importlib
import json

import pytest
from sqlalchemy import select

from app import worker
from app.models import Candidate, Connection, Event, Job, Page
from test_platform import platform  # Reuse the existing authenticated fixture.


def _run_job(worker_job_id):
    return worker.run_job(worker_job_id)


def _prepare_metadata_fixture(client, factory, site_id, *, enabled=True, paused=False):
    response = client.patch(
        f"/api/v1/sites/{site_id}",
        json={"paused": paused},
    )
    assert response.status_code == 200, response.text
    response = client.put(
        f"/api/v1/sites/{site_id}/policy",
        json={
            "settings": {
                "enabled": enabled,
                "allowed_actions": ["metadata"],
                "protected_paths": [],
            },
        },
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        page = Page(
            site_id=site_id,
            resource_key="posts:governed-fixture",
            resource_type="posts",
            url="https://example.test/repair",
            title="Repair preparation",
            source={
                "metadata": {
                    "seo": {
                        "forgeseo": {
                            "title": "Current title",
                            "description": "Existing description for the isolated fixture page.",
                        },
                    },
                },
            },
            source_hash="governed-fixture-source",
        )
        db.add(page)
        db.flush()
        safe = Candidate(
            site_id=site_id,
            page_id=page.id,
            field="seo_title",
            before_value="Current title",
            after_value="Complete repair preparation guide",
            source_hash=page.source_hash,
            status="pending",
            details={},
        )
        review_gated = Candidate(
            site_id=site_id,
            page_id=page.id,
            field="meta_description",
            before_value="Existing description for the isolated fixture page.",
            after_value="A complete repair preparation description for the isolated fixture page.",
            source_hash=page.source_hash,
            status="pending",
            details={"review_only_reasons": ["owner_review_required"]},
        )
        db.add_all([
            safe,
            review_gated,
            Connection(
                site_id=site_id,
                kind="wordpress",
                encrypted_credentials="opaque-test-secret",
                status="connected",
                capabilities={
                    "authenticated": True,
                    "seo": {
                        "write": True,
                        "writable_fields": ["title", "description"],
                    },
                },
            ),
        ])
        db.commit()
        return safe.id, review_gated.id


def _mock_non_audit_stages(monkeypatch, workflows):
    async def fake_stage(_db, _site, stage_job):
        return {"complete": True, "observed_stage": stage_job.payload["full_cycle_stage"]}

    for handler_name in ("availability", "inventory", "plan", "refresh"):
        monkeypatch.setitem(workflows.HANDLERS, handler_name, fake_stage)


def _mock_public_audit(monkeypatch):
    audit_module = importlib.import_module("app.intelligence.audit")

    async def fake_crawl(origin, **_kwargs):
        return {
            "complete": True,
            "pages": [{
                "url": origin + "/repair",
                "status_code": 200,
                "html": (
                    "<html><head><title>Current title</title>"
                    "<meta name='description' content='Existing description for the isolated fixture page.'>"
                    "</head><body><h1>Repair preparation</h1>"
                    "<p>Useful repair preparation instructions for the isolated fixture page.</p>"
                    "</body></html>"
                ),
            }],
            "pending_urls": [],
            "visited_urls": [origin + "/repair"],
            "errors": [],
        }

    monkeypatch.setattr(audit_module, "crawl", fake_crawl)


def test_full_cycle_job_is_accepted_and_registered(platform):
    client, _factory, site_id = platform

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "payload": {"max_pages": 4},
              "idempotency_key": "full-cycle-api-1"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["kind"] == "full_cycle"
    assert body["status"] == "queued"
    assert body["site_id"] == site_id
    from app.workflows import HANDLERS

    assert callable(HANDLERS["full_cycle"])


def test_full_cycle_reports_missing_wordpress_but_runs_public_audit(platform, monkeypatch):
    client, factory, site_id = platform
    workflows = importlib.import_module("app.workflows")
    audit_module = importlib.import_module("app.intelligence.audit")

    async def fake_fetch(_url):
        return {"status_code": 200, "html": ""}

    async def fake_crawl(origin, **_kwargs):
        return {
            "complete": True,
            "pages": [{
                "url": origin + "/",
                "status_code": 200,
                "html": (
                    "<html><head><title>Independent test</title></head>"
                    "<body><h1>Independent test</h1><p>Repairs.</p></body></html>"
                ),
            }],
            "pending_urls": [],
            "visited_urls": [origin + "/"],
            "errors": [],
        }

    monkeypatch.setattr(workflows, "fetch", fake_fetch)
    monkeypatch.setattr(audit_module, "crawl", fake_crawl)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "idempotency_key": "full-cycle-no-wp"},
    )
    assert response.status_code == 202, response.text
    parent_id = response.json()["id"]

    result = _run_job(parent_id)

    assert result["workflow"] == "full_cycle"
    assert result["complete"] is False
    stages = {stage["name"]: stage for stage in result["stages"]}
    assert stages["inventory"]["status"] == "needs_connection"
    assert stages["inventory"]["reason"] == "verified_wordpress_connection_required"
    assert stages["public_audit"]["status"] == "complete"
    assert stages["public_audit"]["result"]["checked_pages"] == 1
    assert {stage["status"] for name, stage in stages.items() if name != "inventory"} == {"complete"}
    assert any(
        action["action"] == "connect_wordpress"
        and action["status"] == "needs_connection"
        for action in result["next_actions"]
    )
    assert json.dumps(result).lower().find("password") == -1

    with factory() as db:
        parent = db.get(Job, parent_id)
        assert parent.status == "partial"
        stage_rows = [
            row for row in db.scalars(select(Job).where(Job.site_id == site_id)).all()
            if isinstance(row.payload, dict)
            and row.payload.get("full_cycle_parent_job_id") == parent_id
        ]
        assert {row.kind for row in stage_rows} == {
            "availability", "inventory", "public_audit", "content_plan", "refresh_evaluation",
        }
        event_kinds = [
            row.kind for row in db.scalars(
                select(Event).where(Event.site_id == site_id)
            ).all()
        ]
        assert "full_cycle_started" in event_kinds
        assert "full_cycle_finished" in event_kinds


def test_full_cycle_runs_all_mocked_stages_and_persists_stable_stage_jobs(platform, monkeypatch):
    client, factory, site_id = platform
    workflows = importlib.import_module("app.workflows")
    calls = []

    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="opaque-test-secret",
            status="connected",
            capabilities={"authenticated": True},
        ))
        db.commit()

    async def fake_stage(db, site, stage_job):
        calls.append((stage_job.kind, stage_job.id))
        return {"complete": True, "observed_stage": stage_job.payload["full_cycle_stage"]}

    for handler_name in ("availability", "inventory", "audit", "plan", "refresh"):
        monkeypatch.setitem(workflows.HANDLERS, handler_name, fake_stage)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "idempotency_key": "full-cycle-success"},
    )
    assert response.status_code == 202, response.text
    parent_id = response.json()["id"]
    result = _run_job(parent_id)

    assert result["workflow"] == "full_cycle"
    assert result["complete"] is True
    assert [stage["name"] for stage in result["stages"]] == [
        "availability", "inventory", "public_audit", "content_plan", "refresh_evaluation",
    ]
    assert all(stage["status"] == "complete" for stage in result["stages"])
    assert [name for name, _stage_id in calls] == [
        "availability", "inventory", "public_audit", "content_plan", "refresh_evaluation",
    ]
    assert {action["action"] for action in result["next_actions"]} >= {
        "publish", "metadata_writes", "paid_visibility", "remote_mutations",
    }
    assert all(
        action["status"] in {"policy_gated", "not_run"}
        for action in result["next_actions"]
        if action["action"] in {"publish", "metadata_writes", "paid_visibility", "remote_mutations"}
    )

    with factory() as db:
        parent = db.get(Job, parent_id)
        assert parent.status == "complete"
        stage_rows = [
            row for row in db.scalars(select(Job).where(Job.site_id == site_id)).all()
            if isinstance(row.payload, dict)
            and row.payload.get("full_cycle_parent_job_id") == parent_id
        ]
        by_kind = {row.kind: row for row in stage_rows}
        assert set(by_kind) == {
            "availability", "inventory", "public_audit", "content_plan", "refresh_evaluation",
        }
        assert all(len(row.id) == 32 and row.status == "complete" for row in stage_rows)
        assert {
            stage["stage_job_id"] for stage in result["stages"]
        } == {row.id for row in stage_rows}


def test_full_cycle_default_mode_keeps_metadata_read_only(platform, monkeypatch):
    client, factory, site_id = platform
    workflows = importlib.import_module("app.workflows")
    safe_id, review_gated_id = _prepare_metadata_fixture(client, factory, site_id)
    _mock_non_audit_stages(monkeypatch, workflows)
    _mock_public_audit(monkeypatch)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "idempotency_key": "full-cycle-read-only-candidate"},
    )
    assert response.status_code == 202, response.text
    result = _run_job(response.json()["id"])

    assert result["mode"] == "read_only"
    assert result["execution_summary"]["metadata"]["status"] == "not_run"
    assert result["execution_summary"]["metadata"]["gate_blockers"] == ["read_only_mode"]
    with factory() as db:
        safe = db.get(Candidate, safe_id)
        review_gated = db.get(Candidate, review_gated_id)
        candidate_jobs = db.scalars(
            select(Job).where(Job.site_id == site_id, Job.kind == "candidate")
        ).all()
        assert safe.status == "pending"
        assert review_gated.status == "pending"
        assert candidate_jobs == []
        public_audit = db.scalar(
            select(Job).where(
                Job.site_id == site_id,
                Job.kind == "public_audit",
                Job.payload["full_cycle_parent_job_id"].as_string() == response.json()["id"],
            )
        )
        assert public_audit.payload["suppress_automation"] is True


def test_full_cycle_governed_queues_only_authorized_metadata_candidates(platform, monkeypatch):
    from app.config import settings

    client, factory, site_id = platform
    workflows = importlib.import_module("app.workflows")
    safe_id, review_gated_id = _prepare_metadata_fixture(client, factory, site_id)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    _mock_non_audit_stages(monkeypatch, workflows)
    _mock_public_audit(monkeypatch)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "full_cycle",
            "payload": {"mode": "governed", "max_pages": 4},
            "idempotency_key": "full-cycle-governed-candidate",
        },
    )
    assert response.status_code == 202, response.text
    parent_id = response.json()["id"]
    result = _run_job(parent_id)

    assert result["mode"] == "governed"
    metadata = result["execution_summary"]["metadata"]
    assert metadata["authorized_candidate_ids"] == [safe_id]
    assert len(metadata["queued_job_ids"]) == 1
    assert metadata["authorized"] == [{
        "candidate_id": safe_id,
        "job_id": metadata["queued_job_ids"][0],
    }]
    gated = next(
        item for item in metadata["review_gated"]
        if item["candidate_id"] == review_gated_id
    )
    assert gated["reasons"] == ["owner_review_required"]
    actions = {item["action"]: item for item in result["execution_summary"]["review_gated"]}
    assert actions["publish"]["status"] == "review_gated"
    assert actions["paid_visibility"]["status"] == "not_run"
    assert actions["remote_mutations"]["status"] == "not_run"
    assert result["execution_summary"]["content_publishing"] == actions["publish"]
    assert result["execution_summary"]["paid_visibility"] == actions["paid_visibility"]
    assert result["execution_summary"]["remote_mutations"] == actions["remote_mutations"]
    assert json.dumps(result).lower().find("password") == -1

    with factory() as db:
        safe = db.get(Candidate, safe_id)
        review_gated = db.get(Candidate, review_gated_id)
        candidate_jobs = db.scalars(
            select(Job).where(Job.site_id == site_id, Job.kind == "candidate")
        ).all()
        assert safe.status == "approved"
        assert review_gated.status == "pending"
        assert len(candidate_jobs) == 1
        assert candidate_jobs[0].id == metadata["queued_job_ids"][0]
        assert candidate_jobs[0].payload == {"candidate_id": safe_id}
        public_audit = db.scalar(
            select(Job).where(
                Job.site_id == site_id,
                Job.kind == "public_audit",
                Job.payload["full_cycle_parent_job_id"].as_string() == parent_id,
            )
        )
        assert public_audit.payload["full_cycle_mode"] == "governed"
        assert public_audit.payload["suppress_automation"] is False
        assert db.scalars(
            select(Job).where(
                Job.site_id == site_id,
                Job.kind.in_(["generate", "publish", "visibility"]),
            )
        ).all() == []


def test_site_autopilot_reuses_the_policy_governed_metadata_gate(platform, monkeypatch):
    from app.config import settings

    client, factory, site_id = platform
    workflows = importlib.import_module("app.workflows")
    safe_id, review_gated_id = _prepare_metadata_fixture(client, factory, site_id)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    _mock_non_audit_stages(monkeypatch, workflows)
    _mock_public_audit(monkeypatch)

    async def fake_content_autopilot(_db, _site, _stage_job):
        return {
            "workflow": "content_autopilot",
            "status": "gated",
            "complete": False,
            "blockers": ["author_unverified"],
        }

    monkeypatch.setitem(workflows.HANDLERS, "content_autopilot", fake_content_autopilot)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "full_cycle",
            "payload": {"mode": "autopilot", "max_pages": 4},
            "idempotency_key": "full-cycle-autopilot-metadata",
        },
    )
    assert response.status_code == 202, response.text
    parent_id = response.json()["id"]
    result = _run_job(parent_id)

    assert result["mode"] == "autopilot"
    metadata = result["execution_summary"]["metadata"]
    assert metadata["authorized_candidate_ids"] == [safe_id]
    assert len(metadata["queued_job_ids"]) == 1
    gated = next(
        item for item in metadata["review_gated"]
        if item["candidate_id"] == review_gated_id
    )
    assert gated["reasons"] == ["owner_review_required"]
    metadata_action = next(
        item for item in result["next_actions"] if item["action"] == "metadata_writes"
    )
    assert metadata_action["status"] == "queued"
    assert metadata_action["candidate_ids"] == [safe_id]
    assert result["execution_summary"]["content_publishing"]["status"] == "gated"

    with factory() as db:
        safe = db.get(Candidate, safe_id)
        review_gated = db.get(Candidate, review_gated_id)
        candidate_jobs = db.scalars(
            select(Job).where(Job.site_id == site_id, Job.kind == "candidate")
        ).all()
        assert safe.status == "approved"
        assert review_gated.status == "pending"
        assert len(candidate_jobs) == 1
        public_audit = db.scalar(
            select(Job).where(
                Job.site_id == site_id,
                Job.kind == "public_audit",
                Job.payload["full_cycle_parent_job_id"].as_string() == parent_id,
            )
        )
        assert public_audit.payload["full_cycle_mode"] == "governed"
        assert public_audit.payload["suppress_automation"] is False


@pytest.mark.parametrize(
    ("paused", "enabled", "global_pause", "expected_blocker"),
    [
        (True, True, False, "site_paused"),
        (False, False, False, "policy_disabled"),
        (False, True, True, "global_pause"),
    ],
)
def test_full_cycle_governed_pause_or_disabled_policy_queues_no_metadata(
    platform,
    monkeypatch,
    paused,
    enabled,
    global_pause,
    expected_blocker,
):
    from app.config import settings

    client, factory, site_id = platform
    workflows = importlib.import_module("app.workflows")
    safe_id, review_gated_id = _prepare_metadata_fixture(
        client,
        factory,
        site_id,
        enabled=enabled,
        paused=paused,
    )
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", global_pause)
    _mock_non_audit_stages(monkeypatch, workflows)
    _mock_public_audit(monkeypatch)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "full_cycle",
            "payload": {"mode": "governed"},
            "idempotency_key": f"full-cycle-governed-blocked-{expected_blocker}",
        },
    )
    assert response.status_code == 202, response.text
    result = _run_job(response.json()["id"])

    metadata = result["execution_summary"]["metadata"]
    assert metadata["authorized_candidate_ids"] == []
    assert metadata["queued_job_ids"] == []
    assert expected_blocker in metadata["gate_blockers"]
    assert len(metadata["review_gated"]) == 2
    with factory() as db:
        assert db.get(Candidate, safe_id).status == "pending"
        assert db.get(Candidate, review_gated_id).status == "pending"
        assert db.scalars(
            select(Job).where(Job.site_id == site_id, Job.kind == "candidate")
        ).all() == []


def test_full_cycle_unknown_mode_fails_closed_without_stage_execution(platform):
    client, factory, site_id = platform

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "full_cycle",
            "payload": {"mode": "unbounded"},
            "idempotency_key": "full-cycle-unknown-mode",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Unsupported full_cycle mode"}
    with factory() as db:
        assert db.scalars(
            select(Job).where(Job.site_id == site_id, Job.kind == "full_cycle")
        ).all() == []
        assert db.scalars(
            select(Job).where(Job.site_id == site_id, Job.payload["full_cycle_parent_job_id"].is_not(None))
        ).all() == []


def test_full_cycle_site_isolation_and_idempotency(platform):
    client, factory, site_id = platform
    second = client.post(
        "/api/v1/sites",
        json={"name": "Second site", "origin": "https://second.example.test"},
    )
    assert second.status_code == 201, second.text
    second_site_id = second.json()["id"]
    key = "full-cycle-shared-key"

    first = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "idempotency_key": key},
    )
    repeat = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "idempotency_key": key},
    )
    other_site = client.post(
        f"/api/v1/sites/{second_site_id}/jobs",
        json={"kind": "full_cycle", "idempotency_key": key},
    )

    assert first.status_code == repeat.status_code == other_site.status_code == 202
    assert first.json()["id"] == repeat.json()["id"]
    assert first.json()["id"] != other_site.json()["id"]
    assert client.get(f"/api/v1/sites/{second_site_id}/jobs/{first.json()['id']}").status_code == 404
    assert client.get(f"/api/v1/sites/{site_id}/jobs/{other_site.json()['id']}").status_code == 404

    conflict = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "full_cycle", "payload": {"max_pages": 2}, "idempotency_key": key},
    )
    assert conflict.status_code == 409

    with factory() as db:
        rows = db.scalars(select(Job).where(Job.kind == "full_cycle")).all()
        assert {row.site_id for row in rows} == {site_id, second_site_id}


def test_full_cycle_lease_expiry_is_retried_as_read_only_work(platform, monkeypatch):
    from datetime import timedelta

    from app import scheduler
    from app.operations import now

    _client, factory, site_id = platform
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    with factory() as db:
        row = Job(
            id="full-cycle-expired",
            site_id=site_id,
            kind="full_cycle",
            status="running",
            payload={},
            idempotency_key="full-cycle-expired-key",
            attempts=1,
            available_at=now() - timedelta(hours=1),
            lease_until=now() - timedelta(minutes=1),
        )
        db.add(row)
        db.commit()

    scheduler.schedule()

    with factory() as db:
        row = db.get(Job, "full-cycle-expired")
        assert row.status == "retry"
        assert row.result == {"reason": "Worker lease expired", "remote_outcome": "read_only"}
