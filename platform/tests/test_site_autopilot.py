"""Focused coverage for the owner-governed site autopilot command."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import scheduler, worker
from app.config import settings
from app.main import app
from app.models import Connection, Job
from app.operations import now
from test_platform import platform


def _run_job(job_id):
    return worker.run_job(job_id)


def _unpause(client, site_id):
    response = client.patch(f"/api/v1/sites/{site_id}", json={"paused": False})
    assert response.status_code == 200, response.text


def test_site_autopilot_is_owner_only_and_has_a_durable_job_contract(platform):
    client, factory, site_id = platform

    added = client.post(
        "/api/v1/team/members",
        json={
            "email": "autopilot-editor@example.test",
            "name": "Autopilot Editor",
            "password": "editor autopilot password",
            "role": "editor",
        },
    )
    assert added.status_code == 200, added.text

    editor = TestClient(app)
    try:
        login = editor.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://testserver"},
            json={
                "email": "autopilot-editor@example.test",
                "password": "editor autopilot password",
            },
        )
        assert login.status_code == 200, login.text
        editor.headers.update({
            "Origin": "http://testserver",
            "X-CSRF-Token": login.json()["csrf_token"],
        })

        denied = editor.post(
            f"/api/v1/sites/{site_id}/jobs",
            json={
                "kind": "full_cycle",
                "payload": {"mode": "autopilot"},
                "idempotency_key": "site-autopilot-editor-denied",
            },
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Insufficient team role"
    finally:
        editor.close()

    accepted = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "full_cycle",
            "payload": {"mode": "autopilot"},
            "idempotency_key": "site-autopilot-owner-accepted",
        },
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["kind"] == "full_cycle"
    assert body["payload"] == {"mode": "autopilot"}
    assert body["status"] == "queued"
    with factory() as db:
        row = db.get(Job, body["id"])
        assert row is not None
        assert row.payload == {"mode": "autopilot"}


def test_site_autopilot_runs_six_bounded_stages_and_keeps_publication_truthful(
    platform,
    monkeypatch,
):
    client, factory, site_id = platform
    _unpause(client, site_id)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="opaque-test-secret",
            status="connected",
            capabilities={"authenticated": True},
        ))
        db.commit()

    calls = []

    async def fake_stage(_db, _site, stage_job):
        calls.append((stage_job.kind, stage_job.payload.copy()))
        if stage_job.kind == "content_autopilot":
            return {
                "workflow": "content_autopilot",
                "status": "gated",
                "complete": False,
                "blockers": ["author_unverified"],
            }
        return {
            "complete": True,
            "observed_stage": stage_job.payload["full_cycle_stage"],
        }

    import app.workflows as workflows

    for handler_name in (
        "availability",
        "inventory",
        "audit",
        "plan",
        "content_autopilot",
        "refresh",
    ):
        monkeypatch.setitem(workflows.HANDLERS, handler_name, fake_stage)

    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "full_cycle",
            "payload": {"mode": "autopilot", "max_pages": 12},
            "idempotency_key": "site-autopilot-six-stages",
        },
    )
    assert response.status_code == 202, response.text
    parent_id = response.json()["id"]
    result = _run_job(parent_id)

    assert result["workflow"] == "full_cycle"
    assert result["mode"] == "autopilot"
    assert result["complete"] is False
    assert [stage["name"] for stage in result["stages"]] == [
        "availability",
        "inventory",
        "public_audit",
        "content_plan",
        "content_autopilot",
        "refresh_evaluation",
    ]
    assert [name for name, _payload in calls] == [
        "availability",
        "inventory",
        "public_audit",
        "content_plan",
        "content_autopilot",
        "refresh_evaluation",
    ]
    assert calls[3][1]["max_articles"] == 1
    assert calls[4][1]["max_articles"] == 1
    publishing = result["execution_summary"]["content_publishing"]
    assert publishing["status"] == "gated"
    assert publishing["reason"] == "review_required"
    assert publishing["next_action"] == "resolve_content_autopilot_blockers"
    assert any(
        action["action"] == "publish" and action["status"] == "gated"
        for action in result["next_actions"]
    )
    assert not any(
        kind in {"generate", "publish", "visibility"}
        for kind, _payload in calls
    )
    assert "opaque-test-secret" not in str(result)

    with factory() as db:
        stage_rows = [
            row for row in db.scalars(select(Job).where(Job.site_id == site_id)).all()
            if isinstance(row.payload, dict)
            and row.payload.get("full_cycle_parent_job_id") == parent_id
        ]
        assert {row.kind for row in stage_rows} == {
            "availability",
            "inventory",
            "public_audit",
            "content_plan",
            "content_autopilot",
            "refresh_evaluation",
        }
        assert all(row.status in {"complete", "partial"} for row in stage_rows)


def test_paused_site_autopilot_is_held_without_claiming_a_write(platform, monkeypatch):
    _client, factory, site_id = platform
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    created_at = now()
    with factory() as db:
        row = Job(
            id="site-autopilot-paused",
            site_id=site_id,
            kind="full_cycle",
            status="queued",
            payload={"mode": "autopilot"},
            idempotency_key="site-autopilot-paused-key",
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(row)
        db.commit()

    result = _run_job("site-autopilot-paused")

    assert result["workflow"] == "full_cycle"
    assert result["status"] == "held"
    assert result["reason"] == "site_paused"
    with factory() as db:
        row = db.get(Job, "site-autopilot-paused")
        assert row.status == "queued"
        assert row.attempts == 0
        assert row.result["status"] == "held"
        assert row.available_at > created_at
        assert [
            candidate for candidate in db.scalars(select(Job)).all()
            if isinstance(candidate.payload, dict)
            and candidate.payload.get("full_cycle_parent_job_id")
        ] == []


def test_scheduler_does_not_dispatch_autopilot_while_site_is_paused(platform, monkeypatch):
    _client, factory, site_id = platform
    dispatched = []
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(
        worker.execute_job,
        "apply_async",
        lambda *args, **kwargs: dispatched.append(args[0]),
    )
    created_at = now() - timedelta(minutes=2)
    with factory() as db:
        db.add(Job(
            id="site-autopilot-scheduler-paused",
            site_id=site_id,
            kind="full_cycle",
            status="queued",
            payload={"mode": "autopilot"},
            idempotency_key="site-autopilot-scheduler-paused-key",
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        ))
        db.commit()

    scheduler.schedule()

    assert "site-autopilot-scheduler-paused" not in dispatched


def test_expired_site_autopilot_parent_requires_reconciliation(platform, monkeypatch):
    _client, factory, site_id = platform
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    expired = now() - timedelta(minutes=1)
    with factory() as db:
        db.add(Job(
            id="site-autopilot-expired",
            site_id=site_id,
            kind="full_cycle",
            status="running",
            payload={"mode": "autopilot"},
            idempotency_key="site-autopilot-expired-key",
            attempts=1,
            available_at=expired,
            lease_until=expired,
            created_at=expired,
            updated_at=expired,
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        row = db.get(Job, "site-autopilot-expired")
        assert row.status == "needs_reconciliation"
        assert row.result == {
            "reason": "Worker lease expired",
            "remote_outcome": "unknown",
        }
