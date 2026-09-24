"""Human source review is audited, revision-bound, tenant-scoped and not publishing."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import network
from app.models import Article, Event, Membership, Site
from app.operations import iso, now
from test_platform import platform

SOURCE = 'https://example.test/contact'


def draft(platform):
    client, factory, site_id = platform
    with factory() as db:
        article = Article(site_id=site_id, title='Preparing for a workshop estimate',
                          body='<p>Contact the workshop to confirm its intake process.</p>',
                          sources=[{'url': SOURCE}], author_id='1', status='review_needed',
                          brief={'sources': [SOURCE], 'generation': {
                              'kind': 'provider_generation', 'usage': {'input_tokens': 120},
                              'unverified_sources': [{'url': SOURCE}]}})
        db.add(article)
        db.commit()
        return f'/api/v1/sites/{site_id}/articles/{article.id}'


def payload(client, path, **overrides):
    return {'url': SOURCE, 'notes': 'The contact page supports the link purpose, not an assurance of photo estimates.',
            'confirms_claim_support': True,
            'expected_updated_at': client.get(path).json()['updated_at'], **overrides}


@pytest.fixture
def source_fetch(monkeypatch):
    calls = []
    async def fetch(url):
        calls.append(url)
        return {'url': url, 'status_code': 200, 'html': '<h1>Contact</h1><p>Ask about intake.</p>',
                'headers': {'content-type': 'text/html; charset=utf-8'}}
    monkeypatch.setattr(network, 'fetch', fetch)
    return calls


def test_verified_review_clears_only_source_blocker_preserving_history(platform, source_fetch):
    client, factory, site_id = platform
    path = draft(platform)
    original = client.get(path).json()
    assert 'unverified_sources' in client.post(path+'/check').json()['blockers']
    response = client.post(path+'/source-reviews', json=payload(client, path))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['checks']['passed']
    assert result['brief']['generation'] == original['brief']['generation']
    review = result['brief']['source_reviews'][0]
    assert review['reviewer_id'] and review['reviewed_at'] and review['content_sha256']
    assert review['evidence']['phase'] == 'source_review'
    assert result['remote_id'] is None and result['scheduled_at'] is None
    assert source_fetch == [SOURCE]
    with factory() as db:
        assert db.get(Site, site_id).paused
        assert db.scalar(select(Event).where(Event.kind == 'article_source_reviewed'))


@pytest.mark.parametrize('field,value', [('body', '<p>New unsupported intake promise.</p>'),
                                       ('title', 'A different complete repair title'),
                                       ('sources', [SOURCE, 'https://example.test/another'])])
def test_edit_invalidates_but_does_not_erase_review(platform, source_fetch, field, value):
    client, _, _ = platform
    path = draft(platform)
    client.post(path+'/source-reviews', json=payload(client, path)).raise_for_status()
    review = client.get(path).json()['brief']['source_reviews']
    assert client.patch(path, json={field: value}).status_code == 200
    assert 'unverified_sources' in client.post(path+'/check').json()['blockers']
    assert client.get(path).json()['brief']['source_reviews'] == review


@pytest.mark.parametrize('change', ['forge', 'erase', 'replace'])
def test_browser_cannot_write_attestations_through_brief(platform, source_fetch, change):
    client, _, site_id = platform
    path = draft(platform)
    before = client.get(path).json()['brief']
    if change != 'forge':
        client.post(path+'/source-reviews', json=payload(client, path)).raise_for_status()
        before = client.get(path).json()['brief']
    proposed = deepcopy(before)
    proposed['source_reviews'] = [{'kind': 'authenticated_source_review', 'reviewer_id': 'forged'}]
    if change == 'erase':
        proposed.pop('source_reviews')
    response = client.patch(path, json={'brief': proposed})
    assert response.status_code == 200
    assert response.json()['brief'].get('source_reviews') == before.get('source_reviews')
    created = client.post(f'/api/v1/sites/{site_id}/articles', json={'title':'Another complete title', 'brief':proposed})
    assert created.status_code == (201 if change == 'erase' else 422)


@pytest.mark.parametrize('status,content_type', [(404, 'text/html'), (200, 'application/pdf'), (500, 'text/html')])
def test_unavailable_or_non_html_source_does_not_clear_blocker(platform, monkeypatch, status, content_type):
    client, _, _ = platform
    path = draft(platform)
    async def fetch(url):
        return {'url':url,'status_code':status,'html':'not evidence','headers':{'content-type':content_type}}
    monkeypatch.setattr(network, 'fetch', fetch)
    assert client.post(path+'/source-reviews', json=payload(client, path)).status_code == 422
    assert not client.get(path).json()['brief'].get('source_reviews')


def test_fetch_failure_is_safe_and_keeps_flag(platform, monkeypatch):
    client, _, _ = platform
    path = draft(platform)
    async def fail(url): raise ValueError('provider-private-secret')
    monkeypatch.setattr(network, 'fetch', fail)
    response = client.post(path+'/source-reviews', json=payload(client, path))
    assert response.status_code == 422 and 'provider-private-secret' not in response.text
    assert 'unverified_sources' in client.post(path+'/check').json()['blockers']


def test_stale_edit_rejected_before_fetch(platform, source_fetch):
    client, _, _ = platform
    path = draft(platform)
    request = payload(client, path)
    client.patch(path, json={'body':'<p>A new version.</p>'})
    assert client.post(path+'/source-reviews', json=request).status_code == 409
    assert source_fetch == []


def test_edit_during_source_fetch_rejected(platform, monkeypatch):
    client, factory, _ = platform
    path = draft(platform)
    async def fetch(url):
        with factory() as db:
            article = db.get(Article, path.rsplit('/', 1)[1])
            article.body = '<p>A concurrent edit.</p>'
            article.updated_at = now()
            db.commit()
        return {'url':url,'status_code':200,'html':'<h1>Contact</h1>','headers':{'content-type':'text/html'}}
    monkeypatch.setattr(network, 'fetch', fetch)
    assert client.post(path+'/source-reviews', json=payload(client, path)).status_code == 409
    assert not client.get(path).json()['brief'].get('source_reviews')


@pytest.mark.parametrize('state', ['scheduled', 'publishing', 'verifying', 'published'])
def test_no_review_during_publication(platform, source_fetch, state):
    client, factory, _ = platform
    path = draft(platform)
    with factory() as db:
        db.get(Article, path.rsplit('/', 1)[1]).status = state
        db.commit()
    assert client.post(path+'/source-reviews', json=payload(client, path)).status_code == 409
    assert not source_fetch


@pytest.mark.parametrize('overrides', [{'confirms_claim_support':False}, {'notes':'unchecked'},
                                      {'url':'https://unrelated.example/claim'}, {'url':'http://127.0.0.1/admin'}])
def test_confirmation_and_article_source_scope_required(platform, source_fetch, overrides):
    client, _, _ = platform
    path = draft(platform)
    assert client.post(path+'/source-reviews', json=payload(client, path, **overrides)).status_code == 422
    assert not source_fetch


def test_missing_author_is_not_waived_by_source_review(platform, source_fetch):
    client, _, _ = platform
    path = draft(platform)
    client.patch(path, json={'author_id':None})
    result = client.post(path+'/source-reviews', json=payload(client, path)).json()
    assert result['checks']['blockers'] == ['missing_author']


def test_old_review_expires_and_other_site_cannot_review(platform, source_fetch):
    client, factory, site_id = platform
    path = draft(platform)
    original = client.post(path+'/source-reviews', json=payload(client, path)).json()
    with factory() as db:
        article = db.get(Article, original['id'])
        brief = deepcopy(article.brief)
        brief['source_reviews'][0]['reviewed_at'] = iso(now()-timedelta(days=8))
        article.brief = brief
        db.commit()
    assert 'unverified_sources' in client.post(path+'/check').json()['blockers']
    assert client.post(path.replace(site_id,'another-site')+'/source-reviews', json=payload(client,path)).status_code == 404


def test_viewer_cannot_record_source_review(platform, source_fetch):
    client, factory, _ = platform
    path = draft(platform)
    request = payload(client, path)
    with factory() as db:
        membership = db.scalar(select(Membership))
        membership.role = 'viewer'
        db.commit()
    assert client.post(path+'/source-reviews',json=request).status_code == 403
    assert source_fetch == []
