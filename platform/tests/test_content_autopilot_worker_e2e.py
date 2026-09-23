"""Deterministic worker-boundary coverage for automatic content autopilot.

The scheduler and worker run against the real content-autopilot, generation,
and publication orchestration. Only provider/research and WordPress/public
network edges are replaced with local doubles.
"""

import json
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app import operations, scheduler, worker, workflows
from app.config import settings
from app.models import (
    Article,
    BudgetAccount,
    Connection,
    CostReservation,
    Job,
    Publication,
    Revision,
    Site,
)
from app.policies import create_policy
from test_platform import platform


WINDOW = datetime(2026, 9, 21, 14, 0)  # Monday at 09:00 in America/Chicago.


def _install_clock(platform, monkeypatch):
    _client, factory, _site_id = platform
    deliveries = []

    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: WINDOW)
    monkeypatch.setattr(operations, "now", lambda: WINDOW)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "now", lambda: WINDOW)
    monkeypatch.setattr(workflows, "now", lambda: WINDOW)
    monkeypatch.setattr(
        worker.execute_job,
        "apply_async",
        lambda *args, **kwargs: deliveries.append(
            (kwargs.get("args") or args)[0]
        ),
    )
    return factory, deliveries


def _seed_ready_site(factory, site_id):
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        site.timezone = "America/Chicago"
        site.origin = "https://example.test"
        site.facts = {
            "business_name": "Independent test",
            "audience": "local drivers",
            "services": ["Collision repair"],
            "locations": ["Houston"],
            "authors": [{"id": "author-1", "name": "Fixture Writer"}],
        }
        create_policy(
            db,
            site,
            None,
            {
                "enabled": True,
                "allowed_actions": ["publish"],
                "protected_paths": [],
                "posts_per_week": 1,
                "publish_days": [0],
                "monthly_budget_cents": 2,
                "author_id": "author-1",
            },
        )
        db.add_all([
            Connection(
                site_id=site_id,
                kind="wordpress",
                encrypted_credentials="opaque-wordpress-fixture",
                status="connected",
                capabilities={
                    "authenticated": True,
                    "authenticated_author": {
                        "id": "author-1",
                        "name": "Fixture Writer",
                    },
                    "native": {"create": True, "publish": True},
                },
                checked_at=WINDOW,
                created_at=WINDOW,
            ),
            Connection(
                site_id=site_id,
                kind="ai",
                encrypted_credentials="opaque-ai-fixture",
                status="connected",
                capabilities={
                    "authenticated": True,
                    "settings": {
                        "endpoint": "https://ai.example.test/generate",
                        "model": "fixture-model",
                        "estimated_cost_cents": 1,
                        "max_cost_cents": 1,
                    },
                },
                checked_at=WINDOW,
                created_at=WINDOW,
            ),
        ])
        article = Article(
            site_id=site_id,
            title="Prepare for a Collision Repair Visit",
            slug="prepare-for-a-collision-repair-visit",
            status="planned",
            brief={},
            managed=True,
            author_id="author-1",
            created_at=WINDOW,
            updated_at=WINDOW,
        )
        db.add(article)
        db.commit()
        return article.id


