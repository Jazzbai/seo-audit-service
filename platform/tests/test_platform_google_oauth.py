"""Shared application identity must never become a shared customer grant."""
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app import oauth
from app.config import settings
from app.connectors.security import decrypt_credentials
from app.google_config import CLIENT, MODE
from app.models import Connection
from app.operations import credentials
from test_oauth import oauth_api, _site, _connection, _start, _state_from, MASTER_KEY


@pytest.fixture
def shared(oauth_api, monkeypatch):
    monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', 'platform-client')
    monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_SECRET', 'platform-secret')
    return oauth_api


def test_config_is_site_scoped_and_never_discloses_identity_secret(shared):
    client, factory = shared
    site = _site(factory)
    response = client.get(f'/api/v1/sites/{site}/google-oauth/config')
    assert response.status_code == 200
    assert response.json()['configured'] is True
    assert 'platform-secret' not in response.text
    assert 'platform-client' not in response.text
    assert client.get('/api/v1/sites/unknown/google-oauth/config').status_code == 404


def test_new_site_can_authorize_without_credentials_form(shared):
    client, factory = shared
    site = _site(factory)
    response = _start(client, site)
    query = parse_qs(urlparse(response.headers['location']).query)
    assert query['client_id'] == ['platform-client']
    assert query['include_granted_scopes'] == ['false']
    assert 'platform-secret' not in response.headers['location']
    with factory() as db:
        saved = decrypt_credentials(db.scalar(select(Connection)).encrypted_credentials, MASTER_KEY)
        assert 'client_secret' not in saved
        assert 'refresh_token' not in saved


def test_partial_operator_config_fails_closed_without_legacy_fallback(shared, monkeypatch):
    client, factory = shared
    site = _site(factory)
    _connection(factory, site)
    monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_SECRET', '')
    assert client.get(f'/api/v1/sites/{site}/google-oauth/config').json()['configured'] is False
    assert _start(client, site).status_code == 503


def test_callback_binds_customer_grant_without_copying_platform_secret(shared, monkeypatch):
    client, factory = shared
    first, second = _site(factory), _site(factory, name='Second', origin='https://second.example')
    _connection(factory, first, credentials={'client_id':'legacy','client_secret':'legacy-secret','refresh_token':'legacy-refresh'})
    state = _state_from(_start(client, first))
    async def exchange(code, **kwargs):
        assert kwargs['client_id'] == 'platform-client'
        assert kwargs['client_secret'] == 'platform-secret'
        return {'access_token':'site-access','scope':oauth.GOOGLE_SCOPES['gsc'],'expires_in':3600}
    monkeypatch.setattr(oauth, 'exchange_google_code', exchange)
    response = client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'one-time'}, follow_redirects=False)
    assert 'oauth=connected' in response.headers['location']
    with factory() as db:
        row = db.scalar(select(Connection).where(Connection.site_id == first))
        saved = decrypt_credentials(row.encrypted_credentials, MASTER_KEY)
        assert saved[MODE] == 'platform' and saved[CLIENT] == 'platform-client'
        assert 'client_secret' not in saved and 'client_id' not in saved
        assert 'refresh_token' not in saved  # Never retain a different client's token.
        runtime, _ = credentials(db, first, 'gsc')
        assert runtime['client_secret'] == 'platform-secret'
        assert runtime['access_token'] == 'site-access'
        with pytest.raises(ValueError):
            credentials(db, second, 'gsc')
    assert 'site-access' not in response.text
    assert client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'again'}, follow_redirects=False).status_code == 400


def test_runtime_refuses_a_grant_from_previous_client(shared, monkeypatch):
    _, factory = shared
    site = _site(factory)
    _connection(factory, site, credentials={MODE:'platform',CLIENT:'platform-client','refresh_token':'private-refresh'})
    with factory() as db:
        assert credentials(db, site, 'gsc')[0]['refresh_token'] == 'private-refresh'
        monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', 'replacement-client')
        with pytest.raises(ValueError, match='Reconnect'):
            credentials(db, site, 'gsc')


def test_client_change_while_login_pending_rejects_before_exchange(shared, monkeypatch):
    client, factory = shared
    site = _site(factory)
    state = _state_from(_start(client, site))
    monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', 'replacement-client')
    async def unexpected(*args, **kwargs):
        pytest.fail('Cannot exchange a code issued for another client')
    monkeypatch.setattr(oauth, 'exchange_google_code', unexpected)
    response = client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'one-time'}, follow_redirects=False)
    assert response.status_code == 400


