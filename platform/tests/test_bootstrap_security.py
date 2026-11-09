from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth
from app.config import Settings, settings
from app.models import Base, Membership, Team, User


BOOTSTRAP_TOKEN = "test-only-bootstrap-token"


@pytest.fixture()
def bootstrap_client(monkeypatch) -> Iterator[tuple[TestClient, sessionmaker]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with factory() as db:
            yield db

    application = FastAPI()
    application.include_router(auth.router)
    application.dependency_overrides[auth.get_db] = override_db
    monkeypatch.setattr(settings, "BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)
    monkeypatch.setattr(settings, "ACCOUNT_CREATION_ENABLED", True)
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    with auth._throttle._lock:
        auth._throttle._failures.clear()

    with TestClient(application) as client:
        yield client, factory

    engine.dispose()


def _account(email: str = "owner@example.test") -> dict[str, str]:
    return {
        "email": email,
        "password": "A-long-test-passphrase!43",
        "name": "Owner",
        "team_name": "Pilot",
    }


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver"}
    if token is not None:
        headers["X-ForgeSEO-Bootstrap-Token"] = token
    return headers


def test_bootstrap_rejects_missing_and_wrong_deployment_token(bootstrap_client):
    client, factory = bootstrap_client

    for headers in (_headers(), _headers("wrong-token")):
        response = client.post("/api/v1/auth/bootstrap", headers=headers, json=_account())
        assert response.status_code == 403
        assert response.json()["detail"] == "Invalid bootstrap token"
        assert BOOTSTRAP_TOKEN not in response.text

    with factory() as db:
        assert db.scalar(select(User.id)) is None


def test_bootstrap_accepts_the_token_once_and_never_discloses_it(bootstrap_client, caplog):
    client, factory = bootstrap_client

    with caplog.at_level("DEBUG"):
        first = client.post(
            "/api/v1/auth/bootstrap",
            headers=_headers(BOOTSTRAP_TOKEN),
            json=_account(),
        )
    assert first.status_code == 200, first.text
    assert BOOTSTRAP_TOKEN not in first.text
    assert BOOTSTRAP_TOKEN not in "\n".join(first.headers.values())
    assert BOOTSTRAP_TOKEN not in caplog.text

    second = client.post(
        "/api/v1/auth/bootstrap",
        headers=_headers(BOOTSTRAP_TOKEN),
        json=_account("second@example.test"),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "Bootstrap already completed"
    assert BOOTSTRAP_TOKEN not in second.text

    with factory() as db:
        assert len(db.scalars(select(User)).all()) == 1


def test_bootstrap_fails_closed_when_deployment_token_is_unset(bootstrap_client, monkeypatch):
    client, _factory = bootstrap_client
    monkeypatch.setattr(settings, "BOOTSTRAP_TOKEN", "")

    response = client.post(
        "/api/v1/auth/bootstrap",
        headers=_headers(BOOTSTRAP_TOKEN),
        json=_account(),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Initial owner setup is not configured"
    assert BOOTSTRAP_TOKEN not in response.text


def test_account_creation_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ACCOUNT_CREATION_ENABLED", raising=False)
    assert Settings().ACCOUNT_CREATION_ENABLED is False


@pytest.mark.parametrize("token", [None, "wrong-token", BOOTSTRAP_TOKEN])
def test_closed_pilot_blocks_bootstrap_even_with_valid_token(bootstrap_client, monkeypatch, token):
    client, factory = bootstrap_client
    monkeypatch.setattr(settings, "ACCOUNT_CREATION_ENABLED", False)

    assert client.get("/api/v1/auth/status").json() == {
        "initialized": False, "account_creation_enabled": False,
    }
    response = client.post("/api/v1/auth/bootstrap", headers=_headers(token), json=_account())
    assert response.status_code == 403
    assert response.json()["detail"] == "New account creation is disabled"
    assert not response.cookies
    with factory() as db:
        assert db.scalar(select(User.id)) is None
        assert db.scalar(select(Team.id)) is None
        assert db.scalar(select(Membership.id)) is None


@pytest.mark.parametrize("existing_user", [False, True])
def test_closed_pilot_keeps_login_but_blocks_team_additions(bootstrap_client, monkeypatch, existing_user):
    client, factory = bootstrap_client
    account = _account()
    owner = client.post("/api/v1/auth/bootstrap", headers=_headers(BOOTSTRAP_TOKEN), json=account)
    assert owner.status_code == 200
    if existing_user:
        with factory() as db:
            db.add(User(email="member@example.test", name="Existing", password_hash=auth.hash_password(account["password"])))
            db.commit()
    monkeypatch.setattr(settings, "ACCOUNT_CREATION_ENABLED", False)

    # Closing registration must preserve active sessions and password login.
    assert client.get("/api/v1/auth/me").status_code == 200
    client.cookies.clear()
    login = client.post("/api/v1/auth/login", headers=_headers(), json={
        "email": account["email"], "password": account["password"],
    })
    assert login.status_code == 200
    assert client.get("/api/v1/auth/status").json() == {
        "initialized": True, "account_creation_enabled": False,
    }
    response = client.post("/api/v1/team/members", headers={
        **_headers(), "X-CSRF-Token": login.json()["csrf_token"],
    }, json={"email": "member@example.test", "name": "Member", "password": account["password"], "role": "viewer"})
    assert response.status_code == 403
    assert response.json()["detail"] == "New account creation is disabled"
    with factory() as db:
        assert len(db.scalars(select(User)).all()) == (2 if existing_user else 1)
        assert len(db.scalars(select(Membership)).all()) == 1
    assert len(client.get("/api/v1/team").json()["members"]) == 1
