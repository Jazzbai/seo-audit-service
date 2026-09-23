import json

import pytest
from sqlalchemy import select

from app import api, worker
from app.models import Event, Job, Site
from app.operations import event
from app.workflows import HANDLERS
from test_platform import platform  # Reuse the existing authenticated fixture.


@pytest.fixture
def sse_platform(platform, monkeypatch):
    client, factory, site_id = platform
    monkeypatch.setattr(api, 'SessionLocal', factory)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(api.asyncio, 'sleep', no_sleep)
    return client, factory, site_id


def _read_sse(response):
    assert response.status_code == 200, response.text
    records = []
    for block in response.text.split('\n\n'):
        fields = {}
        data = []
        for line in block.splitlines():
            if line.startswith('data: '):
                data.append(line[6:])
            elif ': ' not in line:
                continue
            else:
                key, value = line.split(': ', 1)
                fields[key] = value
        if 'id' in fields and 'event' in fields and data:
            fields['id'] = int(fields['id'])
            fields['data'] = json.loads('\n'.join(data))
            records.append(fields)
    return records


def _insert_events(factory, site_id, rows):
    with factory() as db:
        site = db.get(Site, site_id)
        before = db.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0
        persisted = []
        for kind, message, data in rows:
            persisted.append(event(db, site, kind, message, data))
        db.commit()
        return before, [row.id for row in persisted]


def _stream(client, site_id, cursor=0):
    return _read_sse(client.get(
        f'/api/v1/sites/{site_id}/events',
        headers={'Last-Event-ID': str(cursor)},
    ))


def test_sse_classifies_progress_and_preserves_activity(sse_platform):
    client, factory, site_id = sse_platform
    before, ids = _insert_events(factory, site_id, [
        ('site_updated', 'Activity event', {'kind': 'activity'}),
        ('job_progress', 'Job started', {
            'site_id': site_id,
            'job_id': 'job-1',
            'job_kind': 'audit',
            'status': 'running',
            'phase': 'worker_start',
            'percent': 0,
            'message': 'Job started',
        }),
    ])

    records = _stream(client, site_id, before)

    assert [record['id'] for record in records] == ids
    assert [record['event'] for record in records] == ['activity', 'progress']
    assert records[1]['data']['data']['job_id'] == 'job-1'


def test_sse_last_event_id_replays_activity_and_progress_in_global_order(sse_platform):
    client, factory, site_id = sse_platform
    before, ids = _insert_events(factory, site_id, [
        ('site_updated', 'Before progress', {}),
        ('job_progress', 'Progress update', {
            'site_id': site_id,
            'job_id': 'job-2',
            'job_kind': 'inventory',
            'status': 'complete',
            'phase': 'worker_finish',
            'percent': 100,
            'message': 'Progress update',
        }),
        ('site_updated', 'After progress', {}),
    ])

    first = _stream(client, site_id, before)
    replay = _stream(client, site_id, ids[0])

    assert [record['id'] for record in first] == ids
    assert [record['id'] for record in replay] == ids[1:]
    assert [record['event'] for record in replay] == ['progress', 'activity']


def test_sse_progress_is_site_scoped(sse_platform):
    client, factory, site_id = sse_platform
    response = client.post(
        '/api/v1/sites',
        json={'name': 'Second site', 'origin': 'https://second.example.test'},
    )
    assert response.status_code == 201, response.text
    other_site_id = response.json()['id']
    with factory() as db:
        before = db.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0
        first_site = db.get(Site, site_id)
        second_site = db.get(Site, other_site_id)
        event(db, first_site, 'job_progress', 'First site', {
            'site_id': site_id, 'job_id': 'first-job', 'job_kind': 'audit',
            'status': 'running', 'phase': 'worker_start', 'percent': 0,
            'message': 'First site',
        })
        event(db, second_site, 'job_progress', 'Second site', {
            'site_id': other_site_id, 'job_id': 'second-job', 'job_kind': 'audit',
            'status': 'running', 'phase': 'worker_start', 'percent': 0,
            'message': 'Second site',
        })
        db.commit()

    records = _stream(client, site_id, before)

    assert records
    assert all(record['data']['site_id'] == site_id for record in records)
    assert all('second-job' not in json.dumps(record) for record in records)


