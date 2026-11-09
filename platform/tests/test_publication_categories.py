import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import workflows
from app.connectors.errors import ConnectorError
from app.connectors.wordpress import WordPressClient
from app.models import Article, Incident, Job, Site
from app.policies import _validated_settings, create_policy
from test_platform import platform


def policy(**updates):
    return SimpleNamespace(settings=_validated_settings({
        'publication_category_ids': [62, 68], 'publication_category_root_id': 61,
        'publication_default_category_id': 68, **updates,
    }))


def test_category_policy_uses_explicit_default_and_rejects_other_branches():
    assert workflows.publication_categories(policy(), SimpleNamespace(brief={})) == [68]
    assert workflows.publication_categories(policy(), SimpleNamespace(brief={'categories': [62]})) == [62]
    for ids in ([47], [76], [68, 49], [True], [], '68'):
        with pytest.raises(ValueError):
            workflows.publication_categories(policy(), SimpleNamespace(brief={'categories': ids}))
    assert workflows.publication_categories(SimpleNamespace(settings={}), SimpleNamespace(brief={})) is None


@pytest.mark.parametrize('updates', [
    {'publication_category_ids': [True]}, {'publication_category_ids': [-1]},
    {'publication_default_category_id': 49}, {'publication_category_root_id': True},
    {'publication_category_ids': None, 'publication_default_category_id': 68},
])
def test_invalid_category_controls_are_rejected(updates):
    with pytest.raises(ValueError):
        policy(**updates)


@pytest.mark.parametrize('parents,category,valid', [
    ({68: 61}, 68, True), ({68: 62, 62: 61}, 68, True),
    ({49: 47, 47: 0}, 49, False), ({79: 76, 76: 0}, 79, False),
    ({61: 0}, 61, False), ({68: 62, 62: 68}, 68, False),
])
def test_current_wordpress_ancestry_controls_publication(monkeypatch, parents, category, valid):
    client = object.__new__(WordPressClient)
    calls = []

    async def ready(**kwargs):
        return {}

    async def request(method, path):
        assert method == 'GET'
        category_id = int(path.rsplit('/', 1)[-1])
        calls.append(category_id)
        return {'id': category_id, 'parent': parents[category_id]}, None

    monkeypatch.setattr(client, '_ensure_ready', ready)
    monkeypatch.setattr(client, '_wp_path', lambda path: path)
    monkeypatch.setattr(client, '_json_request', request)
    if valid:
        asyncio.run(client.verify_post_categories([category], 61))
    else:
        with pytest.raises(ConnectorError):
            asyncio.run(client.verify_post_categories([category], 61))
    assert calls


def test_remote_draft_categories_and_changed_policy_cannot_bypass_guard():
    class Client:
        async def verify_post_categories(self, ids, root):
            assert ids == [68] and root == 61

    for raw in ({'categories': [49]}, {'categories': [68, 76]}, {}, {'categories': ['68']}):
        with pytest.raises(ValueError):
            asyncio.run(workflows.verify_publication_categories(Client(), policy(), [68], {'raw': raw}))
    asyncio.run(workflows.verify_publication_categories(Client(), policy(), [68], {'raw': {'categories': [68]}}))
    with pytest.raises(ValueError):
        asyncio.run(workflows.verify_publication_categories(Client(), policy(), None))
    with pytest.raises(ValueError):
        asyncio.run(workflows.verify_publication_categories(Client(), policy(publication_category_ids=[62], publication_default_category_id=62), [68]))


def test_reconciliation_does_not_adopt_a_post_in_the_wrong_category():
    article = {'title': 'Visit checklist', 'body': '<p>Checklist</p>', 'categories': [68]}
    record = {'title': article['title'], 'body': article['body'], 'raw': {'categories': [49]}}
    assert not WordPressClient._record_matches_create(record, article)
    record['raw']['categories'] = [68]
    assert WordPressClient._record_matches_create(record, article)


def test_first_failed_availability_probe_is_recorded_instead_of_typeerror(platform, monkeypatch):
    _, factory, site_id = platform

    async def unavailable(url):
        raise RuntimeError('TLS failure')

    monkeypatch.setattr(workflows, 'fetch', unavailable)
    with factory() as db:
        site = db.get(Site, site_id)
        result = asyncio.run(workflows.availability(db, site, SimpleNamespace()))
        assert result == {'available': False, 'status_code': 0, 'confirmed': False}
        db.commit()
        result = asyncio.run(workflows.availability(db, site, SimpleNamespace()))
        assert result['confirmed'] is True
        db.commit()
        assert db.scalar(select(Incident).where(Incident.site_id == site_id, Incident.key == 'availability')).failure_count == 2


@pytest.mark.parametrize('remote_categories,can_publish', [([68], True), ([49], False), ([68, 76], False)])
def test_publication_sends_categories_and_checks_the_remote_draft_before_publish(platform, monkeypatch, remote_categories, can_publish):
    from app import authors
    client, factory, site_id = platform
    assert client.patch('/api/v1/settings', json={'global_pause': False}).status_code == 200
    created, published = [], []
    state = {'status': 'draft'}

    def source():
        return {'id': '99', 'resource_key': 'posts:99', 'resource_type': 'posts',
                'title': 'Visit checklist', 'body': '<p>Bring your appointment questions.</p>',
                'url': 'https://example.test/blog/patient-education/visit-checklist/',
                'status': state['status'], 'source_hash': state['status'],
                'raw': {'categories': remote_categories}}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def verify_post_categories(self, ids, root): assert ids == [68] and root == 61
        async def create_draft(self, payload, key): created.append(payload); return source()
        async def read(self, key): return source()
        def matches_snapshot(self, *args, **kwargs): return True
        async def publish(self, *args, **kwargs): published.append(True); state['status'] = 'publish'

    async def client_for(*args, **kwargs): return Client()
    async def checked(*args, **kwargs): return {'passed': True, 'blockers': []}
    async def author(*args, **kwargs): return None
    async def fetch(*args, **kwargs): return {'status_code': 200, 'html': source()['body']}
    monkeypatch.setattr(workflows, 'client_for', client_for)
    monkeypatch.setattr(workflows, 'checked_article', checked)
    monkeypatch.setattr(authors, 'require_current_author', author)
    monkeypatch.setattr(workflows, 'fetch', fetch)
    monkeypatch.setattr(workflows, 'capture_html', lambda *args: {})
    monkeypatch.setattr(workflows, 'store_page', lambda *args: SimpleNamespace(managed=False, enrolled=False))
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {**policy().settings, 'enabled': True, 'allowed_actions': ['publish'], 'author_id': '1'})
        article = Article(site_id=site_id, title='Visit checklist', body=source()['body'], slug='visit-checklist',
                          author_id='1', managed=True, brief={})
        db.add(article); db.flush()
        job = Job(site_id=site_id, kind='publish', payload={'article_id': article.id}, idempotency_key='category-publication-test')
        db.add(job); db.commit()
        if can_publish:
            assert asyncio.run(workflows.publish(db, site, job))['status'] == 'published'
        else:
            with pytest.raises(ValueError, match='categories'):
                asyncio.run(workflows.publish(db, site, job))
        assert created[0]['categories'] == [68]
        assert len(published) == int(can_publish)
