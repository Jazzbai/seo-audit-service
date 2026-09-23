from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler
from app.config import settings
from app.models import Base, Connection, Heartbeat, Incident, Site, Team
from app.operations import (
    WORDPRESS_CHANGE_POLL_INCIDENT_KEY,
    WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS,
    iso,
    monitoring_status,
)


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _site(db, name, *, checked_at):
    team = Team(name=f"{name} team")
    db.add(team)
    db.flush()
    site = Site(
        team_id=team.id,
        name=name,
        origin=f"https://{name.lower().replace(' ', '-')}.example.test",
    )
    db.add(site)
    db.flush()
    db.add(Connection(
        site_id=site.id,
        kind="wordpress",
        encrypted_credentials="encrypted-test-value",
        status="connected",
        checked_at=checked_at,
        created_at=checked_at,
    ))
    if db.get(Heartbeat, "scheduler") is None:
        db.add(Heartbeat(
            name="scheduler",
            last_seen_at=checked_at,
            details={"queue_delay_seconds": 0, "missed_checks": 0},
        ))
    db.flush()
    return site


def test_verified_connection_reports_not_yet_run_without_opening_initial_incident(monkeypatch):
    engine, factory = _database()
    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr("app.operations.now", lambda: current)
    try:
        with factory() as db:
            site = _site(db, "Freshness not yet run", checked_at=current)
            db.commit()

            result = monitoring_status(db, site_id=site.id)

            poll = result["wordpress_change_poll"]
            assert poll["status"] == "not_yet_run"
            assert poll["last_success_at"] is None
            assert poll["missed_window"] is False
            assert db.scalar(select(Incident).where(
                Incident.site_id == site.id,
                Incident.key == WORDPRESS_CHANGE_POLL_INCIDENT_KEY,
            )) is None
    finally:
        engine.dispose()


def test_scheduler_persists_site_incident_after_poll_window_is_missed(monkeypatch):
    engine, factory = _database()
    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr("app.operations.now", lambda: current)
    monkeypatch.setattr(scheduler, "now", lambda: current)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    from app import worker
    monkeypatch.setattr(worker.execute_job, "apply_async", lambda *args, **kwargs: None)
    try:
        with factory() as db:
            site = _site(
                db,
                "Freshness stale",
                checked_at=current - timedelta(seconds=WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS + 1),
            )
            connection = db.scalar(select(Connection).where(
                Connection.site_id == site.id,
                Connection.kind == "wordpress",
            ))
            connection.capabilities = {
                "change_poll": {
                    "last_success_at": iso(
                        current - timedelta(seconds=WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS + 1),
                    ),
                },
            }
            db.commit()

        scheduler.schedule()

        with factory() as db:
            row = db.scalar(select(Incident).where(
                Incident.site_id == site.id,
                Incident.key == WORDPRESS_CHANGE_POLL_INCIDENT_KEY,
            ))
            assert row is not None
            assert row.status == "open"
            assert row.details["status"] == "stale"
            assert row.details["missed_window"] is True
    finally:
        engine.dispose()


def test_successful_poll_reports_healthy_and_resolves_prior_incident(monkeypatch):
    engine, factory = _database()
    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr("app.operations.now", lambda: current)
    try:
        with factory() as db:
            site = _site(db, "Freshness recovery", checked_at=current - timedelta(hours=1))
            connection = db.scalar(select(Connection).where(
                Connection.site_id == site.id,
                Connection.kind == "wordpress",
            ))
            connection.capabilities = {
                "change_poll": {
                    "last_success_at": iso(
                        current - timedelta(seconds=WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS + 1),
                    ),
                },
            }
            db.commit()
            assert monitoring_status(db, site_id=site.id)["wordpress_change_poll"]["status"] == "stale"
            row = db.scalar(select(Incident).where(
                Incident.site_id == site.id,
                Incident.key == WORDPRESS_CHANGE_POLL_INCIDENT_KEY,
            ))
            assert row.status == "open"

            connection.capabilities = {
                "change_poll": {"last_success_at": iso(current - timedelta(seconds=30))},
            }
            db.commit()
            result = monitoring_status(db, site_id=site.id)

            assert result["wordpress_change_poll"]["status"] == "healthy"
            assert result["wordpress_change_poll"]["seconds_since_success"] == 30
            assert row.status == "resolved"
            assert row.resolved_at == current
    finally:
        engine.dispose()


def test_monitoring_freshness_and_incidents_are_site_scoped(monkeypatch):
    engine, factory = _database()
    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr("app.operations.now", lambda: current)
    try:
        with factory() as db:
            first = _site(db, "Tenant stale", checked_at=current - timedelta(hours=1))
            second = _site(db, "Tenant healthy", checked_at=current)
            first_connection = db.scalar(select(Connection).where(
                Connection.site_id == first.id,
                Connection.kind == "wordpress",
            ))
            second_connection = db.scalar(select(Connection).where(
                Connection.site_id == second.id,
                Connection.kind == "wordpress",
            ))
            first_connection.capabilities = {
                "change_poll": {
                    "last_success_at": iso(
                        current - timedelta(seconds=WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS + 1),
                    ),
                },
            }
            second_connection.capabilities = {
                "change_poll": {"last_success_at": iso(current - timedelta(seconds=30))},
            }
            db.commit()

            first_result = monitoring_status(db, site_id=first.id)
            first_rows = db.scalars(select(Incident)).all()
            assert first_result["wordpress_change_poll"]["status"] == "stale"
            first_poll_rows = [
                row for row in first_rows if row.key == WORDPRESS_CHANGE_POLL_INCIDENT_KEY
            ]
            assert {(row.site_id, row.key) for row in first_poll_rows} == {
                (first.id, WORDPRESS_CHANGE_POLL_INCIDENT_KEY),
            }
            assert not any(row.site_id == second.id for row in first_poll_rows)

            second_result = monitoring_status(db, site_id=second.id)
            rows = db.scalars(select(Incident)).all()
            assert second_result["wordpress_change_poll"]["status"] == "healthy"
            poll_rows = [row for row in rows if row.key == WORDPRESS_CHANGE_POLL_INCIDENT_KEY]
            assert {(row.site_id, row.key) for row in poll_rows} == {
                (first.id, WORDPRESS_CHANGE_POLL_INCIDENT_KEY),
            }
            assert all(row.site_id == first.id for row in poll_rows)
    finally:
        engine.dispose()
