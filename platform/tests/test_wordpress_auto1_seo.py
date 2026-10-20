"""Auto1 bridge contract: narrow SEO writes with independent source verification."""
import copy
import json
import asyncio

import httpx
import pytest

from app.connectors import AmbiguousOutcome, ConnectorError, SourceConflict, UnsupportedField
from app.connectors.wordpress import WordPressClient
from test_wordpress_capabilities import _index
from test_platform import platform


def bridge_fixture(*, writable=True, valid=True, drift=False, ignore_write=False, timeout=False):
    index = _index(namespaces=['wp/v2', 'auto1-ops/v1'])
    paths = {'': 'GET', '/validate': 'POST', '/apply': 'POST', '/rollback': 'POST'}
    for suffix, method in paths.items():
        index['routes']['/auto1-ops/v1/seo/(?P<post_id>\\d+)' + suffix] = {'methods': [method]}
    index['routes']['/auto1-ops/v1/capabilities'] = {'methods': ['GET']}
    caps = {
        'namespace': 'auto1-ops/v1',
        'writable': {'seoFields': writable, 'postTypes': ['post', 'page']},
        'modules': {'seo': {'enabled': True, 'rollback': True}},
        'endpoints': {
            name: {'method': method, 'path': '/wp-json/auto1-ops/v1/seo/<postId>' + suffix, 'available': True}
            for name, method, suffix in [('seo_read', 'GET', ''), ('seo_validate', 'POST', '/validate'),
                                         ('seo_apply', 'POST', '/apply'), ('seo_rollback', 'POST', '/rollback')]
        },
    }
    post = {'id': 7, 'type': 'post', 'slug': 'guide', 'status': 'publish',
            'link': 'https://auto1.fixture.test/guide/', 'author': 3,
            'title': {'raw': 'Guide'}, 'content': {'raw': '<p>Original content</p>'},
            'meta': {'auto1_seo_title': 'Original title', 'auto1_meta_description': '',
                     '_elementor_data': 'unchanged builder data'}}
    calls = []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        data = None
        if path == '/wp-json/':
            data = index
        elif path == '/wp-json/auto1-ops/v1/capabilities':
            data = caps
        elif path == '/wp-json/wp/v2/users/me':
            data = {'id': 3, 'capabilities': {'edit_posts': True, 'edit_others_posts': True,
                                           'publish_posts': True, 'edit_published_posts': True}}
        elif path == '/wp-json/wp/v2/posts/7' and request.method == 'GET':
            data = post
        elif path == '/wp-json/wp/v2/posts/7' and request.method == 'POST':
            assert json.loads(request.content) == {}
            data = post
        elif path == '/wp-json/auto1-ops/v1/seo/7/validate':
            fields = json.loads(request.content)['fields']
            data = {'postId': 7, 'fields': fields, 'validation': {'valid': valid}}
            if drift:
                post['content']['raw'] = '<p>External editor content</p>'
        elif path == '/wp-json/auto1-ops/v1/seo/7/apply':
            fields = json.loads(request.content)['fields']
            assert set(fields) <= {'auto1_seo_title', 'auto1_meta_description'}
            if not ignore_write:
                post['meta'].update(fields)
            if timeout:
                raise httpx.ReadTimeout('fixture timeout', request=request)
            data = {'postId': 7, 'status': 'applied', 'after': post['meta']}
        else:
            pytest.fail(f'Unexpected request: {request.method} {path}')
        return httpx.Response(200, json=copy.deepcopy(data), request=request)

    client = WordPressClient('https://auto1.fixture.test', {'username': 'fixture', 'application_password': 'fixture'},
                             transport=httpx.MockTransport(handler))
    return client, caps, post, calls, index


@pytest.mark.asyncio
async def test_auto1_discovery_and_selected_field_write_preserve_native_and_other_seo_fields():
    client, _, post, calls, _ = bridge_fixture()
    original = copy.deepcopy(post)
    async with client:
        caps = await client.discover()
        assert caps['seo']['provider'] == 'auto1_ops'
        assert caps['seo']['writable_fields'] == ['description', 'title']
        assert all(method == 'GET' for method, _ in calls)
        before = await client.read('post:7')
        updated = await client.update('post:7', {'seo': {'description': 'A useful guide.'}}, before['source_hash'])
        assert updated['metadata']['seo']['auto1_ops']['description'] == 'A useful guide.'
        assert post['content'] == original['content']
        assert post['meta']['_elementor_data'] == original['meta']['_elementor_data']
        assert post['meta']['auto1_seo_title'] == 'Original title'
        assert calls.count(('POST', '/wp-json/wp/v2/posts/7')) == 1
        await client.update('post:7', {'seo': {'description': ''}}, updated['source_hash'])
        assert post['meta']['auto1_meta_description'] == ''
        with pytest.raises(UnsupportedField):
            await client.update('post:7', {'seo': {'canonical_url': 'https://other.example/'}},
                                (await client.read('post:7'))['source_hash'])


