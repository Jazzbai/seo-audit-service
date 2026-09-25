from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import worker
from app.config import settings
from app.connectors.microsoft_graph import MicrosoftGraphError
from app.models import Connection, Job, Membership, Site
from app.operations import now
from app.main import app
from test_platform import platform


TENANT_ID = "11111111-2222-4333-8444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CLIENT_SECRET = "offline-graph-secret-fixture"
GRAPH_SETTINGS = {
    "sender": "reports@example.test",
    "recipients": ["owner@example.test"],
}
SCOPE_REVIEW = {
    "confirms_mailbox_scoped": True,
    "confirms_no_unscoped_send": True,
    "evidence": "Exchange application access policy restricts this app to the report mailbox.",
}


@pytest.fixture
def graph_client(monkeypatch):
    """Replace every Graph operation with an in-process, controllable client."""
    from app.connectors import microsoft_graph

    calls = {
        "constructed": 0,
        "validations": 0,
        "send_calls": 0,
        "post_attempts": 0,
        "outcome": "accepted",
    }

    class FakeMicrosoftGraphMailClient:
        def __init__(self, credentials, config):
            calls["constructed"] += 1
            self.credentials = credentials
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def validate_connection(self):
            calls["validations"] += 1
            return {
                "status": "authentication_verified",
                "authentication_verified": True,
                "mailbox_authorization_verified": False,
                "authorization_unverified": True,
                "scope_review_required": True,
            }

        async def send_message(self, subject, body, operation_id, *, before_send=None):
            calls["send_calls"] += 1
            if before_send is not None:
                await before_send()
            calls["post_attempts"] += 1
            if calls["outcome"] == "timeout":
                raise MicrosoftGraphError(
                    "timeout_ambiguous",
                    "Offline fixture timed out after the send checkpoint.",
                    outcome_unknown=True,
                )
            if calls["outcome"] == "rejected":
                raise MicrosoftGraphError(
                    "permission_denied",
                    "Offline fixture received an explicit rejection.",
                    status_code=403,
                )
            return {"status": "accepted", "accepted": True, "delivered": False}

    monkeypatch.setattr(microsoft_graph, "MicrosoftGraphMailClient", FakeMicrosoftGraphMailClient)
    return calls


def _save_connection(client, site_id: str, *, settings_payload: dict | None = None):
    return client.put(
        f"/api/v1/sites/{site_id}/connections/microsoft_graph",
        json={
            "credentials": {
                "tenant_id": TENANT_ID,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            "settings": settings_payload if settings_payload is not None else GRAPH_SETTINGS,
        },
    )


def _connect_and_test(client, factory, site_id: str) -> str:
    saved = _save_connection(client, site_id)
    assert saved.status_code == 200, saved.text
    queued = client.post(f"/api/v1/sites/{site_id}/connections/microsoft_graph/test")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    worker.run_job(job_id)
    with factory() as db:
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == "microsoft_graph",
        ))
        assert connection.status == "connected"
        assert connection.capabilities["last_connection_test"]["delivery"] == "not_tested"
    return job_id


def _record_scope_review(client, site_id: str):
    return client.post(
        f"/api/v1/sites/{site_id}/connections/microsoft_graph/scope-review",
        json=SCOPE_REVIEW,
    )


def _queue_notification_test(client, site_id: str, *, key: str = "one-time-test-01"):
    return client.post(
        f"/api/v1/sites/{site_id}/notifications/test",
        json={"kind": "microsoft_graph", "idempotency_key": key, "confirm_send": True},
    )


def _ready_notification(platform, graph_client):
    client, factory, site_id = platform
    _connect_and_test(client, factory, site_id)
    reviewed = _record_scope_review(client, site_id)
    assert reviewed.status_code == 200, reviewed.text
    queued = _queue_notification_test(client, site_id)
    assert queued.status_code == 202, queued.text
    return client, factory, site_id, queued.json()["id"]


@contextmanager
def _authenticated_member(owner_client, role: str):
    email = f"{role}@example.test"
    password = "A-long-test-passphrase!54"
    created = owner_client.post(
        "/api/v1/team/members",
        json={"email": email, "name": f"{role.title()} User", "password": password, "role": role},
    )
    assert created.status_code == 200, created.text
    with TestClient(app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://testserver"},
            json={"email": email, "password": password},
        )
        assert login.status_code == 200, login.text
        member_client.headers.update({
            "Origin": "http://testserver",
            "X-CSRF-Token": login.json()["csrf_token"],
        })
        yield member_client


