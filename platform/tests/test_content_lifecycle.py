"""End-to-end content lifecycle gate using the shared authenticated fixture."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app import workflows
from app.config import settings
from app.connectors.security import encrypt_credentials
from app.models import Article, Connection, Job, Page, Publication, Revision, Site
from app.operations import now
from app.policies import create_policy
from test_platform import platform


def test_article_lifecycle_runs_from_research_to_enrolled_refresh_evaluation(
    platform, monkeypatch
):
    """Prove the pilot's connected article path without touching a real site."""

    client, factory, site_id = platform
    source_url = "https://evidence.example.test/collision-repair"
    research_calls = []
    generated_calls = []

    def research_response(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == source_url:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<html><head><title>Repair reference</title></head>"
                    "<body><h1>Repair reference</h1>"
                    "<p>Drivers can prepare questions before a repair visit.</p>"
                    "</body></html>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    research_transport = httpx.MockTransport(research_response)
    real_research = __import__(
        "app.intelligence.research", fromlist=["research_brief"]
    ).research_brief

    async def fixture_research(brief, facts):
        research_calls.append(brief["title"])
        return await real_research(brief, facts, transport=research_transport)

    async def fixture_generation(article_brief, facts, config):
        generated_calls.append(article_brief["title"])
        research = article_brief["research"]
        return {
            "status": "generated",
            "body": (
                "<h2>Prepare for a collision repair visit</h2>"
                "<p>Independent test helps drivers prepare their repair questions.</p>"
            ),
            "sources": research["sources"],
            "provenance": {
                "kind": "fixture_editorial_generation",
                "source": "fixture-editor",
                "approval_required": True,
                "research": research,
            },
            "cost_cents": 1,
            "cost_basis": "provider_actual",
            "usage": {"fixture": True},
        }

    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(
        "app.intelligence.research.research_brief", fixture_research
    )
    monkeypatch.setattr(
        "app.intelligence.content.generate_article", fixture_generation
    )
    monkeypatch.setattr(
        workflows,
        "credentials",
        lambda *args, **kwargs: (
            {"fixture_provider": True},
            {"estimated_cost_cents": 1, "max_cost_cents": 1},
        ),
    )

    class InMemoryWordPress:
        def __init__(self):
            self.current = None
            self.next_id = 900

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def discover_authors(self):
            return {
                "items": [{"id": "1", "name": "Fixture Writer"}],
                "complete": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "authenticated_user_id": "1",
                "blockers": [],
            }

        @staticmethod
        def _content_hash(record):
            return workflows.digest(
                {
                    key: record.get(key)
                    for key in ("resource_key", "title", "body", "slug", "author_id")
                }
            )

        def _record(self, *, status):
            record = dict(self.current)
            record["status"] = status
            record["source_hash"] = self._content_hash(record)
            self.current = record
            return dict(record)

        async def create_draft(self, article, operation_key):
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
            assert self.current["resource_key"] == resource_key
            return dict(self.current)

        @staticmethod
        def matches_snapshot(current, snapshot, *, ignore_status=False):
            fields = ("resource_key", "title", "body", "slug", "author_id", "source_hash")
            return all(current.get(field) == snapshot.get(field) for field in fields)

        async def publish(self, remote_id, expected_hash, operation_key):
            assert remote_id == self.current["id"]
            assert expected_hash == self.current["source_hash"]
            return self._record(status="publish")

    remote = InMemoryWordPress()

    async def fixture_client(db, site, kind="wordpress"):
        assert kind == "wordpress"
        return remote

    async def public_fetch(url):
        assert remote.current is not None
        assert url == remote.current["url"]
        return {
            "status_code": 200,
            "url": url,
            "headers": {"content-type": "text/html"},
            "html": (
                "<html><head><link rel='canonical' href='https://example.test/'>"
                "<meta name='robots' content='index,follow'><style>.entry{color:black}</style>"
                "</head><body><main class='entry'><h1>"
                + remote.current["title"]
                + "</h1>"
                + remote.current["body"]
                + "</main></body></html>"
            ),
        }

    monkeypatch.setattr(workflows, "client_for", fixture_client)
    monkeypatch.setattr(workflows, "fetch", public_fetch)

    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        site.origin = "https://example.test"
        site.facts = {
            "business_name": "Independent test",
            "audience": "local drivers",
            "services": ["Collision repair"],
            "locations": ["Houston"],
            "confirmed_sources": [{"url": source_url, "title": "Repair reference"}],
        }
        policy = create_policy(
            db,
            site,
            None,
            {
                "enabled": True,
                "allowed_actions": ["publish", "refresh"],
                "author_id": "1",
                "tracked_keywords": ["collision repair preparation"],
            },
        )

        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials=encrypt_credentials(
                {"username": "fixture", "application_password": "offline-fixture"},
                settings.ENCRYPTION_KEY,
            ),
            status="connected",
            capabilities={
                "authenticated": True,
                "authenticated_author": {"id": "1", "name": "Fixture Writer"},
                "native": {"create": True, "publish": True},
            },
        ))

        planned = asyncio.run(
            workflows.plan(
                db,
                site,
                Job(
                    id="content-plan-lifecycle",
                    site_id=site_id,
                    kind="plan",
                    payload={},
                    idempotency_key="content-plan-lifecycle",
                ),
            )
        )
        db.commit()
        assert planned["count"] > 0
        article = db.get(Article, planned["article_ids"][0])
        assert article.status == "planned"
        assert article.brief["status"] == "planned"

        generated = asyncio.run(
            workflows.generate(
                db,
                site,
                Job(
                    id="content-generate-lifecycle",
                    site_id=site_id,
                    kind="generate",
                    payload={"article_id": article.id},
                    idempotency_key="content-generate-lifecycle",
                ),
            )
        )
        db.commit()
        assert generated["status"] == "checked"
        assert research_calls == [article.title]
        assert generated_calls == [article.title]
        assert article.brief["research"]["complete"] is True
        assert article.checks["passed"] is True
        assert db.scalar(select(Revision).where(Revision.article_id == article.id)) is not None

        scheduled_at = (now() + timedelta(hours=1)).replace(tzinfo=timezone.utc)
        scheduled_response = client.post(
            f"/api/v1/sites/{site_id}/articles/{article.id}/schedule",
            json={"scheduled_at": scheduled_at.isoformat()},
        )
        assert scheduled_response.status_code == 200, scheduled_response.text
        db.refresh(article)
        assert article.status == "scheduled"
        assert article.scheduled_at is not None

        article.scheduled_at = now() - timedelta(minutes=1)
        db.commit()
        publication_result = asyncio.run(
            workflows.publish(
                db,
                site,
                Job(
                    id="content-publish-lifecycle",
                    site_id=site_id,
                    kind="publish",
                    payload={"article_id": article.id},
                    idempotency_key="content-publish-lifecycle",
                ),
            )
        )
        db.commit()
        publication = db.scalar(
            select(Publication).where(Publication.article_id == article.id)
        )
        page = db.scalar(select(Page).where(Page.site_id == site_id))
        assert publication_result["status"] == "published"
        assert article.status == "published"
        assert publication.status == "published"
        assert publication.policy_version == policy.version
        assert publication.result["public_status"] == 200
        assert page.managed is True and page.enrolled is True

        refresh_result = asyncio.run(
            workflows.refresh(
                db,
                site,
                Job(
                    id="content-refresh-lifecycle",
                    site_id=site_id,
                    kind="refresh",
                    payload={},
                    idempotency_key="content-refresh-lifecycle",
                ),
            )
        )
        db.commit()
        refresh_article = db.get(Article, refresh_result["article_ids"][0])
        assert refresh_result["status"] == "planned"
        assert refresh_article.status == "planned"
        assert refresh_article.brief["purpose"] == "refresh_existing"
        assert refresh_article.brief["refresh_of_page_id"] == page.id
        assert refresh_article.brief["source_hash"] == page.source_hash
