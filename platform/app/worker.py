"""Celery delivery over a durable database queue, with cross-process site locks."""
import asyncio
import hashlib
import threading
from contextlib import contextmanager
from datetime import timedelta

from celery import Celery
from sqlalchemy import select, text, update

from app.config import settings
from app.connectors.errors import IncompleteInventory
from app.db import SessionLocal, engine
from app.models import Heartbeat, Job, Site
from app.operations import (JOB_DISPATCH_COOLDOWN_SECONDS, event,
                             global_controls, now)

celery = Celery('forgeseo', broker=settings.BROKER_URL)
celery.conf.update(task_default_queue='platform', task_acks_late=True,
    task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    task_soft_time_limit=840, task_time_limit=900, broker_connection_retry_on_startup=True,
    broker_transport_options={'confirm_publish':True}, task_publish_retry=False,
    beat_schedule={'dispatch':{'task':'forgeseo.tick','schedule':30.0}},
    task_routes={'forgeseo.tick':{'queue':'scheduler'}})
_locks = {}
_mutex = threading.Lock()


_READ_ONLY_JOB_KINDS = frozenset({
    'inventory', 'poll_changes', 'audit', 'availability', 'browser',
    'plan', 'targeted_audit', 'reconcile_publication',
})
_PAUSE_SENSITIVE_JOB_KINDS = frozenset({
    'generate', 'publish',
})


def _is_read_only_job(job: Job) -> bool:
    """Classify retries from the durable job contract, not just its kind."""

    if job.kind in _READ_ONLY_JOB_KINDS:
        return True
    if job.kind == 'full_cycle':
        payload = job.payload if isinstance(job.payload, dict) else {}
        return payload.get('mode', 'read_only') != 'autopilot'
    return False


def _pause_reason(db, site, job) -> str | None:
    """Hold direct writes before their handler can start.

    ``content_autopilot`` is intentionally not in this set. Its handler starts
    with a local policy/connection preflight and returns a structured gated
    result while paused; suppressing that safe preflight would break the API's
    blocker contract. Its scheduler and enqueue paths still keep it out of the
    broker during a pause, so this exception only covers a message already
    delivered before the pause changed.
    """

    pause_sensitive = job.kind in _PAUSE_SENSITIVE_JOB_KINDS
    if job.kind == 'full_cycle':
        payload = job.payload if isinstance(job.payload, dict) else {}
        pause_sensitive = payload.get('mode', 'read_only') == 'autopilot'
    if not pause_sensitive:
        return None
    if site.paused:
        return 'site_paused'
    if global_controls(db).get('global_pause'):
        return 'global_pause'
    return None


