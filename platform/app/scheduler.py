"""Idempotent periodic work and interrupted-job recovery."""
from datetime import timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from datetime import timezone

from sqlalchemy import and_, func, or_, select, update

from app.db import SessionLocal
from app.intelligence.visibility import _bounded_ai_questions
from app.models import Article, Connection, Event, Heartbeat, Job, Publication, Site
from app.operations import (QUEUE_DELAY_DEGRADED_AFTER_SECONDS,
                             SCHEDULER_STALE_AFTER_SECONDS, enqueue, event,
                             global_controls, now, sync_monitoring_incidents,
                             sync_site_cadence_incidents,
                             sync_wordpress_change_poll_incident,
                             JOB_DISPATCH_COOLDOWN_SECONDS)
from app.policies import current_policy, evaluate_policy


_ACTIVE_PUBLICATION_STATUSES = frozenset({'preparing', 'publishing', 'verifying'})
_ACTIVE_JOB_STATUSES = frozenset({'queued', 'running', 'retry'})
_PAUSE_SENSITIVE_JOB_KINDS = frozenset({'generate', 'publish', 'content_autopilot'})
_QUOTA_ARTICLE_STATUSES = frozenset({'checked', 'scheduled', 'publishing', 'verifying', 'published'})
_EXCLUDED_QUOTA_ARTICLE_STATUSES = frozenset({'planned', 'review_needed', 'failed', 'rolled_back', 'rejected'})
_AUTOPILOT_WINDOW_START_HOUR = 9
_AUTOPILOT_RESERVATION_PREFIX = 'content_autopilot:'
_AUTOPILOT_UNCERTAIN_STATUSES = frozenset({'ambiguous', 'needs_reconciliation'})
_AUTOMATIC_POLICY_BLOCKERS = frozenset({
    'global_pause',
    'site_paused',
    'policy_missing',
    'policy_site_mismatch',
    'policy_invalid',
    'policy_disabled',
    'action_not_allowed',
})
_READ_ONLY_LEASE_RECOVERY_KINDS = frozenset({
    'availability',
    'inventory',
    'poll_changes',
    'audit',
    'browser',
    'plan',
    'refresh',
    'targeted_audit',
    'reconcile_publication',
})


def _job_is_pause_sensitive(job) -> bool:
    """Return whether a durable job must stay out of the broker during pause."""

    if job.kind in _PAUSE_SENSITIVE_JOB_KINDS:
        return True
    if job.kind == 'full_cycle':
        payload = job.payload if isinstance(job.payload, dict) else {}
        return payload.get('mode', 'read_only') == 'autopilot'
    return False


def _job_lease_recovery_is_read_only(job) -> bool:
    """Classify interrupted jobs from their payload, not just their kind."""

    if job.kind in _READ_ONLY_LEASE_RECOVERY_KINDS:
        return True
    if job.kind == 'full_cycle':
        payload = job.payload if isinstance(job.payload, dict) else {}
        return payload.get('mode', 'read_only') != 'autopilot'
    return False


def _recover_full_cycle_stages(db, site, parent, instant):
    """Release stage rows orphaned by an interrupted full-cycle parent.

    Full-cycle stages are durable child rows, but the parent worker executes
    them synchronously while holding the site's lock.  The stage code records
    ``running`` before invoking its handler and intentionally does not own a
    separate worker lease.  Once the parent's lease expires, any still-running
    stage is therefore orphaned and must not remain invisible to recovery or
    be dispatched as an independent job.

    The parent is the only safe resumption boundary: a read-only parent may be
    retried, while an autopilot/otherwise uncertain parent remains in
    reconciliation.  In both cases the stage is released and marked for the
    parent to resume, preserving the no-blind-retry contract.
    """

    if parent.kind != 'full_cycle' or site is None:
        return
    stage_rows = db.scalars(select(Job).where(
        Job.site_id == site.id,
        Job.status == 'running',
    )).all()
    for stage in stage_rows:
        payload = stage.payload if isinstance(stage.payload, dict) else {}
        if payload.get('full_cycle_parent_job_id') != parent.id:
            continue
        stage.status = 'needs_reconciliation'
        stage.available_at = instant
        stage.lease_until = None
        stage.updated_at = instant
        stage.result = {
            'reason': 'Parent worker lease expired',
            'remote_outcome': (
                'read_only' if parent.status == 'retry' else 'unknown'
            ),
            'parent_job_id': parent.id,
        }
        event(
            db,
            site,
            'worker_interrupted',
            f'{stage.kind} interrupted',
            {
                'job_id': stage.id,
                'status': stage.status,
                'parent_job_id': parent.id,
            },
        )


