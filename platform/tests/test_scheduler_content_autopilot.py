"""Scheduler-only coverage for governed content-autopilot dispatch.

These tests exercise durable queue decisions with fixture rows only. They do
not run a worker, decrypt a provider credential, or contact a remote service.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import operations, scheduler, worker
from app.config import settings
from app.models import Article, Connection, Job, Publication, Site
from app.policies import create_policy, current_policy
from test_platform import platform


@pytest.fixture
def autopilot_scheduler(platform, monkeypatch):
    client, factory, site_id = platform
    clock = [datetime(2026, 9, 15, 13, 59)]  # 08:59 in the site's timezone.
    deliveries = []

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'now', lambda: clock[0])
    monkeypatch.setattr(operations, 'now', lambda: clock[0])
    monkeypatch.setattr(
        worker.execute_job,
        'apply_async',
        lambda *args, **kwargs: deliveries.append(
            (kwargs.get('args') or args)[0]
        ),
    )

    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        site.timezone = 'America/Chicago'
        site.facts = {
            'business_name': 'Fixture business',
            'services': ['Repairs'],
            'authors': [{'id': 'author-1', 'name': 'Fixture author'}],
        }
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['publish'],
            'posts_per_week': 2,
            'publish_days': [1, 4],
            'author_id': 'author-1',
        })
        db.add_all([
            Connection(
                site_id=site_id,
                kind='wordpress',
                encrypted_credentials='fixture-wordpress-ciphertext',
                status='connected',
                capabilities={
                    'authenticated': True,
                    'authenticated_author': {'id': 'author-1', 'name': 'Fixture author'},
                    'native': {'create': True, 'publish': True},
                },
                checked_at=clock[0],
                created_at=clock[0],
            ),
            Connection(
                site_id=site_id,
                kind='ai',
                encrypted_credentials='fixture-ai-ciphertext',
                status='connected',
                capabilities={
                    'settings': {
                        'endpoint': 'https://ai.example.test/generate',
                        'model': 'fixture-model',
                        'estimated_cost_cents': 1,
                        'max_cost_cents': 2,
                    },
                },
                checked_at=clock[0],
                created_at=clock[0],
            ),
        ])
        db.commit()

    yield client, factory, site_id, clock, deliveries


def _autopilot_jobs(factory, site_id):
    with factory() as db:
        return db.scalars(select(Job).where(
            Job.site_id == site_id,
            Job.kind == 'content_autopilot',
        ).order_by(Job.created_at, Job.id)).all()


@pytest.mark.parametrize('gate', [
    'policy_disabled',
    'site_paused',
    'global_pause',
    'wordpress_missing',
    'wordpress_unverified',
    'ai_missing',
    'ai_unverified',
])
def test_scheduler_fail_closed_before_queuing_autopilot(autopilot_scheduler, gate):
    _client, factory, site_id, clock, _deliveries = autopilot_scheduler
    clock[0] = datetime(2026, 9, 15, 14, 0)  # 09:00 local on a configured day.

    if gate == 'global_pause':
        settings.GLOBAL_PAUSE = True
    with factory() as db:
        site = db.get(Site, site_id)
        if gate == 'policy_disabled':
            create_policy(db, site, None, {
                'enabled': False,
                'allowed_actions': ['publish'],
                'author_id': 'author-1',
            })
        elif gate == 'site_paused':
            site.paused = True
        elif gate == 'wordpress_missing':
            db.delete(db.scalar(select(Connection).where(
                Connection.site_id == site_id, Connection.kind == 'wordpress',
            )))
        elif gate == 'wordpress_unverified':
            wordpress = db.scalar(select(Connection).where(
                Connection.site_id == site_id, Connection.kind == 'wordpress',
            ))
            wordpress.capabilities = {
                'authenticated': True,
                'native': {'create': True, 'publish': False},
            }
        elif gate == 'ai_missing':
            db.delete(db.scalar(select(Connection).where(
                Connection.site_id == site_id, Connection.kind == 'ai',
            )))
        elif gate == 'ai_unverified':
            ai = db.scalar(select(Connection).where(
                Connection.site_id == site_id, Connection.kind == 'ai',
            ))
            ai.status = 'needs_test'
        db.commit()

    scheduler.schedule()

    assert _autopilot_jobs(factory, site_id) == []


def test_scheduler_uses_local_publish_window_and_daily_idempotency(
    autopilot_scheduler,
):
    _client, factory, site_id, clock, deliveries = autopilot_scheduler

    # Before the 09:00 local window, no parent is queued.
    scheduler.schedule()
    assert _autopilot_jobs(factory, site_id) == []

    # The first eligible tick queues one parent; repeated ticks reuse the same
    # site-scoped day key and do not create another parent or generate job.
    clock[0] = datetime(2026, 9, 15, 14, 0)
    with factory() as db:
        db.add(Article(
            site_id=site_id,
            title='Existing planned brief',
            slug='existing-planned-brief',
            status='planned',
            managed=True,
            created_at=clock[0],
            updated_at=clock[0],
        ))
        db.commit()
    scheduler.schedule()
    scheduler.schedule()

    jobs = _autopilot_jobs(factory, site_id)
    assert len(jobs) == 1
    with factory() as db:
        policy_version = current_policy(db, site_id).version
    assert jobs[0].payload == {
        'max_articles': 1,
        'authorization': {
            'type': 'policy',
            'action': 'publish',
            'policy_version': policy_version,
        },
        'policy_version': policy_version,
    }
    assert jobs[0].idempotency_key == f'{site_id}:content-autopilot:2026-09-15'
    with factory() as db:
        assert db.scalars(select(Job).where(
            Job.site_id == site_id,
            Job.kind == 'generate',
        )).all() == []

    # Wednesday is not configured; Friday is configured and receives a
    # distinct daily parent while the first queued parent remains a quota
    # reservation.
    clock[0] = datetime(2026, 9, 16, 14, 0)
    scheduler.schedule()
    assert len(_autopilot_jobs(factory, site_id)) == 1
    clock[0] = datetime(2026, 9, 18, 14, 0)
    scheduler.schedule()
    jobs = _autopilot_jobs(factory, site_id)
    assert len(jobs) == 2
    assert [job.idempotency_key for job in jobs] == [
        f'{site_id}:content-autopilot:2026-09-15',
        f'{site_id}:content-autopilot:2026-09-18',
    ]
    # Scheduler only handed durable job ids to the broker shim; it never ran a
    # workflow or made an HTTP/provider call.
    assert deliveries
    assert all(isinstance(job_id, str) for job_id in deliveries)


def test_active_parent_and_published_article_bound_weekly_quota(autopilot_scheduler):
    _client, factory, site_id, clock, _deliveries = autopilot_scheduler
    clock[0] = datetime(2026, 9, 15, 14, 0)
    with factory() as db:
        article = Article(
            site_id=site_id,
            title='Already published',
            slug='already-published',
            status='published',
            managed=True,
            created_at=clock[0] - timedelta(hours=1),
            updated_at=clock[0] - timedelta(hours=1),
        )
        db.add(article)
        db.flush()
        db.add(Publication(
            site_id=site_id,
            article_id=article.id,
            operation_key=f'fixture-published:{site_id}',
            status='published',
            policy_version=1,
            created_at=article.created_at,
            updated_at=article.updated_at,
            result={'status': 'published'},
        ))
        db.commit()

    # One existing publication plus one active parent fills the two-post
    # weekly capacity; the Friday tick must not create a third write attempt.
    scheduler.schedule()
    assert len(_autopilot_jobs(factory, site_id)) == 1
    clock[0] = datetime(2026, 9, 18, 14, 0)
    scheduler.schedule()
    assert len(_autopilot_jobs(factory, site_id)) == 1


def test_expired_autopilot_lease_requires_reconciliation(autopilot_scheduler):
    _client, factory, site_id, clock, deliveries = autopilot_scheduler
    expired = clock[0] - timedelta(minutes=1)
    with factory() as db:
        row = Job(
            site_id=site_id,
            kind='content_autopilot',
            status='running',
            payload={'max_articles': 1},
            result={},
            idempotency_key=f'{site_id}:content-autopilot:expired',
            attempts=1,
            available_at=expired,
            lease_until=expired,
        )
        db.add(row)
        db.commit()
        job_id = row.id

    scheduler.schedule()

    with factory() as db:
        recovered = db.get(Job, job_id)
        assert recovered.status == 'needs_reconciliation'
        assert recovered.result == {
            'reason': 'Worker lease expired',
            'remote_outcome': 'unknown',
        }
    assert job_id not in deliveries