def test_graph_credentials_are_encrypted_and_never_read_back(platform, graph_client):
    client, factory, site_id = platform

    saved = _save_connection(client, site_id)

    assert saved.status_code == 200, saved.text
    assert CLIENT_SECRET not in saved.text
    assert "client_secret" not in saved.json()
    with factory() as db:
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == "microsoft_graph",
        ))
        assert CLIENT_SECRET not in connection.encrypted_credentials
        assert connection.encrypted_credentials
        from app.connectors.security import decrypt_credentials

        assert decrypt_credentials(connection.encrypted_credentials, settings.ENCRYPTION_KEY)["client_secret"] == CLIENT_SECRET
    listed = client.get(f"/api/v1/sites/{site_id}/connections")
    assert listed.status_code == 200, listed.text
    assert CLIENT_SECRET not in listed.text
    assert graph_client["validations"] == 0
    assert graph_client["send_calls"] == 0


def test_connection_test_verifies_authentication_and_sends_nothing(platform, graph_client):
    client, factory, site_id = platform

    job_id = _connect_and_test(client, factory, site_id)

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "complete"
        assert job.result["delivery"] == "not_tested"
    assert graph_client["validations"] == 1
    assert graph_client["send_calls"] == 0
    assert graph_client["post_attempts"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {**SCOPE_REVIEW, "confirms_mailbox_scoped": False},
        {**SCOPE_REVIEW, "confirms_no_unscoped_send": False},
        {**SCOPE_REVIEW, "evidence": "Too short"},
        {**SCOPE_REVIEW, "confirms_mailbox_scoped": "true"},
    ],
)
def test_owner_scope_review_requires_two_strict_confirmations_and_evidence(
    platform, graph_client, payload
):
    client, factory, site_id = platform
    _connect_and_test(client, factory, site_id)

    rejected = client.post(
        f"/api/v1/sites/{site_id}/connections/microsoft_graph/scope-review",
        json=payload,
    )
    accepted = _record_scope_review(client, site_id)

    assert rejected.status_code == 422
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["capabilities"]["scope_review"]["kind"] == "owner_attested_exchange_rbac"


def test_explicit_test_is_owner_approved_one_time_and_idempotent(platform, graph_client):
    client, factory, site_id = platform
    _connect_and_test(client, factory, site_id)
    assert _record_scope_review(client, site_id).status_code == 200

    no_confirmation = client.post(
        f"/api/v1/sites/{site_id}/notifications/test",
        json={"kind": "microsoft_graph", "idempotency_key": "no-confirm-01", "confirm_send": False},
    )
    unsupported = client.post(
        f"/api/v1/sites/{site_id}/notifications/test",
        json={"kind": "smtp", "idempotency_key": "wrong-kind-01", "confirm_send": True},
    )
    first = _queue_notification_test(client, site_id)
    duplicate = _queue_notification_test(client, site_id)
    assert no_confirmation.status_code == 422
    assert unsupported.status_code == 422
    assert first.status_code == duplicate.status_code == 202
    job_id = first.json()["id"]
    assert duplicate.json()["id"] == job_id

    worker.run_job(job_id)
    worker.run_job(job_id)
    replay = _queue_notification_test(client, site_id)

    assert replay.status_code == 202
    assert replay.json()["id"] == job_id
    assert graph_client["send_calls"] == 1
    assert graph_client["post_attempts"] == 1
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "complete"
        assert job.result["status"] == "accepted"


def test_generic_job_cannot_forge_notification_test(platform, graph_client):
    client, factory, site_id = platform
    _save_connection(client, site_id)

    forged = client.post(
        f"/api/v1/sites/{site_id}/jobs",
        json={"kind": "notification_test", "payload": {"kind": "microsoft_graph"}},
    )
    connection_test = client.post(f"/api/v1/sites/{site_id}/connections/microsoft_graph/test")
    assert connection_test.status_code == 202, connection_test.text
    worker.run_job(connection_test.json()["id"])

    assert forged.status_code == 422
    assert graph_client["validations"] == 1
    assert graph_client["send_calls"] == 0
    assert graph_client["post_attempts"] == 0


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_editor_and_viewer_cannot_review_scope_or_approve_test(platform, graph_client, role):
    client, _factory, site_id = platform
    _connect_and_test(client, _factory, site_id)
    assert _record_scope_review(client, site_id).status_code == 200

    with _authenticated_member(client, role) as member_client:
        scope = member_client.post(
            f"/api/v1/sites/{site_id}/connections/microsoft_graph/scope-review",
            json=SCOPE_REVIEW,
        )
        test = _queue_notification_test(member_client, site_id, key=f"{role}-attempt-01")

    assert scope.status_code == 403
    assert test.status_code == 403
    assert graph_client["post_attempts"] == 0


