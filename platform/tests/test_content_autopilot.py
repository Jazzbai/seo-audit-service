"""Fixture-only coverage for the explicit one-article content autopilot."""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app import workflows, worker
from app.config import settings
from app.connectors.security import encrypt_credentials
from app.models import Article, Connection, Event, Job, Publication, Site
from test_platform import platform


def _ready_site(client, factory, site_id, *, posts_per_week=2, author_id="1", authors=None):
    """Configure a disposable site without contacting any external system."""

    authors = authors or [{"id": author_id, "name": "Fixture Writer"}]
    response = client.patch(
        f"/api/v1/sites/{site_id}",
        json={
            "paused": False,
            "facts": {
                "business_name": "Independent test",
                "services": ["Repairs"],
                "authors": authors,
            },
        },
    )
    assert response.status_code == 200, response.text
    response = client.put(
        f"/api/v1/sites/{site_id}/policy",
        json={
            "settings": {
                "enabled": True,
                "allowed_actions": ["publish"],
                "protected_paths": [],
                "posts_per_week": posts_per_week,
                "author_id": author_id,
            },
        },
    )
    assert response.status_code == 200, response.text
    with factory() as db:
        db.add_all([
            Connection(
                site_id=site_id,
                kind="wordpress",
                encrypted_credentials=encrypt_credentials(
                    {"username": "fixture", "application_password": "offline-fixture"},
                    settings.ENCRYPTION_KEY,
                ),
                status="connected",
                capabilities={
                    "authenticated": True,
                    "authenticated_author": {"id": author_id, "name": "Fixture Writer"},
                    "native": {"create": True, "publish": True},
                },
            ),
            Connection(
                site_id=site_id,
                kind="ai",
                encrypted_credentials="opaque-ai-fixture",
                status="connected",
                capabilities={"authenticated": True},
            ),
        ])
        db.commit()


def _ready_credentials(monkeypatch, *, author_ids=("1",)):
    def credentials(_db, _site_id, kind):
        if kind == "wordpress":
            return {"fixture": True}, {}
        return (
            {"api_key": "fixture-only"},
            {
                "endpoint": "https://ai.example.test/generate",
                "model": "fixture-model",
                "estimated_cost_cents": 1,
                "max_cost_cents": 2,
            },
        )

    monkeypatch.setattr(workflows, "credentials", credentials)

    class FixtureWordPress:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def discover_authors(self):
            return {
                "items": [
                    {"id": author_id, "name": f"Fixture Writer {author_id}"}
                    for author_id in author_ids
                ],
                "complete": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "authenticated_user_id": "1",
                "blockers": [],
            }

    async def client_for(_db, _site, kind="wordpress"):
        assert kind == "wordpress"
        return FixtureWordPress()

    monkeypatch.setattr(workflows, "client_for", client_for)


def _add_planned_article(factory, site_id, title="Fixture article", author_id="1"):
    with factory() as db:
        article = Article(
            site_id=site_id,
            title=title,
            slug="fixture-article",
            brief={"research": {"complete": True, "sources": []}},
            status="planned",
            managed=True,
            author_id=author_id,
        )
        db.add(article)
        db.commit()
        return article.id


