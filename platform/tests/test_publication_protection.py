"""Publication policy must include the intended and WordPress-resolved target."""
import asyncio

import pytest
from sqlalchemy import select

from app import workflows
from app.config import settings
from app.models import Article, Job, Publication, Site
from app.policies import create_policy
from test_platform import platform


def seed(factory, site_id, protected):
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        site.facts = {'business_name': 'Independent Workshop', 'services': ['Repairs'],
                      'confirmed_sources': [{'url': 'https://example.test/about', 'title': 'Fixture facts'}]}
        create_policy(db, site, None, {'enabled': True, 'allowed_actions': ['publish'],
                                      'author_id': '1', 'protected_paths': protected})
        article = Article(site_id=site_id, title='Prepare for a repair visit', slug='repair-guide',
                          body='<p>Independent Workshop provides Repairs.</p>', author_id='1',
                          sources=site.facts['confirmed_sources'],
                          brief={'generation': {'kind': 'fixture_authored'}})
        db.add(article)
        db.commit()
        return article.id


def test_protected_intended_path_blocks_even_draft_creation(platform, monkeypatch):
    _, factory, site_id = platform
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    article_id = seed(factory, site_id, ['/repair-guide*'])

    async def forbidden(*args):
        pytest.fail('Protected target must be rejected before opening the connector')

    monkeypatch.setattr(workflows, 'client_for', forbidden)
    with factory() as db, pytest.raises(ValueError, match='protected_path'):
        asyncio.run(workflows.publish(db, db.get(Site, site_id), Job(payload={'article_id': article_id})))


@pytest.mark.parametrize('target', [
    {'permalink_template': 'https://example.test/blog/%postname%/', 'generated_slug': 'repair-guide'},
    {'permalink_template': 'https://example.test/blog/repair-guide/'},
])
def test_resolved_protected_path_never_becomes_public(platform, monkeypatch, target):
    _, factory, site_id = platform
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    article_id = seed(factory, site_id, ['/blog*'])
    writes = []
    draft = {'id': '9', 'resource_key': 'posts:9', 'status': 'draft', 'source_hash': 'draft-hash',
             'url': 'https://example.test/?p=9', 'raw': target}

    class Remote:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create_draft(self, *args):
            writes.append('create_draft')
            return draft
        async def read(self, *args): return draft
        def matches_snapshot(self, *args, **kwargs): return True
        async def publish(self, *args, **kwargs):
            pytest.fail('A draft in a protected path must never be published')
        async def restore(self, *args, **kwargs):
            writes.append('restore_draft')
            return draft

    async def connector(*args): return Remote()
    monkeypatch.setattr(workflows, 'client_for', connector)
    with factory() as db:
        with pytest.raises(ValueError, match='protected_path'):
            asyncio.run(workflows.publish(db, db.get(Site, site_id), Job(payload={'article_id': article_id})))
        pub = db.scalar(select(Publication).where(Publication.article_id == article_id))
        assert pub.remote_id == '9'
        assert pub.status != 'published'
        assert 'create_draft' in writes


@pytest.mark.parametrize('source', [
    {'url': ''},
    {'url': 'https://unrelated.example/repair-guide'},
    {'url': 'https://user:pass@example.test/repair-guide'},
    {'url': 'https://example.test/repair-guide#unexpected'},
    {'raw': {'permalink_template': 'https://example.test/%postname%/'}},
    {'raw': {'permalink_template': 'https://example.test/%unknown%/'}},
])
def test_unresolved_or_foreign_permalink_fails_closed(source):
    site = Site(origin='https://example.test')
    article = Article(slug='repair-guide', managed=True)
    with pytest.raises(ValueError):
        workflows.publication_target(site, article, source)


def test_remote_generated_slug_and_base_path_are_preserved():
    target = workflows.publication_target(Site(origin='https://example.test'), Article(managed=True), {
        'url': 'https://example.test/?p=9',
        'raw': {'permalink_template': 'https://example.test/blog/%postname%/',
                'generated_slug': 'repair-guide-2'},
    })
    assert target == {'url': 'https://example.test/blog/repair-guide-2/', 'enrolled': True}


def test_unmanaged_article_requires_enrollment(platform, monkeypatch):
    _, factory, site_id = platform
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    article_id = seed(factory, site_id, [])
    with factory() as db:
        db.get(Article, article_id).managed = False
        db.commit()
        with pytest.raises(ValueError, match='page_not_enrolled'):
            asyncio.run(workflows.publish(db, db.get(Site, site_id), Job(payload={'article_id': article_id})))
