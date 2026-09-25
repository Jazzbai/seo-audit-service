"""Durable work submission, safe reporting, and deployment controls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import Article, Connection, Event, Heartbeat, Incident, Job, Publication, Site


SCHEDULER_STALE_AFTER_SECONDS = 150
QUEUE_DELAY_DEGRADED_AFTER_SECONDS = 600
SCHEDULER_HEALTH_INCIDENT_KEY = "monitoring:scheduler"
QUEUE_HEALTH_INCIDENT_KEY = "monitoring:queue"
WORDPRESS_CHANGE_POLL_INCIDENT_KEY = "monitoring:wordpress_change_poll"
WORDPRESS_CHANGE_POLL_INTERVAL_SECONDS = 300
# Allow one missed five-minute window before declaring the connection stale.
WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS = WORDPRESS_CHANGE_POLL_INTERVAL_SECONDS * 2
CADENCE_INTERVAL_SECONDS = {
    "availability": 60,
    "wordpress_change_poll": WORDPRESS_CHANGE_POLL_INTERVAL_SECONDS,
    "inventory": 86400,
    "audit": 604800,
    "plan": 604800,
    "refresh": 604800,
}
CADENCE_STALE_AFTER_SECONDS = {
    name: interval * 2 for name, interval in CADENCE_INTERVAL_SECONDS.items()
}
CADENCE_JOB_KINDS = frozenset({
    "availability", "poll_changes", "inventory", "audit", "plan", "refresh",
})
CADENCE_FAILURE_STATUSES = frozenset({"failed", "blocked", "needs_reconciliation"})
CADENCE_ACTIVE_STATUSES = frozenset({"queued", "retry", "running"})
CADENCE_INCIDENT_PREFIX = "monitoring:cadence:"
_PAUSE_SENSITIVE_JOB_KINDS = frozenset({"generate", "publish", "content_autopilot"})
# Beat uses a queued row's delivery lease as an attempt marker.  This keeps an
# accepted (or ambiguous) broker publish from being re-enqueued on every short
# scheduler tick while still allowing a later sweep to recover a message that
# never reached a worker.
JOB_DISPATCH_COOLDOWN_SECONDS = 60


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value):
    return value.isoformat() + "Z" if isinstance(value, datetime) else value


def _parse_timestamp(value) -> datetime | None:
    """Parse persisted UTC timestamps without trusting their timezone shape."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def record(value, exclude=()) -> dict:
    return {c.name: iso(getattr(value, c.name)) for c in value.__table__.columns
            if c.name not in set(exclude)}


def connection_view(value: Connection) -> dict:
    return record(value, ("encrypted_credentials",))


def event(db, site, kind: str, message: str, data=None):
    row = Event(site_id=site.id, team_id=site.team_id, kind=kind,
                message=message, data=data or {})
    db.add(row)
    return row


def global_controls(db) -> dict:
    row = db.get(Heartbeat, "platform_controls")
    return {"global_pause": settings.GLOBAL_PAUSE,
            **(row.details if row is not None else {})}


