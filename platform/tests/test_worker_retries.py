from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from celery.exceptions import Retry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import worker
from app.models import Base, Job, Site, Team
from app.workflows import HANDLERS


@pytest.fixture
def worker_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "now", lambda: current)
    yield factory, current
    engine.dispose()


@pytest.mark.parametrize("job_kind", ["plan", "targeted_audit"])
def test_scheduled_read_only_transient_failure_is_bounded_retry(
    worker_database, monkeypatch, job_kind,
):
    factory, current = worker_database
    with factory() as db:
        team = Team(name="Worker retry team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Worker retry site",
            origin="https://worker-retry.example.test",
        )
        db.add(site)
        db.flush()
        job = Job(
            site_id=site.id,
            kind=job_kind,
            payload={"resource_key": "posts:1"} if job_kind == "targeted_audit" else {},
            idempotency_key=f"{job_kind}-transient-retry",
            available_at=current,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    async def fail_temporarily(db, site, job):
        raise RuntimeError("temporary connector outage")

    monkeypatch.setitem(HANDLERS, job_kind, fail_temporarily)

    result = worker.run_job(job_id)

    assert result["retryable"] is True
    with factory() as db:
        retried = db.get(Job, job_id)
        assert retried.status == "retry"
        assert retried.attempts == 1
        assert retried.lease_until is None
        assert retried.available_at == current + timedelta(seconds=60)


def test_orphaned_job_persists_terminal_failure_without_active_lease(worker_database):
    factory, current = worker_database
    with factory() as db:
        job = Job(
            site_id="missing-site",
            kind="audit",
            idempotency_key="orphaned-site-job",
            available_at=current,
            updated_at=current - timedelta(minutes=1),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    result = worker.run_job(job_id)

    assert result == {"reason": "Site no longer exists"}
    with factory() as db:
        failed = db.get(Job, job_id)
        assert failed.status == "failed"
        assert failed.result == {"reason": "Site no longer exists"}
        assert failed.lease_until is None
        assert failed.updated_at > current - timedelta(minutes=1)


def test_site_busy_deferral_keeps_durable_delivery_lease(worker_database, monkeypatch):
    factory, current = worker_database
    with factory() as db:
        team = Team(name="Worker contention team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Worker contention site",
            origin="https://worker-contention.example.test",
        )
        db.add(site)
        db.flush()
        job = Job(
            site_id=site.id,
            kind="browser",
            idempotency_key="site-busy-delivery",
            available_at=current,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    @contextmanager
    def busy_lock(_key):
        yield False

    monkeypatch.setattr(worker, "exclusive", busy_lock)

    assert worker.run_job(job_id) == {"deferred": "site_busy"}
    with factory() as db:
        deferred = db.get(Job, job_id)
        assert deferred.status == "queued"
        assert deferred.lease_until == current + timedelta(seconds=60)


@pytest.mark.parametrize("job_kind", ["generate", "publish"])
def test_worker_holds_pause_sensitive_write_jobs_before_handler(
    worker_database, monkeypatch, job_kind,
):
    factory, current = worker_database
    with factory() as db:
        team = Team(name="Worker pause team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Worker pause site",
            origin="https://worker-pause.example.test",
            paused=True,
        )
        db.add(site)
        db.flush()
        job = Job(
            site_id=site.id,
            kind=job_kind,
            payload={"mode": "autopilot"} if job_kind == "content_autopilot" else {},
            idempotency_key=f"paused-{job_kind}",
            available_at=current,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    async def unexpected_handler(db, site, job):
        pytest.fail("pause-sensitive work must not start after the site is paused")

    monkeypatch.setitem(HANDLERS, job_kind, unexpected_handler)

    result = worker.run_job(job_id)

    assert result == {"status": "held", "reason": "site_paused"}
    with factory() as db:
        held = db.get(Job, job_id)
        assert held.status == "queued"
        assert held.attempts == 0
        assert held.available_at == current + timedelta(seconds=30)
        assert held.lease_until is None
        assert held.result == result


def test_celery_task_retries_only_site_contention(monkeypatch):
    monkeypatch.setattr(worker, "run_job", lambda _job_id: {"deferred": "site_busy"})

    with pytest.raises(Retry):
        worker.execute_job.run("busy-job")
