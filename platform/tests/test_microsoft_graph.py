from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest

from app.connectors.microsoft_graph import (
    MicrosoftGraphError,
    MicrosoftGraphMailClient,
    validate_graph_credentials,
    validate_graph_settings,
)


TENANT_ID = "11111111-2222-4333-8444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CREDS = {
    "tenant_id": TENANT_ID,
    "client_id": CLIENT_ID,
    "client_secret": "offline-test-secret",
}
SETTINGS = {
    "sender": "Reports@Example.Test",
    "recipients": ["Owner@Example.Test", "ops@example.test"],
}


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


def _token_response(token: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": token or _jwt({"roles": []}), "expires_in": 3600},
    )


def _token_url(tenant_id: str = TENANT_ID) -> str:
    return (
        "https://login.microsoftonline.com/"
        f"{tenant_id}/oauth2/v2.0/token"
    )


def _send_path() -> str:
    return "/v1.0/users/reports%40example.test/sendMail"


@pytest.mark.parametrize(
    "credentials",
    [
        {**CREDS, "tenant_id": "11111111222243338444555555555555"},
        {**CREDS, "client_secret": "secret\r\ninjected"},
        {**CREDS, "unexpected": "field"},
    ],
)
def test_graph_credentials_are_strict_and_normalized(credentials: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_graph_credentials(credentials)


def test_graph_settings_are_strict_and_normalized() -> None:
    normalized = validate_graph_settings(SETTINGS)
    assert normalized == {
        "sender": "reports@example.test",
        "recipients": ["owner@example.test", "ops@example.test"],
        "digest_enabled": False,
    }
    assert UUID(validate_graph_credentials(CREDS)["tenant_id"]) == UUID(TENANT_ID)
    with pytest.raises(ValueError):
        validate_graph_settings({**SETTINGS, "sender": "reports@example.test/../x"})
    with pytest.raises(ValueError):
        validate_graph_settings({**SETTINGS, "other": True})
    with pytest.raises(ValueError):
        validate_graph_settings({**SETTINGS, "digest_enabled": 1})


@pytest.mark.asyncio
async def test_validate_connection_gets_token_and_never_sends_mail() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == _token_url()
        form = parse_qs(request.content.decode())
        assert form["client_id"] == [CLIENT_ID]
        assert form["client_secret"] == [CREDS["client_secret"]]
        assert form["scope"] == ["https://graph.microsoft.com/.default"]
        assert form["grant_type"] == ["client_credentials"]
        return _token_response()

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        status = await client.validate_connection()

    assert len(requests) == 1
    assert status["authentication_verified"] is True
    assert status["mailbox_authorization_verified"] is False
    assert status["authorization_unverified"] is True
    assert status["scope_review_required"] is True
    assert status["scope_diagnostic"] == "no_broad_entra_mail_roles_detected"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["Mail.Send", "Mail.ReadWrite"])
async def test_broad_entra_mail_roles_are_rejected_before_send(role: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _token_response(_jwt({"roles": [role]}))

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGraphError) as caught:
            await client.send_message("Monthly report", "Hello", "job:unsafe-scope")

    assert caught.value.code == "broad_entra_mail_role_blocked"
    assert len(seen) == 1
    assert seen[0].url.host == "login.microsoftonline.com"


@pytest.mark.asyncio
async def test_mail_send_uses_only_configured_sender_and_recipients() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return _token_response()
        assert request.url.host == "graph.microsoft.com"
        assert request.url.raw_path == _send_path().encode()
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["client-request-id"]
        assert request.headers["x-forgeseo-operation"] == "digest:2026-09"
        UUID(request.headers["client-request-id"])
        return httpx.Response(202, request=request)

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        result = await client.send_message("Monthly report", "Hello", "digest:2026-09")

    assert len(requests) == 2
    payload = json.loads(requests[1].content)
    assert payload["message"]["toRecipients"] == [
        {"emailAddress": {"address": "owner@example.test"}},
        {"emailAddress": {"address": "ops@example.test"}},
    ]
    assert payload["message"]["subject"] == "Monthly report"
    assert payload["message"]["body"] == {"contentType": "Text", "content": "Hello"}
    assert payload["saveToSentItems"] is True
    assert result["status"] == "accepted"
    assert result["accepted"] is True
    assert result["delivered"] is False
    assert result["scope_review_required"] is True