def _install_local_boundaries(monkeypatch, *, publication_mode):
    research_calls = []
    generation_calls = []
    local_fetch_calls = []

    async def fixture_research(brief, facts):
        # The autopilot passes the persisted brief to research before the
        # article title is merged into the generation request.
        research_calls.append("research")
        return {
            "complete": True,
            "sources": [
                {
                    "url": "https://evidence.example.test/collision-repair",
                    "title": "Repair reference",
                    "status": "ok",
                }
            ],
            "research_notes": [{"kind": "fixture", "status": "complete"}],
        }

    async def fixture_generation(article_brief, facts, config):
        generation_calls.append(article_brief["title"])
        return {
            "status": "generated",
            "body": (
                "<h2>Prepare for a collision repair visit</h2>"
                "<p>Independent test helps drivers prepare their repair questions.</p>"
            ),
            "sources": article_brief["research"]["sources"],
            "provenance": {
                "kind": "fixture_editorial_generation",
                "approval_required": True,
                "research": article_brief["research"],
                "research_sources": article_brief["research"]["sources"],
            },
            "cost_cents": 1,
            "cost_basis": "provider_actual",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

    monkeypatch.setattr(
        "app.intelligence.research.research_brief", fixture_research
    )
    monkeypatch.setattr(
        "app.intelligence.content.generate_article", fixture_generation
    )

    def fixture_credentials(_db, _site_id, kind):
        if kind == "wordpress":
            return {"fixture": True}, {}
        if kind == "ai":
            return (
                {"api_key": "fixture-only"},
                {
                    "endpoint": "https://ai.example.test/generate",
                    "model": "fixture-model",
                    "estimated_cost_cents": 1,
                    "max_cost_cents": 1,
                },
            )
        raise AssertionError(f"unexpected connection kind: {kind}")

    monkeypatch.setattr(workflows, "credentials", fixture_credentials)

    class InMemoryWordPress:
        def __init__(self):
            self.current = None
            self.next_id = 901
            self.create_calls = 0
            self.publish_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @staticmethod
        def _content_hash(record):
            return workflows.digest(
                {
                    key: record.get(key)
                    for key in (
                        "resource_key",
                        "title",
                        "body",
                        "slug",
                        "author_id",
                    )
                }
            )

        def _record(self, *, status):
            record = dict(self.current)
            record["status"] = status
            record["source_hash"] = self._content_hash(record)
            self.current = record
            return dict(record)

        async def create_draft(self, article, operation_key):
            self.create_calls += 1
            if publication_mode == "ambiguous":
                raise TimeoutError("fixture remote outcome is unknown")
            self.current = {
                "id": str(self.next_id),
                "resource_key": f"posts:{self.next_id}",
                "resource_type": "posts",
                "url": "https://example.test/" + article["slug"],
                "title": article["title"],
                "body": article["body"],
                "slug": article["slug"],
                "author_id": article["author_id"],
            }
            return self._record(status="draft")

        async def read(self, resource_key):
            assert self.current is not None
            assert self.current["resource_key"] == resource_key
            return dict(self.current)

        @staticmethod
        def matches_snapshot(current, snapshot, *, ignore_status=False):
            fields = (
                "resource_key",
                "title",
                "body",
                "slug",
                "author_id",
                "source_hash",
            )
            return all(current.get(field) == snapshot.get(field) for field in fields)

        async def publish(self, remote_id, expected_hash, operation_key):
            self.publish_calls += 1
            assert remote_id == self.current["id"]
            assert expected_hash == self.current["source_hash"]
            return self._record(status="publish")

    remote = InMemoryWordPress()

    async def fixture_client(_db, _site, kind="wordpress"):
        assert kind == "wordpress"
        return remote

    async def public_fetch(url):
        local_fetch_calls.append(url)
        assert remote.current is not None
        assert url == remote.current["url"]
        return {
            "status_code": 200,
            "url": url,
            "headers": {"content-type": "text/html"},
            "html": (
                "<html><head><link rel='canonical' href='https://example.test/'>"
                "<meta name='robots' content='index,follow'>"
                "<style>.entry{color:black}</style></head>"
                "<body><main class='entry'><h1>"
                + remote.current["title"]
                + "</h1>"
                + remote.current["body"]
                + "</main></body></html>"
            ),
        }

    monkeypatch.setattr(workflows, "client_for", fixture_client)
    monkeypatch.setattr(workflows, "fetch", public_fetch)

    def unexpected_external_client(*_args, **_kwargs):
        raise AssertionError("unexpected external HTTP client construction")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_external_client)

    return {
        "research_calls": research_calls,
        "generation_calls": generation_calls,
        "local_fetch_calls": local_fetch_calls,
        "remote": remote,
    }


