import asyncio
from datetime import datetime

from app import workflows
from app.config import settings
from app.models import Article, Base, Page, Site, Team
from app.policies import create_policy
from app.models import Job
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_managed_article_refresh_enters_planned_pipeline_only_when_policy_allows_it(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    timestamp = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(workflows, "now", lambda: timestamp)
    monkeypatch.setattr("app.operations.now", lambda: timestamp)
    with factory() as db:
        team = Team(name="Maintenance policy team")
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name="Maintenance site", origin="https://maintenance.example.test", paused=False)
        db.add(site)
        db.flush()
        create_policy(db, site, None, {
            "enabled": True,
            "allowed_actions": ["refresh", "publish"],
            "refreshes_per_week": 1,
        })
        source_article = Article(
            site_id=site.id,
            title="Managed guide",
            remote_id="42",
            managed=True,
            status="published",
        )
        db.add(source_article)
        db.flush()
        page = Page(
            site_id=site.id,
            resource_key="posts:42",
            resource_type="posts",
            url=f"{site.origin}/managed-guide",
            title="Managed guide",
            source_hash="source-before",
            enrolled=True,
            source={"title": "Managed guide", "body": "<p>Original</p>", "author": "9"},
            signals={"enrollment": {"mode": "platform_created", "article_id": source_article.id}},
        )
        db.add(page)
        db.commit()

        result = asyncio.run(workflows.refresh(db, site, Job(id="refresh-managed", site_id=site.id, payload={})))
        db.commit()
        draft = db.get(Article, result["article_ids"][0])
        assert result["status"] == "planned"
        assert draft.status == "planned"
        assert draft.brief["automatic_maintenance"] is True
        assert draft.brief["managed_source_article_id"] == source_article.id
        assert draft.managed is False
        assert draft.checks["blockers"] == []

    engine.dispose()
