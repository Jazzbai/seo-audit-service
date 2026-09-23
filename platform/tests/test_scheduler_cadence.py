from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, operations, worker
from app.config import settings
from app.models import Base, Connection, Heartbeat, Incident, Job, Site, Team


@pytest.fixture
def cadence_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = datetime(2026, 9, 21, 16, 0)
    monkeypatch.setattr(operations, "now", lambda: current)
    yield factory, current
    engine.dispose()


def _site(db, current, *, wordpress=True, capabilities=None):
    team = Team(name="Cadence test team")
    db.add(team)
    db.flush()
    site = Site(
        team_id=team.id,
        name="Cadence test site",
        origin="https://cadence.example.test",
    )
    db.add(site)
    db.flush()
    if wordpress:
        db.add(Connection(
            site_id=site.id,
            kind="wordpress",
            encrypted_credentials="encrypted-test-value",
            status="connected",
            capabilities=capabilities or {},
            checked_at=current,
            created_at=current,
        ))
    db.add(Heartbeat(
        name="scheduler",
        last_seen_at=current,
        details={"queue_delay_seconds": 0, "missed_checks": 0},
    ))
    db.flush()
    return site


def _job(db, site, kind, at, *, status="complete", result=None, suffix=""):
    row = Job(
        site_id=site.id,
        kind=kind,
        status=status,
        payload={},
        result=result or {"complete": True},
        idempotency_key=f"cadence:{site.id}:{kind}:{at.isoformat()}:{suffix}",
        available_at=at,
        created_at=at,
        updated_at=at,
    )
    db.add(row)
    return row


def test_cadence_health_is_unknown_until_durable_success_exists(cadence_database):
    factory, current = cadence_database
    with factory() as db:
        site = _site(db, current)
        db.commit()

        result = operations.monitoring_status(db, site_id=site.id)

        cadence = result["cadence"]
        assert set(cadence) == {
            "availability",
            "wordpress_change_poll",
            "inventory",
            "audit",
            "plan",
            "refresh",
        }
        assert cadence["wordpress_change_poll"]["status"] == "unknown"
        assert cadence["availability"]["status"] == "unknown"
        assert cadence["inventory"]["status"] == "unknown"
        assert cadence["audit"]["status"] == "unknown"
        assert cadence["plan"]["status"] == "unknown"
        assert cadence["refresh"]["status"] == "unknown"
        assert all(item["last_success_at"] is None for item in cadence.values())
        assert not db.scalars(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key.like(f"{operations.CADENCE_INCIDENT_PREFIX}%"),
        )).all()


def test_cadence_health_uses_completed_jobs_and_poll_marker(cadence_database):
    factory, current = cadence_database
    poll_at = current - timedelta(minutes=2)
    with factory() as db:
        site = _site(
            db,
            current,
            capabilities={"change_poll": {"last_success_at": operations.iso(poll_at)}},
        )
        for kind, age in {
            "availability": timedelta(seconds=30),
            "poll_changes": timedelta(minutes=2),
            "inventory": timedelta(hours=1),
            "audit": timedelta(days=1),
            "plan": timedelta(days=1),
            "refresh": timedelta(days=1),
        }.items():
            _job(db, site, kind, current - age)
        db.commit()

        cadence = operations.monitoring_status(db, site_id=site.id)["cadence"]

        assert all(item["status"] == "healthy" for item in cadence.values())
        assert cadence["availability"]["interval_seconds"] == 60
        assert cadence["wordpress_change_poll"]["interval_seconds"] == 300
        assert cadence["inventory"]["interval_seconds"] == 86400
        assert cadence["audit"]["interval_seconds"] == 604800
        assert cadence["plan"]["last_job_status"] == "complete"
        assert cadence["refresh"]["evidence"] == "job"
        assert cadence["wordpress_change_poll"]["evidence"].endswith("last_success_at")


