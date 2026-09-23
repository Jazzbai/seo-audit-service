"""Integration gates: browser auth, tenant boundaries, durable accounting and recovery."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import api, worker
from app.auth import get_db
from app.config import settings
from app.main import app
from app.models import Article, Base, Candidate, Connection, Finding, Heartbeat, Job, Measurement, Page, Site, Team
from app.operations import enqueue, now
from app.workflows import upsert_observation

TEST_BOOTSTRAP_TOKEN = 'test-only-bootstrap-token'


@pytest.fixture
def platform(monkeypatch,tmp_path):
    engine = create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine,expire_on_commit=False)
    def session():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(settings,'PUBLIC_URL','http://testserver')
    monkeypatch.setattr(settings,'COOKIE_SECURE',False)
    monkeypatch.setattr(settings,'BOOTSTRAP_TOKEN',TEST_BOOTSTRAP_TOKEN)
    monkeypatch.setattr(settings,'ENCRYPTION_KEY','x'*44)
    monkeypatch.setattr(settings,'ARTIFACT_ROOT',str(tmp_path/'artifacts'))
    monkeypatch.setattr(worker,'SessionLocal',factory)
    monkeypatch.setattr(worker,'engine',engine)
    monkeypatch.setattr(worker.execute_job,'apply_async',lambda *a,**kw: None)
    client = TestClient(app)
    response = client.post('/api/v1/auth/bootstrap',json={'email':'owner@example.test','password':'A-very-long-test-passphrase!43','name':'Owner','team_name':'Pilot'},headers={'Origin':'http://testserver','X-ForgeSEO-Bootstrap-Token':TEST_BOOTSTRAP_TOKEN})
    assert response.status_code == 200,response.text
    client.headers.update({'X-CSRF-Token':response.json()['csrf_token'],'Origin':'http://testserver'})
    response = client.post('/api/v1/sites',json={'name':'Independent test','origin':'https://example.test','facts':{'business_name':'Independent test','services':['Repairs']}})
    assert response.status_code == 201,response.text
    yield client,factory,response.json()['id']
    client.close()
    app.dependency_overrides.clear()
    engine.dispose()


def test_onboarding_and_explicit_empty_coverage(platform):
    client,factory,site_id = platform
    data = client.get(f'/api/v1/sites/{site_id}/overview').json()
    assert data['coverage']['status'] == 'not_checked'
    assert data['monitoring']['status'] == 'not_running'
    assert data['site']['paused'] is True
    assert data['counts']['pages'] == 0
    assert client.get('/api/v1/sites/not-mine/overview').status_code == 404


def test_article_check_persists_review_state_for_editor(platform):
    client, factory, site_id = platform
    with factory() as db:
        db.add(Article(
            site_id=site_id,
            title='A useful repair guide',
            body='<p>Bring your repair questions.</p>',
            sources=[],
        ))
        db.commit()
        article_id = db.scalar(select(Article).where(Article.site_id == site_id)).id

    checked = client.post(f'/api/v1/sites/{site_id}/articles/{article_id}/check')
    assert checked.status_code == 200
    assert checked.json()['passed'] is False

    stored = client.get(f'/api/v1/sites/{site_id}/articles/{article_id}')
    assert stored.status_code == 200
    assert stored.json()['status'] == 'review_needed'
    assert stored.json()['checks']['passed'] is False
    assert 'missing_author' in stored.json()['checks']['blockers']


def test_overview_labels_completed_audits_with_errors(platform):
    client, factory, site_id = platform
    with factory() as db:
        db.add(Job(
            id='audit-with-errors',
            site_id=site_id,
            kind='audit',
            status='complete',
            result={'complete': True, 'errors': ['one unavailable URL'], 'pending_urls': []},
            idempotency_key='audit-with-errors',
            updated_at=now(),
        ))
        db.commit()
    coverage = client.get(f'/api/v1/sites/{site_id}/overview').json()['coverage']
    assert coverage['status'] == 'complete_with_errors'
    assert coverage['error_count'] == 1
    assert coverage['pending_url_count'] == 0
    assert coverage['last_audit_at']


def test_inventory_canonicalizes_connector_keys_and_persists_authentication(platform, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import workflows

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def validate_connection(self):
            return {
                'kind': 'wordpress',
                'authenticated': True,
                'native': {'create': True, 'publish': True},
            }

        async def inventory(self):
            return [
                {'resource_key': 'post:1', 'resource_type': 'post', 'url': 'https://example.test/one', 'title': 'One'},
                {'resource_key': 'page:2', 'resource_type': 'page', 'url': 'https://example.test/two', 'title': 'Two'},
                {'resource_key': 'author:3', 'resource_type': 'author', 'url': 'https://example.test/author', 'title': 'Author'},
            ]

    async def fake_client_for(db, site, kind='wordpress'):
        assert kind == 'wordpress'
        return FakeClient()

    monkeypatch.setattr(workflows, 'client_for', fake_client_for)
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Connection(site_id=site_id, kind='wordpress', encrypted_credentials='test', status='needs_test'))
        present = Page(site_id=site_id, resource_key='posts:1', resource_type='posts', url='https://example.test/one')
        missing = Page(site_id=site_id, resource_key='posts:9', resource_type='posts', url='https://example.test/missing')
        db.add_all([present, missing])
        db.commit()

        result = asyncio.run(workflows.inventory(db, site, SimpleNamespace(id='inventory-test')))
        db.refresh(present)
        db.refresh(missing)
        connection = db.query(Connection).filter_by(site_id=site_id, kind='wordpress').one()
        stored_keys = {page.resource_key for page in db.query(Page).filter_by(site_id=site_id)}

        assert result == {
            'resources': 3,
            'counts': {'wordpress': {'seen': 3, 'missing': 1}},
            'complete': True,
        }
        assert {'posts:1', 'pages:2', 'authors:3', 'posts:9'} <= stored_keys
        assert present.signals['inventory']['status'] == 'present'
        assert missing.signals['inventory']['status'] == 'missing'
        assert connection.status == 'connected'
        assert connection.capabilities['authenticated'] is True


@pytest.mark.parametrize('job_kind', ['inventory', 'poll_changes'])
@pytest.mark.parametrize('failure', ['limit', 'pagination'])
def test_incomplete_inventory_preserves_evidence_and_reports_job_failure(platform, monkeypatch, job_kind, failure):
    import httpx
    from app import workflows
    from app.connectors.errors import IncompleteInventory
    from app.connectors.wordpress import WordPressClient
    from app.models import Event

    class FailingCollectionClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def validate_connection(self):
            return {'authenticated': True}

        async def inventory(self, *, modified_after=None):
            # Exercise the real connector boundary, including rejection of an
            # untrusted malformed header before it reaches activity output.
            header = '2' if failure == 'limit' else 'never-log-this-provider-secret'
            transport = httpx.MockTransport(lambda request: httpx.Response(
                200, json=[{'id': 1}], headers={'X-WP-TotalPages': header}, request=request,
            ))
            async with WordPressClient('https://example.test', {'token': 'fixture-secret'}, transport=transport) as connector:
                return await connector._fetch_collection('posts', max_pages=1)

    async def fake_client_for(db, site, kind='wordpress'):
        return FailingCollectionClient()

    monkeypatch.setattr(workflows, 'client_for', fake_client_for)
    client, factory, site_id = platform
    original_capabilities = {'change_poll': {'cursor': '2026-09-16T15:55:00Z', 'last_seen_count': 4}}
    original_signals = {'inventory': {'status': 'present', 'checked_at': '2026-09-16T15:55:00Z'}}
    with factory() as db:
        connection = Connection(site_id=site_id, kind='wordpress', encrypted_credentials='test',
                                status='connected', capabilities=original_capabilities)
        page = Page(site_id=site_id, resource_key='posts:9', resource_type='posts',
                    url='https://example.test/nine', signals=original_signals, source_hash='original')
        job = Job(site_id=site_id, kind=job_kind, idempotency_key=f'incomplete:{job_kind}:{failure}', available_at=now())
        db.add_all([connection, page, job])
        db.flush()
        finding = Finding(site_id=site_id, page_id=page.id, key='still-open', code='missing_meta_description',
                          severity='warning', title='Unresolved page metadata')
        candidate = Candidate(site_id=site_id, page_id=page.id, field='meta_description',
                              source_hash='original', after_value='Pending description')
        db.add_all([finding, candidate])
        db.commit()
        job_id, page_id, connection_id = job.id, page.id, connection.id
        finding_id, candidate_id = finding.id, candidate.id

    result = worker.run_job(job_id)
    assert result['error_type'] == 'IncompleteInventory'
    assert result['reason'] == IncompleteInventory.MESSAGES[failure]
    assert result['inventory_issue'] == failure
    assert result['retryable'] is (failure != 'limit')
    with factory() as db:
        assert db.get(Page, page_id).signals == original_signals
        assert db.get(Page, page_id).source_hash == 'original'
        assert db.query(Page).filter_by(site_id=site_id).count() == 1
        assert db.get(Connection, connection_id).capabilities == original_capabilities
        assert db.get(Connection, connection_id).checked_at is None
        assert db.get(Finding, finding_id).status == 'open'
        assert db.get(Finding, finding_id).resolved_at is None
        assert db.get(Candidate, candidate_id).status == 'pending'
        assert db.get(Job, job_id).status == ('failed' if failure == 'limit' else 'retry')
        assert db.get(Job, job_id).lease_until is None
        assert not db.query(Event).filter_by(site_id=site_id, kind='inventory_complete').all()
        assert 'never-log-this-provider-secret' not in json.dumps([row.data for row in db.query(Event)])
    response = client.get(f'/api/v1/sites/{site_id}/jobs/{job_id}')
    assert response.status_code == 200
    assert response.json()['result']['reason'] == result['reason']
    assert 'never-log-this-provider-secret' not in response.text


def test_poll_changes_queues_targeted_audits_and_advances_only_after_read(platform, monkeypatch):
    import asyncio
    from datetime import datetime
    from types import SimpleNamespace
    from app import workflows

    current = datetime(2026, 9, 16, 16, 0)
    monkeypatch.setattr(workflows, 'now', lambda: current)
    seen_cursors = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def inventory(self, *, modified_after):
            seen_cursors.append(modified_after)
            return [
                {'resource_key': 'post:7', 'resource_type': 'post', 'url': 'https://example.test/changed', 'title': 'Changed'},
                {'resource_key': 'author:3', 'resource_type': 'author', 'url': 'https://example.test/author', 'title': 'Author'},
            ]

    async def fake_client_for(db, site, kind='wordpress'):
        assert kind == 'wordpress'
        return FakeClient()

    monkeypatch.setattr(workflows, 'client_for', fake_client_for)
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Connection(site_id=site_id, kind='wordpress', encrypted_credentials='test', status='connected'))
        db.commit()
        result = asyncio.run(workflows.poll_changes(db, site, SimpleNamespace(id='poll-test')))
        jobs = db.scalars(select(Job).where(Job.site_id == site_id, Job.kind == 'targeted_audit')).all()
        connection = db.scalar(select(Connection).where(Connection.site_id == site_id, Connection.kind == 'wordpress'))

        assert result['complete'] is True
        assert result['seen'] == 2
        assert len(result['targeted_audit_job_ids']) == 1
        assert seen_cursors == ['2026-09-16T15:50:00Z']
        assert len(jobs) == 1
        assert jobs[0].payload == {'resource_key': 'posts:7'}
        assert connection.capabilities['change_poll']['last_seen_count'] == 2
        assert connection.capabilities['change_poll']['last_scheduled_count'] == 1


def test_poll_changes_keeps_cursor_when_wordpress_read_fails(platform, monkeypatch):
    import asyncio
    from app import workflows

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def inventory(self, *, modified_after):
            raise RuntimeError('temporary WordPress failure')

    async def fake_client_for(db, site, kind='wordpress'):
        return FailingClient()

    monkeypatch.setattr(workflows, 'client_for', fake_client_for)
    _, factory, site_id = platform
    original = {
        'change_poll': {
            'cursor': '2026-09-16T15:55:00Z',
            'last_seen_count': 4,
        },
    }
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Connection(
            site_id=site_id,
            kind='wordpress',
            encrypted_credentials='test',
            status='connected',
            capabilities=original,
        ))
        db.commit()
        with pytest.raises(RuntimeError, match='temporary WordPress failure'):
            asyncio.run(workflows.poll_changes(db, site, None))
        db.expire_all()
        connection = db.scalar(select(Connection).where(Connection.site_id == site_id, Connection.kind == 'wordpress'))
        assert connection.capabilities == original


def test_cookie_csrf_and_tenant_isolation(platform):
    client,factory,site_id = platform
    assert client.post(f'/api/v1/sites/{site_id}/jobs',json={'kind':'audit'},headers={'X-CSRF-Token':''}).status_code == 403
    assert client.post(f'/api/v1/sites/{site_id}/jobs',json={'kind':'audit'},headers={'Origin':'https://evil.test'}).status_code == 403
    with factory() as db:
        team = Team(name='Another team')
        db.add(team)
        db.flush()
        site = Site(team_id=team.id,name='Private',origin='https://other.test')
        db.add(site)
        db.commit()
        other_id = site.id
    assert client.get(f'/api/v1/sites/{other_id}').status_code == 404
    assert client.patch(f'/api/v1/sites/{other_id}',json={'paused':False}).status_code == 404


def test_secrets_never_returned(platform):
    client,factory,site_id = platform
    payload = {'credentials':{'username':'tester','application_password':'secret never returned'},'settings':{}}
    response = client.put(f'/api/v1/sites/{site_id}/connections/wordpress',json=payload)
    assert response.status_code == 200,response.text
    assert 'secret never returned' not in response.text
    assert 'encrypted_credentials' not in response.text
    assert client.get(f'/api/v1/sites/{site_id}/connections').json()['items'][0]['status'] == 'needs_test'


def test_smtp_digest_setting_requires_a_boolean_and_persists(platform):
    client, factory, site_id = platform
    saved = client.put(
        f'/api/v1/sites/{site_id}/connections/smtp',
        json={'credentials': {'username': 'smtp-user', 'password': 'smtp-password'}, 'settings': {'digest_enabled': False}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()['capabilities']['settings']['digest_enabled'] is False

    malformed = client.put(
        f'/api/v1/sites/{site_id}/connections/smtp',
        json={'credentials': {}, 'settings': {'digest_enabled': 'false'}},
    )
    assert malformed.status_code == 422

    with factory() as db:
        connection = db.scalar(select(Connection).where(Connection.site_id == site_id, Connection.kind == 'smtp'))
        assert connection.capabilities['settings']['digest_enabled'] is False


def test_ga4_report_settings_are_bounded_and_persisted_without_credentials(platform):
    client, factory, site_id = platform
    response = client.put(
        f'/api/v1/sites/{site_id}/connections/ga4',
        json={
            'credentials': {'client_secret': 'never-returned'},
            'settings': {
                'property_id': '123456789',
                'conversion_event_names': ['generate_lead', 'purchase'],
                'dimensions': ['date', 'eventName'],
                'metrics': ['sessions', 'conversions'],
            },
        },
    )
    assert response.status_code == 200, response.text
    assert 'never-returned' not in response.text
    stored = response.json()['capabilities']['settings']
    assert stored == {
        'property_id': '123456789',
        'conversion_event_names': ['generate_lead', 'purchase'],
        'dimensions': ['date', 'eventName'],
        'metrics': ['sessions', 'conversions'],
    }

    too_many = client.put(
        f'/api/v1/sites/{site_id}/connections/ga4',
        json={
            'credentials': {},
            'settings': {'metrics': [f'metric_{index}' for index in range(11)]},
        },
    )
    assert too_many.status_code == 422

    with factory() as db:
        connection = db.scalar(select(Connection).where(Connection.site_id == site_id, Connection.kind == 'ga4'))
        assert connection.capabilities['settings']['metrics'] == ['sessions', 'conversions']


def test_google_connection_test_verifies_read_only_access_without_persisting_provider_body(platform, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import workflows
    from app.connectors.security import encrypt_credentials
    from app.intelligence import visibility

    client, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Connection(
            site_id=site_id,
            kind='gsc',
            encrypted_credentials=encrypt_credentials(
                {'access_token': 'access-secret'}, settings.ENCRYPTION_KEY,
            ),
            status='needs_test',
            capabilities={'settings': {}},
        ))
        db.commit()

    calls = []

    async def fake_collect(kind, credentials, config):
        calls.append((kind, credentials, config))
        return {
            'status': 'ok',
            'metadata': {'token_refreshed': True},
            'data': {'provider_response_secret': 'must-not-persist'},
        }

    monkeypatch.setattr(visibility, 'collect', fake_collect)
    with factory() as db:
        site = db.get(Site, site_id)
        result = asyncio.run(workflows.connection_test(
            db, site, SimpleNamespace(payload={'kind': 'gsc'}),
        ))
        db.commit()
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id, Connection.kind == 'gsc',
        ))

        assert result == {
            'kind': 'gsc',
            'status': 'verified',
            'read_only': True,
            'message': 'gsc read-only access verified',
        }
        assert connection.status == 'connected'
        assert connection.capabilities['authenticated'] is True
        assert connection.capabilities['last_connection_test'] == {
            'status': 'verified',
            'kind': 'gsc',
            'tested_at': connection.capabilities['last_connection_test']['tested_at'],
            'read_only': True,
            'token_refreshed': True,
        }
        assert 'must-not-persist' not in json.dumps(connection.capabilities)
        assert 'access-secret' not in json.dumps(connection.capabilities)

    assert calls == [('gsc', {'access_token': 'access-secret'}, {'site_url': 'https://example.test'})]


def test_google_connection_test_records_safe_provider_failure(platform, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import workflows
    from app.connectors.security import encrypt_credentials
    from app.intelligence import visibility

    _, factory, site_id = platform
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind='ga4',
            encrypted_credentials=encrypt_credentials(
                {'access_token': 'access-secret'}, settings.ENCRYPTION_KEY,
            ),
            status='needs_test',
            capabilities={'settings': {'property_id': '123456789'}},
        ))
        db.commit()

    async def fake_collect(kind, credentials, config):
        return {
            'status': 'error',
            'error': {
                'code': 'remote_error',
                'message': 'provider response contained a secret-body-value',
            },
        }

    monkeypatch.setattr(visibility, 'collect', fake_collect)
    with factory() as db:
        site = db.get(Site, site_id)
        with pytest.raises(ValueError, match='ga4 connection test failed: remote_error'):
            asyncio.run(workflows.connection_test(
                db, site, SimpleNamespace(payload={'kind': 'ga4'}),
            ))
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id, Connection.kind == 'ga4',
        ))
        assert connection.status == 'error'
        assert connection.capabilities['last_connection_test']['error_code'] == 'remote_error'
        assert 'secret-body-value' not in json.dumps(connection.capabilities)
        assert 'provider response' not in json.dumps(connection.capabilities)


def test_openai_connection_test_records_read_only_model_capability(platform, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import workflows
    from app.connectors.security import encrypt_credentials
    from app.intelligence import visibility

    _, factory, site_id = platform
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind='ai',
            encrypted_credentials=encrypt_credentials(
                {'api_key': 'api-secret'}, settings.ENCRYPTION_KEY,
            ),
            status='needs_test',
            capabilities={
                'settings': {
                    'endpoint': 'https://api.openai.example/v1/responses',
                    'request_format': 'openai_responses_web_search',
                    'model': 'gpt-5.4-mini',
                },
            },
        ))
        db.commit()

    calls = []

    async def fake_verify(credentials, config):
        calls.append((credentials, config))
        return {
            'status': 'verified',
            'kind': 'ai',
            'provider': 'OpenAI',
            'model': 'gpt-5.4-mini',
            'model_available': True,
            'read_only': True,
            'provider_body': 'must-not-persist',
        }

    monkeypatch.setattr(visibility, 'verify_ai_connection', fake_verify)
    with factory() as db:
        site = db.get(Site, site_id)
        result = asyncio.run(workflows.connection_test(
            db, site, SimpleNamespace(payload={'kind': 'ai'}),
        ))
        db.commit()
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id, Connection.kind == 'ai',
        ))

        assert result == {
            'kind': 'ai',
            'status': 'verified',
            'provider': 'OpenAI',
            'model': 'gpt-5.4-mini',
            'model_available': True,
            'read_only': True,
            'message': 'OpenAI model access verified with a read-only check',
        }
        assert connection.status == 'connected'
        assert connection.capabilities['authenticated'] is True
        assert connection.capabilities['model_available'] is True
        assert connection.capabilities['last_connection_test']['status'] == 'verified'
        assert connection.capabilities['last_connection_test']['model'] == 'gpt-5.4-mini'
        assert 'must-not-persist' not in json.dumps(connection.capabilities)
        assert 'api-secret' not in json.dumps(connection.capabilities)

    assert calls == [(
        {'api_key': 'api-secret'},
        {
            'endpoint': 'https://api.openai.example/v1/responses',
            'request_format': 'openai_responses_web_search',
            'model': 'gpt-5.4-mini',
        },
    )]


def test_dataforseo_connection_test_rejects_blank_credentials_before_scheduling(platform, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import workflows

    _, factory, site_id = platform
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind='dataforseo',
            encrypted_credentials='encrypted-test-value',
            status='needs_test',
        ))
        db.commit()

    monkeypatch.setattr(
        workflows,
        'credentials',
        lambda *args, **kwargs: ({'login': '   ', 'password': 'provider-password'}, {}),
    )
    with factory() as db:
        site = db.get(Site, site_id)
        with pytest.raises(ValueError, match='login and password'):
            asyncio.run(workflows.connection_test(
                db,
                site,
                SimpleNamespace(payload={'kind': 'dataforseo'}),
            ))
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == 'dataforseo',
        ))
        assert connection.status == 'error'
        assert connection.capabilities['last_connection_test']['error_code'] == 'missing_credentials'
        assert connection.capabilities.get('credential_shape_verified') is not True


def test_partial_selection_and_finding_recurrence(platform):
    client,factory,site_id = platform
    with factory() as db:
        site = db.get(Site,site_id)
        page = Page(site_id=site_id,resource_key='posts:1',url='https://example.test/blog',source_hash='first')
        db.add(page)
        db.flush()
        db.add(Connection(
            site_id=site_id,
            kind='wordpress',
            encrypted_credentials='test',
            status='connected',
            capabilities={'seo': {'write': True, 'writable_fields': ['title', 'description']}},
        ))
        observation = {'signals':{},'findings':[{'key':'meta','code':'missing_meta','severity':'medium','title':'Missing metadata'}],
                       'candidates':[{'field':'seo_title','before_value':'','after_value':'Complete useful title'}, {'field':'meta_description','before_value':'','after_value':'A useful complete description.'}]}
        upsert_observation(db,site,page,observation)
        db.commit()
        rows = list(db.scalars(select(Candidate).where(Candidate.site_id == site_id)))
        candidate_id,sibling_id,page_id = rows[0].id,rows[1].id,page.id
    assert client.post(f'/api/v1/sites/{site_id}/candidates/{candidate_id}/execute').status_code == 409
    response = client.post(f'/api/v1/sites/{site_id}/candidates/{candidate_id}/decision',json={'decision':'approve'})
    assert response.status_code == 200,response.text
    with factory() as db:
        assert db.get(Candidate,sibling_id).status == 'pending'
        finding = db.scalar(select(Finding).where(Finding.site_id == site_id))
        assert finding.status == 'open'
        page,site = db.get(Page,page_id),db.get(Site,site_id)
        upsert_observation(db,site,page,{'signals':{},'findings':[]},complete=False)
        assert finding.status == 'open'
        upsert_observation(db,site,page,{'signals':{},'findings':[]},complete=True)
        assert finding.status == 'resolved'
        db.commit()
        upsert_observation(db,site,page,observation)
        assert finding.status == 'open'
        assert finding.recurrence_count == 1


def test_durable_queue_idempotency_and_delivery(platform,monkeypatch):
    client,factory,site_id = platform
    request = {'kind':'availability','idempotency_key':'same-request-123'}
    one = client.post(f'/api/v1/sites/{site_id}/jobs',json=request)
    two = client.post(f'/api/v1/sites/{site_id}/jobs',json=request)
    assert one.status_code == 202,one.text
    assert one.json()['id'] == two.json()['id']
    calls = []
    async def handler(db,site,job):
        calls.append(job.id)
        return {'complete':True}
    from app.workflows import HANDLERS
    monkeypatch.setitem(HANDLERS,'availability',handler)
    worker.run_job(one.json()['id'])
    worker.run_job(one.json()['id'])
    assert calls == [one.json()['id']]
    with factory() as db:
        assert db.get(Job,one.json()['id']).status == 'complete'


def test_source_and_browser_findings_resolve_only_from_matching_evidence(platform):
    _,factory,site_id=platform
    with factory() as db:
        site=db.get(Site,site_id)
        page=Page(site_id=site_id,resource_key='posts:7',url=site.origin+'/seven')
        db.add(page)
        db.flush()
        source={'signals':{'observation_type':'source_html','title':'Source'},'findings':[
            {'code':'missing_title','severity':'medium','title':'Source title missing'}]}
        browser={'signals':{'observation_type':'browser_rendered','title':'Rendered'},'findings':[
            {'key':'browser:missing_title','code':'missing_title','severity':'medium','title':'Browser title missing'}]}
        upsert_observation(db,site,page,source)
        upsert_observation(db,site,page,browser)
        db.flush()
        rows={row.key:row for row in db.scalars(select(Finding).where(Finding.page_id==page.id))}
        assert all(row.status=='open' for row in rows.values())
        assert page.signals['title']=='Source'
        assert page.signals['browser']['title']=='Rendered'
        upsert_observation(db,site,page,{'signals':{'observation_type':'source_html'},'findings':[]})
        assert rows['posts:7:missing_title'].status=='resolved'
        assert rows['posts:7:browser:missing_title'].status=='open'
        assert page.signals['browser']['title']=='Rendered'
        upsert_observation(db,site,page,{'signals':{'observation_type':'browser_rendered'},'findings':[]},complete=False)
        assert rows['posts:7:browser:missing_title'].status=='open'
        upsert_observation(db,site,page,{'signals':{'observation_type':'browser_rendered'},'findings':[]})
        assert rows['posts:7:browser:missing_title'].status=='resolved'


def test_observation_coalesces_repeated_page_findings_and_candidates(platform):
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        page = Page(site_id=site_id, resource_key='posts:8', url='https://example.test/repeated', source_hash='source')
        db.add(page)
        db.flush()
        observation = {
            'signals': {},
            'findings': [
                {'code': 'invalid_link', 'severity': 'warning', 'title': 'Link target is missing or invalid', 'details': {'index': 1}},
                {'code': 'invalid_link', 'severity': 'warning', 'title': 'Link target is missing or invalid', 'details': {'index': 2}},
            ],
            'candidates': [
                {'field': 'seo_title', 'before_value': '', 'after_value': 'A complete page title'},
                {'field': 'seo_title', 'before_value': '', 'after_value': 'A complete page title'},
            ],
        }
        upsert_observation(db, site, page, observation)
        # Production sessions intentionally use autoflush=False; a repeated
        # URL in one crawl must still reuse the pending durable rows.
        db.autoflush = False
        upsert_observation(db, site, page, observation)
        db.flush()
        findings = list(db.scalars(select(Finding).where(Finding.page_id == page.id)))
        candidates = list(db.scalars(select(Candidate).where(Candidate.page_id == page.id)))
        assert len(findings) == 1
        assert findings[0].details['occurrence_count'] == 2
        assert len(candidates) == 1
        db.commit()


def test_audit_continues_bounded_crawl_without_restarting_or_claiming_completion(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.models import Job, Site
    calls=[]
    async def fake_crawl(origin, max_pages=100, transport=None, seed_urls=None, visited_urls=None):
        calls.append({'seed_urls':seed_urls, 'visited_urls':visited_urls, 'max_pages':max_pages})
        if len(calls) == 1:
            return {'pages':[{'url':origin+'/', 'html':'<html><head><title>Home</title></head><body><h1>Home</h1></body></html>', 'status_code':200, 'error':None}],
                    'complete':False, 'pending_urls':[origin+'/next'], 'visited_urls':[origin+'/'], 'errors':[]}
        return {'pages':[{'url':origin+'/next', 'html':'<html><head><title>Next</title></head><body><h1>Next</h1></body></html>', 'status_code':200, 'error':None}],
                'complete':True, 'pending_urls':[], 'visited_urls':[origin+'/', origin+'/next'], 'errors':[]}
    monkeypatch.setattr('app.intelligence.audit.crawl', fake_crawl)
    _, factory, site_id = platform
    with factory() as db:
        site=db.get(Site,site_id)
        first=Job(id='audit-first',site_id=site_id,payload={'max_pages':1})
        result=asyncio.run(workflows.audit(db,site,first))
        db.commit()
        assert result['complete'] is False
        assert result['continuation_job_id']
        assert len(result['browser_job_ids']) == 1
        browser_job = db.get(Job, result['browser_job_ids'][0])
        assert browser_job.kind == 'browser'
        assert browser_job.payload['page_id']
        assert browser_job.payload['audit_job_id'] == 'audit-first'
        continuation=db.get(Job,result['continuation_job_id'])
        assert continuation.payload['seed_urls']==[site.origin+'/next']
        assert continuation.payload['visited_urls']==[site.origin+'/']
        second=Job(id='audit-second',site_id=site_id,payload=continuation.payload)
        done=asyncio.run(workflows.audit(db,site,second))
        db.commit()
        assert done['complete'] is True
        assert calls[0]['seed_urls'] is None
        assert calls[1]['seed_urls']==[site.origin+'/next']
        assert calls[1]['visited_urls']==[site.origin+'/']


def test_audit_deduplicates_browser_samples_for_duplicate_crawl_urls(platform, monkeypatch):
    import asyncio
    from app import workflows

    async def duplicate_crawl(origin, max_pages=100, transport=None, seed_urls=None, visited_urls=None):
        html = '<html><head><title>Home</title></head><body><h1>Home</h1></body></html>'
        return {
            'pages': [
                {'url': origin + '/', 'html': html, 'status_code': 200, 'error': None},
                {'url': origin + '/', 'html': html, 'status_code': 200, 'error': None},
            ],
            'complete': True,
            'pending_urls': [],
            'visited_urls': [origin + '/'],
            'errors': [],
        }

    monkeypatch.setattr('app.intelligence.audit.crawl', duplicate_crawl)
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        result = asyncio.run(workflows.audit(db, site, Job(id='audit-duplicate', site_id=site_id, payload={})))
        db.commit()
        assert result['complete'] is True
        assert len(result['browser_job_ids']) == 1
        assert len(set(result['browser_job_ids'])) == 1


def test_enabled_metadata_policy_authorizes_and_queues_safe_candidates(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.config import settings
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)

    async def crawl_once(origin, max_pages=100, transport=None, seed_urls=None, visited_urls=None):
        return {
            'pages': [{'url': origin + '/', 'html': '<html><head><title>Home</title></head><body><h1>Home</h1></body></html>', 'status_code': 200, 'error': None}],
            'complete': True,
            'pending_urls': [],
            'visited_urls': [origin + '/'],
            'errors': [],
        }

    monkeypatch.setattr('app.intelligence.audit.crawl', crawl_once)
    monkeypatch.setattr(
        'app.intelligence.audit.audit_page',
        lambda *args, **kwargs: {
            'signals': {'title': 'Home'},
            'findings': [],
            'candidates': [{
                'field': 'seo_title',
                'before_value': '',
                'after_value': 'A complete home page title',
                'details': {'reason': 'missing_seo_title'},
            }],
        },
    )

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        from app.policies import create_policy
        policy = create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['metadata'],
            'protected_paths': [],
        })
        db.add(Page(
            site_id=site_id,
            resource_key='posts:1',
            resource_type='posts',
            url=site.origin + '/',
            source_hash='existing-source',
        ))
        db.add(Connection(
            site_id=site_id,
            kind='wordpress',
            encrypted_credentials='test',
            status='connected',
            capabilities={'seo': {'write': True, 'writable_fields': ['title', 'description']}},
        ))
        db.commit()
        result = asyncio.run(workflows.audit(
            db,
            site,
            Job(id='audit-policy', site_id=site_id, payload={'visited_urls': []}),
        ))
        db.commit()
        candidate = db.scalar(select(Candidate).where(Candidate.site_id == site_id))
        assert result['complete'] is True
        assert candidate is not None
        assert candidate.status == 'approved'
        assert candidate.policy_version == policy.version
        assert candidate.details['authorization']['type'] == 'policy'
        queued = db.scalar(select(Job).where(Job.site_id == site_id, Job.kind == 'candidate'))
        assert queued is not None
        assert queued.payload == {'candidate_id': candidate.id}


def test_woocommerce_metadata_candidate_stays_review_only(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.policies import create_policy

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)

    async def crawl_once(origin, max_pages=100, transport=None, seed_urls=None, visited_urls=None):
        return {
            'pages': [{'url': origin + '/product', 'html': '<html><head><title>Product</title></head><body><main><h1>Product</h1></main></body></html>', 'status_code': 200, 'error': None}],
            'complete': True,
            'pending_urls': [],
            'visited_urls': [origin + '/product'],
            'errors': [],
        }

    monkeypatch.setattr('app.intelligence.audit.crawl', crawl_once)
    monkeypatch.setattr(
        'app.intelligence.audit.audit_page',
        lambda *args, **kwargs: {
            'signals': {'title': 'Product'},
            'findings': [],
            'candidates': [{
                'field': 'meta_description',
                'before_value': '',
                'after_value': 'A complete product description for customers.',
                'details': {'reason': 'missing_meta_description'},
            }],
        },
    )

    client, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['metadata'],
            'protected_paths': [],
        })
        db.add(Page(
            site_id=site_id,
            resource_key='products:1',
            resource_type='products',
            url=site.origin + '/product',
            source_hash='product-source',
        ))
        db.commit()
        result = asyncio.run(workflows.audit(db, site, Job(id='audit-woo-metadata', site_id=site_id, payload={})))
        db.commit()
        candidate = db.scalar(select(Candidate).where(Candidate.site_id == site_id))
        queued = db.scalar(select(Job).where(Job.site_id == site_id, Job.kind == 'candidate'))
        assert result['complete'] is True
        assert candidate is not None
        assert candidate.status == 'pending'
        assert candidate.details['execution_readiness']['execution_ready'] is False
        assert candidate.details['review_only_reasons'] == ['woocommerce_connection_required']
        assert queued is None

    response = client.post(
        f'/api/v1/sites/{site_id}/candidates/{candidate.id}/decision',
        json={'decision': 'approve'},
    )
    assert response.status_code == 409
    assert 'review-only' in response.json()['detail']


def test_woocommerce_product_metadata_readiness_requires_verified_connector(platform):
    from app.workflows import metadata_candidate_readiness

    _client, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        page = Page(
            site_id=site_id,
            resource_key='products:5',
            resource_type='products',
            url=site.origin + '/product',
            source_hash='product-source',
        )
        db.add(page)
        db.add(Connection(
            site_id=site_id,
            kind='woocommerce',
            encrypted_credentials='encrypted-test-credentials',
            status='connected',
            capabilities={
                'seo': {
                    'read': True,
                    'write': True,
                    'writable_fields': ['title', 'description'],
                    'resource_types': ['product'],
                },
            },
        ))
        db.flush()
        assert metadata_candidate_readiness(db, site, page, 'seo_title') is None
        assert metadata_candidate_readiness(db, site, page, 'meta_description') is None
        page.resource_type = 'product_categories'
        assert metadata_candidate_readiness(db, site, page, 'seo_title')['blockers'] == [
            'woocommerce_seo_writer_not_verified'
        ]
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == 'woocommerce',
        ))
        connection.capabilities['seo']['resource_types'] = ['product', 'category']
        assert metadata_candidate_readiness(db, site, page, 'meta_description') is None


def test_metadata_candidate_recovers_after_wordpress_writer_is_verified(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.policies import create_policy

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)

    async def crawl_once(origin, max_pages=100, transport=None, seed_urls=None, visited_urls=None):
        return {
            'pages': [{'url': origin + '/page', 'html': '<html><head><title>Page</title></head><body><main><h1>Page</h1><p>Complete repair information helps customers understand their options and prepare for a useful consultation.</p></main></body></html>', 'status_code': 200, 'error': None}],
            'complete': True,
            'pending_urls': [],
            'visited_urls': [origin + '/page'],
            'errors': [],
        }

    monkeypatch.setattr('app.intelligence.audit.crawl', crawl_once)
    monkeypatch.setattr(
        'app.intelligence.audit.audit_page',
        lambda *args, **kwargs: {
            'signals': {'title': 'Page'},
            'findings': [],
            'candidates': [{
                'field': 'meta_description',
                'before_value': '',
                'after_value': 'A complete repair description for customers.',
                'details': {'reason': 'missing_meta_description'},
            }],
        },
    )

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['metadata'],
            'protected_paths': [],
        })
        db.add(Page(
            site_id=site_id,
            resource_key='pages:1',
            resource_type='pages',
            url=site.origin + '/page',
            source_hash='page-source',
        ))
        db.commit()
        asyncio.run(workflows.audit(
            db,
            site,
            Job(id='audit-before-connection', site_id=site_id, payload={'visited_urls': []}),
        ))
        db.commit()
        candidate = db.scalar(select(Candidate).where(Candidate.site_id == site_id))
        assert candidate.status == 'pending'
        assert candidate.details['review_only_reasons'] == ['wordpress_connection_required']

        db.add(Connection(
            site_id=site_id,
            kind='wordpress',
            encrypted_credentials='test',
            status='connected',
            capabilities={'seo': {'write': True, 'writable_fields': ['title', 'description']}},
        ))
        db.commit()
        asyncio.run(workflows.audit(
            db,
            site,
            Job(id='audit-after-connection', site_id=site_id, payload={'visited_urls': []}),
        ))
        db.commit()
        db.refresh(candidate)
        assert candidate.details.get('review_only_reasons') is None
        assert candidate.status == 'approved'
        assert db.scalar(select(Job).where(Job.site_id == site_id, Job.kind == 'candidate')) is not None


def test_pagespeed_visibility_defaults_to_the_site_origin(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.config import settings
    from app.models import Measurement

    observed = {}

    async def collect(kind, credentials, config):
        observed.update({'kind': kind, 'credentials': credentials, 'config': config})
        return {
            'status': 'ok',
            'kind': 'pagespeed',
            'source': 'pagespeed',
            'observed_at': '2026-09-16T12:00:00+00:00',
            'data': {'lighthouseResult': {'categories': {}}},
            'cost_cents': 0,
            'cost_basis': 'estimated',
            'usage': None,
            'metadata': {},
        }

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'credentials', lambda *args, **kwargs: ({'api_key': 'page-speed-test'}, {}))
    monkeypatch.setattr('app.intelligence.visibility.collect', collect)

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Connection(site_id=site_id, kind='pagespeed', encrypted_credentials='test', status='configured'))
        db.commit()
        result = asyncio.run(workflows.visibility(
            db,
            site,
            Job(site_id=site_id, kind='visibility', payload={'kind': 'pagespeed'}),
        ))
        db.commit()
        measurement = db.scalar(select(Measurement).where(Measurement.site_id == site_id))
        assert result['status'] == 'ok'
        assert observed['kind'] == 'pagespeed'
        assert observed['config']['url'] == site.origin
        assert observed['credentials'] == {'api_key': 'page-speed-test'}
        assert measurement is not None
        assert measurement.kind == 'pagespeed'


def test_ai_visibility_persists_provenance_shape_and_provider_cost_metadata(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.config import settings
    from app.models import Measurement

    async def collect(kind, credentials, config):
        assert kind == 'ai_sample'
        assert credentials == {'api_key': 'ai-sample-test'}
        return {
            'status': 'ok',
            'kind': 'ai_sample',
            'source': 'ai_sample',
            'observed_at': '2026-09-17T12:00:00+00:00',
            'data': {
                'provider': 'Example AI',
                'model': 'example-model',
                'question': 'Who repairs windshields?',
                'locale': 'en-US',
                'answer': 'A provider answer',
                'citations': [{'url': 'https://source.example/repair'}],
                'ranking_type': 'ai_answer',
                'consumer_rankings': False,
            },
            'cost_cents': 3,
            'cost_basis': 'provider_actual',
            'usage': {'input_tokens': 10, 'output_tokens': 8},
            'metadata': {'provider': 'Example AI', 'model': 'example-model'},
        }

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'credentials', lambda *args, **kwargs: (
        {'api_key': 'ai-sample-test'},
        {
            'estimated_cost_cents': 5,
            'max_cost_cents': 10,
            'questions': ['Who repairs windshields?'],
        },
    ))
    monkeypatch.setattr('app.intelligence.visibility.collect', collect)

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Connection(site_id=site_id, kind='ai', encrypted_credentials='test', status='configured'))
        db.commit()
        result = asyncio.run(workflows.visibility(
            db,
            site,
            Job(id='ai-visibility-test', site_id=site_id, kind='visibility', payload={'kind': 'ai_sample'}),
        ))
        db.commit()
        measurement = db.scalar(select(Measurement).where(
            Measurement.site_id == site_id,
            Measurement.kind == 'ai_sample',
        ))
        assert result['status'] == 'ok'
        assert measurement is not None
        assert measurement.data['provider'] == 'Example AI'
        assert measurement.data['model'] == 'example-model'
        assert measurement.data['question'] == 'Who repairs windshields?'
        assert measurement.data['locale'] == 'en-US'
        assert measurement.data['answer'] == 'A provider answer'
        assert measurement.data['citations'][0]['url'] == 'https://source.example/repair'
        assert measurement.data['ranking_type'] == 'ai_answer'
        assert measurement.data['consumer_rankings'] is False
        assert measurement.data['_collection']['cost_basis'] == 'provider_actual'
        assert measurement.data['_collection']['observed_at'] == '2026-09-17T12:00:00+00:00'


def test_ai_visibility_rejects_empty_questions_before_budget_reservation(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.config import settings
    from app.models import CostReservation

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'credentials', lambda *args, **kwargs: (
        {'api_key': 'ai-sample-test'},
        {
            'request_format': 'openai_responses_web_search',
            'model': 'gpt-5.4-mini',
            'estimated_cost_cents': 5,
            'max_cost_cents': 10,
            'questions': [],
        },
    ))

    async def must_not_collect(*args, **kwargs):
        raise AssertionError('the provider must not be called without tracked questions')

    monkeypatch.setattr('app.intelligence.visibility.collect', must_not_collect)

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        with pytest.raises(ValueError, match='tracked questions'):
            asyncio.run(workflows.visibility(
                db,
                site,
                Job(id='ai-empty-questions', site_id=site_id, kind='visibility', payload={'kind': 'ai_sample'}),
            ))
        assert db.scalar(select(CostReservation).where(
            CostReservation.site_id == site_id,
        )) is None


@pytest.mark.parametrize(
    'kind,secret,config,error_text',
    [
        (
            'dataforseo',
            {'login': '   ', 'password': 'provider-password'},
            {'estimated_cost_cents': 5, 'max_cost_cents': 10},
            'DataForSEO',
        ),
        (
            'ai_sample',
            {},
            {
                'request_format': 'openai_responses_web_search',
                'model': 'gpt-5.4-mini',
                'estimated_cost_cents': 5,
                'max_cost_cents': 10,
                'questions': ['Which repair service should a driver call?'],
            },
            'OpenAI API key',
        ),
    ],
)
def test_paid_visibility_missing_credentials_stops_before_budget_reservation(
    platform,
    monkeypatch,
    kind,
    secret,
    config,
    error_text,
):
    import asyncio
    from app import workflows
    from app.models import CostReservation

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'credentials', lambda *args, **kwargs: (secret, config))

    async def must_not_collect(*args, **kwargs):
        raise AssertionError('the provider must not be called without paid credentials')

    monkeypatch.setattr('app.intelligence.visibility.collect', must_not_collect)

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        with pytest.raises(ValueError, match=error_text):
            asyncio.run(workflows.visibility(
                db,
                site,
                Job(
                    id=f'{kind}-missing-credentials',
                    site_id=site_id,
                    kind='visibility',
                    payload={'kind': kind},
                ),
            ))
        assert db.scalar(select(CostReservation).where(
            CostReservation.site_id == site_id,
        )) is None


def test_competitor_visibility_uses_policy_competitors_and_separate_paid_job_mode(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.config import settings
    from app.models import Measurement
    from app.policies import create_policy

    observed = {}

    async def collect(kind, credentials, config):
        observed.update({'kind': kind, 'credentials': credentials, 'config': config})
        return {
            'status': 'ok',
            'kind': 'competitor_observation',
            'source': 'dataforseo',
            'observed_at': '2026-09-18T12:00:00+00:00',
            'data': {
                'provider': 'DataForSEO',
                'observation_scope': 'competitor',
                'ranking_type': 'observed_competitor',
                'consumer_rankings': False,
                'target': 'example.test',
                'competitors': ['rival.example'],
            },
            'cost_cents': 4,
            'cost_basis': 'provider_actual',
            'usage': None,
            'metadata': {},
        }

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'credentials', lambda *args, **kwargs: (
        {'login': 'data-login', 'password': 'data-password'},
        {'location_code': 2840, 'estimated_cost_cents': 5, 'max_cost_cents': 10},
    ))
    monkeypatch.setattr('app.intelligence.visibility.collect', collect)

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        create_policy(db, site, None, {
            'enabled': False,
            'competitors': ['https://rival.example'],
        })
        db.add(Connection(site_id=site_id, kind='dataforseo', encrypted_credentials='test', status='configured'))
        db.commit()
        result = asyncio.run(workflows.visibility(
            db,
            site,
            Job(id='competitor-visibility-test', site_id=site_id, kind='visibility', payload={
                'kind': 'dataforseo',
                'mode': 'competitors',
            }),
        ))
        db.commit()
        measurement = db.scalar(select(Measurement).where(
            Measurement.site_id == site_id,
            Measurement.kind == 'competitor_observation',
        ))
        assert result['status'] == 'ok'
        assert observed['kind'] == 'dataforseo'
        assert observed['config']['mode'] == 'competitors'
        assert observed['config']['competitors'] == ['https://rival.example']
        assert observed['config']['location_code'] == 2840
        assert measurement is not None
        assert measurement.data['observation_scope'] == 'competitor'
        assert measurement.data['consumer_rankings'] is False


def test_generation_requires_complete_source_research_before_paid_provider(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.models import Article, Job, Site
    _, factory, site_id = platform
    provider_called=[]
    async def incomplete(brief, facts):
        return {'sources': [], 'research_notes': [{'kind':'source','status':'unavailable'}],
                'blockers':['source_unavailable'], 'complete':False}
    async def provider(*args, **kwargs):
        provider_called.append(True)
        return {'status':'generated'}
    monkeypatch.setattr('app.intelligence.research.research_brief', incomplete)
    monkeypatch.setattr('app.intelligence.content.generate_article', provider)
    with factory() as db:
        site=db.get(Site,site_id)
        site.facts={'business_name':'Independent test','services':['Repairs'],
                    'confirmed_sources':['https://source.example/repair']}
        article=Article(site_id=site_id,title='Repair guide',brief={'sources':['https://source.example/repair']},author_id='author-1')
        db.add(article)
        db.commit()
        with pytest.raises(ValueError,match='Research needs review'):
            asyncio.run(workflows.generate(db,site,Job(id='generation-test',payload={'article_id':article.id})))
        db.refresh(article)
        assert article.status=='review_needed'
        assert article.brief['research']['complete'] is False
        assert provider_called==[]


def test_refresh_creates_one_reviewable_enrolled_draft_without_touching_remote_page(platform):
    import asyncio
    from app import workflows
    from app.models import Article, Job, Page, Site
    from app.policies import create_policy
    _, factory, site_id = platform
    with factory() as db:
        site=db.get(Site,site_id)
        site.paused=False
        create_policy(db,site,None,{'enabled':True,'allowed_actions':['refresh'],'refreshes_per_week':1})
        page=Page(site_id=site_id,resource_key='posts:42',resource_type='posts',url=site.origin+'/enrolled',
                  title='Existing repair guide',source_hash='source-before',enrolled=True,managed=False,
                  source={'resource_type':'posts','title':'Existing repair guide','body':'<p>Existing body stays unchanged.</p>',
                          'slug':'enrolled','author':'9'})
        db.add(page)
        db.commit()
        result=asyncio.run(workflows.refresh(db,site,Job(id='refresh-one',payload={})))
        db.commit()
        assert result['evaluated']==1
        assert len(result['article_ids'])==1
        article=db.get(Article,result['article_ids'][0])
        assert article.status=='review_needed'
        assert article.managed is False
        assert article.brief['purpose']=='refresh_existing'
        assert article.brief['source_hash']=='source-before'
        assert page.source['body']=='<p>Existing body stays unchanged.</p>'
        second=asyncio.run(workflows.refresh(db,site,Job(id='refresh-two',payload={})))
        db.commit()
        assert second['article_ids']==[]
        assert db.query(Article).filter(Article.site_id==site_id).count()==1


def test_content_plan_uses_recent_search_and_competitor_measurements_as_bounded_evidence(platform):
    import asyncio
    from app import workflows
    from app.models import Article, Job, Site

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        db.add(Measurement(
            site_id=site_id,
            kind='gsc',
            source='gsc',
            data={
                'rows': [
                    {'keys': ['windshield repair Houston', 'https://example.test/repairs'], 'clicks': 4},
                    {'keys': ['https://example.test/ignored-page'], 'clicks': 1},
                ],
            },
            observed_at=now(),
        ))
        db.add(Measurement(
            site_id=site_id,
            kind='competitor_observation',
            source='dataforseo',
            data={
                'provider': 'DataForSEO',
                'target': 'example.test',
                'competitors': ['rival.example'],
                'consumer_rankings': False,
            },
            observed_at=now(),
        ))
        db.commit()

        result = asyncio.run(workflows.plan(db, site, Job(id='measurement-backed-plan', payload={})))
        db.commit()
        articles = db.scalars(select(Article).where(Article.site_id == site_id)).all()
        article = next(item for item in articles if item.title == 'windshield repair Houston guide')

        assert result['count'] == len(result['article_ids'])
        assert article.brief['evidence'][0]['kind'] == 'search_observation'
        assert article.brief['evidence'][0]['query'] == 'windshield repair Houston'
        competitor_context = [
            item for item in article.brief['research_evidence']
            if item.get('kind') == 'competitor_observation'
        ]
        assert competitor_context[0]['competitor_url'] == 'https://rival.example/'
        assert 'ignored-page' not in json.dumps(article.brief)


def test_refresh_skips_duplicate_page_before_using_weekly_slot(platform):
    import asyncio
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from app import workflows
    from app.models import Article, Job, Page, Site
    from app.policies import create_policy

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {'enabled': True, 'allowed_actions': ['refresh'], 'refreshes_per_week': 1})
        first = Page(
            site_id=site_id,
            resource_key='posts:100',
            resource_type='posts',
            url=site.origin + '/already-evaluated',
            title='Already evaluated',
            source_hash='source-first',
            enrolled=True,
            source={'resource_type': 'posts', 'title': 'Already evaluated', 'body': 'First'},
            last_seen_at=now() - timedelta(days=2),
        )
        second = Page(
            site_id=site_id,
            resource_key='posts:101',
            resource_type='posts',
            url=site.origin + '/next-enrolled',
            title='Next enrolled page',
            source_hash='source-second',
            enrolled=True,
            source={'resource_type': 'posts', 'title': 'Next enrolled page', 'body': 'Second'},
            last_seen_at=now() - timedelta(days=1),
        )
        db.add_all([first, second])
        db.flush()
        local_now = now().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(site.timezone))
        week_key = (local_now - timedelta(days=local_now.weekday())).date().isoformat()
        db.add(Article(
            site_id=site_id,
            title='Already evaluated refresh',
            status='review_needed',
            brief={'refresh_key': f'{first.id}:{week_key}'},
        ))
        db.commit()

        result = asyncio.run(workflows.refresh(db, site, Job(id='refresh-skip-duplicate', payload={})))
        db.commit()

        assert result['evaluated'] == 2
        assert len(result['article_ids']) == 1
        created = db.get(Article, result['article_ids'][0])
        assert created.brief['refresh_of_page_id'] == second.id


def test_enrolled_refresh_applies_in_place_and_can_roll_back(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.config import settings
    from app.models import Article, Job, Page, Site
    from app.policies import create_policy

    class RefreshClient:
        def __init__(self):
            self.before = {
                'resource_key': 'posts:42', 'resource_type': 'posts', 'id': '42',
                'url': 'https://example.test/enrolled', 'title': 'Existing repair guide',
                'body': '<div><p>Original repair guidance.</p></div>', 'status': 'publish',
                'metadata': {}, 'source_hash': 'source-before', 'raw': {},
            }
            self.current = dict(self.before)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self, resource_key):
            assert resource_key == self.current['resource_key']
            return dict(self.current)

        async def update(self, resource_key, changes, expected_hash, *, operation_key=None):
            assert expected_hash == self.current['source_hash']
            self.current = {**self.current, **changes, 'source_hash': 'source-after'}
            return dict(self.current)

        async def restore(self, resource_key, snapshot, expected_hash=None, *, operation_key=None):
            assert expected_hash == self.current['source_hash']
            self.current = dict(snapshot)
            return dict(self.current)

    remote = RefreshClient()

    async def fixture_client(db, site, kind='wordpress'):
        return remote

    async def public_fetch(url):
        html = (
            '<html><head><meta name="robots" content="index,follow">'
            '<link rel="canonical" href="https://example.test/enrolled">'
            '<style>.content{color:black}</style></head><body><main class="content">'
            f'<h1>{remote.current["title"]}</h1>{remote.current["body"]}'
            '</main></body></html>'
        )
        return {'status_code': 200, 'html': html, 'url': url, 'headers': {'content-type': 'text/html'}}

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'client_for', fixture_client)
    monkeypatch.setattr(workflows, 'fetch', public_fetch)
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        site.facts = {'business_name': 'Independent test', 'services': ['Repairs']}
        create_policy(db, site, None, {'enabled': True, 'allowed_actions': ['refresh'], 'refreshes_per_week': 1})
        page = Page(
            site_id=site_id, resource_key='posts:42', resource_type='posts',
            url='https://example.test/enrolled', title='Existing repair guide',
            source_hash='source-before', enrolled=True,
            source={**remote.before},
        )
        db.add(page)
        db.flush()
        article = Article(
            site_id=site_id, title='Updated repair guide',
            body='<div><p>Updated repair guidance with useful preparation steps.</p></div>',
            author_id='9', status='checked',
            brief={'purpose': 'refresh_existing', 'refresh_of_page_id': page.id,
                   'source_hash': 'source-before',
                   'sources': [{'url': page.url}],
                   'research': {'complete': True, 'sources': [{'url': page.url}], 'research_notes': []},
                   'generation': {'kind': 'reviewed_refresh', 'source': 'editor', 'approval_required': True}},
            sources=[{'url': page.url, 'status': 'fetched', 'content_hash': 'e' * 64}],
            checks={'passed': True, 'blockers': [], 'warnings': []}, managed=False,
        )
        db.add(article)
        db.commit()
        result = asyncio.run(workflows.publish(db, site, Job(id='refresh-apply', site_id=site_id,
                                                              payload={'article_id': article.id})))
        db.commit()
        assert result['status'] == 'refreshed'
        assert article.status == 'refreshed'
        assert page.source_hash == 'source-after'
        assert remote.current['body'] == article.body
        rollback = asyncio.run(workflows.rollback(db, site, Job(id='refresh-rollback', site_id=site_id,
                                                                  payload={'article_id': article.id})))
        db.commit()
        assert rollback['status'] == 'rolled_back'
        assert article.status == 'rolled_back'
        assert remote.current['source_hash'] == 'source-before'


def test_publish_lease_expiry_requires_reconciliation(platform,monkeypatch):
    from app import scheduler
    client,factory,site_id = platform
    monkeypatch.setattr(scheduler,'SessionLocal',factory)
    with factory() as db:
        row = Job(site_id=site_id,kind='publish',status='running',payload={},idempotency_key='expired-publish',
                  attempts=1,available_at=now()-timedelta(hours=1),lease_until=now()-timedelta(minutes=1))
        db.add(row)
        db.commit()
        job_id = row.id
    scheduler.schedule()
    with factory() as db:
        assert db.get(Job,job_id).status == 'needs_reconciliation'
        heartbeat = db.get(Heartbeat,'scheduler')
        assert heartbeat is not None
        assert heartbeat.details['queue_delay_seconds'] == 0
        assert heartbeat.details['missed_checks'] == 0


def test_global_pause_blocks_write_before_connection(platform):
    import asyncio
    from app.workflows import publish
    from app.models import Article
    client,factory,site_id = platform
    with factory() as db:
        article = Article(site_id=site_id,title='Complete useful title')
        db.add(article)
        db.flush()
        with pytest.raises(ValueError,match='paused|pause'):
            asyncio.run(publish(db,db.get(Site,site_id),Job(payload={'article_id':article.id})))


def test_connection_partial_update_preserves_secret(platform):
    from app.models import Connection
    from app.connectors.security import decrypt_credentials
    client,factory,site_id=platform
    url=f'/api/v1/sites/{site_id}/connections/wordpress'
    assert client.put(url,json={'credentials':{'username':'before','application_password':'keep-this'},'settings':{}}).status_code==200
    assert client.put(url,json={'credentials':{'username':'after','application_password':''},'settings':{}}).status_code==200
    with factory() as db:
        connection=db.scalar(select(Connection).where(Connection.site_id==site_id))
        actual=decrypt_credentials(connection.encrypted_credentials,settings.ENCRYPTION_KEY)
        assert actual=={'username':'after','application_password':'keep-this'}


def test_revocation_blocks_connector_work_and_settings_cannot_reactivate_it(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.models import Job

    client, factory, site_id = platform
    url = f'/api/v1/sites/{site_id}/connections/wordpress'
    secret = 'revoked-application-password-must-not-escape'
    assert client.put(url, json={
        'credentials': {'username': 'tester', 'application_password': secret},
        'settings': {},
    }).status_code == 200
    revoked = client.delete(url)
    assert revoked.status_code == 200
    assert secret not in revoked.text

    # Updating non-secret settings after revocation must not create an
    # unauthenticated connector row.  A new credential is required to restore
    # the connection.
    settings_only = client.put(url, json={
        'credentials': {},
        'settings': {'provider': 'native-wordpress'},
    })
    assert settings_only.status_code == 200, settings_only.text
    assert settings_only.json()['status'] == 'revoked'
    assert secret not in settings_only.text

    connector_calls = []

    async def forbidden_client_for(*args, **kwargs):
        connector_calls.append(True)
        raise AssertionError('revoked credentials must not construct a connector')

    monkeypatch.setattr(workflows, 'client_for', forbidden_client_for)
    with factory() as db:
        site = db.get(Site, site_id)
        with pytest.raises(ValueError, match='Connection is not configured'):
            asyncio.run(workflows.connection_test(
                db, site, Job(site_id=site_id, kind='connection_test', payload={'kind': 'wordpress'})
            ))
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id, Connection.kind == 'wordpress'
        ))
        assert connection.status == 'revoked'
        assert connection.encrypted_credentials == ''
    assert connector_calls == []
    assert secret not in client.get(f'/api/v1/sites/{site_id}/connections').text


def test_stale_candidate_is_paused_without_remote_write_and_remains_recorded(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.models import Job, Publication
    from app.policies import create_policy

    _, factory, site_id = platform
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    remote_reads = []
    remote_writes = []

    class StaleClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self, resource_key):
            remote_reads.append(resource_key)
            return {
                'resource_key': resource_key,
                'source_hash': 'remote-newer-source',
                'metadata': {'seo': {'forgeseo': {'title': ''}}},
            }

        async def update(self, *args, **kwargs):
            remote_writes.append(True)
            raise AssertionError('stale evidence must stop before update')

    async def fake_client_for(*args, **kwargs):
        return StaleClient()

    monkeypatch.setattr(workflows, 'client_for', fake_client_for)
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['metadata'],
            'protected_paths': [],
        })
        page = Page(
            site_id=site_id,
            resource_key='posts:stale-candidate',
            resource_type='posts',
            url=site.origin + '/stale-candidate',
            title='Candidate page',
            source_hash='evidence-at-draft-time',
        )
        db.add(Connection(
            site_id=site_id,
            kind='wordpress',
            encrypted_credentials='test',
            status='connected',
            capabilities={'seo': {'write': True, 'writable_fields': ['title', 'description']}},
        ))
        candidate = Candidate(
            site_id=site_id,
            page_id=page.id,
            field='seo_title',
            before_value='',
            after_value='A complete candidate title',
            source_hash='evidence-at-draft-time',
            status='approved',
        )
        db.add(page)
        db.flush()
        candidate.page_id = page.id
        db.add(candidate)
        db.commit()

        with pytest.raises(ValueError, match='Source changed since this candidate was generated'):
            asyncio.run(workflows.candidate(
                db, site, Job(site_id=site_id, kind='candidate', payload={'candidate_id': candidate.id})
            ))
        db.refresh(candidate)
        publication = db.scalar(select(Publication).where(Publication.candidate_id == candidate.id))
        assert candidate.status == 'stale'
        assert publication is not None
        assert publication.status == 'failed'
        assert publication.result['reason'] == 'source_conflict'
        assert remote_reads == [page.resource_key]
        assert remote_writes == []


def test_stale_refresh_source_pauses_and_preserves_article_for_review(platform, monkeypatch):
    import asyncio
    from app import workflows
    from app.models import Article, Incident, Job, Publication
    from app.policies import create_policy

    _, factory, site_id = platform
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    remote_writes = []

    class StaleRefreshClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self, resource_key):
            return {
                'resource_key': resource_key,
                'resource_type': 'posts',
                'id': '42',
                'title': 'Existing repair guide',
                'body': '<p>External edit remains authoritative.</p>',
                'source_hash': 'remote-newer-source',
                'status': 'publish',
            }

        async def update(self, *args, **kwargs):
            remote_writes.append(True)
            raise AssertionError('stale refresh evidence must stop before update')

    async def fake_client_for(*args, **kwargs):
        return StaleRefreshClient()

    monkeypatch.setattr(workflows, 'client_for', fake_client_for)
    monkeypatch.setattr(
        'app.intelligence.content.check_article',
        lambda *args, **kwargs: {'passed': True, 'blockers': [], 'warnings': []},
    )
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['refresh'],
            'protected_paths': [],
        })
        page = Page(
            site_id=site_id,
            resource_key='posts:42',
            resource_type='posts',
            url=site.origin + '/enrolled',
            title='Existing repair guide',
            source_hash='evidence-at-draft-time',
            enrolled=True,
            source={'title': 'Existing repair guide', 'body': '<p>Original body.</p>', 'source_hash': 'evidence-at-draft-time'},
        )
        db.add(page)
        db.flush()
        article = Article(
            site_id=site_id,
            title='Updated repair guide',
            body='<p>Updated repair guidance with useful preparation steps.</p>',
            author_id='42',
            status='checked',
            brief={
                'purpose': 'refresh_existing',
                'refresh_of_page_id': page.id,
                'source_hash': 'evidence-at-draft-time',
                'sources': [{'url': page.url}],
            },
            checks={'passed': True, 'blockers': [], 'warnings': []},
        )
        db.add(article)
        db.commit()
        original_body = article.body

        with pytest.raises(ValueError, match='Source changed since this refresh was evaluated'):
            asyncio.run(workflows.publish(
                db, site, Job(site_id=site_id, kind='publish', payload={'article_id': article.id})
            ))
        db.refresh(article)
        publication = db.scalar(select(Publication).where(Publication.article_id == article.id))
        conflict = db.scalar(select(Incident).where(Incident.key == f'refresh_conflict:{article.id}'))
        assert article.status == 'review_needed'
        assert article.body == original_body
        assert publication is not None
        assert publication.status == 'failed'
        assert publication.result['status'] == 'source_conflict'
        assert article.brief['source_hash'] == 'evidence-at-draft-time'
        assert page.source_hash == 'evidence-at-draft-time'
        assert conflict is not None and conflict.status == 'open'
        assert remote_writes == []


def test_signed_notifications_reject_forgery_and_debounce(platform):
    import hashlib,hmac,json
    from datetime import datetime,timezone
    from app.models import Publication
    client,factory,site_id=platform
    secret='random-webhook-secret-long-enough-for-testing'
    client.put(f'/api/v1/sites/{site_id}/connections/wordpress',json={'credentials':{'webhook_secret':secret},'settings':{}})
    url=f'/api/v1/webhooks/wordpress/{site_id}'
    payload={'event':'post.changed','occurred_at':datetime.now(timezone.utc).isoformat(),'data':{'id':9,'resource_type':'posts'}}
    body=json.dumps(payload).encode()
    assert client.post(url,content=body,headers={'X-ForgeSEO-Signature':'bad'}).status_code==401
    signature=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    one=client.post(url,content=body,headers={'X-ForgeSEO-Signature':signature})
    two=client.post(url,content=body,headers={'X-ForgeSEO-Signature':signature})
    assert one.status_code==202,one.text
    assert one.json()['job_id']==two.json()['job_id']
    with factory() as db:
        job=db.get(Job,one.json()['job_id'])
        assert job.kind=='targeted_audit'
        assert job.payload=={'resource_key':'posts:9'}
        assert db.get(Site,site_id).paused is True
        db.add(Publication(
            site_id=site_id,
            operation_key='refresh:site:article',
            policy_version=1,
            status='published',
        ))
        db.commit()
    payload['data']['operation_key']='refresh:site:article'
    body=json.dumps(payload).encode()
    signature=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    ignored=client.post(url,content=body,headers={'X-ForgeSEO-Signature':signature})
    assert ignored.status_code==202
    assert ignored.json()=={'status':'ignored','reason':'platform_write_already_has_verification'}
    payload['occurred_at']='2000-01-01T00:00:00+00:00'
    body=json.dumps(payload).encode()
    signature=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    assert client.post(url,content=body,headers={'X-ForgeSEO-Signature':signature}).status_code==422


def test_signed_notifications_do_not_reveal_site_existence(platform):
    import json
    from datetime import datetime,timezone
    client,factory,site_id=platform
    payload={'event':'post.changed','occurred_at':datetime.now(timezone.utc).isoformat(),
             'data':{'id':9,'resource_type':'posts'}}
    body=json.dumps(payload).encode()
    forged=client.post(
        f'/api/v1/webhooks/wordpress/{site_id}',
        content=body,
        headers={'X-ForgeSEO-Signature':'bad'},
    )
    unknown=client.post(
        '/api/v1/webhooks/wordpress/site-that-does-not-exist',
        content=body,
        headers={'X-ForgeSEO-Signature':'bad'},
    )
    assert forged.status_code == unknown.status_code == 401
    assert forged.json() == unknown.json() == {'detail':'Notification authentication failed'}


def test_estimated_cost_stays_reserved_until_actual_reconciliation(platform):
    from app.budgets import reserve
    from app.models import BudgetAccount,CostReservation
    from app.workflows import reconcile_provider_cost
    client,factory,site_id=platform
    with factory() as db:
        reservation=reserve(db,site_id,'paid-operation-1',75,30000)
        reservation_id=reservation.id
        outcome=reconcile_provider_cost(db,db.get(Site,site_id),reservation,{'cost_basis':'estimated','cost_cents':15})
        db.commit()
        assert outcome=='reserved_pending_actual_cost'
        account=db.get(BudgetAccount,reservation.account_id)
        assert account.spent_cents==0
        assert account.reserved_cents==75
    response=client.post(f'/api/v1/sites/{site_id}/budgets/reservations/{reservation_id}/settle',json={'actual_cents':17,'evidence':'Provider invoice test-001 line 4'})
    assert response.status_code==200,response.text
    with factory() as db:
        reservation=db.get(CostReservation,reservation_id)
        account=db.get(BudgetAccount,reservation.account_id)
        assert reservation.actual_cents==17
        assert account.spent_cents==17
        assert account.reserved_cents==0
    assert client.get(f'/api/v1/sites/{site_id}/budgets').status_code==200


def test_measurement_import_preserves_provider_provenance_and_timestamp(platform):
    client, factory, site_id = platform
    response = client.post(
        f'/api/v1/sites/{site_id}/measurements/import',
        json={'items': [
            {
                'kind': 'ai_sample',
                'source': 'provider-export',
                'observed_at': '2026-09-15T12:30:00-05:00',
                'data': {
                    'provider': 'Example AI',
                    'model': 'example-model',
                    'question': 'Who repairs windshields?',
                    'locale': 'en-US',
                    'answer': 'A provider answer',
                    'citations': [{'url': 'https://source.example/repair'}],
                },
            },
            {'kind': 'referral_traffic', 'source': 'ga4-export', 'data': {'sessions': 4}},
        ]},
    )
    assert response.status_code == 201, response.text
    assert response.json() == {'imported': 2}
    with factory() as db:
        rows = db.scalars(select(Measurement).where(Measurement.site_id == site_id).order_by(Measurement.created_at)).all()
        assert [row.kind for row in rows] == ['ai_sample', 'referral']
        assert rows[0].source == 'provider-export'
        assert rows[0].observed_at.isoformat() == '2026-09-15T17:30:00'
        assert rows[0].data['model'] == 'example-model'
        assert rows[0].data['citations'][0]['url'] == 'https://source.example/repair'
        assert rows[0].data['_import']['observed_at_basis'] == 'provided'
        assert rows[1].data['_import']['observed_at_basis'] == 'imported_at'


def test_measurement_import_rejects_nested_credentials_without_persisting_and_accepts_observation(platform):
    client, factory, site_id = platform
    rejected = client.post(
        f'/api/v1/sites/{site_id}/measurements/import',
        json={'items': [{
            'kind': 'backlink_observation',
            'source': 'provider-export',
            'data': {
                'source_url': 'https://referrer.example/article',
                'target_url': 'https://example.test/services',
                'credentials': {'api_key': 'nested-secret-fixture'},
            },
        }]},
    )
    assert rejected.status_code == 422, rejected.text
    assert 'credentials' in rejected.json()['detail']
    with factory() as db:
        assert db.scalar(select(Measurement).where(Measurement.site_id == site_id)) is None

    imported = client.post(
        f'/api/v1/sites/{site_id}/measurements/import',
        json={'items': [{
            'kind': 'backlink_observation',
            'source': 'provider-export',
            'data': {
                'source_url': 'https://referrer.example/article',
                'target_url': 'https://example.test/services',
                'anchor_text': 'auto repair services',
            },
        }]},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json() == {'imported': 1}
    with factory() as db:
        row = db.scalar(select(Measurement).where(Measurement.site_id == site_id))
        assert row is not None
        assert row.kind == 'backlink_observation'
        assert row.source == 'provider-export'
        assert row.data['source_url'] == 'https://referrer.example/article'
        assert row.data['target_url'] == 'https://example.test/services'
        assert row.data['anchor_text'] == 'auto repair services'
        assert row.data['consumer_rankings'] is False


def test_measurement_import_persists_scoped_competitor_observation(platform):
    client, factory, site_id = platform
    response = client.post(
        f'/api/v1/sites/{site_id}/measurements/import',
        json={'items': [{
            'kind': 'competitor_observation',
            'source': 'serp-provider-export',
            'observed_at': '2026-09-17T12:00:00Z',
            'data': {
                'competitor_url': 'https://competitor.example/',
                'query': 'auto glass repair houston',
                'position': 4,
                'provider': 'Example SERP provider',
            },
        }]},
    )
    assert response.status_code == 201, response.text
    with factory() as db:
        row = db.scalar(select(Measurement).where(
            Measurement.site_id == site_id,
            Measurement.kind == 'competitor_observation',
        ))
        assert row is not None
        assert row.data['observation_scope'] == 'competitor'
        assert row.data['ranking_type'] == 'observed_competitor'
        assert row.data['position'] == 4
        assert row.data['consumer_rankings'] is False


def test_measurement_views_redact_nested_credential_shaped_values(platform):
    client, factory, site_id = platform
    with factory() as db:
        db.add(Measurement(
            site_id=site_id,
            kind='ai_sample',
            source='provider-fixture',
            data={
                'answer': 'A useful observed answer',
                'provider': {'model': 'fixture-model', 'access_token': 'must-not-send'},
                'metadata': {'api_key': 'must-not-send-either', 'locale': 'en-US'},
            },
        ))
        db.commit()

    measurements = client.get(f'/api/v1/sites/{site_id}/measurements')
    weekly = client.get(f'/api/v1/sites/{site_id}/reports/weekly')
    assert measurements.status_code == weekly.status_code == 200
    for response in (measurements, weekly):
        encoded = json.dumps(response.json())
        assert 'must-not-send' not in encoded
        assert 'A useful observed answer' in encoded
        assert '[redacted]' in encoded

    item = measurements.json()['items'][0]
    assert item['data']['provider']['access_token'] == '[redacted]'
    assert item['data']['metadata']['locale'] == 'en-US'


def test_browser_views_redact_nested_connector_values_and_preserve_useful_fields(platform):
    from app.models import Publication

    client, factory, site_id = platform
    secrets = {
        'page-access-token-fixture',
        'page-api-key-fixture',
        'article-client-secret-fixture',
        'article-webhook-secret-fixture',
        'publication-consumer-secret-fixture',
        'publication-bearer-token-fixture',
    }
    with factory() as db:
        page = Page(
            site_id=site_id,
            resource_key='posts:browser-safe',
            url='https://example.test/browser-safe',
            title='Visible page title',
            source={
                'remote_title': 'Useful remote title',
                'connector': {'access_token': 'page-access-token-fixture'},
            },
            signals={
                'h1': 'Visible heading',
                'headers': [{'apiKey': 'page-api-key-fixture', 'content_type': 'text/html'}],
            },
        )
        article = Article(
            site_id=site_id,
            title='Visible article title',
            brief={
                'intent': 'Explain the repair process',
                'research': {'client_secret': 'article-client-secret-fixture'},
            },
            sources=[
                {'url': 'https://source.example/repair', 'webhook_secret': 'article-webhook-secret-fixture'},
                {'title': 'Useful source note'},
            ],
        )
        db.add_all([page, article])
        db.flush()
        publication = Publication(
            site_id=site_id,
            article_id=article.id,
            operation_key='browser-safe-publication',
            policy_version=1,
            snapshot={
                'article_title': 'Visible article title',
                'connector': {'consumer_secret': 'publication-consumer-secret-fixture'},
            },
            result={
                'status': 'verified',
                'remote': {'bearer_token': 'publication-bearer-token-fixture'},
            },
        )
        db.add(publication)
        db.commit()
        page_id, article_id = page.id, article.id

    responses = [
        client.get(f'/api/v1/sites/{site_id}/pages'),
        client.patch(f'/api/v1/sites/{site_id}/pages/{page_id}', json={'enrolled': True}),
        client.get(f'/api/v1/sites/{site_id}/articles/{article_id}'),
        client.get(f'/api/v1/sites/{site_id}/publications'),
        client.get(f'/api/v1/sites/{site_id}/reports/weekly'),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        encoded = json.dumps(response.json())
        for secret in secrets:
            assert secret not in encoded

    page_item = responses[0].json()['items'][0]
    assert page_item['title'] == 'Visible page title'
    assert page_item['source']['remote_title'] == 'Useful remote title'
    assert page_item['source']['connector']['access_token'] == '[redacted]'
    assert page_item['signals']['h1'] == 'Visible heading'
    assert page_item['signals']['headers'][0]['content_type'] == 'text/html'
    assert page_item['signals']['headers'][0]['apiKey'] == '[redacted]'

    article_item = responses[2].json()
    assert article_item['title'] == 'Visible article title'
    assert article_item['brief']['intent'] == 'Explain the repair process'
    assert article_item['brief']['research']['client_secret'] == '[redacted]'
    assert article_item['sources'][0]['url'] == 'https://source.example/repair'
    assert article_item['sources'][0]['webhook_secret'] == '[redacted]'
    assert article_item['sources'][1]['title'] == 'Useful source note'

    publication_item = responses[3].json()['items'][0]
    assert publication_item['snapshot']['article_title'] == 'Visible article title'
    assert publication_item['snapshot']['connector']['consumer_secret'] == '[redacted]'
    assert publication_item['result']['status'] == 'verified'
    assert publication_item['result']['remote']['bearer_token'] == '[redacted]'


def test_evidence_download_checks_site_access_and_integrity(platform):
    from pathlib import Path
    from app.workflows import capture_html
    client,factory,site_id=platform
    with factory() as db:
        site=db.get(Site,site_id)
        evidence=capture_html(site,Job(id='evidence-test'),site.origin,'<h1>Captured evidence</h1>')
        page=Page(site_id=site_id,resource_key='posts:99',url=site.origin+'/evidence',signals={'evidence':evidence})
        db.add(page)
        db.commit()
        page_id=page.id
    url=f'/api/v1/sites/{site_id}/pages/{page_id}/evidence'
    response=client.get(url)
    assert response.status_code==200
    assert response.text=='<h1>Captured evidence</h1>'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert client.get(f'/api/v1/sites/not-mine/pages/{page_id}/evidence').status_code==404
    (Path(settings.ARTIFACT_ROOT)/evidence['artifact']).write_text('tampered fixture data')
    assert client.get(url).status_code==409
