"""Focused regression tests for browser sessions, tenant access, and candidates."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import api, auth, worker
from app.config import settings
from app.models import (
    Base,
    BudgetAccount,
    Candidate,
    Connection,
    CostReservation,
    Membership,
    Page,
    Policy,
    Session as AuthSession,
    Site,
    Team,
    User,
)

TEST_BOOTSTRAP_TOKEN = "test-only-bootstrap-token"


@pytest.fixture()
def foundation_api(monkeypatch) -> Iterator[tuple[TestClient, sessionmaker]]:
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
    application.dependency_overrides[auth.get_db] = override_db
    application.dependency_overrides[api.get_db] = override_db
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(settings, "BOOTSTRAP_TOKEN", TEST_BOOTSTRAP_TOKEN)
    monkeypatch.setattr(worker.execute_job, "apply_async", lambda *args, **kwargs: None)

    with TestClient(application) as client:
        boot = client.post(
            "/api/v1/auth/bootstrap",
            headers={
                "Origin": "http://testserver",
                "X-ForgeSEO-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN,
            },
            json={
                "email": "owner@example.test",
                "password": "A-very-long-test-passphrase!43",
                "name": "Owner",
                "team_name": "Primary team",
            },
        )
        assert boot.status_code == 200, boot.text
        client.headers.update(
            {"Origin": "http://testserver", "X-CSRF-Token": boot.json()["csrf_token"]}
        )
        yield client, factory

    engine.dispose()


def _create_site(factory, team_id: str, *, name: str, origin: str) -> str:
    with factory() as db:
        site = Site(team_id=team_id, name=name, origin=origin)
        db.add(site)
        db.commit()
        return site.id


def test_login_binds_session_and_site_queries_to_selected_team(foundation_api):
    client, factory = foundation_api
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "owner@example.test"))
        primary_membership = db.scalar(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)
        )
        secondary_team = Team(name="Secondary team")
        db.add(secondary_team)
        db.flush()
        db.add(Membership(team_id=secondary_team.id, user_id=user.id, role="viewer"))
        db.commit()
        primary_team_id = primary_membership.team_id
        secondary_team_id = secondary_team.id

    primary_site_id = _create_site(factory, primary_team_id, name="Primary site", origin="https://primary.test")
    secondary_site_id = _create_site(
        factory, secondary_team_id, name="Secondary site", origin="https://secondary.test"
    )

    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver"},
        json={
            "email": "owner@example.test",
            "password": "A-very-long-test-passphrase!43",
            "team_id": secondary_team_id,
        },
    )
    assert login.status_code == 200, login.text
    assert login.json()["team"]["id"] == secondary_team_id
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]

    assert client.get(f"/api/v1/sites/{secondary_site_id}").status_code == 200
    assert client.get(f"/api/v1/sites/{primary_site_id}").status_code == 404
    sites = client.get("/api/v1/sites").json()
    assert sites["total"] == 1
    assert sites["items"][0]["id"] == secondary_site_id
    assert client.post(
        "/api/v1/sites",
        json={"name": "Not allowed", "origin": "https://blocked.test"},
    ).status_code == 403

    with factory() as db:
        session = db.scalar(select(AuthSession).order_by(AuthSession.created_at.desc()))
        assert session is not None
        assert session.team_id == secondary_team_id


def test_policy_api_versions_are_append_only_and_site_scoped(foundation_api):
    client, factory = foundation_api
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "owner@example.test"))
        primary_membership = db.scalar(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)
        )
        other_team = Team(name="Other team")
        db.add(other_team)
        db.flush()
        other_site = Site(team_id=other_team.id, name="Other site", origin="https://other.test")
        db.add(other_site)
        db.commit()
        team_id = primary_membership.team_id
        other_site_id = other_site.id

    site_a_id = _create_site(factory, team_id, name="Site A", origin="https://site-a.test")
    site_b_id = _create_site(factory, team_id, name="Site B", origin="https://site-b.test")

    first = client.put(
        f"/api/v1/sites/{site_a_id}/policy",
        json={"settings": {"enabled": False, "allowed_actions": ["metadata"], "protected_paths": []}},
    )
    assert first.status_code == 200, first.text
    assert first.json()["site_id"] == site_a_id
    assert first.json()["version"] == 1

    second = client.put(
        f"/api/v1/sites/{site_a_id}/policy",
        json={"settings": {"enabled": True, "allowed_actions": ["metadata"], "protected_paths": []}},
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2

    site_b = client.put(
        f"/api/v1/sites/{site_b_id}/policy",
        json={"settings": {"enabled": False, "allowed_actions": ["refresh"], "protected_paths": []}},
    )
    assert site_b.status_code == 200, site_b.text
    assert site_b.json()["site_id"] == site_b_id
    assert site_b.json()["version"] == 1

    assert client.get(f"/api/v1/sites/{site_a_id}/policy").json()["version"] == 2
    assert client.get(f"/api/v1/sites/{site_b_id}/policy").json()["version"] == 1
    assert client.get(f"/api/v1/sites/{other_site_id}/policy").status_code == 404
    assert client.put(
        f"/api/v1/sites/{other_site_id}/policy",
        json={"settings": {"enabled": True, "allowed_actions": ["metadata"]}},
    ).status_code == 404

    with factory() as db:
        versions = db.scalars(
            select(Policy).where(Policy.site_id == site_a_id).order_by(Policy.version)
        ).all()
        assert [row.version for row in versions] == [1, 2]
        assert versions[0].settings["enabled"] is False
        assert versions[1].settings["enabled"] is True
        assert all(row.created_by == user.id for row in versions)


def test_legacy_unbound_session_fails_closed(foundation_api):
    client, factory = foundation_api
    with factory() as db:
        session = db.scalar(select(AuthSession).order_by(AuthSession.created_at.desc()))
        assert session is not None
        session.team_id = None
        db.commit()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_candidate_decisions_are_monotonic_and_execution_requires_approval(foundation_api):
    client, factory = foundation_api
    with factory() as db:
        team = db.scalar(select(Team))
        site = Site(team_id=team.id, name="Primary site", origin="https://primary.test")
        db.add(site)
        db.flush()
        page = Page(
            site_id=site.id,
            resource_key="posts:1",
            url="https://primary.test/post",
            title="Post",
            source_hash="source-1",
        )
        db.add(page)
        db.flush()
        db.add(Connection(
            site_id=site.id,
            kind="wordpress",
            encrypted_credentials="test",
            status="connected",
            capabilities={"seo": {"write": True, "writable_fields": ["title", "description"]}},
        ))
        first = Candidate(
            site_id=site.id,
            page_id=page.id,
            field="seo_title",
            before_value="Old title",
            after_value="Complete useful title",
            source_hash="source-1",
        )
        second = Candidate(
            site_id=site.id,
            page_id=page.id,
            field="meta_description",
            before_value="",
            after_value="A complete useful description.",
            source_hash="source-1",
        )
        db.add_all([first, second])
        db.commit()
        first_id, second_id = first.id, second.id

    pending_execution = client.post(f"/api/v1/sites/{site.id}/candidates/{first_id}/execute")
    assert pending_execution.status_code == 409

    approved = client.post(
        f"/api/v1/sites/{site.id}/candidates/{first_id}/decision",
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    reverse_decision = client.post(
        f"/api/v1/sites/{site.id}/candidates/{first_id}/decision",
        json={"decision": "reject"},
    )
    assert reverse_decision.status_code == 409

    execution = client.post(f"/api/v1/sites/{site.id}/candidates/{first_id}/execute")
    assert execution.status_code == 202, execution.text
    assert execution.json()["kind"] == "candidate"

    rejected = client.post(
        f"/api/v1/sites/{site.id}/candidates/{second_id}/decision",
        json={"decision": "reject"},
    )
    assert rejected.status_code == 200
    assert client.post(
        f"/api/v1/sites/{site.id}/candidates/{second_id}/decision",
        json={"decision": "approve"},
    ).status_code == 409

    with factory() as db:
        rows = {row.id: row for row in db.scalars(select(Candidate)).all()}
        assert rows[first_id].status == "approved"
        assert rows[second_id].status == "rejected"
        assert rows[first_id].details["authorization"]["type"] == "person"


def test_cross_site_candidate_page_reference_is_not_actionable(foundation_api):
    client, factory = foundation_api
    with factory() as db:
        team = db.scalar(select(Team))
        site_a = Site(team_id=team.id, name="Primary site", origin="https://primary.test")
        db.add(site_a)
        db.flush()
        site_b = Site(team_id=team.id, name="Second site", origin="https://second.test")
        db.add(site_b)
        db.flush()
        page_b = Page(site_id=site_b.id, resource_key="posts:2", url=site_b.origin + "/post")
        db.add(page_b)
        db.flush()
        candidate = Candidate(
            site_id=site_a.id,
            page_id=page_b.id,
            field="seo_title",
            after_value="A complete title",
            source_hash="source-2",
        )
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id

    response = client.post(
        f"/api/v1/sites/{site_a.id}/candidates/{candidate_id}/decision",
        json={"decision": "approve"},
    )
    assert response.status_code == 404


def test_cross_site_budget_account_reference_cannot_be_settled(foundation_api):
    client, factory = foundation_api
    with factory() as db:
        team = db.scalar(select(Team))
        site_a = Site(team_id=team.id, name="Primary site", origin="https://primary.test")
        site_b = Site(team_id=team.id, name="Second site", origin="https://second.test")
        db.add_all([site_a, site_b])
        db.flush()
        account_b = BudgetAccount(
            site_id=site_b.id,
            period="2026-09",
            limit_cents=30000,
            reserved_cents=25,
            spent_cents=0,
        )
        db.add(account_b)
        db.flush()
        reservation = CostReservation(
            site_id=site_a.id,
            account_id=account_b.id,
            operation_key="cross-site-budget-reference",
            estimated_cents=25,
            status="reserved",
        )
        db.add(reservation)
        db.commit()
        site_a_id, reservation_id, account_b_id = site_a.id, reservation.id, account_b.id

    response = client.post(
        f"/api/v1/sites/{site_a_id}/budgets/reservations/{reservation_id}/settle",
        json={"actual_cents": 25, "evidence": "fixture-account-mismatch"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Budget reservation accounting is inconsistent"

    with factory() as db:
        account = db.get(BudgetAccount, account_b_id)
        stored = db.get(CostReservation, reservation_id)
        assert (account.reserved_cents, account.spent_cents) == (25, 0)
        assert stored.status == "reserved"
