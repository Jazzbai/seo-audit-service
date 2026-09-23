"""Repeatable scheduler/worker interruption drill for unattended operation."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, worker
from app.config import settings
from app.models import Base, Event, Heartbeat, Incident, Job, Site, Team
from app.operations import QUEUE_HEALTH_INCIDENT_KEY
from app.workflows import HANDLERS


@pytest.fixture
def scheduler_longrun(monkeypatch):
    """Use one deterministic database/clock for repeated scheduler ticks."""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = [datetime(2026, 9, 18, 16, 0)]
    deliveries = []

    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: clock[0])
    monkeypatch.setattr("app.operations.now", lambda: clock[0])
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "now", lambda: clock[0])

    def capture_delivery(*args, **kwargs):
        deliveries.append((kwargs["args"][0], kwargs.get("queue")))

    monkeypatch.setattr(worker.execute_job, "apply_async", capture_delivery)
    yield factory, clock, deliveries
    engine.dispose()


def _site(factory):
    with factory() as db:
        team = Team(name="Scheduler long-run team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Scheduler long-run site",
            origin="https://scheduler-longrun.example.test",
            paused=True,
        )
        db.add(site)
        db.commit()
        return site.id


def test_repeated_ticks_keep_dispatch_bounded_and_keep_stale_queue_visible(
    scheduler_longrun,
):
    """A broker/worker outage cannot replay the same rows on every tick."""

    factory, clock, deliveries = scheduler_longrun
    site_id = _site(factory)
    overdue_at = clock[0] - timedelta(minutes=11)
    backlog_size = 205

    with factory() as db:
        # Reserve the scheduler's routine availability slot so its immediate
        # enqueue delivery cannot be confused with the bounded queue sweep.
        db.add(Job(
            site_id=site_id,
            kind="availability",
            status="queued",
            payload={},
            idempotency_key=(
                f"{site_id}:schedule:availability::"
                f"{scheduler.bucket(clock[0], 60)}"
            ),
            available_at=clock[0],
        ))
        for index in range(backlog_size):
            db.add(Job(
                site_id=site_id,
                kind="audit",
                status="queued",
                payload={"batch": index},
                idempotency_key=f"longrun-audit-{index}",
                available_at=overdue_at,
            ))
        db.commit()

    per_tick = []
    for _ in range(3):
        before = len(deliveries)
        scheduler.schedule()
        per_tick.append(len(deliveries) - before)
        clock[0] += timedelta(seconds=20)

    # The first batch is delivered once.  The six rows outside that batch are
    # still eligible and are delivered on the next tick; the first 200 are in
    # their broker-delivery cooldown and are not replayed.
    assert per_tick == [200, 6, 0]

    with factory() as db:
        heartbeat = db.get(Heartbeat, "scheduler")
        assert heartbeat is not None
        assert heartbeat.details["dispatched"] == 0
        assert heartbeat.details["due_jobs"] == backlog_size + 1
        assert heartbeat.details["missed_checks"] == backlog_size
        assert heartbeat.details["queue_delay_seconds"] == 700

        incident = db.scalar(select(Incident).where(
            Incident.site_id == site_id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        ))
        assert incident is not None
        assert incident.status == "open"
        assert incident.failure_count == 3
        assert incident.details["due_jobs"] == backlog_size + 1
        assert incident.details["missed_checks"] == backlog_size

        # The durable rows remain available for a future healthy worker; the
        # scheduler never marks delivery as completion merely because it sent
        # a bounded broker batch.
        assert db.scalar(select(Job).where(
            Job.site_id == site_id,
            Job.status == "queued",
        )) is not None


def test_queued_delivery_retries_after_cooldown(scheduler_longrun):
    """An unclaimed durable row is retried later without per-tick flooding."""

    factory, clock, deliveries = scheduler_longrun
    site_id = _site(factory)
    with factory() as db:
        job = Job(
            site_id=site_id,
            kind="audit",
            status="queued",
            payload={"batch": "cooldown"},
            idempotency_key="longrun-dispatch-cooldown",
            available_at=clock[0] - timedelta(minutes=2),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    scheduler.schedule()
    assert [identifier for identifier, _queue in deliveries].count(job_id) == 1

    clock[0] += timedelta(seconds=30)
    scheduler.schedule()
    assert [identifier for identifier, _queue in deliveries].count(job_id) == 1

    clock[0] += timedelta(seconds=31)
    scheduler.schedule()
    assert [identifier for identifier, _queue in deliveries].count(job_id) == 2


def test_interrupted_worker_lease_is_recovered_and_can_complete(
    scheduler_longrun, monkeypatch,
):
    """A process interruption leaves a lease that a later tick safely retries."""

    factory, clock, deliveries = scheduler_longrun
    site_id = _site(factory)
    with factory() as db:
        job = Job(
            site_id=site_id,
            kind="targeted_audit",
            status="queued",
            payload={"resource_key": "posts:interrupted"},
            idempotency_key="longrun-interrupted-targeted-audit",
            available_at=clock[0],
        )
        db.add(job)
        db.commit()
        job_id = job.id

    async def process_interrupts(db, site, interrupted_job):
        raise KeyboardInterrupt("simulated worker process interruption")

    monkeypatch.setitem(HANDLERS, "targeted_audit", process_interrupts)
    with pytest.raises(KeyboardInterrupt, match="simulated worker process interruption"):
        worker.run_job(job_id)

    with factory() as db:
        interrupted = db.get(Job, job_id)
        assert interrupted.status == "running"
        assert interrupted.attempts == 1
        assert interrupted.lease_until == clock[0] + timedelta(minutes=16)

    # The worker is now gone; the next scheduler tick is later than its lease.
    clock[0] += timedelta(minutes=17)
    scheduler.schedule()

    with factory() as db:
        recovered = db.get(Job, job_id)
        assert recovered.status == "retry"
        assert recovered.attempts == 1
        assert recovered.lease_until == clock[0] + timedelta(seconds=60)
        assert recovered.available_at == clock[0]
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
        assert job_id in {job_id for job_id, _queue in deliveries}

    async def complete_after_recovery(db, site, recovered_job):
        return {"complete": True, "recovered": True}

    monkeypatch.setitem(HANDLERS, "targeted_audit", complete_after_recovery)
    result = worker.run_job(job_id)

    assert result == {"complete": True, "recovered": True}
    with factory() as db:
        completed = db.get(Job, job_id)
        assert completed.status == "complete"
        assert completed.attempts == 2
        assert completed.lease_until is None


@pytest.mark.parametrize(
    ("mode", "parent_status", "parent_delivery"),
    [
        ("read_only", "retry", True),
        ("autopilot", "needs_reconciliation", False),
    ],
)
def test_interrupted_full_cycle_releases_orphaned_stage_without_blind_retry(
    scheduler_longrun, mode, parent_status, parent_delivery,
):
    """An orphaned full-cycle stage is resumed only through its parent."""

    factory, clock, deliveries = scheduler_longrun
    site_id = _site(factory)
    with factory() as db:
        parent = Job(
            site_id=site_id,
            kind="full_cycle",
            status="running",
            payload={"mode": mode},
            idempotency_key=f"full-cycle-parent-{mode}",
            attempts=1,
            available_at=clock[0] - timedelta(hours=1),
            lease_until=clock[0] - timedelta(minutes=1),
        )
        db.add(parent)
        db.flush()
        stage = Job(
            site_id=site_id,
            kind="public_audit",
            status="running",
            payload={
                "full_cycle_parent_job_id": parent.id,
                "full_cycle_stage": "public_audit",
            },
            idempotency_key=f"full-cycle-stage-{mode}",
            attempts=1,
            available_at=clock[0] - timedelta(hours=1),
            # Full-cycle stages are owned by the parent and historically did
            # not receive a separate execution lease.
            lease_until=None,
        )
        db.add(stage)
        db.commit()
        parent_id, stage_id = parent.id, stage.id

    scheduler.schedule()

    with factory() as db:
        recovered_parent = db.get(Job, parent_id)
        recovered_stage = db.get(Job, stage_id)
        assert recovered_parent.status == parent_status
        assert recovered_stage.status == "needs_reconciliation"
        assert recovered_stage.lease_until is None
        assert recovered_stage.result == {
            "reason": "Parent worker lease expired",
            "remote_outcome": "read_only" if parent_status == "retry" else "unknown",
            "parent_job_id": parent_id,
        }
        assert recovered_stage.available_at == clock[0]

    assert (parent_id in [job_id for job_id, _queue in deliveries]) is parent_delivery
    assert stage_id not in [job_id for job_id, _queue in deliveries]
