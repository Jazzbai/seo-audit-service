import asyncio
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.connectors.security import encrypt_credentials
from app.models import Base, Connection, Event, Incident, Site, Team
from app.notifications import (
    SMTP_DIGEST_INCIDENT_KEY,
    NotificationDeliveryError,
    digest,
)


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _site(db):
    team = Team(name="Notification test team")
    db.add(team)
    db.flush()
    site = Site(
        team_id=team.id,
        name="Notification test site",
        origin="https://notifications.example.test",
    )
    db.add(site)
    db.flush()
    return site


def _smtp_connection(db, site, *, recipients=None, digest_enabled=None):
    credentials = {
        "host": "smtp.example.test",
        "username": "smtp-user",
        "password": "smtp-password-secret",
        "from_address": "reports@example.test",
    }
    connection_settings = {"recipients": recipients or ["owner@example.test"]}
    if digest_enabled is not None:
        connection_settings["digest_enabled"] = digest_enabled
    db.add(Connection(
        site_id=site.id,
        kind="smtp",
        encrypted_credentials=encrypt_credentials(credentials, settings.ENCRYPTION_KEY),
        status="configured",
        capabilities={"settings": connection_settings},
    ))
    db.commit()


class _FailingSMTP:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, username, password):
        raise RuntimeError("provider response exposed smtp-password-secret")

    def send_message(self, message):  # pragma: no cover - login fails first
        raise AssertionError("send should not run after login failure")


class _SuccessfulSMTP:
    deliveries = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, username, password):
        return (235, b"ok")

    def send_message(self, message):
        type(self).deliveries += 1


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "x" * 44)
    engine, factory = _database()
    try:
        yield factory
    finally:
        engine.dispose()


def test_digest_without_smtp_delivers_in_app_and_reports_needs_connection(database):
    with database() as db:
        site = _site(db)

        result = asyncio.run(digest(db, site, None))

        assert result["in_app"] == "delivered"
        assert result["email"] == "needs_connection"
        assert db.scalar(select(Incident).where(Incident.key == SMTP_DIGEST_INCIDENT_KEY)) is None
        assert db.scalar(select(Event).where(Event.kind == "weekly_digest")) is not None


def test_disabled_smtp_digest_keeps_in_app_delivery_and_skips_secret_work(database, monkeypatch):
    import app.notifications as notifications

    monkeypatch.setattr(
        notifications,
        "credentials",
        lambda *args, **kwargs: pytest.fail("disabled digest must not decrypt SMTP credentials"),
    )

    async def unexpected_dns(*args, **kwargs):
        pytest.fail("disabled digest must not resolve the SMTP host")

    monkeypatch.setattr(notifications, "public_addresses", unexpected_dns)
    monkeypatch.setattr(
        notifications.smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: pytest.fail("disabled digest must not open an SMTP connection"),
    )

    with database() as db:
        site = _site(db)
        _smtp_connection(db, site, digest_enabled=False)

        result = asyncio.run(digest(db, site, None))

        assert result["in_app"] == "delivered"
        assert result["email"] == "disabled"
        assert db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == SMTP_DIGEST_INCIDENT_KEY,
        )) is None
        assert db.scalar(select(Event).where(
            Event.site_id == site.id,
            Event.kind == "weekly_digest",
        )) is not None


def test_smtp_failure_opens_and_refreshes_one_safe_incident(database, monkeypatch):
    import app.notifications as notifications

    monkeypatch.setattr(notifications, "public_addresses", lambda host, port: asyncio.sleep(0))
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FailingSMTP)

    with database() as db:
        site = _site(db)
        _smtp_connection(db, site)

        with pytest.raises(NotificationDeliveryError) as first_error:
            asyncio.run(digest(db, site, None))
        first = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == SMTP_DIGEST_INCIDENT_KEY,
        ))
        assert first is not None
        assert first.status == "open"
        assert first.failure_count == 1
        assert first.details == {"channel": "smtp", "error_type": "smtp_login"}
        assert "smtp-password-secret" not in json.dumps(first.details)
        assert "provider response" not in str(first_error.value)
        assert not isinstance(first_error.value, ValueError)

        with pytest.raises(NotificationDeliveryError):
            asyncio.run(digest(db, site, None))
        refreshed = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == SMTP_DIGEST_INCIDENT_KEY,
        ))
        assert refreshed.id == first.id
        assert refreshed.status == "open"
        assert refreshed.failure_count == 2
        assert db.scalar(select(Incident).where(Incident.site_id == site.id)).id == first.id


def test_successful_digest_resolves_same_incident_idempotently(database, monkeypatch):
    import app.notifications as notifications

    monkeypatch.setattr(notifications, "public_addresses", lambda host, port: asyncio.sleep(0))
    _SuccessfulSMTP.deliveries = 0
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _SuccessfulSMTP)

    with database() as db:
        site = _site(db)
        _smtp_connection(db, site)
        incident = Incident(
            site_id=site.id,
            key=SMTP_DIGEST_INCIDENT_KEY,
            kind="notification",
            severity="medium",
            title="SMTP weekly digest delivery failed",
            status="open",
            details={"channel": "smtp", "error_type": "smtp_login"},
            failure_count=2,
        )
        db.add(incident)
        db.commit()

        first_result = asyncio.run(digest(db, site, None))
        db.refresh(incident)
        resolved_at = incident.resolved_at
        assert first_result["email"] == "delivered"
        assert incident.status == "resolved"
        assert resolved_at is not None
        assert incident.failure_count == 2

        asyncio.run(digest(db, site, None))
        db.refresh(incident)
        assert incident.status == "resolved"
        assert incident.resolved_at == resolved_at
        assert _SuccessfulSMTP.deliveries == 2
        assert db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == SMTP_DIGEST_INCIDENT_KEY,
        )).id == incident.id
