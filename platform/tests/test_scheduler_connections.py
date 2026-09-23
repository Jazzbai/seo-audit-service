from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, worker
from app.config import settings
from app.models import Base, Connection, Heartbeat, Incident, Job, Policy, Site, Team
from app.operations import QUEUE_HEALTH_INCIDENT_KEY
from app.policies import create_policy


@pytest.fixture
def scheduler_connections(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr("app.operations.now", lambda: current)
    monkeypatch.setattr(worker.execute_job, "apply_async", lambda *args, **kwargs: None)
    yield factory
    engine.dispose()


def _site(factory, *, competitors=None, tracked_questions=None):
    with factory() as db:
        team = Team(name="Scheduler connection team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Scheduler connection site",
            origin="https://scheduler-connection.example.test",
            paused=False,
        )
        db.add(site)
        db.flush()
        settings = {"enabled": True, "allowed_actions": []}
        if competitors is not None:
            settings["competitors"] = competitors
        if tracked_questions is not None:
            settings["tracked_questions"] = tracked_questions
        create_policy(db, site, None, settings)
        db.commit()
        return site.id


def test_scheduler_waits_for_verified_wordpress_connection(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="encrypted-test-value",
            status="needs_test",
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        kinds = {job.kind for job in db.scalars(select(Job).where(Job.site_id == site_id)).all()}
        assert kinds == {"availability"}


def test_scheduler_queues_wordpress_work_after_capability_test(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="encrypted-test-value",
            status="connected",
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        kinds = {job.kind for job in db.scalars(select(Job).where(Job.site_id == site_id)).all()}
        assert {"availability", "poll_changes", "inventory", "audit", "plan", "refresh", "digest"} <= kinds


@pytest.mark.parametrize(
    "kind,status,capabilities",
    [
        ("gsc", "needs_test", {}),
        ("gsc", "configured", {}),
        ("ga4", "needs_test", {}),
        ("ai", "configured", {
            "settings": {
                "endpoint": "https://api.example.test/responses",
                "model": "fixture-model",
                "estimated_cost_cents": 1,
                "max_cost_cents": 2,
            },
        }),
        ("dataforseo", "configured", {"settings": {"location_code": 2840}}),
    ],
)
def test_scheduler_waits_for_verified_or_paid_ready_visibility_connection(
    scheduler_connections,
    kind,
    status,
    capabilities,
):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind=kind,
            encrypted_credentials="encrypted-test-value",
            status=status,
            capabilities=capabilities,
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(
            Job.site_id == site_id,
            Job.kind == "visibility",
        )).all()
        assert jobs == []


def test_scheduler_queues_configured_dataforseo_after_credential_shape_check(
    scheduler_connections,
):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="dataforseo",
            encrypted_credentials="encrypted-test-value",
            status="configured",
            capabilities={"credential_shape_verified": True, "settings": {
                "estimated_cost_cents": 2,
                "max_cost_cents": 5,
            }},
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(
            Job.site_id == site_id,
            Job.kind == "visibility",
        )).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {"kind": "dataforseo"}


def test_scheduler_queues_ai_sample_only_for_valid_tracked_questions(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory, tracked_questions=["Which service should a driver call?"])
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="ai",
            encrypted_credentials="encrypted-test-value",
            status="connected",
            capabilities={"settings": {
                "endpoint": "https://api.example.test/responses",
                "model": "fixture-model",
                "estimated_cost_cents": 1,
                "max_cost_cents": 2,
            }},
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(
            Job.site_id == site_id,
            Job.kind == "visibility",
        )).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {"kind": "ai_sample"}


def test_scheduler_holds_ai_sample_for_malformed_tracked_questions(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory, tracked_questions=["valid question"])
    with factory() as db:
        policy = db.scalar(select(Policy).where(Policy.site_id == site_id))
        db.execute(update(Policy).where(Policy.id == policy.id).values(
            settings={**policy.settings, "tracked_questions": ["", "valid question"]},
        ))
        db.add(Connection(
            site_id=site_id,
            kind="ai",
            encrypted_credentials="encrypted-test-value",
            status="connected",
            capabilities={"settings": {
                "endpoint": "https://api.example.test/responses",
                "model": "fixture-model",
                "estimated_cost_cents": 1,
                "max_cost_cents": 2,
            }},
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(
            Job.site_id == site_id,
            Job.kind == "visibility",
        )).all()
        assert jobs == []


def test_scheduler_queues_policy_competitor_observation_as_distinct_dataforseo_job(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory, competitors=["https://rival.example", "second.example"])
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="dataforseo",
            encrypted_credentials="encrypted-test-value",
            status="connected",
            capabilities={"settings": {
                "location_code": 2840,
                "estimated_cost_cents": 2,
                "max_cost_cents": 5,
            }},
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(Job.site_id == site_id, Job.kind == "visibility")).all()
        normal = [job for job in jobs if job.payload.get("mode") is None]
        competitor = [job for job in jobs if job.payload.get("mode") == "competitors"]
        assert len(normal) == 1
        assert normal[0].payload == {"kind": "dataforseo"}
        assert len(competitor) == 1
        assert competitor[0].payload == {"kind": "dataforseo", "mode": "competitors"}
        assert normal[0].idempotency_key != competitor[0].idempotency_key
        assert ":competitors:" in competitor[0].idempotency_key


