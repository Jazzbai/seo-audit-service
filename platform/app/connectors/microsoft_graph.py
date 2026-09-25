"""Bounded Microsoft Graph mail connector with a deliberately narrow surface.

Token role inspection is diagnostic only: the access token is accepted only
from Microsoft's fixed HTTPS token endpoint, but this module does not validate
JWT signatures or claim that Graph token claims prove Exchange RBAC scope.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from app.network import PublicTransport


LOGIN_ORIGIN = "https://login.microsoftonline.com"
GRAPH_ORIGIN = "https://graph.microsoft.com"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0, read=10.0, write=10.0, pool=5.0)
_MAX_TOKEN_RESPONSE_BYTES = 32 * 1024
_MAX_ACCESS_TOKEN_CHARS = 16 * 1024
_MAX_SECRET_BYTES = 4 * 1024
_MAX_RECIPIENTS = 10
_MAX_SUBJECT_BYTES = 998
_MAX_BODY_BYTES = 512 * 1024
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_LOCAL_PART_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}\Z")
_DOMAIN_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_BROAD_ENTRA_MAIL_ROLES = frozenset({"mail.send", "mail.readwrite"})


class MicrosoftGraphError(Exception):
    """A provider error with a stable, payload-free classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        outcome_unknown: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.retry_after_seconds = retry_after_seconds


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return value


def _strict_keys(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], name: str
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        raise ValueError(f"{name} has missing or unsupported fields")


def _normalize_uuid(value: Any, name: str) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str) or _UUID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a UUID")
    return str(uuid.UUID(value))


def validate_graph_credentials(credentials: Mapping[str, Any]) -> dict[str, str]:
    """Validate and normalize the exact credentials accepted by this client.

    The returned mapping includes the secret because the parent connection
    store must encrypt it before persistence. This function never logs it.
    """

    values = _require_mapping(credentials, "credentials")
    _strict_keys(
        values,
        required={"tenant_id", "client_id", "client_secret"},
        optional=set(),
        name="credentials",
    )
    secret = values["client_secret"]
    if not isinstance(secret, str) or not secret or secret.strip() != secret:
        raise ValueError("client_secret must be a non-empty string")
    try:
        secret_bytes = secret.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("client_secret is invalid") from exc
    if len(secret_bytes) > _MAX_SECRET_BYTES or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in secret
    ):
        raise ValueError("client_secret is invalid")
    return {
        "tenant_id": _normalize_uuid(values["tenant_id"], "tenant_id"),
        "client_id": _normalize_uuid(values["client_id"], "client_id"),
        "client_secret": secret,
    }


def _normalize_email(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError(f"{name} must be a plain email address")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"{name} must be a plain email address")
    if value.count("@") != 1:
        raise ValueError(f"{name} must be a plain email address")
    local, domain = value.rsplit("@", 1)
    if (
        _LOCAL_PART_PATTERN.fullmatch(local) is None
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        raise ValueError(f"{name} must be a plain email address")
    try:
        normalized_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"{name} must be a plain email address") from exc
    labels = normalized_domain.split(".")
    if (
        len(labels) < 2
        or any(_DOMAIN_LABEL_PATTERN.fullmatch(label) is None for label in labels)
        or len(normalized_domain) > 253
    ):
        raise ValueError(f"{name} must be a plain email address")
    normalized = f"{local.lower()}@{normalized_domain}"
    if len(normalized) > 254:
        raise ValueError(f"{name} must be a plain email address")
    return normalized


def validate_graph_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the connector's exact settings and return normalized values."""

    values = _require_mapping(settings, "settings")
    _strict_keys(
        values,
        required={"sender", "recipients"},
        optional={"digest_enabled"},
        name="settings",
    )
    sender = _normalize_email(values["sender"], "sender")
    recipients = values["recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= _MAX_RECIPIENTS:
        raise ValueError("recipients must be a list of 1 to 10 email addresses")
    normalized_recipients = [
        _normalize_email(address, "recipient") for address in recipients
    ]
    if len(set(normalized_recipients)) != len(normalized_recipients):
        raise ValueError("recipients must not contain duplicates")
    digest_enabled = values.get("digest_enabled", False)
    if type(digest_enabled) is not bool:
        raise ValueError("digest_enabled must be a boolean")
    return {
        "sender": sender,
        "recipients": normalized_recipients,
        "digest_enabled": digest_enabled,
    }


class _AuthorityTransport(httpx.AsyncBaseTransport):
    """Route only the two fixed Microsoft authorities to public transports."""

    def __init__(self, injected: httpx.AsyncBaseTransport | None) -> None:
        if injected is None:
            self._by_host: dict[str, httpx.AsyncBaseTransport] = {
                "login.microsoftonline.com": PublicTransport(LOGIN_ORIGIN),
                "graph.microsoft.com": PublicTransport(GRAPH_ORIGIN),
            }
        else:
            # The supplied transport is intended for offline tests. Both
            # authorities share it, and this wrapper closes it exactly once.
            self._by_host = {
                "login.microsoftonline.com": injected,
                "graph.microsoft.com": injected,
            }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            url.scheme != "https"
            or url.port not in (None, 443)
            or url.username
            or url.password
        ):
            raise ValueError("Microsoft Graph connector authority is fixed")
        transport = self._by_host.get(url.host.lower())
        if transport is None:
            raise ValueError("Microsoft Graph connector authority is fixed")
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        closed: set[int] = set()
        for transport in self._by_host.values():
            if id(transport) not in closed:
                closed.add(id(transport))
                await transport.aclose()


