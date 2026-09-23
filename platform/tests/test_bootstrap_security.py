from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth
from app.config import settings
from app.models import Base, User


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