@pytest.mark.asyncio
async def test_header_injection_is_rejected_before_any_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _token_response()

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await client.send_message("Subject\r\nBcc: victim@example.test", "Body", "op:1")
        with pytest.raises(ValueError):
            await client.send_message("Subject", "Body", "op\r\nX-Injected: yes")

    assert requests == []


@pytest.mark.asyncio
async def test_before_send_runs_after_token_and_blocks_post_when_revoked() -> None:
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            events.append("token")
            return _token_response()
        events.append("sendMail")
        return httpx.Response(202, request=request)

    async def revoked_authority_check() -> None:
        events.append("before_send")
        raise MicrosoftGraphError("scope_review_required", "Owner scope review is missing.")

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGraphError) as caught:
            await client.send_message(
                "Report",
                "Body",
                "job:guarded",
                before_send=revoked_authority_check,
            )

    assert events == ["token", "before_send"]
    assert caught.value.code == "scope_review_required"


@pytest.mark.asyncio
async def test_before_send_runs_immediately_before_single_graph_post() -> None:
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            events.append("token")
            return _token_response()
        events.append("sendMail")
        return httpx.Response(202, request=request)

    async def authority_check() -> None:
        events.append("before_send")

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        result = await client.send_message(
            "Report", "Body", "job:before-send", before_send=authority_check
        )

    assert events == ["token", "before_send", "sendMail"]
    assert result["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "authentication_failed"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (503, "provider_error"),
    ],
)
async def test_graph_statuses_have_bounded_classification(
    status_code: int, expected_code: str
) -> None:
    graph_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal graph_requests
        if request.url.host == "login.microsoftonline.com":
            return _token_response()
        graph_requests += 1
        return httpx.Response(
            status_code,
            headers={"Retry-After": "15", "Location": "https://evil.example/"},
            content=b'{"error":"private provider details"}',
            request=request,
        )

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGraphError) as caught:
            await client.send_message("Report", "Body", "operation-1")

    assert graph_requests == 1
    assert caught.value.code == expected_code
    assert "private provider details" not in str(caught.value)
    assert "offline-test-secret" not in str(caught.value)
    if status_code == 429:
        assert caught.value.retry_after_seconds == 15
        assert caught.value.outcome_unknown is False
    if status_code == 503:
        assert caught.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_send_timeout_is_ambiguous_and_never_retried() -> None:
    token_requests = 0
    send_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, send_requests
        if request.url.host == "login.microsoftonline.com":
            token_requests += 1
            return _token_response()
        send_requests += 1
        raise httpx.ReadTimeout("private response text", request=request)

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGraphError) as caught:
            await client.send_message("Report", "Body", "timeout:1")

    assert token_requests == 1
    assert send_requests == 1
    assert caught.value.code == "timeout_ambiguous"
    assert caught.value.outcome_unknown is True
    assert "private response text" not in str(caught.value)


@pytest.mark.asyncio
async def test_redirect_is_rejected_without_following_location() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return _token_response()
        return httpx.Response(
            307,
            headers={"Location": "https://evil.example/collect"},
            request=request,
        )

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGraphError) as caught:
            await client.send_message("Report", "Body", "redirect:1")

    assert [request.url.host for request in requests] == [
        "login.microsoftonline.com",
        "graph.microsoft.com",
    ]
    assert caught.value.code == "redirect_rejected"
    assert caught.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_oversized_token_response_is_rejected_with_no_payload_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (32 * 1024 + 1),
            request=request,
        )

    async with MicrosoftGraphMailClient(CREDS, SETTINGS, httpx.MockTransport(handler)) as client:
        with pytest.raises(MicrosoftGraphError) as caught:
            await client.validate_connection()

    assert caught.value.code == "invalid_response"
    assert "xxxx" not in str(caught.value)
