from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app import auth
from app.budgets import release, reserve, settle
from app.models import (
    Base,
    BudgetAccount,
    CostReservation,
    Membership,
    Page,
    Policy,
    Site,
    Team,
    User,
)
from app.policies import create_policy, evaluate_policy

TEST_BOOTSTRAP_TOKEN = "test-only-bootstrap-token"


@pytest.fixture()
def database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


@pytest.fixture()
def auth_client(database, monkeypatch) -> Iterator[TestClient]:
    _engine, factory = database
    monkeypatch.setattr(auth.settings, "BOOTSTRAP_TOKEN", TEST_BOOTSTRAP_TOKEN)
    application = FastAPI()
    application.include_router(auth.router)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[auth.get_db] = override_db
    with TestClient(application) as client:
        client.headers.update({"X-ForgeSEO-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN})
        yield client


def _site(factory, *, paused: bool = False, facts: dict | None = None) -> Site:
    with factory() as db:
        team = Team(name="Test team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Test site",
            origin="https://example.test",
            paused=paused,
            facts=facts or {},
        )
        db.add(site)
        db.commit()
        return site


def test_bootstrap_login_session_hashes_and_csrf(auth_client, database):
    response = auth_client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "Owner@Example.test",
            "password": "correct horse battery staple",
            "name": "Owner",
            "team_name": "Acme",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["role"] == "owner"
    assert payload["user"]["email"] == "owner@example.test"
    assert payload["csrf_token"]

    with database[1]() as db:
        session = db.scalar(select(auth.AuthSession))
        assert session is not None
        assert session.token_hash != auth_client.cookies.get(auth.SESSION_COOKIE_NAME)
        assert session.csrf_token != payload["csrf_token"]
        user = db.scalar(select(User))
        assert user is not None
        assert "correct horse" not in user.password_hash
        assert auth.verify_password("correct horse battery staple", user.password_hash)

    me = auth_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == payload["user"]["id"]

    missing_csrf = auth_client.post(
        "/api/v1/team/members",
        json={
            "email": "editor@example.test",
            "name": "Editor",
            "password": "editor password",
            "role": "editor",
        },
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "CSRF validation failed"

    added = auth_client.post(
        "/api/v1/team/members",
        headers={"X-CSRF-Token": payload["csrf_token"]},
        json={
            "email": "editor@example.test",
            "name": "Editor",
            "password": "editor password",
            "role": "editor",
        },
    )
    assert added.status_code == 200
    assert added.json()["member"]["role"] == "editor"

    second_bootstrap = auth_client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "second@example.test",
            "password": "another password",
            "name": "Second",
            "team_name": "Other",
        },
    )
    assert second_bootstrap.status_code == 409


def test_auth_failures_are_generic_and_roles_are_enforced(auth_client):
    wrong_known = auth_client.post(
        "/api/v1/auth/login",
        json={"email": "not-created@example.test", "password": "wrongpass"},
    )
    assert wrong_known.status_code == 401
    assert wrong_known.json()["detail"] == "Invalid email or password"

    boot = auth_client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "owner@example.test",
            "password": "owner password",
            "name": "Owner",
            "team_name": "Acme",
        },
    )
    assert boot.status_code == 200
    csrf = boot.json()["csrf_token"]
    added = auth_client.post(
        "/api/v1/team/members",
        headers={"X-CSRF-Token": csrf},
        json={
            "email": "viewer@example.test",
            "name": "Viewer",
            "password": "viewer password",
            "role": "viewer",
        },
    )
    assert added.status_code == 200

    editor = auth_client.post(
        "/api/v1/auth/login",
        json={"email": "viewer@example.test", "password": "viewer password"},
    )
    assert editor.status_code == 200
    denied = auth_client.post(
        "/api/v1/team/members",
        headers={"X-CSRF-Token": editor.json()["csrf_token"]},
        json={
            "email": "blocked@example.test",
            "name": "Blocked",
            "password": "blocked password",
            "role": "viewer",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Insufficient team role"


def test_owner_role_changes_and_removal_revalidate_existing_sessions(auth_client):
    boot = auth_client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "owner@example.test",
            "password": "owner password",
            "name": "Owner",
            "team_name": "Acme",
        },
    )
    assert boot.status_code == 200
    owner_csrf = boot.json()["csrf_token"]
    owner_cookies = dict(auth_client.cookies)
    added = auth_client.post(
        "/api/v1/team/members",
        headers={"X-CSRF-Token": owner_csrf},
        json={
            "email": "editor@example.test",
            "name": "Editor",
            "password": "editor password",
            "role": "editor",
        },
    )
    assert added.status_code == 200, added.text
    member_id = added.json()["member"]["id"]

    member_login = auth_client.post(
        "/api/v1/auth/login",
        json={"email": "editor@example.test", "password": "editor password"},
    )
    assert member_login.status_code == 200, member_login.text
    member_csrf = member_login.json()["csrf_token"]
    member_cookies = dict(auth_client.cookies)
    assert member_login.json()["role"] == "editor"
    denied = auth_client.patch(
        f"/api/v1/team/members/{member_id}",
        headers={"X-CSRF-Token": member_csrf},
        json={"role": "viewer"},
    )
    assert denied.status_code == 403

    auth_client.cookies.clear()
    auth_client.cookies.update(owner_cookies)
    auth_client.headers["X-CSRF-Token"] = owner_csrf
    changed = auth_client.patch(
        f"/api/v1/team/members/{member_id}",
        json={"role": "viewer"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["member"]["role"] == "viewer"

    # The already-issued browser session reads its current membership rather
    # than retaining the role that was present at login time.
    auth_client.cookies.clear()
    auth_client.cookies.update(member_cookies)
    auth_client.headers["X-CSRF-Token"] = member_csrf
    refreshed = auth_client.get("/api/v1/auth/me")
    assert refreshed.status_code == 200
    assert refreshed.json()["role"] == "viewer"

    auth_client.cookies.clear()
    auth_client.cookies.update(owner_cookies)
    auth_client.headers["X-CSRF-Token"] = owner_csrf
    removed = auth_client.delete(f"/api/v1/team/members/{member_id}")
    assert removed.status_code == 200, removed.text
    auth_client.cookies.clear()
    auth_client.cookies.update(member_cookies)
    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_owner_membership_guards_and_cross_team_lookup(auth_client, database):
    boot = auth_client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "owner@example.test",
            "password": "owner password",
            "name": "Owner",
            "team_name": "Acme",
        },
    )
    assert boot.status_code == 200
    csrf = boot.json()["csrf_token"]
    owner_id = boot.json()["user"]["id"]
    self_demote = auth_client.patch(
        f"/api/v1/team/members/{owner_id}",
        headers={"X-CSRF-Token": csrf},
        json={"role": "viewer"},
    )
    assert self_demote.status_code == 409
    self_remove = auth_client.delete(
        f"/api/v1/team/members/{owner_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert self_remove.status_code == 409

    with database[1]() as db:
        other_team = Team(name="Other")
        other_user = User(
            email="other@example.test",
            name="Other",
            password_hash=auth.hash_password("other password"),
        )
        db.add_all([other_team, other_user])
        db.flush()
        db.add(Membership(team_id=other_team.id, user_id=other_user.id, role="owner"))
        db.commit()
        other_user_id = other_user.id

    cross_team_update = auth_client.patch(
        f"/api/v1/team/members/{other_user_id}",
        headers={"X-CSRF-Token": csrf},
        json={"role": "viewer"},
    )
    assert cross_team_update.status_code == 404
    cross_team_remove = auth_client.delete(
        f"/api/v1/team/members/{other_user_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert cross_team_remove.status_code == 404


def test_same_origin_is_explicit(auth_client):
    blocked = auth_client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": "https://evil.example"},
        json={
            "email": "owner@example.test",
            "password": "owner password",
            "name": "Owner",
            "team_name": "Acme",
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Cross-origin request blocked"
    allowed = auth_client.post(
        "/api/v1/auth/bootstrap",
        headers={"Referer": "http://testserver/account/settings"},
        json={
            "email": "owner@example.test",
            "password": "owner password",
            "name": "Owner",
            "team_name": "Acme",
        },
    )
    assert allowed.status_code == 200


def test_refresh_requires_enrollment_and_policy_is_append_only(database):
    _engine, factory = database
    site = _site(
        factory,
        facts={"business_name": "Acme", "services": ["consulting"]},
    )
    with factory() as db:
        persistent_site = db.get(Site, site.id)
        policy = create_policy(
            db,
            persistent_site,
            None,
            {
                "enabled": True,
                "allowed_actions": ["refresh", "publish"],
                "author_id": "wp-author-1",
            },
        )
        db.commit()
        page = Page(
            site_id=site.id,
            resource_key="post:1",
            url="https://example.test/blog/one",
            title="One",
            source_hash="hash",
            enrolled=False,
        )
        db.add(page)
        db.commit()
        assert "page_not_enrolled" in evaluate_policy(persistent_site, policy, "refresh", page)
        page.enrolled = True
        db.commit()
        assert evaluate_policy(persistent_site, policy, "refresh", page) == []

        policy.settings = {**policy.settings, "enabled": False}
        with pytest.raises(ValueError, match="append-only"):
            db.commit()
        db.rollback()


def test_policy_versions_are_serialized_on_sqlite(database):
    _engine, factory = database
    site = _site(factory)
    barrier = Barrier(2)

    def append_version() -> int:
        with factory() as db:
            persistent_site = db.get(Site, site.id)
            db.commit()
            barrier.wait()
            policy = create_policy(
                db,
                persistent_site,
                None,
                {"enabled": False, "allowed_actions": ["metadata"]},
            )
            db.commit()
            return policy.version

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = sorted(pool.map(lambda _value: append_version(), range(2)))

    assert versions == [1, 2]
    with factory() as db:
        assert db.scalar(select(Policy).where(Policy.site_id == site.id).order_by(Policy.version.desc())).version == 2


def test_policy_validation_and_site_binding_fail_closed(database):
    _engine, factory = database
    site = _site(factory)
    other_site = _site(factory)

    with factory() as db:
        persisted = db.get(Site, site.id)
        with pytest.raises(ValueError, match="unsupported policy settings"):
            create_policy(db, persisted, None, {"unknown_control": True})
        with pytest.raises(ValueError, match="monthly_budget_cents"):
            create_policy(db, persisted, None, {"monthly_budget_cents": 30_001})
        with pytest.raises(ValueError, match="posts_per_week"):
            create_policy(db, persisted, None, {"posts_per_week": 3})
        with pytest.raises(ValueError, match="refreshes_per_week"):
            create_policy(db, persisted, None, {"refreshes_per_week": 2})

        policy = create_policy(db, persisted, None, {"enabled": True, "allowed_actions": ["metadata"]})
        assert "policy_site_mismatch" in evaluate_policy(db.get(Site, other_site.id), policy, "metadata")

        malformed = Policy(
            site_id=site.id,
            version=policy.version + 1,
            settings={"enabled": True, "allowed_actions": "metadata", "protected_paths": []},
        )
        assert "policy_invalid" in evaluate_policy(persisted, malformed, "metadata")


def test_protected_paths_match_canonicalized_url_paths(database):
    _engine, factory = database
    site = _site(factory)

    with factory() as db:
        persisted = db.get(Site, site.id)
        policy = create_policy(
            db,
            persisted,
            None,
            {
                "enabled": True,
                "allowed_actions": ["metadata"],
                "protected_paths": ["/contact*"],
            },
        )

        for url in (
            "https://example.test/marketing/../contact",
            "https://example.test/%6darketing/%2e%2e/contact?source=test",
        ):
            assert "protected_path" in evaluate_policy(
                persisted,
                policy,
                "metadata",
                {"url": url},
            )


def test_budget_idempotency_overflow_and_release(database):
    _engine, factory = database
    site = _site(factory)

    with factory() as db:
        first = reserve(db, site.id, "same-operation", 80, limit_cents=100)
        again = reserve(db, site.id, "same-operation", 80, limit_cents=100)
        assert first.id == again.id

        settle(db, first.id, 130)
        account = db.scalar(select(BudgetAccount).where(BudgetAccount.site_id == site.id))
        assert account.spent_cents == 130
        assert account.reserved_cents == 0
        with pytest.raises(ValueError, match="budget exceeded"):
            reserve(db, site.id, "after-overflow", 1, limit_cents=100)

        hold = reserve(db, site.id, "release-me", 0, limit_cents=100)
        release(db, hold.id)
        assert db.get(CostReservation, hold.id).status == "released"

        with pytest.raises(ValueError):
            reserve(db, site.id, "unknown", None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            settle(db, first.id, -1)
        with pytest.raises(ValueError, match=r"\$300/site/month ceiling"):
            reserve(db, site.id, "above-platform-ceiling", 1, limit_cents=30_001)


def test_budget_limit_reduction_applies_to_an_existing_month(database):
    _engine, factory = database
    site = _site(factory)

    with factory() as db:
        reserve(db, site.id, "before-limit-reduction", 40, limit_cents=100)
        reserve(db, site.id, "after-limit-reduction", 10, limit_cents=50)

        account = db.scalar(select(BudgetAccount).where(BudgetAccount.site_id == site.id))
        assert account is not None
        assert account.limit_cents == 50
        assert account.reserved_cents == 50

        with pytest.raises(ValueError, match="monthly budget exceeded"):
            reserve(db, site.id, "blocked-by-reduced-limit", 1, limit_cents=50)


def test_budget_reservations_are_concurrent_and_atomic(database):
    _engine, factory = database
    site = _site(factory)
    barrier = Barrier(2)

    def attempt(number: int) -> str:
        with factory() as db:
            barrier.wait()
            try:
                reservation = reserve(db, site.id, f"concurrent-{number}", 60, limit_cents=100)
                return reservation.status
            except ValueError:
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, range(2)))

    assert outcomes == ["denied", "reserved"]
    with factory() as db:
        account = db.scalar(select(BudgetAccount).where(BudgetAccount.site_id == site.id))
        assert (account.reserved_cents, account.spent_cents) == (60, 0)