class _ResponseTooLarge(Exception):
    pass


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise _ResponseTooLarge
        body.extend(chunk)
    return bytes(body)


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after")
    if value is None or re.fullmatch(r"[0-9]{1,8}", value.strip()) is None:
        return None
    return min(int(value.strip()), 86_400)


def _provider_error(
    status_code: int,
    *,
    sending: bool,
    headers: Mapping[str, str] | None = None,
) -> MicrosoftGraphError:
    if status_code in (400, 401):
        return MicrosoftGraphError(
            "authentication_failed",
            "Microsoft rejected the application credentials or access token.",
            status_code=status_code,
        )
    if status_code == 403:
        return MicrosoftGraphError(
            "forbidden",
            "Microsoft Graph denied this mailbox operation.",
            status_code=status_code,
        )
    if status_code == 429:
        return MicrosoftGraphError(
            "rate_limited",
            "Microsoft Graph rate limited the request; no automatic retry was made.",
            status_code=status_code,
            retry_after_seconds=_retry_after(headers or {}),
        )
    if 500 <= status_code <= 599:
        return MicrosoftGraphError(
            "provider_error",
            "Microsoft returned a server error; the send outcome may be unknown."
            if sending
            else "Microsoft returned a server error while obtaining an access token.",
            status_code=status_code,
            outcome_unknown=sending,
        )
    if 300 <= status_code <= 399:
        return MicrosoftGraphError(
            "redirect_rejected",
            "Microsoft returned a redirect; redirects are never followed.",
            status_code=status_code,
            outcome_unknown=sending,
        )
    return MicrosoftGraphError(
        "provider_error",
        "Microsoft rejected the request with an unexpected status.",
        status_code=status_code,
        outcome_unknown=sending and 200 <= status_code <= 299,
    )


def _role_diagnostic(access_token: str) -> str:
    """Inspect visible JWT role names without treating decoding as verification."""

    parts = access_token.split(".")
    if len(parts) != 3 or len(parts[1]) > _MAX_ACCESS_TOKEN_CHARS:
        return "unavailable"
    try:
        encoded = parts[1]
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "unavailable"
    if not isinstance(claims, dict):
        return "unavailable"
    roles = claims.get("roles", [])
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list):
        return "unavailable"
    normalized_roles = {
        role.casefold() for role in roles if isinstance(role, str)
    }
    if normalized_roles & _BROAD_ENTRA_MAIL_ROLES:
        raise MicrosoftGraphError(
            "broad_entra_mail_role_blocked",
            "The token shows a broad Entra Mail.Send or Mail.ReadWrite application role.",
        )
    return "no_broad_entra_mail_roles_detected"