def _job_result(client, site_id, *, payload=None, key="content-autopilot-fixture"):
    response = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={
            "kind": "content_autopilot",
            "payload": payload or {"max_articles": 1},
            "idempotency_key": key,
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    return job_id, worker.run_job(job_id)


def _patch_successful_content_steps(monkeypatch, calls):
    async def fake_generate(db, site, job):
        calls.append("generate")
        article = db.scalar(select(Article).where(Article.site_id == site.id, Article.id == job.payload["article_id"]))
        article.body = "<p>Useful fixture guidance for the reader.</p>"
        article.checks = {"passed": True, "blockers": [], "warnings": []}
        article.status = "checked"
        db.commit()
        return {"status": "checked", "cost_status": "settled"}

    async def fake_publish(db, site, job):
        calls.append("publish")
        article = db.scalar(select(Article).where(Article.site_id == site.id, Article.id == job.payload["article_id"]))
        article.status = "published"
        db.commit()
        return {"status": "published", "public_status": 200}

    monkeypatch.setattr(workflows, "generate", fake_generate)
    monkeypatch.setattr(workflows, "publish", fake_publish)


def test_content_autopilot_api_is_registered_and_gates_before_providers(platform, monkeypatch):
    client, factory, site_id = platform
    generate_calls = []
    publish_calls = []

    async def should_not_generate(*_args, **_kwargs):
        generate_calls.append(True)
        raise AssertionError("generation must not run while the site is gated")

    async def should_not_publish(*_args, **_kwargs):
        publish_calls.append(True)
        raise AssertionError("publication must not run while the site is gated")

    monkeypatch.setattr(workflows, "generate", should_not_generate)
    monkeypatch.setattr(workflows, "publish", should_not_publish)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    job_id, result = _job_result(client, site_id, key="content-autopilot-gated")

    assert result["workflow"] == "content_autopilot"
    assert result["status"] == "gated"
    assert "site_paused" in result["blockers"]
    assert "policy_disabled" in result["blockers"]
    assert "wordpress_connection_required" in result["blockers"]
    assert "ai_connection_required" in result["blockers"]
    assert generate_calls == []
    assert publish_calls == []
    with factory() as db:
        stored = db.get(Job, job_id)
        assert stored.status == "partial"
        assert all("secret" not in str(event.data).lower() for event in db.scalars(select(Event)).all())


def test_content_autopilot_runs_one_checked_article_and_records_child_jobs(platform, monkeypatch):
    client, factory, site_id = platform
    _ready_site(client, factory, site_id)
    article_id = _add_planned_article(factory, site_id)
    _ready_credentials(monkeypatch)
    calls = []
    _patch_successful_content_steps(monkeypatch, calls)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    parent_id, result = _job_result(client, site_id, key="content-autopilot-success")

    assert result["workflow"] == "content_autopilot"
    assert result["status"] == "published"
    assert result["complete"] is True
    assert result["article_id"] == article_id
    assert [stage["status"] for stage in result["stages"] if stage["name"] in {"generate", "publication"}] == [
        "generated_checked", "published",
    ]
    assert calls == ["generate", "publish"]
    with factory() as db:
        children = [
            row for row in db.scalars(select(Job).where(Job.site_id == site_id)).all()
            if isinstance(row.payload, dict)
            and row.payload.get("content_autopilot_parent_job_id") == parent_id
        ]
        assert {row.kind for row in children} == {"generate", "publish"}
        assert all(row.status == "complete" for row in children)
        assert db.get(Article, article_id).status == "published"
        assert db.scalars(select(Publication).where(Publication.site_id == site_id)).all() == []


def test_content_autopilot_prefers_configured_verified_author(platform, monkeypatch):
    client, factory, site_id = platform
    _ready_site(
        client,
        factory,
        site_id,
        author_id="2",
        authors=[
            {"id": "1", "name": "First Fixture Writer"},
            {"id": "2", "name": "Configured Fixture Writer"},
        ],
    )
    article_id = _add_planned_article(factory, site_id, author_id=None)
    _ready_credentials(monkeypatch, author_ids=("1", "2"))
    calls = []
    _patch_successful_content_steps(monkeypatch, calls)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    _parent_id, result = _job_result(client, site_id, key="content-autopilot-configured-author")

    assert result["status"] == "published"
    with factory() as db:
        assert db.get(Article, article_id).author_id == "2"


def test_content_autopilot_generation_review_stops_before_publication(platform, monkeypatch):
    client, factory, site_id = platform
    _ready_site(client, factory, site_id)
    article_id = _add_planned_article(factory, site_id, title="Needs review article")
    _ready_credentials(monkeypatch)
    publish_calls = []

    async def failing_generate(*_args, **_kwargs):
        raise RuntimeError("fixture provider failure")

    async def should_not_publish(*_args, **_kwargs):
        publish_calls.append(True)
        raise AssertionError("publication must not follow failed generation")

    monkeypatch.setattr(workflows, "generate", failing_generate)
    monkeypatch.setattr(workflows, "publish", should_not_publish)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    _parent_id, result = _job_result(client, site_id, key="content-autopilot-generation-failure")

    assert result["status"] == "needs_review"
    assert result["complete"] is False
    assert result["article_id"] == article_id
    assert "generation_failed" in result["blockers"]
    assert publish_calls == []
    with factory() as db:
        assert db.get(Article, article_id).status == "planned"
        assert db.scalars(select(Publication).where(Publication.site_id == site_id)).all() == []


def test_content_autopilot_reuses_completed_parent_result_without_new_article(platform, monkeypatch):
    client, factory, site_id = platform
    _ready_site(client, factory, site_id)
    article_id = _add_planned_article(factory, site_id, title="Idempotent article")
    _ready_credentials(monkeypatch)
    calls = []
    _patch_successful_content_steps(monkeypatch, calls)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    parent_id, first = _job_result(client, site_id, key="content-autopilot-idempotent")
    assert first["status"] == "published"
    with factory() as db:
        site = db.get(Site, site_id)
        parent = db.get(Job, parent_id)
        parent.status = "queued"
        db.commit()
        second = asyncio.run(workflows.content_autopilot(db, site, parent))
        articles = db.scalars(select(Article).where(Article.site_id == site_id)).all()

    assert second["status"] == "published"
    assert second["article_id"] == article_id
    assert len(articles) == 1
    assert calls == ["generate", "publish"]


def test_content_autopilot_enforces_site_scope_and_weekly_quota(platform, monkeypatch):
    client, factory, site_id = platform
    _ready_site(client, factory, site_id, posts_per_week=0)
    article_id = _add_planned_article(factory, site_id, title="Quota article")
    _ready_credentials(monkeypatch)
    calls = []
    _patch_successful_content_steps(monkeypatch, calls)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)

    other = client.post(
        "/api/v1/sites",
        json={
            "name": "Other site",
            "origin": "https://other.example.test",
            "facts": {
                "business_name": "Other site",
                "services": ["Repairs"],
                "authors": [{"id": "1", "name": "Fixture Writer"}],
            },
        },
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    _ready_site(client, factory, other_id)
    cross_id, cross = _job_result(
        client,
        other_id,
        payload={"max_articles": 1, "article_id": article_id},
        key="content-autopilot-cross-site",
    )
    assert cross["status"] == "gated"
    assert "article_not_found_or_cross_site" in cross["blockers"]
    assert calls == []

    _quota_id, quota = _job_result(client, site_id, key="content-autopilot-quota")
    assert quota["status"] == "gated"
    assert "weekly_publication_limit_reached" in quota["blockers"]
    assert calls == []
    with factory() as db:
        assert db.get(Job, cross_id).site_id == other_id
        assert db.get(Article, article_id).site_id == site_id
