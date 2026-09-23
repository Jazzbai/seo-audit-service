"""In-app digests with optional authenticated TLS email delivery."""
import asyncio
import smtplib
import ssl
from collections.abc import Mapping
from datetime import timedelta
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import func, select

from app.models import Finding, Incident, Publication
from app.network import public_addresses
from app.operations import credentials, event, find_connection, now, sync_health_incident


SMTP_DIGEST_INCIDENT_KEY = "notifications:smtp_digest"
SMTP_DIGEST_INCIDENT_TITLE = "SMTP weekly digest delivery failed"


class NotificationDeliveryError(RuntimeError):
    """Safe, non-provider-specific failure raised after an incident is saved."""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__("SMTP digest delivery failed")


class _ValidationFailure(Exception):
    def __init__(self, error_type: str):
        self.error_type = error_type


class _SMTPStageFailure(Exception):
    def __init__(self, error_type: str):
        self.error_type = error_type


def _persist_failure(db, site, error_type: str):
    """Persist only a bounded error classification before raising safely."""

    sync_health_incident(
        db,
        site,
        key=SMTP_DIGEST_INCIDENT_KEY,
        healthy=False,
        title=SMTP_DIGEST_INCIDENT_TITLE,
        kind="notification",
        severity="medium",
        details={"channel": "smtp", "error_type": error_type},
    )
    # The worker rolls back the workflow transaction when the handler raises.
    # Commit the in-app event and incident so the failure remains visible.
    db.commit()
    raise NotificationDeliveryError(error_type) from None


def _validated_recipients(config: Mapping, secret: Mapping) -> list[str]:
    if not isinstance(config, Mapping) or not isinstance(secret, Mapping):
        raise _ValidationFailure("smtp_credentials")
    recipients = config.get("recipients", secret.get("recipients", []))
    if not isinstance(recipients, list) or not recipients or len(recipients) > 10:
        raise _ValidationFailure("smtp_recipient_validation")

    cleaned = []
    for recipient in recipients:
        if not isinstance(recipient, str):
            raise _ValidationFailure("smtp_recipient_validation")
        address = recipient.strip()
        parsed = parseaddr(address)[1]
        if not address or parsed != address or "@" not in address:
            raise _ValidationFailure("smtp_recipient_validation")
        cleaned.append(address)
    return cleaned


def _validated_settings(config, secret) -> tuple[str, str, str, str]:
    if not isinstance(config, Mapping) or not isinstance(secret, Mapping):
        raise _ValidationFailure("smtp_credentials")

    host = config.get("host", secret.get("host", ""))
    sender = config.get("sender") or secret.get("from_address")
    username = secret.get("username")
    password = secret.get("password")
    if not all(isinstance(value, str) and value.strip() for value in (host, sender, username, password)):
        raise _ValidationFailure("smtp_credentials")
    return host.strip(), sender.strip(), username, password


def _digest_email_enabled(connection) -> bool:
    """Honor the site's explicit opt-out without touching SMTP credentials."""

    capabilities = connection.capabilities if isinstance(connection.capabilities, Mapping) else {}
    configured = capabilities.get("settings", {})
    if not isinstance(configured, Mapping):
        return True
    # Missing settings preserve the historical opt-in default.  Only a real
    # boolean False disables delivery; malformed values must not silently turn
    # a configured digest off.
    return configured.get("digest_enabled", True) is not False


def _deliver(host: str, username: str, password: str, message: EmailMessage):
    try:
        with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=20) as smtp:
            try:
                smtp.login(username, password)
            except Exception:
                raise _SMTPStageFailure("smtp_login") from None
            try:
                smtp.send_message(message)
            except Exception:
                raise _SMTPStageFailure("smtp_send") from None
    except _SMTPStageFailure:
        raise
    except Exception:
        raise _SMTPStageFailure("smtp_tls") from None


async def digest(db,site,job):
    counts = {}
    for label,model,condition in [('open_issues',Finding,Finding.status == 'open'),('open_incidents',Incident,Incident.status == 'open'),('publications',Publication,(Publication.status == 'published') & (Publication.created_at >= now()-timedelta(days=7)))]:
        counts[label] = db.scalar(select(func.count()).select_from(model).where(model.site_id == site.id,condition)) or 0
    event(db,site,'weekly_digest','Weekly site report is available',counts)
    # Make the in-app delivery durable even when the optional email channel
    # fails and the worker rolls back the raised exception.
    db.commit()
    connection = find_connection(db,site.id,'smtp')
    if not connection or connection.status == 'revoked':
        return {**counts,'email':'needs_connection','in_app':'delivered'}
    if not _digest_email_enabled(connection):
        return {**counts,'email':'disabled','in_app':'delivered'}

    try:
        secret, config = credentials(db,site.id,'smtp')
    except Exception:
        _persist_failure(db, site, "smtp_credentials")

    try:
        recipients = _validated_recipients(config, secret)
        host, sender, username, password = _validated_settings(config, secret)
    except _ValidationFailure as exc:
        _persist_failure(db, site, exc.error_type)

    try:
        await public_addresses(host,465)
    except Exception:
        _persist_failure(db, site, "smtp_dns_safety")

    try:
        message = EmailMessage()
        message['Subject'] = 'ForgeSEO weekly report: '+site.name
        message['From'] = sender
        message['To'] = ', '.join(recipients)
        message.set_content(f'Site: {site.origin}\nOpen issues: {counts["open_issues"]}\nOpen incidents: {counts["open_incidents"]}\nVerified publications this week: {counts["publications"]}\nOpen ForgeSEO for coverage, costs and evidence. An empty queue does not mean the whole site is optimized.')
    except Exception:
        _persist_failure(db, site, "smtp_credentials")

    try:
        await asyncio.to_thread(_deliver, host, username, password, message)
    except _SMTPStageFailure as exc:
        _persist_failure(db, site, exc.error_type)
    except Exception:
        _persist_failure(db, site, "smtp_tls")

    # A successful later delivery resolves the same site-scoped incident. The
    # helper leaves an already-resolved row unchanged on repeated successes.
    sync_health_incident(
        db,
        site,
        key=SMTP_DIGEST_INCIDENT_KEY,
        healthy=True,
        title=SMTP_DIGEST_INCIDENT_TITLE,
        kind="notification",
        severity="medium",
    )
    db.commit()
    return {**counts,'email':'delivered','in_app':'delivered'}
