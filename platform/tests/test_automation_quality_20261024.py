"""Regressions for the observed unattended pilot failures."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.intelligence.audit import audit_page
from app.intelligence.content import generate_article
from app.models import Connection, Job, Measurement, Site
from app.workflows import _content_autopilot_topic_blockers
from test_platform import platform


@pytest.mark.parametrize('sources, expected', [
    (['https://www.example.test/?tracking=1#top'], False),
    (['https://[', None, {'url': []}, 'not-a-url'], False),
    (['javascript:alert(1)', 'https://user:secret@example.test/services/'], False),
    ([{'url': 'https://example.test/services/repairs/'}], True),
    (['https://independent.test/research'], True),
])
def test_topic_source_shape_rejects_homepage_aliases_and_malformed_entries(sources, expected):
    from app.workflows import _content_autopilot_has_topic_sources
    assert _content_autopilot_has_topic_sources(
        SimpleNamespace(origin='https://example.test'), SimpleNamespace(sources=sources)) is expected


@pytest.mark.parametrize('sources', [[], ['https://example.test/']])
def test_content_autopilot_requires_topic_sources_before_paid_generation(platform, monkeypatch, sources):
    from app import workflows
    from app.config import settings
    from app.models import Article
    from test_content_autopilot import _ready_site, _ready_credentials, _add_planned_article, _job_result
    client, factory, site_id = platform
    _ready_site(client, factory, site_id)
    _ready_credentials(monkeypatch)
    article_id = _add_planned_article(factory, site_id)
    with factory() as db:
        site = db.get(Site, site_id)
        article = db.get(Article, article_id)
        article.sources = [site.origin + '/'] if sources else []
        db.commit()
    async def forbidden(*args):
        pytest.fail('insufficient research must not reach paid generation')
    monkeypatch.setattr(workflows, 'generate', forbidden)
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    _, result = _job_result(client, site_id)
    assert result['status'] == 'needs_review'
    assert result['blockers'] == ['topic_specific_research_required']


def test_google_collection_failure_updates_connection_readiness(platform, monkeypatch):
    import asyncio
    from app import workflows
    client, factory, site_id = platform
    monkeypatch.setattr(workflows, 'credentials', lambda *args: ({}, {}))
    async def collect(*args):
        return {'status': 'error', 'error': {'code': 'oauth_refresh_failed'}}
    monkeypatch.setattr('app.intelligence.visibility.collect', collect)
    with factory() as db:
        site = db.get(Site, site_id)
        row = Connection(site_id=site_id, kind='gsc', encrypted_credentials='fixture', status='connected',
                         capabilities={'settings': {'site_url': site.origin}, 'last_connection_test': {'status': 'verified'}})
        db.add(row)
        db.commit()
        with pytest.raises(ValueError, match='oauth_refresh_failed'):
            asyncio.run(workflows.visibility(db, site, Job(site_id=site_id, kind='visibility', payload={'kind': 'gsc'})))
        db.refresh(row)
        assert row.status == 'error'
        assert row.capabilities['last_connection_test']['error_code'] == 'oauth_refresh_failed'
        assert row.capabilities['settings']['site_url'] == site.origin


def test_local_services_action_is_available_but_does_not_claim_google_writes(platform):
    client, factory, site_id = platform
    response = client.get(f'/api/v1/sites/{site_id}/local-presence/services')
    assert response.status_code == 200
    assert response.json()['remote_write_supported'] is False
    assert response.json()['status'] == 'needs_business_facts'
    assert client.get('/api/v1/sites/not-mine/local-presence/services').status_code == 404
    from app.local_presence import service_plan
    plan = service_plan(SimpleNamespace(id='x', origin='https://example.test', facts={
        'services': ['Repair'], 'confirmed_sources': ['https://[', 'https://example.test/services']}))
    assert plan['status'] == 'needs_profile_access'
    assert plan['sources'] == ['https://example.test/services']


def test_service_review_deduplicates_without_mutating_saved_business_facts():
    from app.local_presence import service_plan
    facts = {'services': [' Bumper  repair ', 'bumper repair', 'CAR PAINTING', '', None],
             'confirmed_sources': ['https://example.test/services/', 'https://example.test/services/',
                                   'https://unrelated.test/services/', 'javascript:alert(1)']}
    plan = service_plan(SimpleNamespace(id='x', origin='https://example.test', facts=facts))
    assert plan['services'] == ['Bumper repair', 'CAR PAINTING']
    assert plan['sources'] == ['https://example.test/services/']
    assert facts['services'][0] == ' Bumper  repair '
    assert len(facts['confirmed_sources']) == 4


def test_tracking_pixels_do_not_become_alt_text_work():
    result = audit_page('https://example.test/', '<html><title>Example</title><body>'
                        '<img src="https://www.facebook.com/tr?id=1&ev=PageView">'
                        '<img src="/tracker.gif" width="1" height="1">'
                        '<img src="/real-pixel-art.png"><img src="/photo.jpg"></body></html>')
    issues = [x for x in result['findings'] if x['code'] == 'missing_image_alt']
    assert len(issues) == 2


def test_visibility_filter_runs_before_pagination_and_keeps_site_boundary(platform):
    client, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        other = Site(team_id=site.team_id, name='Other', origin='https://other.test', timezone='UTC')
        db.add(other)
        db.flush()
        db.add_all([Measurement(site_id=site_id, kind='gsc', source='gsc', data={'clicks': 3}),
                    Measurement(site_id=other.id, kind='gsc', source='gsc', data={'private': True})])
        db.flush()
        db.add_all([Measurement(site_id=site_id, kind='availability', source='http_probe', data={}) for _ in range(205)])
        db.commit()
    data = client.get(f'/api/v1/sites/{site_id}/measurements?source=visibility&limit=1').json()
    assert data['total'] == 1
    assert data['items'][0]['data'] == {'clicks': 3}
    assert client.get(f'/api/v1/sites/{site_id}/measurements?source=gsc').json()['total'] == 1
    assert client.get('/api/v1/sites/not-mine/measurements?source=visibility').status_code == 404


@pytest.mark.parametrize('title', ['24 hour body shop near me guide', '24/7 repair', 'emergency body repair', 'auto accident attorney guide'])
def test_unverified_service_promises_are_not_automatic_topics(title):
    assert _content_autopilot_topic_blockers(SimpleNamespace(facts={}), SimpleNamespace(title=title, brief={}))


@pytest.mark.asyncio
async def test_generation_repairs_only_safe_leading_h1_without_another_paid_call():
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'title': 'Prepare for an estimate',
                                        'body': '<h1>Prepare for an estimate</h1><p>Keep your photos.</p>', 'sources': []})
    result = await generate_article({'title': 'Prepare for an estimate'}, {},
                                    {'provider': 'test', 'model': 'test', 'endpoint': 'https://provider.example/draft',
                                     'estimated_cost_cents': 5, 'max_cost_cents': 10}, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result['body'].startswith('<h2>')
    assert result['provenance']['format_repairs'] == ['leading_h1_demoted_to_h2']
    assert result['publishable'] is False
    assert 'no h1' in calls[0]['messages'][0]['content']
