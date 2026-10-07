"""Archiving preserves history, isolates teams, and stops all scheduled work."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import hmac
import json
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import api, scheduler, worker
from app.main import app
from app.models import Article, Job, Site, Team
from app.operations import enqueue, now
from tests.test_platform import platform


def test_archive_restore_keeps_history_and_cancels_pending_work(platform):
    client, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        article = Article(site_id=site_id, title='Retained article', body='<p>Saved</p>')
        completed = Job(site_id=site_id, kind='audit', status='complete', idempotency_key='old-audit')
        queued = Job(site_id=site_id, kind='publish', status='queued', idempotency_key='future-publish',
                     result={'previous_evidence': {'stage': 'prepared'}})
        db.add_all([article, completed, queued])
        db.commit()
        article_id, completed_id, queued_id = article.id, completed.id, queued.id

    archived = client.post(f'/api/v1/sites/{site_id}/archive')
    assert archived.status_code == 200, archived.text
    assert archived.json()['archived_at']
    assert archived.json()['paused'] is True
    assert client.get('/api/v1/sites').json()['total'] == 0
    assert client.get('/api/v1/sites?include_archived=true').json()['total'] == 1
    assert client.post(f'/api/v1/sites/{site_id}/jobs', json={'kind': 'audit'}).status_code == 409
    assert client.patch(f'/api/v1/sites/{site_id}', json={'paused': False}).status_code == 409
    assert client.get(f'/api/v1/sites/{site_id}/articles/{article_id}').status_code == 200
    assert client.post(f'/api/v1/sites/{site_id}/archive').json()['archived_at'] == archived.json()['archived_at']
    with factory() as db:
        assert db.get(Article, article_id) is not None
        assert db.get(Job, completed_id).status == 'complete'
        assert db.get(Job, queued_id).status == 'cancelled'
        assert db.get(Job, queued_id).result['previous_evidence'] == {'stage': 'prepared'}
        with pytest.raises(ValueError, match='Restore'):
            enqueue(db, db.get(Site, site_id), 'availability')

    restored = client.post(f'/api/v1/sites/{site_id}/restore')
    assert restored.status_code == 200
    assert restored.json()['archived_at'] is None
    assert restored.json()['paused'] is True
    assert client.get('/api/v1/sites').json()['total'] == 1
    with factory() as db:
        assert db.get(Job, queued_id).status == 'cancelled'


@pytest.mark.parametrize('role', ['editor', 'viewer'])
def test_only_owner_can_archive_or_restore(platform, role):
    client, factory, site_id = platform
    with factory() as db:
        team_id = db.get(Site, site_id).team_id
    app.dependency_overrides[api.require_user] = lambda: {'team_id': team_id, 'role': role}
    assert client.post(f'/api/v1/sites/{site_id}/archive').status_code == 403
    assert client.post(f'/api/v1/sites/{site_id}/restore').status_code == 403


def test_archive_is_team_scoped_and_refuses_work_in_progress(platform, monkeypatch):
    client, factory, site_id = platform
    with factory() as db:
        other = Team(name='Other team')
        db.add(other)
        db.flush()
        other_site = Site(team_id=other.id, name='Other site', origin='https://other.example')
        db.add(other_site)
        db.commit()
        other_id = other_site.id
    assert client.post(f'/api/v1/sites/{other_id}/archive').status_code == 404
    assert client.post(f'/api/v1/sites/{other_id}/restore').status_code == 404
    assert other_id not in [s['id'] for s in client.get('/api/v1/sites?include_archived=true').json()['items']]

    @contextmanager
    def locked(_):
        yield False
    monkeypatch.setattr(worker, 'exclusive', locked)
    assert client.post(f'/api/v1/sites/{site_id}/archive').status_code == 409
    with factory() as db:
        assert db.get(Site, site_id).archived_at is None


def test_archived_sites_have_no_scheduler_work_or_worker_execution(platform, monkeypatch):
    _, factory, site_id = platform
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    with factory() as db:
        site = db.get(Site, site_id)
        site.archived_at = now()
        # Simulate a stale delivery arriving after archiving.
        job = Job(site_id=site_id, kind='availability', status='queued',
                  idempotency_key='stale-delivery', available_at=now() - timedelta(minutes=10))
        db.add(job)
        db.commit()
        job_id = job.id
    deliveries = []
    monkeypatch.setattr(worker.execute_job, 'apply_async', lambda *a, **kw: deliveries.append((a, kw)))
    scheduler.schedule()
    assert deliveries == []
    with factory() as db:
        assert len(db.scalars(select(Job).where(Job.site_id == site_id)).all()) == 1
    assert worker.run_job(job_id) == {'reason': 'site_archived'}
    with factory() as db:
        assert db.get(Job, job_id).status == 'cancelled'


def test_archive_waits_for_a_durable_running_job(platform):
    client, factory, site_id = platform
    with factory() as db:
        db.add(Job(site_id=site_id, kind='publish', status='running', idempotency_key='in-progress'))
        db.commit()
    response = client.post(f'/api/v1/sites/{site_id}/archive')
    assert response.status_code == 409
    with factory() as db:
        assert db.get(Site, site_id).archived_at is None


def test_archived_site_acknowledges_signed_notifications_without_work(platform):
    from datetime import datetime, timezone
    client, factory, site_id = platform
    secret = 'archive-notification-secret-long-enough-for-testing'
    assert client.put(f'/api/v1/sites/{site_id}/connections/wordpress',
                      json={'credentials': {'webhook_secret': secret}, 'settings': {}}).status_code == 200
    assert client.post(f'/api/v1/sites/{site_id}/archive').status_code == 200
    body = json.dumps({'event': 'post.changed', 'occurred_at': datetime.now(timezone.utc).isoformat(),
                       'data': {'id': 9, 'resource_type': 'posts'}}).encode()
    url = f'/api/v1/webhooks/wordpress/{site_id}'
    assert client.post(url, content=body, headers={'X-ForgeSEO-Signature': 'bad'}).status_code == 401
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(url, content=body, headers={'X-ForgeSEO-Signature': signature})
    assert response.status_code == 202
    assert response.json() == {'status': 'ignored', 'reason': 'site_archived'}
    with factory() as db:
        assert db.scalar(select(Job.id).where(Job.site_id == site_id)) is None


def test_archive_migration_upgrades_legacy_database_without_losing_sites():
    path = Path(__file__).resolve().parents[1] / 'alembic' / 'versions' / '0003_site_archive.py'
    spec = importlib.util.spec_from_file_location('site_archive_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE sites (id VARCHAR(32) PRIMARY KEY, name VARCHAR(200))'))
        connection.execute(text("INSERT INTO sites (id, name) VALUES ('existing', 'Existing site')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        row = connection.execute(text('SELECT id, name, archived_at FROM sites')).one()
        assert tuple(row) == ('existing', 'Existing site', None)
    engine.dispose()