@pytest.mark.parametrize('missing', ['writable', 'rollback', 'route', 'endpoint'])
@pytest.mark.asyncio
async def test_auto1_requires_complete_authenticated_bridge_contract(missing):
    client, caps, _, calls, index = bridge_fixture()
    if missing == 'writable':
        caps['writable']['seoFields'] = False
    elif missing == 'rollback':
        caps['modules']['seo']['rollback'] = False
    elif missing == 'route':
        del index['routes']['/auto1-ops/v1/seo/(?P<post_id>\\d+)/apply']
    else:
        caps['endpoints']['seo_apply']['path'] = 'https://outside.example/apply'
    async with client:
        discovered = await client.discover()
        assert discovered['seo']['write'] is False
        with pytest.raises(UnsupportedField):
            await client.update('post:7', {'seo': {'description': 'Blocked'}},
                                (await client.read('post:7'))['source_hash'])
    assert not any(method == 'POST' for method, _ in calls)


@pytest.mark.asyncio
async def test_auto1_validation_rejection_does_not_apply():
    client, _, _, calls, _ = bridge_fixture(valid=False)
    async with client:
        with pytest.raises(ConnectorError, match='validation rejected'):
            await client.update('post:7', {'seo': {'description': 'Rejected'}},
                                (await client.read('post:7'))['source_hash'])
    assert not any(path.endswith('/apply') for _, path in calls)


@pytest.mark.asyncio
async def test_auto1_editor_drift_during_validation_stops_before_apply():
    client, _, post, calls, _ = bridge_fixture(drift=True)
    async with client:
        with pytest.raises(SourceConflict):
            await client.update('post:7', {'seo': {'description': 'Stale change'}},
                                (await client.read('post:7'))['source_hash'])
    assert post['content']['raw'] == '<p>External editor content</p>'
    assert not any(path.endswith('/apply') for _, path in calls)


@pytest.mark.asyncio
async def test_auto1_ignoring_write_is_not_reported_as_success():
    client, _, _, _, _ = bridge_fixture(ignore_write=True)
    async with client:
        with pytest.raises(AmbiguousOutcome):
            await client.update('post:7', {'seo': {'description': 'Not stored'}},
                                (await client.read('post:7'))['source_hash'])


@pytest.mark.asyncio
async def test_auto1_timeout_is_reconciled_without_repeating_apply():
    client, _, _, calls, _ = bridge_fixture(timeout=True)
    async with client:
        result = await client.update('post:7', {'seo': {'description': 'Stored before timeout'}},
                                     (await client.read('post:7'))['source_hash'])
        assert result['metadata']['seo']['auto1_ops']['description'] == 'Stored before timeout'
    assert sum(path.endswith('/apply') for _, path in calls) == 1


def test_connection_upgrade_refreshes_existing_candidates_without_new_proposals(platform, monkeypatch):
    from app import workflows
    from app.models import Candidate, Connection, Job, Page, Site

    _, factory, site_id = platform

    class VerifiedBridge:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def validate_connection(self):
            return {'seo': {'write': True, 'writable_fields': ['title', 'description']}}

        async def discover_authors(self):
            return {'items': [], 'complete': True}

    async def client_for(*args):
        return VerifiedBridge()

    monkeypatch.setattr(workflows, 'client_for', client_for)
    with factory() as db:
        page = Page(site_id=site_id, resource_key='posts:7', resource_type='posts',
                    url='https://fixture.test/guide/', source_hash='unchanged-source')
        db.add(page)
        db.flush()
        candidate = Candidate(site_id=site_id, page_id=page.id, field='meta_description',
                              source_hash='unchanged-source', before_value='', after_value='A useful description.',
                              status='pending', details={'review_only_reasons': ['wordpress_seo_writer_not_verified', 'editorial_review_required'],
                                                         'execution_readiness': {'blockers': ['wordpress_seo_writer_not_verified']}})
        db.add(candidate)
        db.add(Connection(site_id=site_id, kind='wordpress', status='configured',
                          encrypted_credentials='fixture-only', capabilities={}))
        db.commit()
        asyncio.run(workflows.connection_test(db, db.get(Site, site_id), Job(payload={'kind': 'wordpress'})))
        assert candidate.status == 'pending'
        assert candidate.source_hash == 'unchanged-source'
        assert candidate.details['review_only_reasons'] == ['editorial_review_required']
        assert candidate.details['execution_readiness']['execution_ready'] is True
