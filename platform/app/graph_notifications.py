"""Explicit, audited Microsoft 365 notifications; acceptance is not delivery."""
from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select

from app.models import Membership
from app.operations import (credentials, event, find_connection, iso, now,
                            sync_health_incident, _parse_timestamp)


KIND = 'microsoft_graph'
SCOPE_REVIEW_DAYS = 7
INCIDENT_KEY = 'notifications:microsoft_graph'


def configuration_fingerprint(connection) -> str:
    config = (connection.capabilities or {}).get('settings', {})
    body = json.dumps({'site_id': connection.site_id, 'connection_id': connection.id,
                       'settings': config, 'credentials': connection.encrypted_credentials},
                      sort_keys=True, separators=(',', ':'))
    return sha256(body.encode()).hexdigest()


def require_owner(db, site, user_id):
    if not isinstance(user_id, str) or not db.scalar(select(Membership.id).where(
            Membership.team_id == site.team_id, Membership.user_id == user_id,
            Membership.role == 'owner')):
        raise ValueError('Notification approval requires a current workspace owner')


def require_scope_review(db, site, connection):
    if connection is None or connection.status != 'connected' or not connection.encrypted_credentials:
        raise ValueError('Test the Microsoft 365 connection before sending')
    review = (connection.capabilities or {}).get('scope_review', {})
    checked = _parse_timestamp(review.get('reviewed_at')) if isinstance(review, dict) else None
    if (not isinstance(review, dict) or review.get('kind') != 'owner_attested_exchange_rbac'
            or checked is None or checked > now() + timedelta(seconds=30)
            or now() - checked > timedelta(days=SCOPE_REVIEW_DAYS)
            or review.get('configuration_sha256') != configuration_fingerprint(connection)):
        raise ValueError('Review the mailbox-scoped Microsoft 365 permissions before sending')
    require_owner(db, site, review.get('reviewer_id'))
    return review


def _failure(db, site, job, status, code):
    job.result = {**(job.result or {}), 'status': status, 'email': status,
                  'error_code': code, 'retryable': False, 'complete': False}
    sync_health_incident(db, site, key=INCIDENT_KEY, healthy=False, kind='notification',
                        severity='medium', title='Microsoft 365 notification needs attention',
                        details={'channel': KIND, 'job_id': job.id, 'status': status, 'error_code': code})
    event(db, site, 'notification_outcome', 'Microsoft 365 notification needs attention',
          {'job_id': job.id, 'status': status})
    db.commit()
    return job.result


async def send_graph_notification(db, site, job, *, subject: str, body: str,
                                  explicit_test: bool = False):
    from app.connectors.microsoft_graph import MicrosoftGraphError, MicrosoftGraphMailClient

    previous = job.result if isinstance(job.result, dict) else {}
    if previous.get('status') in ('accepted', 'recipient_confirmed', 'outcome_unknown', 'failed', 'blocked'):
        return previous
    if previous.get('send_started_at'):
        return _failure(db, site, job, 'outcome_unknown', 'previous_send_requires_review')
    connection = find_connection(db, site.id, KIND)
    try:
        require_scope_review(db, site, connection)
        fingerprint = configuration_fingerprint(connection)
        if explicit_test:
            require_owner(db, site, job.payload.get('approved_by'))
            if (job.payload.get('connection_id') != connection.id
                    or job.payload.get('configuration_sha256') != fingerprint):
                raise ValueError('Notification configuration changed after approval')
        secret, config = credentials(db, site.id, KIND)
        if not explicit_test and config.get('digest_enabled') is not True:
            return {'email': 'disabled', 'status': 'disabled'}
    except Exception:
        return _failure(db, site, job, 'blocked', 'connection_or_scope_review_required')

    async def before_send():
        db.refresh(connection)
        require_scope_review(db, site, connection)
        if configuration_fingerprint(connection) != fingerprint:
            raise ValueError('Notification connection changed before sending')
        if explicit_test:
            require_owner(db, site, job.payload.get('approved_by'))
        job.result = {'status': 'sending', 'email': 'sending', 'channel': KIND,
                      'send_started_at': iso(now()), 'configuration_sha256': fingerprint}
        db.commit()

    try:
        async with MicrosoftGraphMailClient(secret, config) as client:
            result = await client.send_message(subject, body, job.id, before_send=before_send)
        if not isinstance(result, dict) or result.get('status') != 'accepted':
            return _failure(db, site, job, 'outcome_unknown', 'unexpected_send_response')
    except Exception as exc:
        started = (job.result or {}).get('send_started_at')
        known = isinstance(exc, MicrosoftGraphError)
        code = exc.code if known else 'notification_send_failed'
        # A transport exception after intent was committed may hide acceptance.
        # No retry is safe until an operator reconciles the message externally.
        status = ('outcome_unknown' if not known or exc.outcome_unknown else 'failed') if started else 'blocked'
        return _failure(db, site, job, status, code)

    job.result = {**(job.result or {}), 'status': 'accepted', 'email': 'accepted',
                  'accepted_at': iso(now()), 'recipient_confirmation': 'pending', 'retryable': False}
    sync_health_incident(db, site, key=INCIDENT_KEY, healthy=True, kind='notification',
                        severity='medium', title='Microsoft 365 notification needs attention')
    event(db, site, 'notification_accepted', 'Microsoft 365 accepted the notification; receipt is not confirmed',
          {'job_id': job.id, 'status': 'accepted'})
    db.commit()
    return job.result


async def notification_test(db, site, job):
    if job.payload.get('kind') != KIND:
        raise ValueError('Unsupported notification test')
    return await send_graph_notification(
        db, site, job, explicit_test=True,
        subject='ForgeSEO notification delivery test',
        body=(f'This is the one-time notification test approved for {site.name}.\n'
              f'Operation: {job.id}\n'
              'No article was published. Please confirm receipt in ForgeSEO after reading this email.'),
    )
