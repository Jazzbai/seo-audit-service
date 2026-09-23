"""Acceptance-level invariants for unattended monitoring safety."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import operations, scheduler
from app.models import Base, Heartbeat, Incident, Site, Team


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_failed_scheduler_cycle_does_not_refresh_prior_heartbeat(
    monkeypatch,
):
    """A heartbeat must prove a completed cycle, not scheduler entry."""

    engine, factory = _database()
    current = datetime(2026, 9, 23, 12, 0)
    previous = current - timedelta(
        seconds=operations.SCHEDULER_STALE_AFTER_SECONDS + 1,
    )
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr(operations, "now", lambda: current)
    monkeypatch.setattr(
        scheduler,
        "current_policy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated site scheduling failure")
        ),
    )

    try:
        with factory() as db:
            team = Team(name="Monitoring acceptance team")
            db.add(team)
            db.flush()
            site = Site(
                team_id=team.id,
                name="Monitoring acceptance site",
                origin="https://monitoring-acceptance.example.test",
            )
            db.add(site)
            db.add(Heartbeat(
                name="scheduler",
                last_seen_at=previous,
                details={
                    "queue_delay_seconds": 0,
                    "missed_checks": 0,
                    "checked_at": previous.isoformat(),
                },
            ))
            db.commit()
            site_id = site.id

        with pytest.raises(RuntimeError, match="simulated site scheduling failure"):
            scheduler.schedule()

        with factory() as db:
            heartbeat = db.get(Heartbeat, "scheduler")
            assert heartbeat.last_seen_at == previous
            assert heartbeat.details["checked_at"] == previous.isoformat()

            result = operations.monitoring_status(db, site_id=site_id)
            assert result["status"] == "degraded"
            incident = db.scalar(select(Incident).where(
                Incident.site_id == site_id,
                Incident.key == operations.SCHEDULER_HEALTH_INCIDENT_KEY,
            ))
            assert incident is not None
            assert incident.status == "open"
    finally:
        engine.dispose()
