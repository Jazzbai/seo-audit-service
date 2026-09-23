from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base, Job, Site, Team
from app.operations import enqueue


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _site(db, *, paused):
    team = Team(name="Operations acceptance team")
    db.add(team)
    db.flush()
    site = Site(
        team_id=team.id,
        name="Operations acceptance site",
        origin="https://operations-acceptance.example.test",
        paused=paused,
    )
    db.add(site)
    db.flush()
    return site


def test_pause_sensitive_enqueue_stays_durable_without_worker_delivery(monkeypatch):
    engine, factory = _database()
    deliveries = []
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(
        "app.worker.execute_job.apply_async",
        lambda *args, **kwargs: deliveries.append(kwargs["args"][0]),
    )

    try:
        with factory() as db:
            site = _site(db, paused=True)
            job = enqueue(
                db,
                site,
                "generate",
                {"article_id": "article-paused"},
                "paused-generate",
            )

            assert job.status == "queued"
            assert db.get(Job, job.id).status == "queued"
            assert deliveries == []
    finally:
        engine.dispose()


def test_global_pause_holds_write_enqueue_but_read_only_enqueue_still_dispatches(monkeypatch):
    engine, factory = _database()
    deliveries = []
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", True)
    monkeypatch.setattr(
        "app.worker.execute_job.apply_async",
        lambda *args, **kwargs: deliveries.append(kwargs["args"][0]),
    )

    try:
        with factory() as db:
            site = _site(db, paused=False)
            held = enqueue(db, site, "publish", {"article_id": "article-held"}, "paused-publish")
            observed = enqueue(db, site, "audit", {}, "paused-audit")

            assert held.status == "queued"
            assert observed.status == "queued"
            assert deliveries == [observed.id]
    finally:
        engine.dispose()