@pytest.mark.parametrize("change", ["scope", "configuration", "revocation", "owner"])
def test_queued_notification_rechecks_scope_configuration_revocation_and_owner(
    platform, graph_client, change
):
    client, factory, site_id, job_id = _ready_notification(platform, graph_client)
    with factory() as db:
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == "microsoft_graph",
        ))
        capabilities = dict(connection.capabilities or {})
        if change == "scope":
            capabilities.pop("scope_review", None)
            connection.capabilities = capabilities
        elif change == "configuration":
            capabilities["settings"] = {**capabilities["settings"], "sender": "changed@example.test"}
            connection.capabilities = capabilities
        elif change == "revocation":
            connection.status = "revoked"
            connection.encrypted_credentials = ""
        else:
            job = db.get(Job, job_id)
            membership = db.scalar(select(Membership).where(
                Membership.team_id == db.get(Site, site_id).team_id,
                Membership.user_id == job.payload["approved_by"],
            ))
            membership.role = "editor"
        db.commit()

    worker.run_job(job_id)

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.result["status"] == "blocked"
        assert job.result["complete"] is False
        assert job.result["retryable"] is False
    assert graph_client["post_attempts"] == 0


def test_scope_is_checked_again_immediately_before_graph_send(platform, graph_client, monkeypatch):
    client, _factory, site_id, job_id = _ready_notification(platform, graph_client)
    from app import graph_notifications

    original = graph_notifications.require_scope_review
    checks = 0

    def remove_scope_before_second_check(db, site, connection):
        nonlocal checks
        checks += 1
        if checks == 2:
            connection.capabilities = {
                **(connection.capabilities or {}),
                "scope_review": {},
            }
            db.flush()
        return original(db, site, connection)

    monkeypatch.setattr(graph_notifications, "require_scope_review", remove_scope_before_second_check)
    worker.run_job(job_id)

    with _factory() as db:
        job = db.get(Job, job_id)
        assert job.result["status"] == "blocked"
    assert checks == 2
    assert graph_client["send_calls"] == 1
    assert graph_client["post_attempts"] == 0


def test_graph_acceptance_is_not_receipt_and_receipt_requires_owner(platform, graph_client):
    client, factory, site_id, job_id = _ready_notification(platform, graph_client)
    receipt_url = f"/api/v1/sites/{site_id}/notifications/{job_id}/receipt"
    receipt_payload = {"confirms_received": True, "notes": "Recipient confirmed the test arrived."}

    too_early = client.post(receipt_url, json=receipt_payload)
    assert too_early.status_code == 409
    worker.run_job(job_id)
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "complete"
        assert job.result["status"] == "accepted"
        assert job.result["recipient_confirmation"] == "pending"
        assert "delivered" not in job.result

    with _authenticated_member(client, "editor") as editor_client:
        forbidden = editor_client.post(receipt_url, json=receipt_payload)
    confirmed = client.post(receipt_url, json=receipt_payload)

    assert forbidden.status_code == 403
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["result"]["recipient_confirmation"] == "confirmed"
    assert confirmed.json()["result"]["receipt"]["kind"] == "owner_confirmed_recipient_receipt"


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [("timeout", "outcome_unknown"), ("rejected", "failed")],
)
def test_send_failures_are_nonretryable_and_timeout_is_never_blindly_retried(
    platform, graph_client, outcome, expected_status
):
    client, factory, site_id, job_id = _ready_notification(platform, graph_client)
    graph_client["outcome"] = outcome

    worker.run_job(job_id)

    with factory() as db:
        site = db.get(Site, site_id)
        job = db.get(Job, job_id)
        assert job.result["status"] == expected_status
        assert job.result["complete"] is False
        assert job.result["retryable"] is False
        previous = dict(job.result)
        asyncio.run(graph_notifications_call(db, site, job))
        assert job.result["status"] == expected_status
        assert previous["status"] == job.result["status"]

    assert graph_client["send_calls"] == 1
    assert graph_client["post_attempts"] == 1


async def graph_notifications_call(db, site, job):
    from app.graph_notifications import notification_test

    return await notification_test(db, site, job)


def test_graph_digest_is_disabled_by_default(platform, graph_client):
    client, factory, site_id = platform
    saved = _save_connection(client, site_id)
    assert saved.status_code == 200, saved.text
    assert saved.json()["capabilities"]["settings"]["digest_enabled"] is False
    test = client.post(f"/api/v1/sites/{site_id}/connections/microsoft_graph/test")
    assert test.status_code == 202, test.text
    worker.run_job(test.json()["id"])

    with factory() as db:
        db.add(Job(
            site_id=site_id,
            kind="digest",
            payload={},
            idempotency_key=f"{site_id}:offline-digest-default",
            available_at=now(),
        ))
        db.commit()
        digest_job_id = db.scalar(select(Job.id).where(
            Job.site_id == site_id,
            Job.kind == "digest",
        ))
    worker.run_job(digest_job_id)

    with factory() as db:
        digest_job = db.get(Job, digest_job_id)
        assert digest_job.result["email"] == "disabled"
        assert digest_job.result["channel"] == "microsoft_graph"
    assert graph_client["post_attempts"] == 0
