from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, worker
from app.config import settings
from app.models import Base, Job, Site, Team


def test_paused_site_holds_queued_automatic_writes_until_resumed(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = datetime(2026, 9, 18, 16, 0)
    deliveries = []

    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr("app.operations.now", lambda: current)
    monkeypatch.setattr(
        worker.execute_job,
        "apply_async",
        lambda *args, **kwargs: deliveries.append(kwargs["args"][0]),
    )

    try:
        with factory() as db:
            team = Team(name="Scheduler pause acceptance team")
            db.add(team)
            db.flush()
            site = Site(
                team_id=team.id,
                name="Scheduler pause acceptance site",
                origin="https://scheduler-pause.example.test",
                paused=True,
            )
            db.add(site)
            db.flush()
            generate = Job(
                site_id=site.id,
                kind="generate",
                status="queued",
                payload={"article_id": "article-paused"},
                idempotency_key="paused-generate",
                available_at=current,
            )
            publish = Job(
                site_id=site.id,
                kind="publish",
                status="queued",
                payload={"article_id": "article-paused"},
                idempotency_key="paused-publish",
                available_at=current,
            )
            db.add_all([generate, publish])
            db.commit()
            generate_id, publish_id, site_id = generate.id, publish.id, site.id

        scheduler.schedule()

        assert generate_id not in deliveries
        assert publish_id not in deliveries
        with factory() as db:
            assert db.get(Job, generate_id).status == "queued"
            assert db.get(Job, publish_id).status == "queued"
            db.get(Site, site_id).paused = False
            db.commit()

        deliveries.clear()
        scheduler.schedule()

        assert generate_id in deliveries
        assert publish_id in deliveries
    finally:
        engine.dispose()


def test_paused_write_backlog_does_not_starve_read_only_dispatch(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = datetime(2026, 9, 18, 16, 0)
    deliveries = []

    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr("app.operations.now", lambda: current)
    monkeypatch.setattr(
        worker.execute_job,
        "apply_async",
        lambda *args, **kwargs: deliveries.append(kwargs["args"][0]),
    )

    try:
        with factory() as db:
            team = Team(name="Scheduler paused backlog team")
            db.add(team)
            db.flush()
            paused_site = Site(
                team_id=team.id,
                name="Scheduler paused backlog site",
                origin="https://scheduler-paused-backlog.example.test",
                paused=True,
            )
            db.add(paused_site)
            db.flush()
            for index in range(200):
                db.add(Job(
                    site_id=paused_site.id,
                    kind="publish",
                    status="queued",
                    payload={"article_id": f"paused-{index}"},
                    idempotency_key=f"paused-backlog-{index}",
                    available_at=current,
                ))
            read_only = Job(
                site_id=paused_site.id,
                kind="audit",
                status="queued",
                payload={},
                idempotency_key="paused-backlog-audit",
                available_at=current,
            )
            db.add(read_only)
            db.commit()
            read_only_id = read_only.id

        scheduler.schedule()

        assert read_only_id in deliveries
    finally:
        engine.dispose()