def _connection_capabilities(connection):
    capabilities = connection.capabilities if connection is not None else None
    return capabilities if isinstance(capabilities, dict) else {}


def _verified_wordpress_for_autopilot(connection):
    """Read persisted capability evidence without contacting WordPress."""

    if (
        connection is None
        or connection.status != 'connected'
        or not connection.encrypted_credentials
    ):
        return False
    capabilities = _connection_capabilities(connection)
    native = capabilities.get('native')
    native = native if isinstance(native, dict) else {}
    return (
        capabilities.get('authenticated') is True
        and native.get('create') is True
        and native.get('publish') is True
    )


def _verified_ai_for_autopilot(connection):
    """Check only saved, non-secret AI readiness; never call the provider."""

    if (
        connection is None
        or connection.status != 'connected'
        or not connection.encrypted_credentials
    ):
        return False
    settings = _connection_capabilities(connection).get('settings')
    if not isinstance(settings, dict):
        return False
    endpoint = settings.get('endpoint') or settings.get('base_url') or settings.get('url')
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    try:
        parsed = urlsplit(endpoint.strip())
        if (
            parsed.scheme.lower() not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
    except ValueError:
        return False
    if not isinstance(settings.get('model'), str) or not settings.get('model', '').strip():
        return False
    maximum = settings.get('max_cost_cents')
    estimate = settings.get('estimated_cost_cents')
    return (
        isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and maximum > 0
        and isinstance(estimate, int)
        and not isinstance(estimate, bool)
        and estimate >= 0
        and estimate <= maximum
    )


def _paid_visibility_pricing_ready(connection):
    """Return whether a paid visibility source has reservable pricing."""

    settings = _connection_capabilities(connection).get('settings')
    if not isinstance(settings, dict):
        return False
    pricing = settings.get('pricing')
    pricing = pricing if isinstance(pricing, dict) else {}
    estimate = settings.get('estimated_cost_cents')
    if estimate is None:
        estimate = settings.get('cost_cents')
    if estimate is None:
        estimate = pricing.get('estimated_cost_cents')
    if estimate is None:
        estimate = pricing.get('cost_cents')
    maximum = settings.get('max_cost_cents')
    if maximum is None:
        maximum = pricing.get('max_cost_cents')
    if (
        isinstance(estimate, bool)
        or not isinstance(estimate, int)
        or estimate < 0
        or (
            maximum is not None
            and (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or maximum <= 0
                or estimate > maximum
            )
        )
    ):
        return False
    return (maximum if maximum is not None else estimate) > 0


def _visibility_connection_ready(connection, kind):
    """Gate scheduled visibility work on the source's persisted readiness."""

    if connection is None or not connection.encrypted_credentials:
        return False
    status = connection.status
    if kind in {'gsc', 'ga4', 'ai'}:
        # These sources have an explicit read-only capability test. A saved
        # credential or an unsupported test result is not enough to schedule
        # recurring provider work.
        return status == 'connected'
    if kind == 'dataforseo':
        # DataForSEO's first real observation is the provider verification, so
        # a credential-shaped configured row may proceed only when a bounded
        # request cost is known and reservable. Unknown pricing stays paused.
        capabilities = _connection_capabilities(connection)
        credential_shape_verified = capabilities.get('credential_shape_verified') is True
        return (
            status == 'connected' or (status == 'configured' and credential_shape_verified)
        ) and _paid_visibility_pricing_ready(connection)
    if kind == 'pagespeed':
        # PageSpeed may be used without an API key; its public read is the
        # verification boundary, so configured is sufficient for a sample.
        return status in {'configured', 'connected'}
    return False


def _tracked_questions_ready(value):
    """Keep malformed persisted AI question lists out of the broker."""

    return _bounded_ai_questions(
        value,
        singular=isinstance(value, str),
    ) is not None


def _automatic_publish_policy_ready(site, policy, *, global_pause):
    """Validate the structural automation gate without provider I/O.

    Business facts, author selection, and the final write-time checks remain
    owned by the content-autopilot workflow. The scheduler only decides
    whether this site is eligible to receive one bounded parent job.
    """

    try:
        blockers = evaluate_policy(
            site,
            policy,
            'publish',
            None,
            global_pause=global_pause,
        )
    except Exception:
        return False
    return not any(blocker in _AUTOMATIC_POLICY_BLOCKERS for blocker in blockers)


def _publish_authorization(policy):
    """Return non-secret provenance for a scheduler-created publish job."""

    version = getattr(policy, 'version', None)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return {
        'type': 'policy',
        'action': 'publish',
        'policy_version': version,
    }


def _local_publish_window(site, cfg, instant):
    """Return the local date when the configured daily window is open."""

    publish_days = cfg.get('publish_days') if isinstance(cfg, dict) else None
    if (
        not isinstance(publish_days, list)
        or any(
            isinstance(day, bool) or not isinstance(day, int) or day < 0 or day > 6
            for day in publish_days
        )
    ):
        return None
    try:
        local = instant.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(site.timezone))
    except (TypeError, ValueError):
        return None
    if local.weekday() not in publish_days or local.hour < _AUTOPILOT_WINDOW_START_HOUR:
        return None
    return local.date().isoformat()