@contextmanager
def exclusive(key):
    """Dedicated session-level advisory lock survives workflow commits."""
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
    if engine.dialect.name == 'postgresql':
        with engine.connect() as conn:
            acquired = bool(conn.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key':number}))
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key':number})
    else:
        # SQLite is a single-process development/test mode, not production scheduling.
        with _mutex:
            lock = _locks.setdefault(key,threading.Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


def run_job(job_id):
    with SessionLocal() as db:
        job = db.get(Job,job_id)
        if job is None or job.status not in ('queued','retry') or job.available_at > now():
            return {'ignored':True}
        with exclusive('site:' + job.site_id) as acquired:
            if not acquired:
                # A second queue message can reach a worker while another
                # task owns this site's advisory lock.  The message will be
                # acknowledged by Celery, so keep the durable row leased
                # while the task schedules a short broker retry below.
                deferred_until = now() + timedelta(
                    seconds=JOB_DISPATCH_COOLDOWN_SECONDS,
                )
                job.lease_until = deferred_until
                job.updated_at = deferred_until
                db.commit()
                return {'deferred':'site_busy'}
            claim = db.execute(update(Job).where(Job.id == job_id,Job.status.in_(['queued','retry']),Job.available_at <= now())
                .values(status='running',attempts=Job.attempts+1,lease_until=now()+timedelta(minutes=16),updated_at=now()))
            if claim.rowcount != 1:
                db.rollback()
                return {'ignored':True}
            db.commit()
            db.refresh(job)
            site = db.get(Site,job.site_id)
            if site is None:
                job.status,job.result = 'failed',{'reason':'Site no longer exists'}
                job.lease_until,job.updated_at = None,now()
                db.commit()
                return job.result
            pause_reason = _pause_reason(db, site, job)
            if pause_reason:
                job.status = 'queued'
                job.attempts = max(0, (job.attempts or 0) - 1)
                job.available_at = now() + timedelta(seconds=30)
                job.lease_until = None
                if job.kind == 'full_cycle':
                    from app.workflows import _full_cycle_held_result

                    job.result = _full_cycle_held_result(job, 'autopilot', pause_reason)
                else:
                    job.result = {'status': 'held', 'reason': pause_reason}
                job.updated_at = now()
                event_kind = 'full_cycle_held' if job.kind == 'full_cycle' else 'job_held'
                event_message = (
                    'Full cycle held by pause control'
                    if job.kind == 'full_cycle'
                    else f'{job.kind} held by pause control'
                )
                event_data = {'job_id': job.id, 'reason': pause_reason}
                if job.kind == 'full_cycle':
                    event_data['mode'] = 'autopilot'
                else:
                    event_data['kind'] = job.kind
                event(db, site, event_kind, event_message, event_data)
                db.commit()
                return job.result
            from app.workflows import HANDLERS, emit_job_progress

            event(db,site,'job_started',f'{job.kind} started',{'job_id':job.id})
            emit_job_progress(
                db,
                site,
                job_id=job.id,
                job_kind=job.kind,
                status='running',
                phase='worker_start',
                percent=0,
                message=f'{job.kind} started',
            )
            db.commit()
            try:
                if job.kind == 'browser':
                    from app.browser import inspect_page
                    handler = inspect_page
                elif job.kind == 'digest':
                    from app.notifications import digest
                    handler = digest
                else:
                    handler = HANDLERS[job.kind]
                result = asyncio.run(handler(db,site,job))
                job.status = 'partial' if result.get('complete') is False else 'complete'
                job.result = result
                event(db,site,'job_finished',f'{job.kind} {job.status}',{'job_id':job.id,'status':job.status})
                emit_job_progress(
                    db,
                    site,
                    job_id=job.id,
                    job_kind=job.kind,
                    status=job.status,
                    phase='worker_finish',
                    percent=100,
                    message=f'{job.kind} {job.status}',
                )
            except Exception as exc:
                db.rollback()
                job = db.get(Job,job_id)
                # Never expose exception text: providers may include credentials in URLs.
                safe_reason = str(exc) if type(exc) is ValueError and not any(x in str(exc).lower() for x in ('https://','token','password','secret')) else type(exc).__name__
                incomplete_inventory = type(exc) is IncompleteInventory
                if incomplete_inventory:
                    safe_reason = IncompleteInventory.MESSAGES.get(exc.reason, IncompleteInventory.MESSAGES['pagination'])
                read_only = _is_read_only_job(job)
                at_inventory_limit = incomplete_inventory and exc.reason == 'limit'
                job.status = 'retry' if read_only and job.attempts < 3 and not isinstance(exc,ValueError) and not at_inventory_limit else ('blocked' if isinstance(exc,ValueError) else 'failed')
                job.available_at = now()+timedelta(seconds=min(300,30*2**job.attempts))
                job.result = {'error_type':type(exc).__name__,'reason':safe_reason,'retryable':job.status == 'retry'}
                if incomplete_inventory:
                    job.result['inventory_issue'] = exc.reason if exc.reason in IncompleteInventory.MESSAGES else 'pagination'
                event(db,site,'job_exception',f'{job.kind}: {job.status}',{'job_id':job.id,**job.result})
                emit_job_progress(
                    db,
                    site,
                    job_id=job.id,
                    job_kind=job.kind,
                    status=job.status,
                    phase='worker_retry' if job.status == 'retry' else 'worker_failure',
                    percent=0 if job.status == 'retry' else 100,
                    message=f'{job.kind} will retry' if job.status == 'retry' else f'{job.kind} failed',
                )
            job.lease_until,job.updated_at = None,now()
            db.commit()
            return job.result


@celery.task(bind=True, name='forgeseo.execute_job', max_retries=None)
def execute_job(self, job_id):
    result = run_job(job_id)
    if isinstance(result, dict) and result.get('deferred') == 'site_busy':
        # Site work is deliberately serialized across platform and browser
        # workers.  Requeue only this contention result; connector failures,
        # policy blocks, and uncertain writes retain their normal handling.
        raise self.retry(countdown=5)
    return result


@celery.task(name='forgeseo.tick')
def tick():
    from app.scheduler import schedule
    with exclusive('scheduler') as acquired:
        if acquired:
            return schedule()
    return {'leader':False}