def test_scheduler_does_not_dispatch_newly_queued_jobs_twice(scheduler_connections, monkeypatch):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="encrypted-test-value",
            status="connected",
        ))
        db.commit()

    deliveries = []
    monkeypatch.setattr(
        worker.execute_job,
        "apply_async",
        lambda *args, **kwargs: deliveries.append(tuple(kwargs["args"])),
    )

    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(Job.site_id == site_id)).all()
        assert len(deliveries) == len(jobs)
        assert {delivery[0] for delivery in deliveries} == {job.id for job in jobs}


def test_scheduler_polls_every_five_minutes_but_reconciles_inventory_daily(scheduler_connections, monkeypatch):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="encrypted-test-value",
            status="connected",
        ))
        db.commit()

    current = [datetime(2026, 9, 16, 16, 0)]
    monkeypatch.setattr(scheduler, "now", lambda: current[0])
    monkeypatch.setattr("app.operations.now", lambda: current[0])
    scheduler.schedule()
    current[0] += timedelta(minutes=5)
    scheduler.schedule()

    with factory() as db:
        jobs = db.scalars(select(Job).where(Job.site_id == site_id)).all()
        assert len([job for job in jobs if job.kind == "poll_changes"]) == 2
        assert len([job for job in jobs if job.kind == "inventory"]) == 1


def test_scheduler_retries_expired_targeted_audit(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Job(
            site_id=site_id,
            kind="targeted_audit",
            status="running",
            payload={"resource_key": "posts:9"},
            idempotency_key="targeted-audit-expired",
            lease_until=datetime(2026, 9, 16, 15, 59),
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        job = db.scalar(select(Job).where(Job.idempotency_key == "targeted-audit-expired"))
        assert job is not None
        assert job.status == "retry"
        assert job.result["remote_outcome"] == "read_only"
        assert job.available_at >= datetime(2026, 9, 16, 16, 0)


def test_scheduler_retries_a_lease_expiring_at_the_current_tick(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory)
    with factory() as db:
        db.add(Job(
            site_id=site_id,
            kind="targeted_audit",
            status="running",
            payload={"resource_key": "posts:10"},
            idempotency_key="targeted-audit-expiring-now",
            lease_until=datetime(2026, 9, 16, 16, 0),
        ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        job = db.scalar(select(Job).where(Job.idempotency_key == "targeted-audit-expiring-now"))
        assert job is not None
        assert job.status == "retry"
        assert job.result["reason"] == "Worker lease expired"


def test_scheduler_reports_full_backlog_beyond_dispatch_batch(scheduler_connections):
    factory = scheduler_connections
    site_id = _site(factory)
    current = datetime(2026, 9, 16, 16, 0)
    overdue_at = current - timedelta(minutes=11)
    with factory() as db:
        # Reserve the normal availability slot so the scheduler does not add
        # another row while this test isolates queue-health accounting.
        db.add(Job(
            site_id=site_id,
            kind="availability",
            status="queued",
            payload={},
            idempotency_key=f"{site_id}:schedule:availability::{scheduler.bucket(current, 60)}",
            available_at=current,
        ))
        for index in range(201):
            db.add(Job(
                site_id=site_id,
                kind="audit",
                status="queued",
                payload={"batch": index},
                idempotency_key=f"backlog-audit-{index}",
                available_at=overdue_at,
            ))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        heartbeat = db.get(Heartbeat, "scheduler")
        assert heartbeat.details["due_jobs"] == 202
        assert heartbeat.details["missed_checks"] == 201
        assert heartbeat.details["queue_delay_seconds"] == 660
        incident = db.scalar(select(Incident).where(
            Incident.site_id == site_id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        ))
        assert incident is not None
        assert incident.status == "open"
        assert incident.details["due_jobs"] == 202
        assert incident.details["missed_checks"] == 201
