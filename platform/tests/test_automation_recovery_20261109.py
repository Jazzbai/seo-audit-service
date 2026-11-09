"""Regressions from the October 30 / November 6 unattended failures."""
from types import SimpleNamespace

import httpx
import pytest

from app.intelligence.content import business_topic_matches, generate_article, plan_topics
from app.intelligence.research import research_brief, research_needs_refresh
from app.workflows import _content_autopilot_topic_blockers
from test_platform import platform


FACTS = {'business_name': 'Auto1StopShop', 'locations': ['Houston, TX'],
         'services': ['Collision and auto body repair', 'Car painting', 'Bumper repair', 'Scratch repair'],
         'confirmed_sources': ['https://example.test/', 'https://example.test/services/collision-body/',
                               'https://example.test/services/car-painting/', 'https://example.test/services/bumper/',
                               'https://example.test/services/scratch/']}


@pytest.mark.parametrize('topic, expected', [
    ('1stopmarketing', False), ('1stop auto repair', False), ('a1 collision', False),
    ('body shops houston', True), ('bumper repair houston', True), ('scratch repair options', True),
    ('auto repair near me', False), ('real estate Houston', False),
])
def test_search_topic_needs_evidence_of_business_fit(topic, expected):
    assert business_topic_matches(topic, FACTS) is expected
    brief = {'topic': topic, 'evidence': [{'kind': 'search_observation'}]}
    blockers = _content_autopilot_topic_blockers(SimpleNamespace(facts=FACTS), SimpleNamespace(title=topic, brief=brief))
    assert bool(blockers) is not expected


def test_planned_research_uses_relevant_subset_not_every_business_page():
    briefs = plan_topics(FACTS, [], origin='https://example.test')
    bumper = next(b for b in briefs if b['topic'] == 'Bumper repair')
    assert [s['url'] for s in bumper['sources']] == ['https://example.test/services/bumper/']
    assert bumper['source_urls'] == ['https://example.test/services/bumper/']
    assert len(FACTS['confirmed_sources']) == 5


def test_automatic_plan_filters_before_its_cap_without_starving_real_services():
    briefs = plan_topics(FACTS, [], keywords=['1stopmarketing', 'a1 collision', '1stop auto repair'],
                        origin='https://example.test', automatic=True)
    assert all('1stop' not in b['title'] and 'a1 collision' not in b['title'] for b in briefs)
    assert any(b['topic'] == 'Bumper repair' for b in briefs)


@pytest.mark.asyncio
async def test_requested_contact_link_is_fetched_before_it_can_be_a_draft_source():
    requests = []
    async def handler(request):
        requests.append(str(request.url))
        if request.url.host == 'provider.example':
            return httpx.Response(200, json={'title': 'Scratch options', 'body': '<p>Ask for an inspection.</p>',
                                           'sources': ['https://example.test/contact/']})
        return httpx.Response(200, text='<main><p>Contact the shop for an inspection.</p></main>',
                              headers={'content-type': 'text/html'})
    brief = {'sources': ['https://example.test/scratch/'], 'internal_links': [{'url': 'https://example.test/contact/'}]}
    research = await research_brief(brief, {}, transport=httpx.MockTransport(handler))
    assert research['complete'] is True
    assert requests == ['https://example.test/scratch/', 'https://example.test/contact/']
    assert all(s['content_hash'] and s['authority'] == 'untrusted' for s in research['sources'])
    brief['research'] = research
    assert research_needs_refresh(brief) is False
    generated = await generate_article(brief, {}, {'provider': 'test', 'model': 'test',
        'endpoint': 'https://provider.example/draft', 'estimated_cost_cents': 1, 'max_cost_cents': 2},
        transport=httpx.MockTransport(handler))
    assert generated['provenance']['unverified_sources'] == []
    assert generated['publishable'] is False


@pytest.mark.asyncio
async def test_internal_link_does_not_bypass_private_network_or_required_source_failure():
    requested = []
    async def handler(request):
        requested.append(str(request.url))
        return httpx.Response(404, text='not found')
    brief = {'sources': ['https://example.test/scratch/'], 'internal_links': [{'url': 'http://127.0.0.1/admin'}]}
    result = await research_brief(brief, {}, transport=httpx.MockTransport(handler))
    assert result['complete'] is False
    assert 'private_url' in result['blockers']
    assert 'source_unavailable' in result['blockers']
    assert requested == ['https://example.test/scratch/']


def test_old_cached_research_must_cover_a_requested_navigation_link():
    brief = {'research': {'complete': True, 'sources': []}, 'internal_links': [{'url': 'https://example.test/contact/'}]}
    assert research_needs_refresh(brief) is True


def test_outcome_health_is_site_scoped_and_clears_recovered_failures(platform):
    from datetime import datetime, timedelta, timezone
    from app.models import Connection, Job, Site
    client, factory, site_id = platform
    instant = datetime.now(timezone.utc)
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        other = Site(team_id=site.team_id, name='Other', origin='https://other.example', timezone='UTC')
        db.add(other)
        db.flush()
        connection = Connection(site_id=site_id, kind='gsc', status='error', encrypted_credentials='opaque')
        db.add(connection)
        db.add(Connection(site_id=other.id, kind='ga4', status='error', encrypted_credentials='opaque'))
        db.add(Job(site_id=site_id, kind='content_autopilot', status='partial', idempotency_key='old-autopilot',
                   result={'blockers': ['research_review_required', 'secret=should-not-leak']}, created_at=instant))
        db.commit()
        data = client.get(f'/api/v1/sites/{site_id}/overview').json()['automation_health']
        assert [issue['code'] for issue in data['issues']] == ['google_connection_failed', 'content_autopilot_needs_attention']
        assert 'ga4' not in str(data)
        assert 'should-not-leak' not in str(data)
        connection.status = 'connected'
        db.add(Job(site_id=site_id, kind='content_autopilot', status='complete', idempotency_key='new-autopilot',
                   result={'status': 'published'}, created_at=instant + timedelta(seconds=1)))
        db.commit()
    data = client.get(f'/api/v1/sites/{site_id}/overview').json()['automation_health']
    assert data['status'] == 'no_recorded_blockers'
    assert data['issues'] == []
    assert client.get('/api/v1/sites/not-mine/overview').status_code == 404
