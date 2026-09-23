from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import operations
from app.models import Base, Heartbeat, Incident, Site, Team


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _site(db):
    team = Team(name="Queue metrics team")
    db.add(team)
    db.flush()
    site = Site(
        team_id=team.id,
        name="Queue metrics site",
        origin="https://queue-metrics.example.test",
    )
    db.add(site)
    db.flush()
    return site


@pytest.mark.parametrize(
    ("details", "error"),
    [
        ({"queue_delay_seconds": 0}, "missed_checks_missing"),
        ({"missed_checks": 0}, "queue_delay_seconds_missing"),
        ({"queue_delay_seconds": -1, "missed_checks": 0}, "queue_delay_seconds_invalid"),
        ({"queue_delay_seconds": "0", "missed_checks": 0}, "queue_delay_seconds_invalid"),
        ({"queue_delay_seconds": float("inf"), "missed_checks": 0}, "queue_delay_seconds_invalid"),
        ({"queue_delay_seconds": 0, "missed_checks": True}, "missed_checks_invalid"),
    ],
)
def test_fresh_heartbeat_with_invalid_queue_metrics_is_degraded(details, error, monkeypatch):
    engine, factory = _database()
    current = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(operations, "now", lambda: current)

    try:
        with factory() as db:
            site = _site(db)
            db.add(Heartbeat(name="scheduler", last_seen_at=current, details=details))
            db.commit()

            result = operations.monitoring_status(db, site_id=site.id)

            assert result["status"] == "degraded"
            assert result["queue_metrics_valid"] is False
            assert error in result["queue_metrics_errors"]
            assert result["message"] == "Scheduler heartbeat queue metrics are missing or invalid"

            incident = db.scalar(select(Incident).where(
                Incident.site_id == site.id,
                Incident.key == operations.QUEUE_HEALTH_INCIDENT_KEY,
            ))
            assert incident is not None
            assert incident.status == "open"
            assert incident.details["metrics_valid"] is False
            assert error in incident.details["validation_errors"]
    finally:
        engine.dispose()