def sync_health_incident(db, site: Site, *, key: str, healthy: bool,
                        title: str, details: dict[str, Any] | None = None,
                        kind: str = "monitoring", severity: str = "high") -> Incident | None:
    """Open, refresh, or resolve one durable site-scoped health incident.

    The incident key is intentionally supplied by the caller and is scoped by
    the existing ``(site_id, key)`` uniqueness constraint.  A healthy reading
    does not create an incident, and repeated healthy readings leave an
    already-resolved incident unchanged.  Repeated degraded readings refresh
    the same row and retain a count of observed failures.
    """
    row = next(
        (candidate for candidate in db.new
         if isinstance(candidate, Incident)
         and candidate.site_id == site.id
         and candidate.key == key),
        None,
    )
    if row is None:
        row = db.scalar(select(Incident).where(
            Incident.site_id == site.id,
            Incident.key == key,
        ))

    if healthy:
        if row is None:
            return row
        if row.status == "resolved" and row.resolved_at is not None:
            return row
        timestamp = now()
        row.status = "resolved"
        row.resolved_at = timestamp
        return row

    timestamp = now()
    payload = details if isinstance(details, dict) else {}

    def refresh(existing: Incident) -> Incident:
        """Merge one degraded observation into an existing incident row."""

        existing.status = "open"
        existing.kind = kind
        existing.severity = severity
        existing.title = title
        existing.details = payload
        existing.failure_count = (existing.failure_count or 0) + 1
        existing.last_seen_at = timestamp
        existing.resolved_at = None
        return existing

    if row is None:
        row = Incident(
            site_id=site.id,
            key=key,
            kind=kind,
            severity=severity,
            title=title,
            status="open",
            details=payload,
            failure_count=1,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        # The scheduler is leader-locked, but monitoring_status can be called
        # concurrently by multiple browser sessions.  Let the existing unique
        # constraint arbitrate an already-persisted row without creating a
        # second in-memory row on repeated calls in one session.
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            # A concurrent insert may have won the unique key race.  Do not
            # hide unrelated database failures; only retry when the row is now
            # visible under this exact site/key pair.
            existing = db.scalar(select(Incident).where(
                Incident.site_id == site.id,
                Incident.key == key,
            ))
            if existing is None:
                raise
            # The concurrent observation is still real evidence.  Merge it
            # into the row that won the unique-key race instead of returning
            # that row unchanged and losing its count/latest timestamp.
            row = refresh(existing)
    else:
        row = refresh(row)
    return row


def sync_monitoring_incidents(db, *, scheduler_healthy: bool,
                              queue_healthy: bool | None,
                              scheduler_details: dict[str, Any] | None = None,
                              queue_details: dict[str, Any] | None = None,
                              site_id: str | None = None) -> None:
    """Synchronize global scheduler/queue health into every site's incidents.

    Queue health is optional because a missing or stale scheduler heartbeat
    means the last queue measurement is not fresh enough to resolve or open a
    queue incident.  The next healthy scheduler cycle supplies a fresh queue
    result and closes it when appropriate.
    """
    sites = [db.get(Site, site_id)] if site_id is not None else db.scalars(select(Site)).all()
    for site in sites:
        if site is None:
            continue
        sync_health_incident(
            db,
            site,
            key=SCHEDULER_HEALTH_INCIDENT_KEY,
            healthy=scheduler_healthy,
            title="Scheduler heartbeat is stale",
            details=scheduler_details,
        )
        if queue_healthy is not None:
            sync_health_incident(
                db,
                site,
                key=QUEUE_HEALTH_INCIDENT_KEY,
                healthy=queue_healthy,
                title="Queue processing is delayed",
                details=queue_details,
            )


def wordpress_change_poll_status(connection: Connection | None,
                                 *, at: datetime | None = None) -> dict[str, Any]:
    """Derive freshness for one verified WordPress connection.

    Incremental polling stores its successful completion marker in the
    connection capability JSON.  Keeping the derived state here means the
    scheduler and the site overview use the same clock, threshold, and
    handling for malformed or absent markers without adding another mutable
    monitoring table.
    """

    instant = at or now()
    base = {
        "status": "not_connected",
        "last_success_at": None,
        "seconds_since_success": None,
        "poll_interval_seconds": WORDPRESS_CHANGE_POLL_INTERVAL_SECONDS,
        "stale_after_seconds": WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS,
        "missed_window": False,
        "message": "Connect and verify WordPress to monitor changes",
    }
    if connection is None or connection.kind != "wordpress" or connection.status != "connected":
        return base

    capabilities = connection.capabilities if isinstance(connection.capabilities, dict) else {}
    state = capabilities.get("change_poll")
    state = state if isinstance(state, dict) else {}
    raw_last_success = state.get("last_success_at")
    last_success = _parse_timestamp(raw_last_success)
    invalid_last_success = bool(raw_last_success) and last_success is None
    if last_success is not None:
        age = max(0.0, (instant - last_success).total_seconds())
        if last_success > instant:
            return {
                **base,
                "status": "unknown",
                "invalid_timestamp": True,
                "message": "The last WordPress change-poll timestamp is in the future",
            }
        stale = age >= WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS
        return {
            **base,
            "status": "stale" if stale else "healthy",
            "last_success_at": iso(last_success),
            "seconds_since_success": round(age),
            "missed_window": stale,
            "invalid_timestamp": False,
            "message": "WordPress changes are being monitored" if not stale
                       else "WordPress change polling has missed its freshness window",
        }

    if invalid_last_success:
        return {
            **base,
            "status": "unknown",
            "invalid_timestamp": True,
            "message": "The last WordPress change-poll timestamp is invalid",
        }

    # A verified connection with no successful poll is not immediately an
    # incident: give the first scheduled poll its normal freshness window.
    verified_at = _parse_timestamp(connection.checked_at) or _parse_timestamp(connection.created_at)
    age = None if verified_at is None else max(0.0, (instant - verified_at).total_seconds())
    if verified_at is not None and verified_at > instant:
        return {
            **base,
            "status": "unknown",
            "invalid_timestamp": True,
            "message": "The WordPress verification timestamp is in the future",
        }
    missed_window = age is not None and age >= WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS
    return {
        **base,
        "status": "stale" if missed_window else "not_yet_run",
        "seconds_since_verification": round(age) if age is not None else None,
        "missed_window": missed_window,
        "invalid_timestamp": False,
        "message": "The first WordPress change poll has not completed" if not missed_window
                   else "WordPress change polling has not completed within its first freshness window",
    }


def sync_wordpress_change_poll_incident(db, site: Site, connection: Connection | None,
                                        *, at: datetime | None = None) -> dict[str, Any]:
    """Persist one site-scoped incident for an overdue verified WP poll.

    A not-yet-run connection is left incident-free during its initial window.
    Once overdue it opens the same durable incident as a stale connection;
    only a subsequent successful poll (``healthy`` state) resolves it.
    """

    status = wordpress_change_poll_status(connection, at=at)
    if connection is None or connection.kind != "wordpress" or connection.status != "connected":
        return status
    if status["status"] == "healthy":
        sync_health_incident(
            db,
            site,
            key=WORDPRESS_CHANGE_POLL_INCIDENT_KEY,
            healthy=True,
            title="WordPress change polling is stale",
            details=status,
            severity="medium",
        )
    elif status["missed_window"]:
        sync_health_incident(
            db,
            site,
            key=WORDPRESS_CHANGE_POLL_INCIDENT_KEY,
            healthy=False,
            title="WordPress change polling is stale",
            details=status,
            severity="medium",
        )
    return status


def _pause_sensitive_job(kind: str, payload: dict | None = None) -> bool:
    """Identify queued work that must not start while a pause is active."""

    if kind in _PAUSE_SENSITIVE_JOB_KINDS:
        return True
    if kind == "full_cycle" and isinstance(payload, dict):
        return payload.get("mode", "read_only") == "autopilot"
    return False


def enqueue(db, site, kind: str, payload: dict | None = None,
            idempotency_key: str | None = None) -> Job:
    allowed = {"audit", "inventory", "poll_changes", "plan", "generate", "publish", "availability",
               "visibility", "refresh", "connection_test", "candidate", "rollback", "full_cycle",
               "content_autopilot", "reconcile_publication", "browser", "digest", "targeted_audit", "notification_test"}
    if kind not in allowed:
        raise ValueError("Unsupported job kind")
    payload = payload or {}
    key = f"{site.id}:{idempotency_key or uuid4().hex}"
    row = db.scalar(select(Job).where(Job.idempotency_key == key))
    if row:
        if row.kind != kind or row.payload != payload:
            raise ValueError("Idempotency key already belongs to different work")
        return row
    row = Job(site_id=site.id, kind=kind, payload=payload, idempotency_key=key,
              status="queued", available_at=now(), created_at=now(), updated_at=now())
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.scalar(select(Job).where(Job.idempotency_key == key))
        if row is None or row.kind != kind or row.payload != payload:
            raise ValueError("Conflicting concurrent job request")
    event(db, site, "job_queued", f"{kind.replace('_', ' ').capitalize()} queued", {"job_id": row.id})
    db.commit()
    if _pause_sensitive_job(kind, payload) and (
        site.paused or global_controls(db)["global_pause"]
    ):
        # Keep pause-sensitive work durable for the scheduler, but never
        # deliver a newly queued write-capable job while a pause is active.
        return row
    # The database is the durable queue. Beat dispatches queued work if the broker is down.
    dispatch_at = now()
    try:
        from app.worker import execute_job
        execute_job.apply_async(args=[row.id], queue="browser" if kind == "browser" else "platform")
    except Exception:
        # A publish failure may be ambiguous: the broker can accept a message
        # just before the client observes an error.  Mark the attempt so the
        # durable sweep does not immediately flood the broker with repeats.
        pass
    # ``lease_until`` is also the broker-delivery lease while a job is still
    # queued/retrying.  The worker replaces it with its execution lease when
    # it claims the message.  A conditional update avoids overwriting a
    # worker's running/completed state if it won the race with this publisher.
    db.execute(
        update(Job)
        .where(Job.id == row.id, Job.status.in_(("queued", "retry")))
        .values(
            lease_until=dispatch_at + timedelta(seconds=JOB_DISPATCH_COOLDOWN_SECONDS),
            updated_at=dispatch_at,
        )
    )
    db.commit()
    return row


def enqueue_article_publish(db, site, article_id: str, *, requested_by: str | None = None) -> Job:
    """Replay the one article operation without replacing scheduler provenance.

    Failed/uncertain work is never blindly retried. After successful read-only
    reconciliation, an explicit request can create a distinct, linked attempt;
    the original job and remote operation identity are preserved.
    """
    key = f"{site.id}:publish:{article_id}"

    def existing():
        row = db.scalar(select(Job).where(Job.idempotency_key == key))
        if row is None:
            return None
        payload = row.payload
        valid = payload == {"article_id": article_id}
        if isinstance(payload, dict) and set(payload) == {"article_id", "authorization", "policy_version"}:
            version = payload.get("policy_version")
            valid = (
                payload.get("article_id") == article_id
                and type(version) is int and version > 0
                and payload.get("authorization") == {
                    "type": "policy", "action": "publish", "policy_version": version,
                }
            )
        if row.site_id != site.id or row.kind != "publish" or not valid:
            raise ValueError("Idempotency key already belongs to different work")
        publication = db.scalar(select(Publication).where(
            Publication.site_id == site.id,
            Publication.article_id == article_id,
            Publication.operation_key == f'publish:{site.id}:{article_id}',
        ))
        if publication is None:
            return row
        snapshot = publication.snapshot if isinstance(publication.snapshot, dict) else {}
        reconciliation_id = snapshot.get('reconciliation_job_id')
        if not isinstance(reconciliation_id, str):
            return row
        reconciliation = db.get(Job, reconciliation_id)
        result = reconciliation.result if reconciliation is not None and isinstance(reconciliation.result, dict) else {}
        if (reconciliation is None or reconciliation.site_id != site.id
                or reconciliation.kind != 'reconcile_publication'
                or reconciliation.status not in ('complete', 'partial')
                or reconciliation.payload != {'publication_id': publication.id}
                or result.get('status') != 'draft_reconciled'
                or result.get('publication_id') != publication.id
                or result.get('article_id') != article_id):
            raise ValueError('Publication recovery evidence requires review')
        resume_key = f'publish:{article_id}:resume:{reconciliation_id}'
        resume_payload = {'article_id': article_id, 'resumes_job_id': row.id,
                          'reconciliation_job_id': reconciliation_id}
        resumed = db.scalar(select(Job).where(Job.idempotency_key == f'{site.id}:{resume_key}'))
        if resumed is not None:
            if resumed.site_id != site.id or resumed.kind != 'publish' or resumed.payload != resume_payload:
                raise ValueError('Conflicting publication recovery job')
            return resumed
        article = db.get(Article, article_id)
        if (row.status not in ('needs_reconciliation', 'failed', 'blocked')
                or publication.status != 'preparing'
                or not isinstance(publication.result, dict)
                or publication.result.get('status') != 'draft_reconciled'
                or article is None or article.site_id != site.id or article.status != 'checked'
                or not publication.remote_id or not snapshot.get('draft')):
            return row
        # A new durable job is safe only after remote identity is known. Worker
        # policy, editorial, freshness and source checks still run in full.
        resumed = enqueue(db, site, 'publish', resume_payload, resume_key)
        event(db, site, 'publication_resume_requested', 'Explicit publication resume requested',
              {'job_id': resumed.id, 'publication_id': publication.id,
               'previous_job_id': row.id, 'user_id': requested_by})
        db.commit()
        return resumed

    row = existing()
    if row is not None:
        return row
    try:
        return enqueue(db, site, "publish", {"article_id": article_id}, f"publish:{article_id}")
    except ValueError:
        # The scheduler can win the insert after the first read. Re-read the
        # canonical operation and apply exactly the same site/payload checks.
        row = existing()
        if row is None:
            raise
        return row


def find_connection(db, site_id: str, kind: str):
    return db.scalar(select(Connection).where(Connection.site_id == site_id, Connection.kind == kind))


def _cadence_timestamp(value, instant: datetime) -> tuple[datetime | None, bool]:
    """Return a usable UTC timestamp and whether the stored value is invalid."""

    parsed = _parse_timestamp(value)
    if parsed is None:
        return None, bool(value)
    if parsed > instant:
        return None, True
    return parsed, False


def _job_cadence_state(job) -> str:
    status = str(job.status or "").lower()
    result = job.result if isinstance(job.result, dict) else {}
    # Worker completion uses ``partial`` for handlers that explicitly report
    # incomplete coverage.  Keep this guard for imported/legacy rows that may
    # still say complete while retaining the incomplete result flag.
    if status == "complete" and result.get("complete") is False:
        return "partial"
    # An audit can finish its crawl while still returning transport errors or
    # pending URLs.  The API exposes that result as ``complete_with_errors``;
    # cadence health must not promote it to a fully successful observation.
    # Keep this audit-specific because availability and other cadence handlers
    # have different result contracts and do not all carry a ``complete`` key.
    if status == "complete" and getattr(job, "kind", None) == "audit":
        if result.get("complete") is not True:
            return "unknown"
        for key in ("errors", "pending_urls"):
            if key in result and (
                not isinstance(result[key], list) or bool(result[key])
            ):
                return "partial"
        reconciliation = result.get("reconciliation")
        if isinstance(reconciliation, dict) and reconciliation.get("resolution_allowed") is False:
            return "partial"
    return status


def _cadence_entry(name: str, *, status: str = "unknown", message: str,
                   last_success_at: datetime | None = None,
                   last_attempt_at: datetime | None = None,
                   last_job_status: str | None = None,
                   evidence: str | None = None,
                   invalid_timestamp: bool = False,
                   at: datetime | None = None) -> dict[str, Any]:
    interval = CADENCE_INTERVAL_SECONDS[name]
    stale_after = CADENCE_STALE_AFTER_SECONDS[name]
    success_age = None
    attempt_age = None
    instant = at or now()
    if last_success_at is not None:
        success_age = max(0, round((instant - last_success_at).total_seconds()))
    if last_attempt_at is not None:
        attempt_age = max(0, round((instant - last_attempt_at).total_seconds()))
    freshness = "unknown"
    if success_age is not None:
        freshness = "stale" if success_age >= stale_after else (
            "due" if success_age >= interval else "current"
        )
    next_due = (
        last_success_at + timedelta(seconds=interval)
        if last_success_at is not None else None
    )
    return {
        "status": status,
        "freshness": freshness,
        "interval_seconds": interval,
        "stale_after_seconds": stale_after,
        "last_success_at": iso(last_success_at),
        "seconds_since_success": success_age,
        "last_attempt_at": iso(last_attempt_at),
        "seconds_since_attempt": attempt_age,
        "last_job_status": last_job_status,
        "next_due_at": iso(next_due),
        "evidence": evidence,
        "invalid_timestamp": invalid_timestamp,
        "message": message,
    }


def _derive_job_cadence(name: str, jobs: list[Job], instant: datetime) -> dict[str, Any]:
    """Derive health without treating queued work as a successful check."""

    ordered: list[tuple[datetime, Job, str]] = []
    invalid_latest = False
    for job in jobs:
        timestamp, invalid = _cadence_timestamp(job.updated_at, instant)
        if timestamp is None and not invalid:
            timestamp, invalid = _cadence_timestamp(job.created_at, instant)
        if invalid:
            # A future/malformed newest timestamp must not let an older row
            # manufacture a healthy status.
            invalid_latest = True
            continue
        if timestamp is not None:
            ordered.append((timestamp, job, _job_cadence_state(job)))

    ordered.sort(key=lambda item: (item[0], str(item[1].id)))
    latest = ordered[-1] if ordered else None
    successful = [item for item in ordered if item[2] == "complete"]
    last_success = successful[-1][0] if successful else None
    latest_job_status = latest[2] if latest else None
    last_attempt = latest[0] if latest else None

    if invalid_latest:
        return _cadence_entry(
            name,
            message=f"No trustworthy {name.replace('_', ' ')} timestamp is available",
            invalid_timestamp=True,
            at=instant,
        )

    if latest_job_status in CADENCE_FAILURE_STATUSES:
        status = "failed"
        message = f"The latest {name.replace('_', ' ')} job did not complete"
    elif latest_job_status == "partial":
        status = "partial"
        message = f"The latest {name.replace('_', ' ')} job completed only partially"
    elif latest_job_status in CADENCE_ACTIVE_STATUSES:
        status = "pending" if last_success is not None else "unknown"
        message = (
            f"A {name.replace('_', ' ')} job is {latest_job_status}; "
            "no new successful completion is available yet"
        )
    elif last_success is not None:
        age = (instant - last_success).total_seconds()
        if age >= CADENCE_STALE_AFTER_SECONDS[name]:
            status = "stale"
            message = f"No successful {name.replace('_', ' ')} check completed within its freshness window"
        elif age >= CADENCE_INTERVAL_SECONDS[name]:
            status = "due"
            message = f"The last {name.replace('_', ' ')} check is past its target cadence"
        else:
            status = "healthy"
            message = f"{name.replace('_', ' ').capitalize()} checks are completing on cadence"
    else:
        status = "unknown"
        message = f"No completed {name.replace('_', ' ')} evidence has been recorded"

    return _cadence_entry(
        name,
        status=status,
        message=message,
        last_success_at=last_success,
        last_attempt_at=last_attempt,
        last_job_status=latest_job_status,
        evidence="job" if ordered else None,
        invalid_timestamp=invalid_latest,
        at=instant,
    )


def _derive_wordpress_cadence(connection: Connection | None, jobs: list[Job],
                              instant: datetime) -> dict[str, Any]:
    poll = wordpress_change_poll_status(connection, at=instant)
    if connection is None or connection.kind != "wordpress" or connection.status != "connected":
        return _cadence_entry(
            "wordpress_change_poll",
            status="not_connected",
            message="Connect and verify WordPress before monitoring change polling",
            at=instant,
        )

    state = connection.capabilities if isinstance(connection.capabilities, dict) else {}
    poll_state = state.get("change_poll") if isinstance(state.get("change_poll"), dict) else {}
    raw_marker = poll_state.get("last_success_at")
    marker, invalid = _cadence_timestamp(raw_marker, instant)
    # A missing or malformed success marker is not evidence that polling is
    # healthy, even when the connection itself was verified.
    derived = _derive_job_cadence("wordpress_change_poll", jobs, instant)
    if marker is not None:
        job_success = _parse_timestamp(derived.get("last_success_at"))
        last_success = max(filter(None, (marker, job_success)), default=None)
        latest_status = derived.get("last_job_status")
        if latest_status in CADENCE_FAILURE_STATUSES:
            status = "failed"
            message = "The latest WordPress change-poll job did not complete"
        elif latest_status in CADENCE_ACTIVE_STATUSES:
            status = "pending"
            message = "A WordPress change-poll job is still in progress"
        else:
            age = (instant - last_success).total_seconds()
            if age >= WORDPRESS_CHANGE_POLL_STALE_AFTER_SECONDS:
                status = "stale"
                message = "WordPress change polling has missed its freshness window"
            elif age >= WORDPRESS_CHANGE_POLL_INTERVAL_SECONDS:
                status = "due"
                message = "WordPress change polling is past its target cadence"
            else:
                status = "healthy"
                message = "WordPress changes are being monitored"
        return _cadence_entry(
            "wordpress_change_poll",
            status=status,
            message=message,
            last_success_at=last_success,
            last_attempt_at=_parse_timestamp(derived.get("last_attempt_at")),
            last_job_status=latest_status,
            evidence="connection.capabilities.change_poll.last_success_at",
            invalid_timestamp=False,
            at=instant,
        )

    if invalid or poll.get("invalid_timestamp"):
        return _cadence_entry(
            "wordpress_change_poll",
            message="No trustworthy WordPress change-poll timestamp is available",
            last_attempt_at=_parse_timestamp(derived.get("last_attempt_at")),
            last_job_status=derived.get("last_job_status"),
            evidence="connection.capabilities.change_poll",
            invalid_timestamp=True,
            at=instant,
        )

    # Preserve failure/partial information from the durable poll job, but do
    # not promote a queued or completed row to healthy without its marker.
    if derived.get("status") in {"failed", "partial"}:
        return _cadence_entry(
            "wordpress_change_poll",
            status=derived["status"],
            message=derived["message"],
            last_attempt_at=_parse_timestamp(derived.get("last_attempt_at")),
            last_job_status=derived.get("last_job_status"),
            evidence="job",
            at=instant,
        )
    return _cadence_entry(
        "wordpress_change_poll",
        message="No successful WordPress change poll has been recorded",
        last_attempt_at=_parse_timestamp(derived.get("last_attempt_at")),
        last_job_status=derived.get("last_job_status"),
        evidence="connection.capabilities.change_poll" if poll_state else None,
        at=instant,
    )


def site_cadence_status(db, site: Site | str, *, at: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Return truthful per-site health for every required monitoring cadence.

    This function is deliberately read-only with respect to external systems:
    it examines durable job rows and the persisted WordPress poll marker only.
    A queued/running job is scheduling evidence, not successful monitoring
    evidence, and a site with no completed evidence remains ``unknown``.
    """

    site_row = db.get(Site, site) if isinstance(site, str) else site
    if site_row is None:
        return {}
    jobs = db.scalars(select(Job).where(
        Job.site_id == site_row.id,
        Job.kind.in_(CADENCE_JOB_KINDS),
    )).all()
    instant = at or now()
    grouped = {kind: [] for kind in CADENCE_JOB_KINDS}
    grouped["wordpress_change_poll"] = []
    for job in jobs:
        key = "wordpress_change_poll" if job.kind == "poll_changes" else job.kind
        grouped.setdefault(key, []).append(job)
    connection = find_connection(db, site_row.id, "wordpress")
    result = {
        "availability": _derive_job_cadence("availability", grouped["availability"], instant),
        "wordpress_change_poll": _derive_wordpress_cadence(
            connection, grouped["wordpress_change_poll"], instant,
        ),
        "inventory": _derive_job_cadence("inventory", grouped["inventory"], instant),
        "audit": _derive_job_cadence("audit", grouped["audit"], instant),
        "plan": _derive_job_cadence("plan", grouped["plan"], instant),
        "refresh": _derive_job_cadence("refresh", grouped["refresh"], instant),
    }
    return result


def sync_site_cadence_incidents(db, site: Site, *, at: datetime | None = None,
                                cadence: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Persist incidents for proven cadence failures without resolving unknowns."""

    cadence = cadence or site_cadence_status(db, site, at=at)
    for name, details in cadence.items():
        if name == "wordpress_change_poll":
            # Its existing incident has connection-specific first-window
            # semantics and is synchronized by sync_wordpress_change_poll_incident.
            continue
        status = details.get("status")
        if status in {"stale", "failed", "partial"}:
            sync_health_incident(
                db,
                site,
                key=f"{CADENCE_INCIDENT_PREFIX}{name}",
                healthy=False,
                title=f"{name.replace('_', ' ').capitalize()} cadence needs attention",
                details=details,
                severity="medium",
            )
        elif status == "healthy":
            sync_health_incident(
                db,
                site,
                key=f"{CADENCE_INCIDENT_PREFIX}{name}",
                healthy=True,
                title=f"{name.replace('_', ' ').capitalize()} cadence needs attention",
                details=details,
                severity="medium",
            )
    return cadence


def credentials(db, site_id: str, kind: str) -> tuple[dict, dict]:
    from app.connectors.security import decrypt_credentials
    row = find_connection(db, site_id, kind)
    if row is None or row.status == "revoked" or not row.encrypted_credentials:
        raise ValueError(f"Connect {kind} in Settings before running this task")
    return decrypt_credentials(row.encrypted_credentials, settings.ENCRYPTION_KEY), (row.capabilities or {}).get("settings", {})


def _queue_metric_validation(details: Any) -> tuple[bool, list[str]]:
    """Validate the scheduler's persisted queue-health measurements.

    A heartbeat proves that the scheduler process wrote recently; it does not
    prove that the queue measurement was present or meaningful.  Treat
    malformed measurements as degraded instead of allowing them to bypass the
    threshold comparisons below and appear healthy.
    """

    errors: list[str] = []
    payload = details if isinstance(details, dict) else {}
    if not isinstance(details, dict):
        errors.append("heartbeat_details_not_object")

    queue_delay = payload.get("queue_delay_seconds")
    if "queue_delay_seconds" not in payload:
        errors.append("queue_delay_seconds_missing")
    elif (
        isinstance(queue_delay, bool)
        or not isinstance(queue_delay, (int, float))
        or (isinstance(queue_delay, float) and not isfinite(queue_delay))
        or queue_delay < 0
    ):
        errors.append("queue_delay_seconds_invalid")

    missed_checks = payload.get("missed_checks")
    if "missed_checks" not in payload:
        errors.append("missed_checks_missing")
    elif (
        isinstance(missed_checks, bool)
        or not isinstance(missed_checks, int)
        or missed_checks < 0
    ):
        errors.append("missed_checks_invalid")

    return not errors, errors


def monitoring_status(db, site_id: str | None = None) -> dict[str, Any]:
    """Return global health plus truthful site-scoped cadence evidence."""

    instant = now()
    row = db.get(Heartbeat, "scheduler")
    if row is None:
        result = {"status": "not_running", "last_seen_at": None,
                  "queue_delay_seconds": None, "missed_checks": None,
                  "message": "No scheduler heartbeat has been received"}
        sync_monitoring_incidents(
            db,
            scheduler_healthy=False,
            queue_healthy=None,
            scheduler_details={"reason": "No scheduler heartbeat has been received"},
            site_id=site_id,
        )
        if site_id is not None:
            site = db.get(Site, site_id)
            if site is not None:
                result["wordpress_change_poll"] = sync_wordpress_change_poll_incident(
                    db, site, find_connection(db, site_id, "wordpress"), at=instant,
                )
                cadence = site_cadence_status(db, site, at=instant)
                result["cadence"] = sync_site_cadence_incidents(
                    db, site, at=instant, cadence=cadence,
                )
        db.commit()
        return result
    age = (instant - row.last_seen_at).total_seconds()
    heartbeat_in_future = age < 0
    heartbeat_age = None if heartbeat_in_future else round(age)
    details = row.details if isinstance(row.details, dict) else {}
    queue_delay = details.get("queue_delay_seconds")
    missed_checks = details.get("missed_checks")
    queue_metrics_valid, queue_metrics_errors = _queue_metric_validation(row.details)
    scheduler_healthy = not heartbeat_in_future and age < SCHEDULER_STALE_AFTER_SECONDS
    queue_healthy = scheduler_healthy and queue_metrics_valid and not (
        queue_delay > QUEUE_DELAY_DEGRADED_AFTER_SECONDS
        or missed_checks > 0
    )
    degraded = not scheduler_healthy or not queue_healthy
    result = {
        "status": "degraded" if degraded else "running",
        "last_seen_at": iso(row.last_seen_at),
        "seconds_since_heartbeat": heartbeat_age,
        "queue_delay_seconds": queue_delay,
        "missed_checks": missed_checks,
        "queue_metrics_valid": queue_metrics_valid,
        "queue_metrics_errors": queue_metrics_errors,
        "due_jobs": details.get("due_jobs"),
        "message": (
            "Scheduler heartbeat timestamp is in the future"
            if heartbeat_in_future
            else "Scheduler heartbeat queue metrics are missing or invalid"
            if not queue_metrics_valid
            else "Checks are scheduled" if not degraded
            else "Scheduler or queue health needs attention"
        ),
    }
    sync_monitoring_incidents(
        db,
        scheduler_healthy=scheduler_healthy,
        queue_healthy=queue_healthy if scheduler_healthy else None,
        scheduler_details={
            "seconds_since_heartbeat": heartbeat_age,
            "threshold_seconds": SCHEDULER_STALE_AFTER_SECONDS,
            "last_seen_at": iso(row.last_seen_at),
            "invalid_timestamp": heartbeat_in_future,
        },
        queue_details={
            "queue_delay_seconds": queue_delay,
            "missed_checks": missed_checks,
            "metrics_valid": queue_metrics_valid,
            "validation_errors": queue_metrics_errors,
            "threshold_seconds": QUEUE_DELAY_DEGRADED_AFTER_SECONDS,
            "due_jobs": details.get("due_jobs"),
        },
        site_id=site_id,
    )
    if site_id is not None:
        site = db.get(Site, site_id)
        if site is not None:
            poll_status = sync_wordpress_change_poll_incident(
                db, site, find_connection(db, site_id, "wordpress"), at=instant,
            )
            result["wordpress_change_poll"] = poll_status
            if poll_status["status"] == "stale":
                result["status"] = "degraded"
                result["message"] = "Scheduler is running, but WordPress change monitoring is stale"
            cadence = site_cadence_status(db, site, at=instant)
            result["cadence"] = sync_site_cadence_incidents(
                db, site, at=instant, cadence=cadence,
            )
            if any(
                details.get("status") in {"stale", "failed", "partial"}
                for details in cadence.values()
            ):
                result["status"] = "degraded"
                result["message"] = "Scheduler is running, but a monitoring cadence needs attention"
    db.commit()
    return result
