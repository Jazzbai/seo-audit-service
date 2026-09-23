"""Focused provenance coverage for scheduler-created write jobs."""

from datetime import datetime, timedelta
import json
import re

import pytest
from sqlalchemy import select, update

from app import operations, scheduler, worker
from app.config import settings
from app.models import Article, Connection, Job, Policy, Site
from app.policies import create_policy, current_policy
from test_platform import platform


@pytest.fixture
def authorization_scheduler(platform, monkeypatch):
    _client, factory, site_id = platform
    clock = [datetime(2026, 9, 15, 14, 0)]  # 09:00 in the site's timezone.
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
        policy = create_policy(db, site, None, {
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
        policy_version = policy.version

    yield factory, site_id, clock, deliveries, policy_version


def _jobs(factory, site_id):
    with factory() as db:
        return db.scalars(select(Job).where(
            Job.site_id == site_id,
        ).order_by(Job.created_at, Job.id)).all()


def _write_jobs(factory, site_id):
    return [job for job in _jobs(factory, site_id) if job.kind in {
        'publish', 'content_autopilot',
    }]


def test_scheduled_write_jobs_persist_current_policy_provenance_and_read_only_jobs_do_not(
    authorization_scheduler,
):
    factory, site_id, clock, _deliveries, policy_version = authorization_scheduler
    with factory() as db:
        db.add(Article(
            site_id=site_id,
            title='Scheduled fixture article',
            slug='scheduled-fixture-article',
            status='scheduled',
            scheduled_at=clock[0] - timedelta(minutes=1),
            managed=True,
            created_at=clock[0],
            updated_at=clock[0],
        ))
        db.commit()

    scheduler.schedule()

    jobs = _jobs(factory, site_id)
    writes = {job.kind: job for job in jobs if job.kind in {
        'publish', 'content_autopilot',
    }}
    assert set(writes) == {'publish', 'content_autopilot'}
    for job in writes.values():
        assert job.payload['policy_version'] == policy_version
        assert job.payload['authorization'] == {
            'type': 'policy',
            'action': 'publish',
            'policy_version': policy_version,
        }

    for job in jobs:
        if job.kind not in {'publish', 'content_autopilot'}:
            assert 'authorization' not in job.payload
            assert 'policy_version' not in job.payload

    serialized = json.dumps([job.payload for job in jobs], sort_keys=True)
    assert not re.search(
        r'password|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|credential',
        serialized,
        re.IGNORECASE,
    )


def test_later_scheduling_uses_new_policy_version_without_rewriting_prior_job(
    authorization_scheduler,
):
    factory, site_id, clock, _deliveries, first_version = authorization_scheduler

    scheduler.schedule()
    first = [job for job in _write_jobs(factory, site_id) if job.kind == 'content_autopilot']
    assert len(first) == 1
    assert first[0].payload['policy_version'] == first_version

    with factory() as db:
        site = db.get(Site, site_id)
        second_policy = create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['publish'],
            'posts_per_week': 2,
            'publish_days': [1, 4],
            'author_id': 'author-1',
        })
        db.commit()
        second_version = second_policy.version
        assert second_version > first_version

    clock[0] = datetime(2026, 9, 18, 14, 0)
    scheduler.schedule()
    scheduler.schedule()

    parents = [job for job in _write_jobs(factory, site_id) if job.kind == 'content_autopilot']
    assert len(parents) == 2
    assert [job.payload['policy_version'] for job in parents] == [
        first_version,
        second_version,
    ]
    assert parents[0].payload['authorization']['policy_version'] == first_version
    assert parents[1].payload['authorization']['policy_version'] == second_version
    with factory() as db:
        assert current_policy(db, site_id).version == second_version


@pytest.mark.parametrize('gate', [
    'paused',
    'invalid_policy',
    'wordpress_connection',
    'ai_connection',
])
def test_scheduler_gates_write_jobs_before_enqueue(authorization_scheduler, gate):
    factory, site_id, clock, _deliveries, _policy_version = authorization_scheduler
    with factory() as db:
        site = db.get(Site, site_id)
        if gate == 'paused':
            site.paused = True
        elif gate == 'invalid_policy':
            # Policies are append-only through the ORM. This direct fixture
            # update represents a legacy/corrupt persisted row without
            # changing the production policy contract.
            db.execute(update(Policy).where(
                Policy.site_id == site_id,
                Policy.version == current_policy(db, site_id).version,
            ).values(settings={
                'enabled': True,
                'allowed_actions': 'publish',
            }))
        elif gate in {'wordpress_connection', 'ai_connection'}:
            connection = db.scalar(select(Connection).where(
                Connection.site_id == site_id,
                Connection.kind == gate.split('_')[0],
            ))
            connection.status = 'needs_test'
        db.commit()

    scheduler.schedule()

    assert _write_jobs(factory, site_id) == []
