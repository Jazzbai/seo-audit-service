from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, worker
from app.config import settings
from app.models import Base, Event, Job, Site, Team


def test_expired_weekly_refresh_lease_is_retried_as_read_only_work(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = datetime(2026, 9, 18, 16, 0)

    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr("app.operations.now", lambda: current)
    monkeypatch.setattr(worker.execute_job, "apply_async", lambda *args, **kwargs: None)

    try:
        with factory() as db:
            team = Team(name="Scheduler acceptance team")
            db.add(team)
            db.flush()
            site = Site(
                team_id=team.id,
                name="Scheduler acceptance site",
                origin="https://scheduler-acceptance.example.test",
                paused=True,
            )
            db.add(site)
            db.flush()
            job = Job(
                site_id=site.id,
                kind="refresh",
                status="running",
                payload={},
                idempotency_key="expired-weekly-refresh",
                attempts=1,
                available_at=current - timedelta(hours=1),
                lease_until=current,
            )
            db.add(job)
            db.commit()
            job_id = job.id
            site_id = site.id

        scheduler.schedule()

        with factory() as db:
            recovered = db.get(Job, job_id)
            assert recovered.status == "retry"
            assert recovered.available_at == current
            # The scheduler immediately re-delivers the recovered durable row
            # and holds a short broker-delivery lease to prevent per-tick
            # duplicates until a worker claims it.
            assert recovered.lease_until == current + timedelta(minutes=1)
            assert recovered.result == {
                "reason": "Worker lease expired",
                "remote_outcome": "read_only",
            }
            interruption = db.scalar(select(Event).where(
                Event.site_id == site_id,
                Event.kind == "worker_interrupted",
            ))
            assert interruption is not None
            assert interruption.data == {"job_id": job_id, "status": "retry"}
    finally:
        engine.dispose()
