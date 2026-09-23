"""Opt-in PostgreSQL evidence for durable worker site-lock behavior.

These tests use the disposable fixture from ``test_postgres_live`` and replace
the selected workflow handler with an in-process handler.  They therefore
exercise real PostgreSQL advisory locks and durable job rows without contacting
WordPress or any paid provider.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from celery.exceptions import Retry
from sqlalchemy import select

from app import worker, workflows
from app.models import Job, utcnow
from tests.test_postgres_live import (
    PostgresDatabase,
    _site,
    postgres_database,
    postgres_live,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("FORGE_LIVE_PG") != "1",
    reason="Explicit isolated PostgreSQL integration opt-in required",
)


def _job(postgres_database: PostgresDatabase, site_id: str, key: str) -> str:
    with postgres_database.factory() as db:
        row = Job(
            site_id=site_id,
            kind="audit",
            payload={"fixture": key},
            idempotency_key=key,
            available_at=utcnow(),
        )
        db.add(row)
        db.commit()
        return row.id


def test_postgres_workers_progress_for_different_sites_without_shared_locks(
    postgres_database: PostgresDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different site locks permit two durable workers to progress together."""

    site_ids = [
        _site(postgres_database.factory, paused=False),
        _site(postgres_database.factory, paused=False),
    ]
    job_ids = [
        _job(postgres_database, site_id, f"different-site-{number}")
        for number, site_id in enumerate(site_ids)
    ]
    handlers_entered = Barrier(2)
    both_handlers_entered = Event()
    release_handlers = Event()

    async def local_audit(_db, site, _job):
        handlers_entered.wait(timeout=10)
        both_handlers_entered.set()
        if not release_handlers.wait(timeout=10):
            raise RuntimeError("isolated concurrency handler was not released")
        return {"complete": True, "fixture_site_id": site.id}

    monkeypatch.setitem(workflows.HANDLERS, "audit", local_audit)
    monkeypatch.setattr(worker, "SessionLocal", postgres_database.factory)
    monkeypatch.setattr(worker, "engine", postgres_database.engine)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker.run_job, job_id) for job_id in job_ids]
        try:
            assert both_handlers_entered.wait(timeout=10)
        finally:
            release_handlers.set()
        results = [future.result(timeout=10) for future in futures]

    assert [result["complete"] for result in results] == [True, True]
    with postgres_database.factory() as db:
        jobs = db.scalars(select(Job).where(Job.id.in_(job_ids))).all()
        assert {job.status for job in jobs} == {"complete"}
        assert {job.result["fixture_site_id"] for job in jobs} == set(site_ids)


def test_postgres_same_site_contention_keeps_durable_job_queued_for_retry(
    postgres_database: PostgresDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy site leaves the second job queued and retryable, never complete."""

    site_id = _site(postgres_database.factory, paused=False)
    holder_id = _job(postgres_database, site_id, "same-site-holder")
    contender_id = _job(postgres_database, site_id, "same-site-contender")
    holder_started = Event()
    release_holder = Event()

    async def local_audit(_db, site, _job):
        holder_started.set()
        if not release_holder.wait(timeout=10):
            raise RuntimeError("isolated contention handler was not released")
        return {"complete": True, "fixture_site_id": site.id}

    monkeypatch.setitem(workflows.HANDLERS, "audit", local_audit)
    monkeypatch.setattr(worker, "SessionLocal", postgres_database.factory)
    monkeypatch.setattr(worker, "engine", postgres_database.engine)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(worker.run_job, holder_id)
        assert holder_started.wait(timeout=10)

        contender = pool.submit(worker.execute_job.run, contender_id)
        with pytest.raises(Retry):
            contender.result(timeout=10)

        with postgres_database.factory() as db:
            queued = db.get(Job, contender_id)
            assert queued is not None
            assert queued.status == "queued"
            assert queued.attempts == 0
            assert queued.lease_until is not None
            assert queued.lease_until >= queued.updated_at
            assert queued.available_at <= queued.lease_until
            assert queued.result == {}

        release_holder.set()
        assert holder.result(timeout=10)["complete"] is True

    with postgres_database.factory() as db:
        holder_row = db.get(Job, holder_id)
        contender_row = db.get(Job, contender_id)
        assert holder_row is not None and contender_row is not None
        assert holder_row.status == "complete"
        assert contender_row.status == "queued"
        assert contender_row.status != "complete"
        assert contender_row.lease_until is not None