def _posts_per_week(cfg):
    value = cfg.get('posts_per_week', 2) if isinstance(cfg, dict) else 2
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


def _autopilot_reservation_id(job_id):
    return f'{_AUTOPILOT_RESERVATION_PREFIX}{job_id}'


def _autopilot_has_uncertain_outcome(db, site):
    """Hold future automatic writes after an unknown remote outcome."""

    jobs = db.scalars(select(Job).where(
        Job.site_id == site.id,
        Job.kind == 'content_autopilot',
    )).all()
    for job in jobs:
        result = job.result if isinstance(job.result, dict) else {}
        if job.status == 'needs_reconciliation' or result.get('status') in _AUTOPILOT_UNCERTAIN_STATUSES:
            return True
    return False


def _local_week_bounds(instant, site_timezone):
    """Return the current local calendar week as naive UTC bounds and a date key."""

    utc_instant = instant if instant.tzinfo is not None else instant.replace(tzinfo=timezone.utc)
    local = utc_instant.astimezone(ZoneInfo(site_timezone))
    week_start_local = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    week_end_local = week_start_local + timedelta(days=7)
    return (
        week_start_local.astimezone(timezone.utc).replace(tzinfo=None),
        week_end_local.astimezone(timezone.utc).replace(tzinfo=None),
        week_start_local.date().isoformat(),
    )


def _in_week(value, week_start, week_end):
    return value is not None and week_start <= value < week_end


def _quota_article(article):
    """Return whether an article can occupy an automatic publication slot."""

    if article is None or article.status in _EXCLUDED_QUOTA_ARTICLE_STATUSES:
        return False
    brief = article.brief if isinstance(article.brief, dict) else {}
    return article.status in _QUOTA_ARTICLE_STATUSES and brief.get('purpose') != 'refresh_existing'


