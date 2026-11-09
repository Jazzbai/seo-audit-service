import asyncio
import httpx
import pytest

from app.google_properties import discover_properties, GooglePropertyError
from app import google_properties
from app.connectors.security import encrypt_credentials
from app.config import settings
from app.models import Connection
from test_platform import platform


def test_analytics_property_discovery_paginates_and_exposes_no_tokens():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'analyticsadmin.googleapis.com'
        if request.url.params.get('pageToken'):
            return httpx.Response(200, json={'accountSummaries': [{'propertySummaries': [{'property': 'properties/123', 'displayName': 'Other'}]}]})
        return httpx.Response(200, json={'accountSummaries': [{'propertySummaries': [{'property': 'properties/468885883', 'displayName': 'H-Town'}]}], 'nextPageToken': 'next'})

    result = asyncio.run(discover_properties('ga4', {'access_token': 'test-secret'}, {}, transport=httpx.MockTransport(handler)))
    assert result == {'items': [{'value': '468885883', 'label': 'H-Town'}, {'value': '123', 'label': 'Other'}], 'complete': True}
    assert len(requests) == 2 and 'test-secret' not in str(result)


def test_search_console_lists_exact_property_names():
    result = asyncio.run(discover_properties('gsc', {'access_token': 'test-secret'}, {}, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'siteEntry': [{'siteUrl': 'https://example.test/', 'permissionLevel': 'siteOwner'}]})
    )))
    assert result['items'] == [{'value': 'https://example.test/', 'label': 'https://example.test/'}]


def test_property_failure_is_safe_and_does_not_return_provider_payload():
    with pytest.raises(GooglePropertyError) as exc:
        asyncio.run(discover_properties('ga4', {'access_token': 'private-secret'}, {}, transport=httpx.MockTransport(
            lambda r: httpx.Response(403, json={'private': 'provider-secret'})
        )))
    assert 'private-secret' not in str(exc.value) and 'provider-secret' not in str(exc.value)


def test_property_api_is_site_scoped_and_owner_only(platform, monkeypatch):
    client, factory, site_id = platform
    route = f'/api/v1/sites/{site_id}/google-properties/ga4'
    assert client.get(route).status_code == 409
    with factory() as db:
        db.add(Connection(site_id=site_id, kind='ga4', status='needs_test',
                          encrypted_credentials=encrypt_credentials({'access_token': 'private-test-token'}, settings.ENCRYPTION_KEY),
                          capabilities={'settings': {}}))
        db.commit()
    calls = []

    async def discover(kind, secret, config):
        assert secret['access_token'] == 'private-test-token'
        calls.append(kind)
        return {'items': [{'value': '468885883', 'label': 'H-Town'}], 'complete': True}

    monkeypatch.setattr(google_properties, 'discover_properties', discover)
    response = client.get(route)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert 'private-test-token' not in response.text
    assert client.get('/api/v1/sites/other-site/google-properties/ga4').status_code == 404
    assert client.post('/api/v1/team/members', json={'email': 'viewer@example.test', 'name': 'Viewer', 'password': 'Long-passphrase!43', 'role': 'viewer'}).status_code == 200
    client.cookies.clear()
    assert client.post('/api/v1/auth/login', json={'email': 'viewer@example.test', 'password': 'Long-passphrase!43'}).status_code == 200
    assert client.get(route).status_code == 403
    assert calls == ['ga4']
