from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Heartbeat, Incident, Site, Team
from app.operations import (
    QUEUE_HEALTH_INCIDENT_KEY,
    SCHEDULER_HEALTH_INCIDENT_KEY,
    monitoring_status,
    sync_health_incident,
    sync_monitoring_incidents,
)


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _site(db, name="Health test site"):
    team = Team(name=f"{name} team")
    db.add(team)
    db.flush()
    site = Site(team_id=team.id, name=name, origin="https://health.example.test")
    db.add(site)
    db.flush()
    return site


def test_scheduler_incident_is_site_scoped_and_refreshes_idempotently(monkeypatch):
    factory = _database()
    first = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    second = first + timedelta(seconds=30)
    current = [first]

    import app.operations as operations

    monkeypatch.setattr(operations, "now", lambda: current[0])
    with factory() as db:
        site_a = _site(db, "Site A")
        site_b = _site(db, "Site B")
        sync_monitoring_incidents(
            db,
            scheduler_healthy=False,
            queue_healthy=None,
            scheduler_details={"seconds_since_heartbeat": 200},
        )
        db.commit()
        first_rows = db.scalars(select(Incident).order_by(Incident.site_id)).all()
        assert len(first_rows) == 2
        assert {row.site_id for row in first_rows} == {site_a.id, site_b.id}
        assert all(row.key == SCHEDULER_HEALTH_INCIDENT_KEY for row in first_rows)
        assert all(row.status == "open" and row.failure_count == 1 for row in first_rows)

        current[0] = second
        sync_monitoring_incidents(
            db,
            scheduler_healthy=False,
            queue_healthy=None,
            scheduler_details={"seconds_since_heartbeat": 230},
        )
        db.commit()
        refreshed = db.scalars(select(Incident).order_by(Incident.site_id)).all()
        assert [row.id for row in refreshed] == [row.id for row in first_rows]
        assert all(row.failure_count == 2 for row in refreshed)
        assert all(row.last_seen_at == second for row in refreshed)


def test_queue_incident_resolves_once_when_health_returns(monkeypatch):
    factory = _database()
    first = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    second = first + timedelta(seconds=30)
    third = second + timedelta(seconds=30)
    current = [first]

    import app.operations as operations

    monkeypatch.setattr(operations, "now", lambda: current[0])
    with factory() as db:
        site = _site(db)
        sync_monitoring_incidents(
            db,
            scheduler_healthy=True,
            queue_healthy=False,
            queue_details={"queue_delay_seconds": 900},
        )
        db.commit()
        row = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        ))
        assert row is not None
        assert row.status == "open"
        assert row.failure_count == 1

        current[0] = second
        sync_monitoring_incidents(
            db,
            scheduler_healthy=True,
            queue_healthy=False,
            queue_details={"queue_delay_seconds": 1200},
        )
        db.commit()
        assert row.id == db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        )).id
        assert row.failure_count == 2
        assert row.last_seen_at == second

        current[0] = third
        sync_monitoring_incidents(db, scheduler_healthy=True, queue_healthy=True)
        db.commit()
        assert row.status == "resolved"
        resolved_at = row.resolved_at
        assert resolved_at == third

        sync_monitoring_incidents(db, scheduler_healthy=True, queue_healthy=True)
        db.commit()
        assert row.status == "resolved"
        assert row.resolved_at == resolved_at
        assert db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        )).id == row.id


def test_concurrent_incident_insert_merges_the_losing_observation(monkeypatch):
    factory = _database()
    first = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    second = first + timedelta(seconds=30)
    current = [second]

    import app.operations as operations

    monkeypatch.setattr(operations, "now", lambda: current[0])
    with factory() as db:
        site = _site(db, "Concurrent incident site")
        winner = Incident(
            site_id=site.id,
            key="monitoring:concurrent",
            kind="monitoring",
            severity="high",
            title="Queue processing is delayed",
            status="open",
            details={"queue_delay_seconds": 900},
            failure_count=1,
            first_seen_at=first,
            last_seen_at=first,
        )
        db.add(winner)
        db.commit()

        original_scalar = db.scalar
        calls = 0

        def hide_first_lookup(statement, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(db, "scalar", hide_first_lookup)
        merged = sync_health_incident(
            db,
            site,
            key=winner.key,
            healthy=False,
            title=winner.title,
            details={"queue_delay_seconds": 1200},
        )

        assert merged.id == winner.id
        assert merged.status == "open"
        assert merged.failure_count == 2
        assert merged.first_seen_at == first
        assert merged.last_seen_at == second
        assert merged.details == {"queue_delay_seconds": 1200}
        db.commit()
        stored = db.get(Incident, winner.id)
        assert stored.failure_count == 2
        assert stored.last_seen_at == second
        assert stored.details == {"queue_delay_seconds": 1200}


def test_monitoring_status_persists_stale_scheduler_and_healthy_cycle_resolves(monkeypatch):
    factory = _database()
    current = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    later = current + timedelta(seconds=1)
    current_clock = [current]

    import app.operations as operations

    monkeypatch.setattr(operations, "now", lambda: current_clock[0])
    with factory() as db:
        site = _site(db)
        db.add(Heartbeat(
            name="scheduler",
            last_seen_at=current - timedelta(seconds=200),
            details={"queue_delay_seconds": 0, "missed_checks": 0},
        ))
        db.commit()

        result = monitoring_status(db)
        assert result["status"] == "degraded"
        row = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == SCHEDULER_HEALTH_INCIDENT_KEY,
        ))
        assert row is not None
        assert row.status == "open"

        heartbeat = db.get(Heartbeat, "scheduler")
        heartbeat.last_seen_at = later
        heartbeat.details = {"queue_delay_seconds": 0, "missed_checks": 0}
        db.commit()
        current_clock[0] = later
        assert monitoring_status(db)["status"] == "running"
        assert row.status == "resolved"


def test_monitoring_status_treats_missed_checks_as_queue_degraded(monkeypatch):
    factory = _database()
    current = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)

    import app.operations as operations

    monkeypatch.setattr(operations, "now", lambda: current)
    with factory() as db:
        site = _site(db)
        db.add(Heartbeat(
            name="scheduler",
            last_seen_at=current - timedelta(seconds=1),
            details={"queue_delay_seconds": 300, "missed_checks": 1},
        ))
        db.commit()

        result = monitoring_status(db)
        assert result["status"] == "degraded"
        row = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        ))
        assert row is not None
        assert row.status == "open"


def test_monitoring_status_fails_closed_on_future_scheduler_heartbeat(monkeypatch):
    factory = _database()
    current = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)

    import app.operations as operations

    monkeypatch.setattr(operations, "now", lambda: current)
    with factory() as db:
        site = _site(db, "Future heartbeat site")
        db.add(Heartbeat(
            name="scheduler",
            last_seen_at=current + timedelta(seconds=30),
            details={"queue_delay_seconds": 0, "missed_checks": 0},
        ))
        db.commit()

        result = monitoring_status(db)

        assert result["status"] == "degraded"
        assert result["seconds_since_heartbeat"] is None
        assert result["message"] == "Scheduler heartbeat timestamp is in the future"
        row = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == SCHEDULER_HEALTH_INCIDENT_KEY,
        ))
        assert row is not None
        assert row.status == "open"
        assert row.details["invalid_timestamp"] is True
        assert db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == QUEUE_HEALTH_INCIDENT_KEY,
        )) is None