def _publication_quota(db, site, instant):
    """Return article/reservation ids occupying automatic capacity.

    Published rows use their latest persisted timestamp for the site's local
    calendar week. Active publication records and active publish jobs are
    reservations regardless of when they started: an interrupted operation
    must not become a second write merely because the calendar rolled over.
    Article ids are counted once even when a job and publication record both
    exist for the same operation. A content-autopilot parent without an
    article yet uses a synthetic reservation id.
    """

    week_start, week_end, week_key = _local_week_bounds(instant, site.timezone)
    articles = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    by_id = {article.id: article for article in articles}
    occupied = set()

    publications = db.scalars(select(Publication).where(
        Publication.site_id == site.id,
        Publication.article_id.is_not(None),
        Publication.status.in_([*_ACTIVE_PUBLICATION_STATUSES, 'published']),
    )).all()
    for publication in publications:
        article = by_id.get(publication.article_id)
        if not _quota_article(article):
            continue
        if publication.status in _ACTIVE_PUBLICATION_STATUSES:
            occupied.add(article.id)
        elif _in_week(publication.updated_at or publication.created_at, week_start, week_end):
            occupied.add(article.id)

    for article in articles:
        if not _quota_article(article):
            continue
        if article.status in {'publishing', 'verifying'}:
            occupied.add(article.id)
        elif article.status == 'published' and _in_week(
            article.updated_at or article.created_at, week_start, week_end,
        ):
            occupied.add(article.id)

    jobs = db.scalars(select(Job).where(
        Job.site_id == site.id,
        Job.kind == 'publish',
        Job.status.in_(_ACTIVE_JOB_STATUSES),
    )).all()
    for job in jobs:
        payload = job.payload if isinstance(job.payload, dict) else {}
        article = by_id.get(payload.get('article_id'))
        if _quota_article(article):
            occupied.add(article.id)

    # A parent may not have selected an article yet.  Treat queued/running
    # parents, and parents held for reconciliation, as durable reservations so
    # another publish window cannot create a second automatic write attempt.
    autopilot_jobs = db.scalars(select(Job).where(
        Job.site_id == site.id,
        Job.kind == 'content_autopilot',
    )).all()
    for job in autopilot_jobs:
        result = job.result if isinstance(job.result, dict) else {}
        if (
            job.status in _ACTIVE_JOB_STATUSES
            or job.status == 'needs_reconciliation'
            or result.get('status') in _AUTOPILOT_UNCERTAIN_STATUSES
        ):
            occupied.add(_autopilot_reservation_id(job.id))

    return occupied, week_key


def _record_quota_deferral(db, site, article, *, used, limit, week_key):
    """Explain one over-cap article without producing an event every tick."""

    rows = db.scalars(select(Event).where(
        Event.site_id == site.id,
        Event.kind == 'publication_deferred_quota',
    )).all()
    if any(
        isinstance(row.data, dict)
        and row.data.get('article_id') == article.id
        and row.data.get('week_key') == week_key
        for row in rows
    ):
        return
    event(
        db,
        site,
        'publication_deferred_quota',
        f'Publication deferred until a later eligible week: {article.title}',
        {
            'article_id': article.id,
            'reason': 'posts_per_week',
            'used': used,
            'limit': limit,
            'week_key': week_key,
        },
    )


def bucket(instant,seconds):
    return int(instant.replace(tzinfo=timezone.utc).timestamp()) // seconds