@pytest.mark.parametrize("publication_mode", ["success", "ambiguous"])
def test_scheduler_parent_runs_real_autopilot_worker_boundary(
    platform, monkeypatch, publication_mode
):
    client, factory, site_id = platform
    del client
    factory, deliveries = _install_clock(platform, monkeypatch)
    article_id = _seed_ready_site(factory, site_id)
    boundaries = _install_local_boundaries(
        monkeypatch, publication_mode=publication_mode
    )

    # The scheduler creates exactly one durable parent in the local publish
    # window. A repeated dispatch tick reuses its stable site/day key.
    scheduler.schedule()
    scheduler.schedule()
    with factory() as db:
        parents = db.scalars(
            select(Job).where(
                Job.site_id == site_id,
                Job.kind == "content_autopilot",
            )
        ).all()
        assert len(parents) == 1
        parent_id = parents[0].id
        assert parents[0].payload["max_articles"] == 1
        assert parents[0].payload["authorization"] == {
            "type": "policy",
            "action": "publish",
            "policy_version": parents[0].payload["policy_version"],
        }

    result = worker.run_job(parent_id)

    # A worker replay is ignored after the durable parent reaches its terminal
    # state, and a later scheduler tick cannot create a second weekly write.
    replay = worker.run_job(parent_id)
    scheduler.schedule()
    with factory() as db:
        parents = db.scalars(
            select(Job).where(
                Job.site_id == site_id,
                Job.kind == "content_autopilot",
            )
        ).all()
        article = db.get(Article, article_id)
        children = [
            row
            for row in db.scalars(select(Job).where(Job.site_id == site_id)).all()
            if row.payload.get("content_autopilot_parent_job_id") == parent_id
        ]
        publication = db.scalar(
            select(Publication).where(Publication.article_id == article_id)
        )
        reservations = db.scalars(
            select(CostReservation).where(CostReservation.site_id == site_id)
        ).all()
        account = db.scalar(
            select(BudgetAccount).where(BudgetAccount.site_id == site_id)
        )
        revisions = db.scalars(
            select(Revision).where(Revision.article_id == article_id)
        ).all()
        parent = db.get(Job, parent_id)

    assert len(parents) == 1
    assert replay == {"ignored": True}
    assert boundaries["research_calls"] == ["research"]
    assert boundaries["generation_calls"] == [
        "Prepare for a Collision Repair Visit"
    ]
    assert result["status"] == (
        "published" if publication_mode == "success" else "ambiguous"
    ), json.dumps(
        {
            "result": result,
            "checks": article.checks,
            "brief": article.brief,
            "sources": article.sources,
        },
        indent=2,
    )
    assert deliveries.count(parent_id) == 1
    assert boundaries["local_fetch_calls"] == (
        [] if publication_mode == "ambiguous" else [
            "https://example.test/prepare-for-a-collision-repair-visit"
        ]
    )

    if publication_mode == "success":
        assert result["workflow"] == "content_autopilot"
        assert result["status"] == "published"
        assert result["complete"] is True
        assert article.status == "published"
        assert boundaries["remote"].create_calls == 1
        assert boundaries["remote"].publish_calls == 1
        assert publication is not None
        assert publication.status == "published"
        assert publication.operation_key == f"publish:{site_id}:{article_id}"
        assert publication.remote_id == "901"
        assert publication.snapshot["article"]["author_id"] == "author-1"
        assert publication.snapshot["draft"]["resource_key"] == "posts:901"
        assert publication.result["status"] == "published"
        assert publication.result["public_status"] == 200
        assert {stage["name"] for stage in result["stages"]} == {
            "plan",
            "research",
            "generate",
            "publication",
        }
        assert {row.kind for row in children} == {"generate", "publish"}
        assert all(row.status == "complete" for row in children)
        assert len(revisions) == 1
        assert len(reservations) == 1
        assert reservations[0].status == "settled"
        assert reservations[0].actual_cents == 1
        assert reservations[0].estimated_cents == 1
        assert account.reserved_cents == 0
        assert account.spent_cents == 1
        assert parent.status == "complete"
    else:
        assert result["workflow"] == "content_autopilot"
        assert result["status"] == "ambiguous"
        assert result["complete"] is False
        assert article.status == "failed"
        assert boundaries["remote"].create_calls == 1
        assert boundaries["remote"].publish_calls == 0
        assert publication is not None
        assert publication.status == "ambiguous"
        assert publication.operation_key == f"publish:{site_id}:{article_id}"
        assert {row.kind for row in children} == {"generate", "publish"}
        assert next(row for row in children if row.kind == "publish").status == "needs_reconciliation"
        assert next(row for row in children if row.kind == "publish").result == {
            "status": "ambiguous",
            "blockers": ["publication_ambiguous"],
        }
        assert len(reservations) == 1
        assert reservations[0].status == "settled"
        assert account.reserved_cents == 0
        assert account.spent_cents == 1
        assert parent.status == "partial"

    # The worker-facing result and persisted operation records never expose
    # the fixture credential or an external endpoint secret.
    rendered = json.dumps(
        {"result": result, "publication": publication.result if publication else {}},
        sort_keys=True,
    )
    assert "fixture-only" not in rendered
    assert "application_password" not in rendered