def test_sse_progress_payload_filters_unsafe_provider_data(sse_platform):
    client, factory, site_id = sse_platform
    before, _ids = _insert_events(factory, site_id, [
        ('job_progress', 'Safe progress message', {
            'site_id': site_id,
            'job_id': 'job-safe',
            'job_kind': 'audit',
            'status': 'running',
            'phase': 'stage_start',
            'stage': 'public_audit',
            'stage_index': 3,
            'stage_count': 5,
            'percent': 40,
            'message': 'Safe progress message',
            'credentials': {'password': 'do-not-send'},
            'provider_response': {'token': 'do-not-send'},
            'raw_html': '<html>do-not-send</html>',
        }),
    ])

    records = _stream(client, site_id, before)
    progress = records[0]['data']['data']

    assert set(progress) <= {
        'site_id', 'job_id', 'job_kind', 'status', 'phase', 'stage',
        'stage_index', 'stage_count', 'percent', 'message',
    }
    assert progress['site_id'] == site_id
    assert progress['percent'] == 40
    body = json.dumps(records[0])
    assert 'do-not-send' not in body
    assert '<html>' not in body


def test_activity_overview_and_report_filter_raw_event_payloads(sse_platform):
    client, factory, site_id = sse_platform
    _insert_events(factory, site_id, [
        ('site_updated', 'Safe activity summary', {
            'status': 'complete',
            'job_id': 'job-activity-safe',
            'credentials': {'password': 'do-not-send'},
            'idempotency_key': 'private-idempotency-key',
            'provider_response': {'token': 'do-not-send'},
            'raw_html': '<html>do-not-send</html>',
        }),
    ])

    activity = client.get(f'/api/v1/sites/{site_id}/activity')
    overview = client.get(f'/api/v1/sites/{site_id}/overview')
    report = client.get(f'/api/v1/sites/{site_id}/reports/weekly')
    assert activity.status_code == overview.status_code == report.status_code == 200

    for body in (activity.json(), overview.json(), report.json()):
        encoded = json.dumps(body)
        assert 'do-not-send' not in encoded
        assert 'private-idempotency-key' not in encoded
        assert '<html>' not in encoded

    assert activity.json()['items'][0]['data'] == {
        'job_id': 'job-activity-safe',
        'status': 'complete',
    }


def test_worker_emits_safe_start_finish_progress(platform, monkeypatch):
    client, factory, site_id = platform

    async def fake_handler(_db, _site, _job):
        return {
            'complete': True,
            'provider_response': {'token': 'do-not-persist-in-progress'},
            'html': '<html>do-not-persist-in-progress</html>',
        }

    monkeypatch.setitem(HANDLERS, 'audit', fake_handler)
    response = client.post(
        f'/api/v1/sites/{site_id}/jobs',
        json={'kind': 'audit', 'idempotency_key': 'progress-worker-test'},
    )
    assert response.status_code == 202, response.text
    worker.run_job(response.json()['id'])

    with factory() as db:
        rows = db.scalars(select(Event).where(
            Event.site_id == site_id,
            Event.kind == 'job_progress',
        ).order_by(Event.id)).all()

    assert [row.data['status'] for row in rows] == ['running', 'complete']
    assert all(set(row.data) <= {
        'site_id', 'job_id', 'job_kind', 'status', 'phase', 'stage',
        'stage_index', 'stage_count', 'percent', 'message',
    } for row in rows)
    assert 'do-not-persist-in-progress' not in json.dumps([row.data for row in rows])


def test_full_cycle_emits_stage_progress_for_needs_connection_and_failure(platform, monkeypatch):
    client, factory, site_id = platform

    async def complete_stage(_db, _site, _job):
        return {'complete': True}

    async def failed_stage(_db, _site, _job):
        raise RuntimeError('provider token and raw html must stay private')

    for handler_name in ('availability', 'inventory', 'plan', 'refresh'):
        monkeypatch.setitem(HANDLERS, handler_name, complete_stage)
    monkeypatch.setitem(HANDLERS, 'audit', failed_stage)

    response = client.post(
        f'/api/v1/sites/{site_id}/jobs',
        json={'kind': 'full_cycle', 'idempotency_key': 'progress-full-cycle-test'},
    )
    assert response.status_code == 202, response.text
    worker.run_job(response.json()['id'])

    with factory() as db:
        rows = db.scalars(select(Event).where(
            Event.site_id == site_id,
            Event.kind == 'job_progress',
        ).order_by(Event.id)).all()

    stage_rows = [row for row in rows if row.data.get('stage')]
    assert {row.data['phase'] for row in stage_rows} >= {
        'stage_start', 'stage_complete', 'stage_needs_connection', 'stage_failure',
    }
    assert all(row.data['stage_count'] == 5 for row in stage_rows)
    assert all(0 <= row.data['percent'] <= 100 for row in stage_rows)
    assert 'provider token' not in json.dumps([row.data for row in rows])
    assert '<html>' not in json.dumps([row.data for row in rows])