def schedule():
    from app.worker import execute_job
    instant = now()
    dispatched = 0
    newly_queued = set()

    def enqueue_scheduled(db, site, kind, payload, idempotency_key):
        """Submit scheduled work once per tick, while retaining retry fallback.

        ``enqueue`` delivers a newly-created row immediately.  The durable
        queue sweep below is still needed for older rows whose broker delivery
        failed, but must not submit the just-created row a second time in this
        same tick.  If the immediate delivery failed, the row remains queued
        and the next tick's sweep will retry it.
        """

        key = f'{site.id}:{idempotency_key}'
        existed = db.scalar(select(Job).where(Job.idempotency_key == key)) is not None
        row = enqueue(db, site, kind, payload, idempotency_key)
        if not existed:
            newly_queued.add(row.id)
        return row

    with SessionLocal() as db:
        heartbeat = db.get(Heartbeat,'scheduler')
        for job in db.scalars(select(Job).where(Job.status == 'running',Job.lease_until <= instant)):
            # Paid and writing tasks can have succeeded remotely. Never replay blindly.
            # Targeted audits are read-only just like full audits.  A lost
            # worker lease must retry them; only work with an uncertain remote
            # write or paid side effect requires reconciliation.
            # A content-autopilot parent can already have generated or
            # published remotely.  Its expired lease must therefore stop for
            # reconciliation rather than being replayed as a write.
            safe = _job_lease_recovery_is_read_only(job)
            job.status = 'retry' if safe and job.attempts < 3 else 'needs_reconciliation'
            job.result = {'reason':'Worker lease expired','remote_outcome':'unknown' if not safe else 'read_only'}
            job.available_at,job.lease_until = instant,None
            site = db.get(Site,job.site_id)
            event(db,site,'worker_interrupted',f'{job.kind} interrupted',{'job_id':job.id,'status':job.status})
            _recover_full_cycle_stages(db, site, job, instant)
        db.commit()
        for site in db.scalars(select(Site)):
            connections = {c.kind:c for c in db.scalars(select(Connection).where(Connection.site_id == site.id,Connection.status != 'revoked'))}
            work = [('availability',60,{})]
            policy = current_policy(db,site.id)
            cfg = policy.settings if policy is not None and isinstance(policy.settings, dict) else {}
            controls = global_controls(db)
            global_pause = bool(controls.get('global_pause'))
            # A saved credential is not proof of authenticated access.  Wait
            # for the explicit capability test before queuing work that would
            # otherwise create noisy failures and false monitoring incidents.
            wordpress_ready = connections.get('wordpress') is not None and connections['wordpress'].status == 'connected'
            if connections.get('wordpress') is not None:
                # This is read-only bookkeeping over the verified connection's
                # persisted poll marker; it does not contact or write to WP.
                sync_wordpress_change_poll_incident(
                    db, site, connections['wordpress'], at=instant,
                )
            if wordpress_ready:
                work += [('poll_changes',300,{}),('inventory',86400,{}),('audit',604800,{}),('plan',604800,{}),('refresh',604800,{}),('digest',604800,{})]
            for kind in ('gsc','ga4','pagespeed','dataforseo'):
                if _visibility_connection_ready(connections.get(kind), kind):
                    work.append(('visibility',604800 if kind in ('dataforseo','pagespeed') else 86400,{'kind':kind}))
                    if kind == 'dataforseo' and isinstance(cfg.get('competitors'), list) and cfg.get('competitors'):
                        work.append(('visibility',604800,{'kind':'dataforseo','mode':'competitors'}))
            ai_ready = _visibility_connection_ready(connections.get('ai'), 'ai')
            if ai_ready and _tracked_questions_ready(cfg.get('tracked_questions')):
                work.append(('visibility',604800,{'kind':'ai_sample'}))
            for kind,seconds,payload in work:
                variant = payload.get('kind', '')
                if payload.get('mode'):
                    variant = f'{variant}:{payload["mode"]}'
                enqueue_scheduled(
                    db,
                    site,
                    kind,
                    payload,
                    f'schedule:{kind}:{variant}:{bucket(instant,seconds)}',
                )
            automatic = cfg.get('enabled') and not site.paused and not global_pause
            publish_authorization = _publish_authorization(policy)
            publish_policy_ready = (
                publish_authorization is not None
                and _automatic_publish_policy_ready(
                    site, policy, global_pause=global_pause,
                )
            )
            if automatic and 'publish' in cfg.get('allowed_actions',[]) and publish_policy_ready:
                # Automatic publication is bounded by this site's local-week
                # policy quota. Editorial failure never fills a quota. The
                # automatic planned-article generate loop intentionally no
                # longer runs here; content_autopilot owns that pipeline.
                local = instant.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(site.timezone))
                occupied, week_key = _publication_quota(db, site, instant)
                posts_per_week = _posts_per_week(cfg)
                if posts_per_week is None:
                    # Preserve the historical manual-publication fallback;
                    # the autopilot gate below still rejects an invalid policy.
                    posts_per_week = 2
                for article in db.scalars(select(Article).where(Article.site_id == site.id,Article.status.in_(['checked','scheduled'])).order_by(Article.created_at)):
                    if article.status == 'checked' and local.weekday() in cfg.get('publish_days',[1,4]) and local.hour >= 9:
                        article.scheduled_at,article.status = instant,'scheduled'
                    if article.status == 'scheduled' and article.scheduled_at and article.scheduled_at <= instant:
                        # An existing active record/job already reserves this
                        # article's slot. The stable key below is still the
                        # final idempotency guard for a concurrent tick.
                        if article.id in occupied:
                            continue
                        if len(occupied) >= posts_per_week:
                            _record_quota_deferral(
                                db,
                                site,
                                article,
                                used=len(occupied),
                                limit=posts_per_week,
                                week_key=week_key,
                            )
                            continue
                        job = enqueue_scheduled(
                            db,
                            site,
                            'publish',
                            {
                                'article_id': article.id,
                                'authorization': publish_authorization,
                                'policy_version': publish_authorization['policy_version'],
                            },
                            f'publish:{article.id}',
                        )
                        if job.status in _ACTIVE_JOB_STATUSES:
                            occupied.add(article.id)

                # One governed parent owns the plan -> research -> generate ->
                # check -> publish -> verify pipeline for each local publish
                # day. The parent key is stable for the site/day through
                # enqueue(), and active parents are already counted above.
                autopilot_window = _local_publish_window(site, cfg, instant)
                autopilot_ready = (
                    autopilot_window is not None
                    and publish_authorization is not None
                    and _automatic_publish_policy_ready(
                        site, policy, global_pause=global_pause,
                    )
                    and _verified_wordpress_for_autopilot(connections.get('wordpress'))
                    and _verified_ai_for_autopilot(connections.get('ai'))
                )
                if (
                    autopilot_ready
                    and posts_per_week > 0
                    and not _autopilot_has_uncertain_outcome(db, site)
                    and len(occupied) < posts_per_week
                ):
                    parent = enqueue_scheduled(
                        db,
                        site,
                        'content_autopilot',
                        {
                            'max_articles': 1,
                            'authorization': publish_authorization,
                            'policy_version': publish_authorization['policy_version'],
                        },
                        f'content-autopilot:{autopilot_window}',
                    )
                    if parent.status in _ACTIVE_JOB_STATUSES:
                        occupied.add(_autopilot_reservation_id(parent.id))
            # Cadence incidents are derived only from durable job completion
            # state and the persisted poll marker.  This does not contact a
            # provider and deliberately leaves no-evidence states unknown.
            sync_site_cadence_incidents(db, site, at=instant)
            db.commit()
        # Broker delivery may be repeated; atomic claim ensures a single handler.
        # Keep dispatch bounded, but calculate health from the complete durable
        # queue.  Otherwise the 201st overdue job could be invisible in the
        # heartbeat and fail to open the queue incident.
        due_filters = (Job.status.in_(['queued', 'retry']), Job.available_at <= instant)
        paused_site_ids = set(db.scalars(select(Site.id).where(Site.paused.is_(True))).all())
        global_pause = global_controls(db)['global_pause']
        # Held write jobs remain part of the durable backlog and its health
        # measurement, but must not consume the bounded dispatch batch.  If a
        # paused site has a large publish/generate backlog, selecting those
        # rows first can otherwise starve read-only checks (including checks
        # for other sites) indefinitely.
        dispatch_filters = list(due_filters)
        # ``enqueue`` can publish immediately, and a prior sweep may have
        # published a row without a worker claiming it yet.  Do not send the
        # same durable row again on every 30-second beat tick; the queued-row
        # lease is refreshed after each attempt, so broker recovery still
        # happens after a bounded cooldown.  Filtering in SQL avoids recent
        # rows occupying the 200-row batch and starving older overdue work.
        dispatch_filters.append(or_(
            Job.lease_until.is_(None),
            Job.lease_until <= instant,
        ))
        pause_sensitive_filter = or_(
            Job.kind.in_(_PAUSE_SENSITIVE_JOB_KINDS),
            and_(
                Job.kind == 'full_cycle',
                Job.payload['mode'].as_string() == 'autopilot',
            ),
        )
        if global_pause:
            dispatch_filters.append(~pause_sensitive_filter)
        elif paused_site_ids:
            dispatch_filters.append(~(
                pause_sensitive_filter
                & Job.site_id.in_(paused_site_ids)
            ))
        pending = db.scalars(
            select(Job).where(*dispatch_filters).order_by(Job.available_at).limit(200)
        ).all()
        due_jobs = int(db.scalar(select(func.count(Job.id)).where(*due_filters)) or 0)
        oldest_due_at = db.scalar(select(func.min(Job.available_at)).where(*due_filters))
        missed_checks = int(db.scalar(select(func.count(Job.id)).where(
            *due_filters,
            Job.available_at < instant - timedelta(minutes=5),
        )) or 0)
        for job in pending:
            # A site pause must also hold automatic write work that was
            # already durable when the operator paused it. Read-only checks
            # continue so the site remains observable while paused.
            if _job_is_pause_sensitive(job) and (
                global_pause or job.site_id in paused_site_ids
            ):
                continue
            if job.id in newly_queued:
                continue
            delivery_failed = False
            try:
                execute_job.apply_async(args=[job.id],queue='browser' if job.kind == 'browser' else 'platform')
                dispatched += 1
            except Exception:
                # The broker may have accepted the message before reporting an
                # error.  Throttle the ambiguous retry just like a successful
                # publish, while leaving the durable row queued for recovery.
                delivery_failed = True
            finally:
                # Update only a still-queued row.  A fast worker may already
                # have claimed it and installed its execution lease.
                db.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.status.in_(['queued', 'retry']))
                    .values(
                        lease_until=instant + timedelta(seconds=JOB_DISPATCH_COOLDOWN_SECONDS),
                        updated_at=instant,
                    )
                )
            # A broker error must stop this sweep, as before; the conditional
            # lease update above still prevents an ambiguous immediate retry.
            if delivery_failed:
                break
        queue_delay = max(0, round((instant - oldest_due_at).total_seconds())) if oldest_due_at else 0
        heartbeat_details = {
            'dispatched': dispatched,
            'due_jobs': due_jobs,
            'oldest_due_at': oldest_due_at.isoformat() if oldest_due_at else None,
            'queue_delay_seconds': queue_delay,
            'missed_checks': missed_checks,
            'checked_at': instant.isoformat(),
        }
        # A heartbeat is evidence that the complete scheduler cycle reached
        # its health measurement, not merely that the process entered this
        # function.  Keep an existing heartbeat unchanged until the final
        # monitoring synchronization and commit succeed; otherwise a failed
        # site pass could make stale queue metrics look fresh.
        if heartbeat is None:
            heartbeat = Heartbeat(
                name='scheduler',
                last_seen_at=instant,
                details=heartbeat_details,
            )
            db.add(heartbeat)
        else:
            heartbeat.last_seen_at = instant
            heartbeat.details = heartbeat_details
        sync_monitoring_incidents(
            db,
            scheduler_healthy=True,
            queue_healthy=(
                queue_delay <= QUEUE_DELAY_DEGRADED_AFTER_SECONDS
                and missed_checks == 0
            ),
            scheduler_details={
                'seconds_since_heartbeat': 0,
                'threshold_seconds': SCHEDULER_STALE_AFTER_SECONDS,
                'last_seen_at': instant.isoformat(),
            },
            queue_details={
                'queue_delay_seconds': queue_delay,
                'missed_checks': missed_checks,
                'threshold_seconds': QUEUE_DELAY_DEGRADED_AFTER_SECONDS,
                'due_jobs': due_jobs,
            },
        )
        db.commit()
    return {'leader':True,'dispatched':dispatched}
