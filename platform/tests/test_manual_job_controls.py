"""Paused manual submissions and safe, site-scoped waiting-job cancellation."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app import api, worker
from app.main import app
from app.models import Article, Event, Heartbeat, Job, Site
from app.operations import now
from test_platform import platform


def waiting_job(factory, site_id, *, status='queued'):
    with factory() as db:
        row = Job(site_id=site_id, kind='generate', status=status,
                  payload={'article_id': 'draft'}, result={'note': 'Retain evidence'},
                  idempotency_key=f'cancel-test-{status}', lease_until=now()+timedelta(minutes=1))
        db.add(row)
        db.commit()
        return row.id


@pytest.mark.parametrize('site_pause,global_pause', [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize('kind', ['generate', 'publish'])
def test_paused_manual_work_rejects_without_job_or_provider_call(platform, site_pause, global_pause, kind):
    client, factory, site_id = platform
    with factory() as db:
        db.get(Site, site_id).paused = site_pause
        db.add(Heartbeat(name='platform_controls', last_seen_at=now(), details={'global_pause': global_pause}))
        db.commit()
    response = client.post(f'/api/v1/sites/{site_id}/jobs', json={
        'kind': kind, 'payload': {'article_id': 'draft'}, 'idempotency_key': 'manual-paused',
    })
    assert response.status_code == 409
    assert 'No job was queued' in response.json()['detail']
    with factory() as db:
        assert list(db.scalars(select(Job))) == []


def test_paused_article_publish_rejects_without_changing_draft(platform):
    client, factory, site_id = platform
    with factory() as db:
        article = Article(site_id=site_id, title='A retained draft', status='checked')
        db.add(article)
        db.commit()
        article_id = article.id
    response = client.post(f'/api/v1/sites/{site_id}/articles/{article_id}/publish')
    assert response.status_code == 409
    with factory() as db:
        assert db.get(Article, article_id).status == 'checked'
        assert list(db.scalars(select(Job))) == []


@pytest.mark.parametrize('status', ['queued', 'retry'])
def test_cancel_waiting_job_is_durable_idempotent_and_late_delivery_ignored(platform, status):
    client, factory, site_id = platform
    job_id = waiting_job(factory, site_id, status=status)
    path = f'/api/v1/sites/{site_id}/jobs/{job_id}/cancel'
    first = client.post(path)
    assert first.status_code == 200, first.text
    assert first.json()['status'] == 'cancelled'
    assert first.json()['lease_until'] is None
    assert first.json()['result']['note'] == 'Retain evidence'
    assert first.json()['result']['reason'] == 'user_cancelled'
    assert client.post(path).status_code == 200
    assert worker.run_job(job_id) == {'ignored': True}
    with factory() as db:
        assert db.get(Job, job_id).status == 'cancelled'
        events = list(db.scalars(select(Event).where(Event.kind == 'job_cancelled')))
        assert len(events) == 1


@pytest.mark.parametrize('status', ['running', 'complete', 'failed', 'blocked', 'partial', 'needs_reconciliation'])
def test_cancel_cannot_replace_running_finished_or_uncertain_result(platform, status):
    client, factory, site_id = platform
    job_id = waiting_job(factory, site_id, status=status)
    assert client.post(f'/api/v1/sites/{site_id}/jobs/{job_id}/cancel').status_code == 409
    with factory() as db:
        row = db.get(Job, job_id)
        assert row.status == status
        assert row.result == {'note': 'Retain evidence'}


def test_cancel_rechecks_status_if_worker_claims_after_initial_read(platform, monkeypatch):
    client, factory, site_id = platform
    job_id = waiting_job(factory, site_id)
    original = api.own
    def claimed_between_read_and_write(db, cls, row_id, scoped_site):
        row = original(db, cls, row_id, scoped_site)
        if cls is Job and row_id == job_id:
            with factory() as other:
                other.execute(update(Job).where(Job.id == job_id).values(status='running'))
                other.commit()
        return row
    monkeypatch.setattr(api, 'own', claimed_between_read_and_write)
    assert client.post(f'/api/v1/sites/{site_id}/jobs/{job_id}/cancel').status_code == 409
    with factory() as db:
        assert db.get(Job, job_id).status == 'running'
        assert list(db.scalars(select(Event).where(Event.kind == 'job_cancelled'))) == []


def test_cancel_is_site_scoped_and_viewer_cannot_cancel(platform):
    client, factory, site_id = platform
    job_id = waiting_job(factory, site_id)
    other = client.post('/api/v1/sites', json={'name': 'Other', 'origin': 'https://other.example.test'}).json()
    assert client.post(f'/api/v1/sites/{other["id"]}/jobs/{job_id}/cancel').status_code == 404
    password = 'Long viewer test password!'
    assert client.post('/api/v1/team/members', json={
        'email': 'viewer@example.test', 'name': 'Viewer', 'password': password, 'role': 'viewer',
    }).status_code == 200
    with TestClient(app) as viewer:
        login = viewer.post('/api/v1/auth/login', headers={'Origin': 'http://testserver'},
                            json={'email': 'viewer@example.test', 'password': password})
        viewer.headers.update({'Origin': 'http://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        assert viewer.post(f'/api/v1/sites/{site_id}/jobs/{job_id}/cancel').status_code == 403
    with factory() as db:
        assert db.get(Job, job_id).status == 'queued'
