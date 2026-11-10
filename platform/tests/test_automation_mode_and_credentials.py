"""Observed Auto1Stop certification plans and lost audit continuation controls."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import workflows
from app.config import settings
from app.intelligence.content import business_topic_matches, plan_topics
from app.models import Candidate, Job, Page, Site
from test_full_cycle import _prepare_metadata_fixture
from test_platform import platform


FACTS = {'business_name': 'Auto1StopShop', 'services': ['Collision repair', 'Auto body repair'],
         'locations': ['Houston']}


@pytest.mark.parametrize('topic', [
    'atascocita ram certified body shop', 'atascocita jeep certified collision center',
    'OEM-approved collision repair', 'factory approved body shop', 'licensed repair shop',
])
def test_credential_discovery_queries_require_review_before_automatic_planning(topic):
    assert not business_topic_matches(topic, FACTS)
    reasons = workflows._content_autopilot_topic_blockers(
        SimpleNamespace(facts=FACTS), SimpleNamespace(title=topic, brief={}))
    assert reasons == ['credential_topic_requires_editorial_review']


def test_planner_keeps_service_topics_without_turning_searches_into_credentials():
    inputs = [{'kind': 'search_observation', 'source': 'gsc', 'query': topic} for topic in
              ['atascocita ram certified body shop', 'atascocita jeep certified collision center',
               'collision repair inspection questions']]
    plans = plan_topics(FACTS, [], research_inputs=inputs, automatic=True)
    assert any('inspection questions' in plan['title'] for plan in plans)
    assert not any('certified' in plan['title'].lower() for plan in plans)


def _two_batches(monkeypatch):
    calls = []
    async def crawl(origin, **kwargs):
        calls.append(kwargs)
        complete = len(calls) > 1
        return {'pages': [], 'complete': complete, 'errors': [],
                'pending_urls': [] if complete else [origin + '/next'],
                'visited_urls': [origin + '/']}
    monkeypatch.setattr('app.intelligence.audit.crawl', crawl)


def _first_batch(factory, site_id, payload):
    with factory() as db:
        site = db.get(Site, site_id)
        job = Job(site_id=site_id, kind='audit', payload=payload, idempotency_key='mode-first-batch')
        db.add(job)
        db.commit()
        result = asyncio.run(workflows.audit(db, site, job))
        db.commit()
        assert result['complete'] is False
        return result['continuation_job_id']


@pytest.mark.parametrize('governed', [False, True])
def test_read_only_continuation_cannot_authorize_a_writable_candidate(platform, monkeypatch, governed):
    client, factory, site_id = platform
    safe_id, _ = _prepare_metadata_fixture(client, factory, site_id)
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    _two_batches(monkeypatch)
    controls = {'skip_inventory': True, 'suppress_automation': True}
    if governed:
        controls['full_cycle_mode'] = 'governed'
    continuation_id = _first_batch(factory, site_id, controls)
    with factory() as db:
        child = db.get(Job, continuation_id)
        result = asyncio.run(workflows.audit(db, db.get(Site, site_id), child))
        db.commit()
        assert result['complete'] is True
        assert db.get(Candidate, safe_id).status == 'pending'
        assert db.scalars(select(Job).where(Job.site_id == site_id, Job.kind == 'candidate')).all() == []


def test_governed_continuation_keeps_metadata_field_restriction(platform, monkeypatch):
    client, factory, site_id = platform
    _prepare_metadata_fixture(client, factory, site_id)
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    _two_batches(monkeypatch)
    child_id = _first_batch(factory, site_id, {
        'skip_inventory': True, 'suppress_automation': False, 'full_cycle_mode': 'governed',
    })
    with factory() as db:
        page = Page(site_id=site_id, resource_key='posts:88', resource_type='posts',
                    url='https://example.test/another-post', source_hash='another-source', source={})
        db.add(page)
        db.flush()
        candidate = Candidate(site_id=site_id, page_id=page.id, field='image_alt',
                              before_value='', after_value='Vehicle inspection photo', source_hash=page.source_hash)
        db.add(candidate)
        db.commit()
        child = db.get(Job, child_id)
        result = asyncio.run(workflows.audit(db, db.get(Site, site_id), child))
        db.commit()
        assert result['complete'] is True
        assert 'metadata_execution' in result
        assert candidate.status == 'pending'
        gated = next(item for item in result['metadata_execution']['review_gated']
                     if item['candidate_id'] == candidate.id)
        assert 'unsupported_metadata_field' in gated['reasons']
        assert not any(j.payload.get('candidate_id') == candidate.id for j in db.scalars(
            select(Job).where(Job.site_id == site_id, Job.kind == 'candidate')))
