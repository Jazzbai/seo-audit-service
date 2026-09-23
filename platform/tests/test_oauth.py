"""Focused, secret-safe regression tests for the bounded Google OAuth flow."""

from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import api, auth, oauth
from app.config import settings
from app.connectors.security import decrypt_credentials, encrypt_credentials
from app.models import Base, Connection, Site, Team


BOOTSTRAP_TOKEN = "oauth-test-bootstrap-token"
MASTER_KEY = "oauth-test-master-key-with-at-least-32-bytes"


@pytest.fixture()
def oauth_api(monkeypatch) -> Iterator[tuple[TestClient, sessionmaker]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    def override_db():
        with factory() as db:
            yield db

    application = FastAPI()
    application.include_router(auth.router)
    application.include_router(api.router)
    application.include_router(oauth.router)
    application.dependency_overrides[auth.get_db] = override_db
    application.dependency_overrides[api.get_db] = override_db
    application.dependency_overrides[oauth.get_db] = override_db
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", MASTER_KEY)
    monkeypatch.setattr(settings, "BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)

    with TestClient(application) as client:
        bootstrap = client.post(
            "/api/v1/auth/bootstrap",
            headers={
                "Origin": "http://testserver",
                "X-ForgeSEO-Bootstrap-Token": BOOTSTRAP_TOKEN,
            },
            json={
                "email": "oauth-owner@example.test",
                "password": "A-very-long-oauth-test-passphrase!43",
                "name": "OAuth owner",
                "team_name": "OAuth team",
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        client.headers.update(
            {"Origin": "http://testserver", "X-CSRF-Token": bootstrap.json()["csrf_token"]}
        )
        yield client, factory

    engine.dispose()


def _site(factory: sessionmaker, *, name: str = "OAuth site", origin: str = "https://oauth.example") -> str:
    with factory() as db:
        team = db.scalar(select(Team))
        row = Site(team_id=team.id, name=name, origin=origin)
        db.add(row)
        db.commit()
        return row.id


def _connection(
    factory: sessionmaker,
    site_id: str,
    *,
    kind: str = "gsc",
    credentials: dict[str, object] | None = None,
    settings_payload: dict[str, object] | None = None,
    status: str = "needs_test",
) -> None:
    with factory() as db:
        row = Connection(
            site_id=site_id,
            kind=kind,
            encrypted_credentials=encrypt_credentials(
                credentials or {"client_id": "client-id", "client_secret": "client-secret"},
                MASTER_KEY,
            ),
            status=status,
            capabilities={"settings": settings_payload or {}},
        )
        db.add(row)
        db.commit()


def _start(client: TestClient, site_id: str, kind: str = "gsc"):
    return client.get(
        f"/api/v1/sites/{site_id}/connections/{kind}/oauth/start",
        follow_redirects=False,
    )


def _state_from(response) -> str:
    assert response.status_code == 307, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def test_start_requires_saved_client_credentials_and_never_discloses_secrets(oauth_api):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(factory, site_id, credentials={"client_id": "client-only"})

    response = _start(client, site_id)

    assert response.status_code == 409
    assert "client secret" in response.json()["detail"]
    assert "client-only" not in response.text
    assert "client-secret" not in response.text


def test_start_rejects_invalid_public_url_and_unsupported_provider(oauth_api, monkeypatch):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(factory, site_id)

    monkeypatch.setattr(settings, "PUBLIC_URL", "https://seo.example/path-not-an-origin")
    response = _start(client, site_id)
    assert response.status_code == 503
    assert "PUBLIC_URL" in response.json()["detail"]

    monkeypatch.setattr(settings, "PUBLIC_URL", "http://testserver")
    response = _start(client, site_id, "wordpress")
    assert response.status_code == 422
    assert "only for Search Console" in response.json()["detail"]


@pytest.mark.parametrize(
    ("kind", "scope"),
    [
        ("gsc", "https://www.googleapis.com/auth/webmasters.readonly"),
        ("ga4", "https://www.googleapis.com/auth/analytics.readonly"),
    ],
)
def test_start_uses_fixed_callback_and_provider_scope(oauth_api, kind, scope):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(factory, site_id, kind=kind)

    response = _start(client, site_id, kind)
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    state = decrypt_credentials(query["state"][0], MASTER_KEY)

    assert response.status_code == 307
    assert location.scheme == "https"
    assert location.netloc == "accounts.google.com"
    assert query["redirect_uri"] == ["http://testserver/api/v1/oauth/google/callback"]
    assert query["scope"] == [scope]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert state["site_id"] == site_id
    assert state["team_id"]
    assert state["user_id"]
    assert state["session_id"]
    assert state["kind"] == kind
    assert state["expires_at"] > int(time.time())
    assert "client-secret" not in response.headers["location"]


def test_tampered_expired_and_sessionless_states_are_rejected(oauth_api):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(factory, site_id)
    state = _state_from(_start(client, site_id))

    tampered = state[:-1] + ("A" if state[-1] != "A" else "B")
    response = client.get(
        f"/api/v1/oauth/google/callback?state={tampered}&code=ignored",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid OAuth state"

    payload = decrypt_credentials(state, MASTER_KEY)
    payload["expires_at"] = int(time.time()) - 1
    expired = encrypt_credentials(payload, MASTER_KEY)
    response = client.get(
        f"/api/v1/oauth/google/callback?state={expired}&code=ignored",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "OAuth state has expired"

    client.cookies.clear()
    response = client.get(
        f"/api/v1/oauth/google/callback?state={state}&code=ignored",
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_callback_rejects_a_state_used_for_another_site(oauth_api):
    client, factory = oauth_api
    first_site = _site(factory, name="First site")
    second_site = _site(factory, name="Second site", origin="https://second.example")
    _connection(factory, first_site)
    state = _state_from(_start(client, first_site))

    response = client.get(
        f"/api/v1/sites/{second_site}/connections/gsc/oauth/callback"
        f"?state={state}&code=ignored",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "OAuth state does not match the requested site"


def test_callback_exchanges_and_persists_only_a_successful_provider_response(oauth_api, monkeypatch):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(
        factory,
        site_id,
        credentials={
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "manual-refresh-token",
        },
        settings_payload={"site_url": "sc-domain:oauth.example", "keep": "this"},
    )
    state = _state_from(_start(client, site_id))
    seen: dict[str, object] = {}

    async def fake_exchange(code, *, client_id, client_secret, redirect_uri, transport=None):
        seen.update(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            transport=transport,
        )
        return {"access_token": "access-secret", "token_type": "Bearer", "expires_in": 3600}

    monkeypatch.setattr(oauth, "exchange_google_code", fake_exchange)
    response = client.get(
        f"/api/v1/oauth/google/callback?state={state}&code=one-time-code",
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"http://testserver/sites/{site_id}/settings/connections?")
    assert "oauth=connected" in location
    assert "access-secret" not in location
    assert "manual-refresh-token" not in location
    assert seen["code"] == "one-time-code"
    assert seen["client_id"] == "client-id"
    assert seen["client_secret"] == "client-secret"
    assert seen["redirect_uri"] == "http://testserver/api/v1/oauth/google/callback"

    with factory() as db:
        row = db.scalar(select(Connection).where(Connection.site_id == site_id, Connection.kind == "gsc"))
        saved = decrypt_credentials(row.encrypted_credentials, MASTER_KEY)
        assert row.status == "connected"
        assert saved["client_id"] == "client-id"
        assert saved["client_secret"] == "client-secret"
        assert saved["access_token"] == "access-secret"
        assert saved["refresh_token"] == "manual-refresh-token"
        assert saved["token_type"] == "Bearer"
        assert isinstance(saved["expires_at"], str)
        assert row.capabilities["settings"] == {"site_url": "sc-domain:oauth.example", "keep": "this"}

    assert "access-secret" not in response.text
    assert "client-secret" not in response.text


def test_callback_rejects_replaying_a_successfully_consumed_state(oauth_api, monkeypatch):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(factory, site_id)
    state = _state_from(_start(client, site_id))
    calls: list[str] = []

    async def fake_exchange(code, *, client_id, client_secret, redirect_uri, transport=None):
        calls.append(code)
        return {"access_token": "access-secret", "expires_in": 3600}

    monkeypatch.setattr(oauth, "exchange_google_code", fake_exchange)
    first = client.get(
        f"/api/v1/oauth/google/callback?state={state}&code=one-time-code",
        follow_redirects=False,
    )
    replay = client.get(
        f"/api/v1/oauth/google/callback?state={state}&code=replayed-code",
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert replay.status_code == 400
    assert replay.json()["detail"] == "OAuth state has already been used or is no longer valid"
    assert calls == ["one-time-code"]


def test_callback_consumes_state_when_google_denies_authorization(oauth_api):
    client, factory = oauth_api
    site_id = _site(factory)
    _connection(factory, site_id)
    state = _state_from(_start(client, site_id))

    denied = client.get(
        f"/api/v1/oauth/google/callback?state={state}&error=access_denied",
        follow_redirects=False,
    )
    replay = client.get(
        f"/api/v1/oauth/google/callback?state={state}&code=late-code",
        follow_redirects=False,
    )

    assert denied.status_code == 303
    assert "oauth=error" in denied.headers["location"]
    assert replay.status_code == 400
    assert replay.json()["detail"] == "OAuth state has already been used or is no longer valid"


@pytest.mark.asyncio
async def test_google_exchange_accepts_a_mock_transport_without_logging_secrets():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(oauth.GOOGLE_TOKEN_URL)
        form = request.content.decode()
        assert "client_secret=client-secret" in form
        return httpx.Response(
            200,
            json={"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600},
        )

    result = await oauth.exchange_google_code(
        "authorization-code",
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="https://seo.example/api/v1/oauth/google/callback",
        transport=httpx.MockTransport(handler),
    )

    assert result["access_token"] == "access-secret"
    assert result["refresh_token"] == "refresh-secret"