def test_audit_errors_are_partial_cadence_and_open_durable_incident(cadence_database):
    factory, current = cadence_database
    with factory() as db:
        site = _site(db, current, wordpress=False)
        _job(
            db,
            site,
            "audit",
            current,
            result={
                "complete": True,
                "errors": ["https://example.test/image.webp: non-html response"],
                "pending_urls": [],
            },
        )
        db.commit()

        result = operations.monitoring_status(db, site_id=site.id)

        audit = result["cadence"]["audit"]
        assert audit["status"] == "partial"
        assert audit["last_job_status"] == "partial"
        assert audit["last_success_at"] is None
        assert result["status"] == "degraded"

        incident = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == f"{operations.CADENCE_INCIDENT_PREFIX}audit",
        ))
        assert incident is not None
        assert incident.status == "open"
        assert incident.details["status"] == "partial"


def test_cadence_does_not_promote_failed_partial_pending_or_future_rows(cadence_database):
    factory, current = cadence_database
    with factory() as db:
        site = _site(db, current)
        _job(
            db,
            site,
            "availability",
            current,
            status="queued",
            result={},
        )
        _job(
            db,
            site,
            "inventory",
            current,
            status="failed",
            result={"reason": "connection revoked"},
        )
        _job(
            db,
            site,
            "audit",
            current,
            status="complete",
            result={"complete": False},
        )
        _job(
            db,
            site,
            "plan",
            current,
            status="running",
            result={},
        )
        _job(
            db,
            site,
            "refresh",
            current + timedelta(seconds=1),
            status="complete",
        )
        db.commit()

        cadence = operations.monitoring_status(db, site_id=site.id)["cadence"]

        assert cadence["availability"]["status"] == "unknown"
        assert cadence["availability"]["last_success_at"] is None
        assert cadence["inventory"]["status"] == "failed"
        assert cadence["audit"]["status"] == "partial"
        assert cadence["plan"]["status"] == "unknown"
        assert cadence["refresh"]["status"] == "unknown"
        assert cadence["refresh"]["invalid_timestamp"] is True
        assert operations.monitoring_status(db, site_id=site.id)["status"] == "degraded"


def test_stale_cadences_open_durable_incidents(cadence_database):
    factory, current = cadence_database
    poll_at = current - timedelta(seconds=operations.WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS + 1)
    with factory() as db:
        site = _site(
            db,
            current,
            capabilities={"change_poll": {"last_success_at": operations.iso(poll_at)}},
        )
        _job(
            db,
            site,
            "availability",
            current - timedelta(seconds=operations.CADENCE_STALE_AFTER_SECONDS["availability"] + 1),
        )
        _job(
            db,
            site,
            "inventory",
            current - timedelta(seconds=operations.CADENCE_STALE_AFTER_SECONDS["inventory"] + 1),
        )
        _job(
            db,
            site,
            "audit",
            current - timedelta(seconds=operations.CADENCE_STALE_AFTER_SECONDS["audit"] + 1),
        )
        db.commit()

        result = operations.monitoring_status(db, site_id=site.id)
        cadence = result["cadence"]

        assert cadence["availability"]["status"] == "stale"
        assert cadence["inventory"]["status"] == "stale"
        assert cadence["audit"]["status"] == "stale"
        assert cadence["wordpress_change_poll"]["status"] == "stale"
        keys = {
            row.key for row in db.scalars(select(Incident).where(Incident.site_id == site.id)).all()
        }
        assert f"{operations.CADENCE_INCIDENT_PREFIX}availability" in keys
        assert f"{operations.CADENCE_INCIDENT_PREFIX}inventory" in keys
        assert f"{operations.CADENCE_INCIDENT_PREFIX}audit" in keys
        assert operations.WORDPRESS_CHANGE_POLL_INCIDENT_KEY in keys


def test_scheduler_persists_stale_cadence_incident_from_durable_job(cadence_database, monkeypatch):
    factory, current = cadence_database
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(worker.execute_job, "apply_async", lambda *args, **kwargs: None)
    with factory() as db:
        site = _site(db, current, wordpress=False)
        row = _job(
            db,
            site,
            "availability",
            current - timedelta(seconds=operations.CADENCE_STALE_AFTER_SECONDS["availability"] + 1),
        )
        row.idempotency_key = f"{site.id}:schedule:availability::{scheduler.bucket(current, 60)}"
        db.commit()

    scheduler.schedule()

    with factory() as db:
        incident = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == f"{operations.CADENCE_INCIDENT_PREFIX}availability",
        ))
        assert incident is not None
        assert incident.status == "open"
        assert incident.details["status"] == "stale"