class MicrosoftGraphMailClient:
    """Send text mail only from one configured mailbox to fixed recipients."""

    def __init__(
        self,
        credentials: Mapping[str, Any],
        settings: Mapping[str, Any],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_credentials = validate_graph_credentials(credentials)
        normalized_settings = validate_graph_settings(settings)
        self._tenant_id = normalized_credentials["tenant_id"]
        self._client_id = normalized_credentials["client_id"]
        self._client_secret = normalized_credentials["client_secret"]
        self.sender = normalized_settings["sender"]
        self.recipients = tuple(normalized_settings["recipients"])
        self.digest_enabled = normalized_settings["digest_enabled"]
        self.scope_review_required = True
        self._scope_diagnostic = "not_checked"
        self._access_token: str | None = None
        self._token_valid_until = 0.0
        self._transport = _AuthorityTransport(transport)
        self._http = httpx.AsyncClient(
            transport=self._transport,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "ForgeSEOPlatform/1.0",
            },
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> MicrosoftGraphMailClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _obtain_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token is not None and now < self._token_valid_until:
            return self._access_token
        token_url = (
            f"{LOGIN_ORIGIN}/{self._tenant_id}/oauth2/v2.0/token"
        )
        form = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        try:
            async with self._http.stream("POST", token_url, data=form) as response:
                if response.status_code != 200:
                    raise _provider_error(
                        response.status_code,
                        sending=False,
                        headers=response.headers,
                    )
                body = await _read_bounded(response, _MAX_TOKEN_RESPONSE_BYTES)
        except MicrosoftGraphError:
            raise
        except _ResponseTooLarge:
            raise MicrosoftGraphError(
                "invalid_response",
                "Microsoft returned an oversized token response.",
                status_code=200,
            ) from None
        except httpx.TimeoutException:
            raise MicrosoftGraphError(
                "authentication_timeout",
                "The Microsoft token request timed out; no mail was sent.",
            ) from None
        except (httpx.TransportError, ValueError):
            raise MicrosoftGraphError(
                "authentication_transport_error",
                "The Microsoft token request could not be completed; no mail was sent.",
            ) from None

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MicrosoftGraphError(
                "invalid_response", "Microsoft returned an invalid token response."
            ) from None
        if not isinstance(payload, dict):
            raise MicrosoftGraphError(
                "invalid_response", "Microsoft returned an invalid token response."
            )
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not 1 <= len(access_token) <= _MAX_ACCESS_TOKEN_CHARS
            or _TOKEN_PATTERN.fullmatch(access_token) is None
            or type(expires_in) is not int
            or expires_in <= 0
        ):
            raise MicrosoftGraphError(
                "invalid_response", "Microsoft returned an invalid token response."
            )
        diagnostic = _role_diagnostic(access_token)
        self._access_token = access_token
        self._token_valid_until = time.monotonic() + max(1, expires_in - 60)
        self._scope_diagnostic = diagnostic
        return access_token

    async def validate_connection(self) -> dict[str, Any]:
        """Verify token issuance only; this sends no message and proves no scope."""

        await self._obtain_access_token()
        return {
            "status": "authentication_verified",
            "authentication_verified": True,
            "mailbox_authorization_verified": False,
            "authorization_unverified": True,
            "scope_review_required": True,
            "scope_diagnostic": self._scope_diagnostic,
        }

    @staticmethod
    def _validate_message(subject: Any, body: Any, operation_id: Any) -> str:
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject must be a non-empty string")
        if not isinstance(body, str):
            raise ValueError("body must be a string")
        try:
            subject_bytes = subject.encode("utf-8")
            body_bytes = body.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("message contains invalid text") from exc
        if (
            len(subject_bytes) > _MAX_SUBJECT_BYTES
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in subject)
        ):
            raise ValueError("subject is invalid or too long")
        if len(body_bytes) > _MAX_BODY_BYTES or any(
            ord(char) < 32 and char not in "\t\r\n"
            or 127 <= ord(char) <= 159
            for char in body
        ):
            raise ValueError("body is invalid or too long")
        if not isinstance(operation_id, str) or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            raise ValueError("operation_id contains invalid characters")
        return str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id))

    async def send_message(
        self,
        subject: str,
        body: str,
        operation_id: str,
        *,
        before_send: Callable[[], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit one sendMail POST; a 202 means accepted, never delivered."""

        client_request_id = self._validate_message(subject, body, operation_id)
        access_token = await self._obtain_access_token()
        encoded_sender = quote(self.sender, safe="")
        send_url = f"{GRAPH_ORIGIN}/v1.0/users/{encoded_sender}/sendMail"
        message = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [
                    {"emailAddress": {"address": recipient}}
                    for recipient in self.recipients
                ],
            },
            "saveToSentItems": True,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "client-request-id": client_request_id,
            "X-ForgeSEO-operation": operation_id,
        }
        if before_send is not None:
            await before_send()
        try:
            async with self._http.stream(
                "POST", send_url, json=message, headers=headers
            ) as response:
                if response.status_code == 202:
                    return {
                        "status": "accepted",
                        "accepted": True,
                        "delivered": False,
                        "scope_review_required": True,
                        "client_request_id": client_request_id,
                    }
                if 200 <= response.status_code <= 299:
                    raise MicrosoftGraphError(
                        "unexpected_response",
                        "Microsoft returned an unexpected success status; send outcome is unknown.",
                        status_code=response.status_code,
                        outcome_unknown=True,
                    )
                raise _provider_error(
                    response.status_code,
                    sending=True,
                    headers=response.headers,
                )
        except MicrosoftGraphError:
            raise
        except httpx.TimeoutException:
            raise MicrosoftGraphError(
                "timeout_ambiguous",
                "The send request timed out; its outcome is unknown and it was not retried.",
                outcome_unknown=True,
            ) from None
        except (httpx.TransportError, ValueError):
            raise MicrosoftGraphError(
                "transport_ambiguous",
                "The send request could not be confirmed; its outcome is unknown and it was not retried.",
                outcome_unknown=True,
            ) from None


__all__ = [
    "MicrosoftGraphError",
    "MicrosoftGraphMailClient",
    "validate_graph_credentials",
    "validate_graph_settings",
]