@pytest.mark.parametrize('scope', [None, 'https://www.googleapis.com/auth/adwords', oauth.GOOGLE_SCOPES['gsc']+' https://www.googleapis.com/auth/adwords'])
def test_shared_callback_rejects_unknown_or_broader_permissions(shared, monkeypatch, scope):
    client, factory = shared
    site = _site(factory)
    state = _state_from(_start(client, site))
    async def exchange(*args, **kwargs):
        return {'access_token':'never-save', 'refresh_token':'never-save-refresh', 'scope':scope}
    monkeypatch.setattr(oauth, 'exchange_google_code', exchange)
    response = client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'one-time'}, follow_redirects=False)
    assert 'unexpected_permissions' in response.headers['location']
    with factory() as db:
        row = db.scalar(select(Connection))
        saved = decrypt_credentials(row.encrypted_credentials, MASTER_KEY)
        assert 'access_token' not in saved and 'refresh_token' not in saved


def test_site_settings_cannot_override_platform_credentials(shared):
    client, factory = shared
    site = _site(factory)
    path = f'/api/v1/sites/{site}/connections/gsc'
    assert client.put(path, json={'credentials':{'client_secret':'override'},'settings':{}}).status_code == 422
    assert client.put(path, json={'settings':{'site_url':'sc-domain:oauth.example'}}).status_code == 200


def test_secret_rotation_retains_only_same_client_site_grant(shared, monkeypatch):
    client, factory = shared
    site = _site(factory)
    _connection(factory, site, credentials={MODE:'platform',CLIENT:'platform-client','refresh_token':'same-client-refresh'})
    monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_SECRET', 'rotated-secret')
    state = _state_from(_start(client, site))
    async def exchange(*args, **kwargs):
        assert kwargs['client_secret'] == 'rotated-secret'
        return {'access_token':'new-access','scope':oauth.GOOGLE_SCOPES['gsc']}
    monkeypatch.setattr(oauth, 'exchange_google_code', exchange)
    response = client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'once'}, follow_redirects=False)
    assert 'oauth=connected' in response.headers['location']
    with factory() as db:
        grant, _ = credentials(db, site, 'gsc')
        assert grant['client_secret'] == 'rotated-secret'
        assert grant['refresh_token'] == 'same-client-refresh'
        saved = decrypt_credentials(db.scalar(select(Connection)).encrypted_credentials, MASTER_KEY)
        assert 'rotated-secret' not in str(saved)


def test_revoked_grant_reconnect_does_not_restore_old_tokens(shared, monkeypatch):
    client, factory = shared
    site = _site(factory)
    _connection(factory, site, status='revoked', credentials={MODE:'platform',CLIENT:'platform-client','refresh_token':'revoked-refresh'})
    state = _state_from(_start(client, site))
    async def exchange(*args, **kwargs):
        return {'access_token':'new-access','refresh_token':'new-refresh','scope':oauth.GOOGLE_SCOPES['gsc']}
    monkeypatch.setattr(oauth, 'exchange_google_code', exchange)
    response = client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'one-time'}, follow_redirects=False)
    assert 'oauth=connected' in response.headers['location']
    with factory() as db:
        assert credentials(db, site, 'gsc')[0]['refresh_token'] == 'new-refresh'


@pytest.mark.parametrize('change', ['revoke', 'replace_client'])
def test_changes_during_token_exchange_cannot_install_stale_grants(shared, monkeypatch, change):
    client, factory = shared
    site = _site(factory)
    state = _state_from(_start(client, site))
    async def exchange(*args, **kwargs):
        if change == 'revoke':
            with factory() as db:
                db.scalar(select(Connection)).status = 'revoked'
                db.commit()
        else:
            monkeypatch.setattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', 'replacement-client')
        return {'access_token':'stale-access', 'scope':oauth.GOOGLE_SCOPES['gsc']}
    monkeypatch.setattr(oauth, 'exchange_google_code', exchange)
    response = client.get('/api/v1/oauth/google/callback', params={'state':state,'code':'once'}, follow_redirects=False)
    assert response.status_code == 409
    with factory() as db:
        saved = decrypt_credentials(db.scalar(select(Connection)).encrypted_credentials, MASTER_KEY)
        assert 'access_token' not in saved
