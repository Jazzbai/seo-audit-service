import json
import socket

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.connectors import SSRFError, WordPressClient
from app.connectors.security import validate_public_url
from app.network import PublicTransport, fetch, public_addresses


HOST = 'htownstarfamilydentistry.com'
IP = '10.0.1.14'


@pytest.fixture
def route(monkeypatch):
    monkeypatch.setattr(settings, 'TRUSTED_HTTPS_ROUTES', json.dumps({HOST: IP}))


def private_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (IP, 443))]


@pytest.mark.parametrize('value', [
    '[]', '{"*.example.com":"10.0.1.14"}', '{"localhost":"10.0.1.14"}',
    '{"example.local":"10.0.1.14"}', '{"10.0.1.14":"10.0.1.14"}',
    '{"https://example.com":"10.0.1.14"}', '{"example.com:443":"10.0.1.14"}',
    '{"example.com":"127.0.0.1"}', '{"example.com":"169.254.169.254"}',
    '{"example.com":"0.0.0.0"}', '{"example.com":"::1"}',
    '{"example.com":"139.138.153.14"}', '{"example.com":false}',
])
def test_invalid_operator_routes_fail_at_configuration_load(value):
    with pytest.raises(ValidationError):
        Settings(TRUSTED_HTTPS_ROUTES=value)


def test_routes_are_empty_by_default_and_normalize_exact_hostnames():
    assert json.loads(Settings().TRUSTED_HTTPS_ROUTES) == {}
    assert json.loads(Settings(TRUSTED_HTTPS_ROUTES='{"HtownStarFamilyDentistry.com.":"10.0.1.14"}').TRUSTED_HTTPS_ROUTES) == {HOST: IP}


@pytest.mark.parametrize('url', [
    f'https://{HOST}/', f'https://{HOST}:443/wp-json/',
])
def test_approved_https_origin_is_usable_without_trusting_dns(route, monkeypatch, url):
    def unexpected_dns(*args, **kwargs):
        raise AssertionError('An operator route must use its fixed address')
    monkeypatch.setattr(socket, 'getaddrinfo', unexpected_dns)
    assert validate_public_url(url, resolve_dns=True)
    client = WordPressClient(url, {'username': 'test', 'application_password': 'fixture-only'})
    assert client.origin.startswith(f'https://{HOST}')


@pytest.mark.parametrize('url', [
    f'http://{HOST}/', f'http://{HOST}:443/', f'https://{HOST}:8443/',
    'https://other.example.com/', f'https://www.{HOST}/', f'https://{HOST}.evil.example/',
    f'https://{IP}/', f'https://user:pass@{HOST}/',
])
def test_route_does_not_approve_other_authorities_or_private_literals(route, monkeypatch, url):
    monkeypatch.setattr(socket, 'getaddrinfo', private_dns)
    with pytest.raises(SSRFError):
        validate_public_url(url, resolve_dns=True)


@pytest.mark.asyncio
async def test_private_dns_and_other_protocol_users_remain_rejected(route, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', private_dns)
    with pytest.raises(ValueError, match='private'):
        await public_addresses(HOST, 465)
    with pytest.raises(ValueError, match='private'):
        await PublicTransport().handle_async_request(httpx.Request('GET', 'https://other.example.com/'))


@pytest.mark.asyncio
async def test_pins_socket_host_and_tls_identity_with_certificate_checks_enabled(route, monkeypatch):
    seen = []
    options = []
    async def handler(request):
        seen.append(request)
        return httpx.Response(200, request=request, text='ok')
    def transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', transport)
    # Even a malicious/stale resolver cannot move an approved connection.
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: private_dns())
    client = PublicTransport(f'https://{HOST}')
    request = httpx.Request('POST', f'https://{HOST}/wp-json/wp/v2/posts',
                            headers={'Host': 'another-internal-site.example'}, json={'status': 'draft'})
    response = await client.handle_async_request(request)
    assert response.status_code == 200
    assert options[0]['verify'] is True
    assert options[0]['trust_env'] is False
    assert seen[0].url.host == IP
    assert seen[0].headers['host'] == HOST
    assert seen[0].extensions['sni_hostname'] == HOST
    assert seen[0].method == 'POST'
    assert seen[0].url.path == '/wp-json/wp/v2/posts'
    await client.aclose()


@pytest.mark.asyncio
async def test_approved_route_cannot_follow_an_http_downgrade(route, monkeypatch):
    def handler(request):
        return httpx.Response(302, request=request, headers={'location': f'http://{HOST}/'})
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kw: httpx.MockTransport(handler))
    with pytest.raises(ValueError, match='outside the selected site'):
        await fetch(f'https://{HOST}/')


@pytest.mark.asyncio
async def test_origin_bound_transport_rejects_other_hostname_before_connecting(route):
    client = PublicTransport(f'https://{HOST}')
    with pytest.raises(ValueError, match='authority'):
        await client.handle_async_request(httpx.Request('GET', 'https://other.example.com/'))
    await client.aclose()
