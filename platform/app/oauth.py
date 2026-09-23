"""Bounded Google OAuth flows for site-scoped Search Console and GA4 access.

The flow deliberately keeps provider tokens inside the existing encrypted
connection record.  The browser only receives a short-lived authenticated
state value and a same-origin result redirect; no provider token is returned
in an API response or redirect URL.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import _authenticated_session, require_role, require_site
from app.config import settings
from app.connectors.security import decrypt_credentials, encrypt_credentials
from app.db import get_db
from app.models import Connection, Site, utcnow
from app.network import PublicTransport
from app.operations import event, find_connection


router = APIRouter(prefix="/api/v1")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALLBACK_PATH = "/api/v1/oauth/google/callback"
OAUTH_STATE_TTL_SECONDS = 10 * 60
_OAUTH_PENDING_STATES_KEY = "_forgeseo_oauth_pending_states"
_MAX_PENDING_OAUTH_STATES = 8

GOOGLE_SCOPES = {
    "gsc": "https://www.googleapis.com/auth/webmasters.readonly",
    "ga4": "https://www.googleapis.com/auth/analytics.readonly",
}


class OAuthExchangeError(RuntimeError):
    """A provider exchange failed without retaining provider response data."""


def _assert_kind(kind: str) -> str:
    if kind not in GOOGLE_SCOPES:
        raise HTTPException(
            status_code=422,
            detail="Google OAuth is supported only for Search Console (gsc) and GA4 (ga4)",
        )
    return kind


def _owner_context(request: Request, db: Session) -> tuple[Any, Any, Any, Any, dict[str, str]]:
    session, user, team, membership = _authenticated_session(
        db, request, check_csrf=False
    )
    context = {
        "user_id": membership.user_id,
        "team_id": membership.team_id,
        "role": membership.role,
    }
    require_role(context, "owner")
    return session, user, team, membership, context


def _public_origin() -> str:
    """Return the fixed configured browser origin, or fail closed."""

    raw = (settings.PUBLIC_URL or "").strip()
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        _ = parsed.port  # Accessing it validates malformed ports.
    except (TypeError, ValueError):
        hostname = None
        parsed = None

    if (
        parsed is None
        or parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in raw)
    ):
        raise HTTPException(
            status_code=503,
            detail="PUBLIC_URL must be a valid HTTP(S) origin before Google OAuth can be used",
        )
    return f"{parsed.scheme.lower()}://{parsed.netloc}".rstrip("/")


def callback_url() -> str:
    """Return the only redirect URI emitted to Google."""

    return f"{_public_origin()}{GOOGLE_CALLBACK_PATH}"


def _connection_credentials(row: Connection | None) -> dict[str, Any]:
    if row is None or row.status == "revoked" or not row.encrypted_credentials:
        return {}
    try:
        payload = decrypt_credentials(row.encrypted_credentials, settings.ENCRYPTION_KEY)
    except Exception as exc:  # Do not expose ciphertext or key errors to a browser.
        raise HTTPException(
            status_code=503,
            detail="The saved Google connection credentials could not be read",
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _required_client_credentials(row: Connection | None) -> tuple[dict[str, Any], str, str]:
    credentials = _connection_credentials(row)
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    if (
        not isinstance(client_id, str)
        or not client_id.strip()
        or not isinstance(client_secret, str)
        or not client_secret.strip()
    ):
        raise HTTPException(
            status_code=409,
            detail="Save the Google OAuth client ID and client secret before connecting",
        )
    return credentials, client_id, client_secret


def _state_payload(*, session: Any, user_id: str, team_id: str, site_id: str, kind: str) -> dict[str, Any]:
    issued_at = int(time.time())
    return {
        "session_id": session.id,
        "user_id": user_id,
        "team_id": team_id,
        "site_id": site_id,
        "kind": kind,
        "issued_at": issued_at,
        "expires_at": issued_at + OAUTH_STATE_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(18),
    }


def _encode_state(payload: dict[str, Any]) -> str:
    payload = {
        **payload,
    }
    try:
        return encrypt_credentials(payload, settings.ENCRYPTION_KEY)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="OAuth state signing is not configured correctly",
        ) from exc


def _signed_state(*, session: Any, user_id: str, team_id: str, site_id: str, kind: str) -> str:
    return _encode_state(
        _state_payload(
            session=session,
            user_id=user_id,
            team_id=team_id,
            site_id=site_id,
            kind=kind,
        )
    )


def _state_fingerprint(nonce: str) -> str:
    configured_key = settings.ENCRYPTION_KEY
    if isinstance(configured_key, bytes):
        key = configured_key
    else:
        key = str(configured_key).encode("utf-8")
    return hmac.new(key, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _pending_state_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": _state_fingerprint(str(payload["nonce"])),
        "session_id": str(payload["session_id"]),
        "user_id": str(payload["user_id"]),
        "team_id": str(payload["team_id"]),
        "site_id": str(payload["site_id"]),
        "kind": str(payload["kind"]),
        "expires_at": int(payload["expires_at"]),
    }


def _pending_state_is_well_formed(value: Any, *, now: int) -> bool:
    if not isinstance(value, dict):
        return False
    if not all(isinstance(value.get(key), str) and value[key].strip() for key in (
        "fingerprint", "session_id", "user_id", "team_id", "site_id", "kind",
    )):
        return False
    expires_at = value.get("expires_at")
    return (
        isinstance(expires_at, int)
        and not isinstance(expires_at, bool)
        and expires_at > now
    )


def _remember_pending_state(
    db: Session,
    row: Connection,
    payload: dict[str, Any],
) -> tuple[Connection, dict[str, Any], str]:
    """Persist a bounded one-time marker without exposing it in the UI."""

    locked = db.scalar(
        select(Connection).where(Connection.id == row.id).with_for_update()
    )
    if locked is None:
        raise HTTPException(status_code=409, detail="The Google connection is no longer available")
    credentials, client_id, _client_secret = _required_client_credentials(locked)
    now = int(time.time())
    existing = credentials.get(_OAUTH_PENDING_STATES_KEY)
    pending = [
        value for value in existing
        if _pending_state_is_well_formed(value, now=now)
    ] if isinstance(existing, list) else []
    pending.append(_pending_state_record(payload))
    credentials[_OAUTH_PENDING_STATES_KEY] = pending[-_MAX_PENDING_OAUTH_STATES:]
    try:
        locked.encrypted_credentials = encrypt_credentials(credentials, settings.ENCRYPTION_KEY)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="The Google OAuth state could not be stored securely",
        ) from exc
    db.commit()
    return locked, credentials, client_id


def _consume_pending_state(
    db: Session,
    row: Connection | None,
    payload: dict[str, Any],
) -> tuple[Connection, dict[str, Any], str, str]:
    """Atomically consume a callback marker before contacting Google."""

    if row is None:
        raise HTTPException(status_code=400, detail="OAuth state has already been used or is no longer valid")
    locked = db.scalar(
        select(Connection).where(Connection.id == row.id).with_for_update()
    )
    if locked is None:
        raise HTTPException(status_code=400, detail="OAuth state has already been used or is no longer valid")
    credentials, client_id, client_secret = _required_client_credentials(locked)
    entries = credentials.get(_OAUTH_PENDING_STATES_KEY)
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="OAuth state has already been used or is no longer valid")

    expected_fingerprint = _state_fingerprint(str(payload["nonce"]))
    now = int(time.time())
    match_index: int | None = None
    for index, value in enumerate(entries):
        if not _pending_state_is_well_formed(value, now=now):
            continue
        if not hmac.compare_digest(value["fingerprint"], expected_fingerprint):
            continue
        if any(value.get(key) != payload.get(key) for key in (
            "session_id", "user_id", "team_id", "site_id", "kind",
        )):
            continue
        match_index = index
        break
    if match_index is None:
        raise HTTPException(status_code=400, detail="OAuth state has already been used or is no longer valid")

    remaining = [
        value for index, value in enumerate(entries)
        if index != match_index and _pending_state_is_well_formed(value, now=now)
    ]
    if remaining:
        credentials[_OAUTH_PENDING_STATES_KEY] = remaining
    else:
        credentials.pop(_OAUTH_PENDING_STATES_KEY, None)
    try:
        locked.encrypted_credentials = encrypt_credentials(credentials, settings.ENCRYPTION_KEY)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="The Google OAuth state could not be consumed securely",
        ) from exc
    db.commit()
    return locked, credentials, client_id, client_secret


def _validated_state(state: str) -> dict[str, Any]:
    if not isinstance(state, str) or not state or len(state) > 4096:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    try:
        payload = decrypt_credentials(state, settings.ENCRYPTION_KEY)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from exc

    required = ("session_id", "user_id", "team_id", "site_id", "kind", "expires_at", "nonce")
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not str(payload.get(key)).strip()
        for key in required[:-2]
    ) or not isinstance(payload.get("nonce"), str) or not payload["nonce"].strip():
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    if float(expires_at) <= time.time():
        raise HTTPException(status_code=400, detail="OAuth state has expired")
    if payload.get("kind") not in GOOGLE_SCOPES:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    return payload


def _settings_redirect(site_id: str, *, kind: str, result: str, reason: str | None = None) -> str:
    origin = _public_origin()
    query: dict[str, str] = {"oauth": result, "kind": kind}
    if reason:
        query["reason"] = reason
    return (
        f"{origin}/sites/{quote(site_id, safe='')}/settings/connections?"
        f"{urlencode(query)}"
    )


def _redirect_result(site_id: str, *, kind: str, result: str, reason: str | None = None):
    response = RedirectResponse(
        _settings_redirect(site_id, kind=kind, result=result, reason=reason),
        status_code=303,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def exchange_google_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code through Google's fixed token endpoint.

    ``transport`` is intentionally injectable for isolated tests. Production
    requests use the DNS-pinned public transport and never follow redirects.
    """

    if not isinstance(code, str) or not code.strip():
        raise OAuthExchangeError("Authorization code was not supplied")
    request_transport = transport or PublicTransport(GOOGLE_TOKEN_URL)
    try:
        async with httpx.AsyncClient(
            transport=request_transport,
            trust_env=False,
            timeout=20,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "ForgeSEOPlatform/1.0"},
        ) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        raise OAuthExchangeError("Google token exchange failed") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise OAuthExchangeError("Google token exchange was not successful")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthExchangeError("Google returned an invalid token response") from exc
    if not isinstance(payload, dict):
        raise OAuthExchangeError("Google returned an invalid token response")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthExchangeError("Google token response did not include an access token")
    return payload


def _expires_at(payload: dict[str, Any], existing: Any = None) -> str | None:
    expires_in = payload.get("expires_in")
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError, OverflowError):
        seconds = 0
    if seconds < 0:
        seconds = 0
    if seconds:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    return existing if isinstance(existing, str) and existing else None


def _persist_token(
    db: Session,
    site: Site,
    row: Connection,
    existing: dict[str, Any],
    token_payload: dict[str, Any],
    kind: str,
) -> None:
    # Only values from Google's validated token response are copied. Existing
    # client credentials and the manual refresh-token entry remain encrypted.
    updated = dict(existing)
    updated["access_token"] = token_payload["access_token"]
    returned_refresh = token_payload.get("refresh_token")
    if isinstance(returned_refresh, str) and returned_refresh:
        updated["refresh_token"] = returned_refresh
    elif isinstance(existing.get("refresh_token"), str) and existing["refresh_token"]:
        # Google commonly omits refresh_token on subsequent consent grants;
        # never replace a usable stored token with malformed provider JSON.
        updated["refresh_token"] = existing["refresh_token"]
    else:
        updated.pop("refresh_token", None)
    returned_type = token_payload.get("token_type")
    if isinstance(returned_type, str) and returned_type:
        updated["token_type"] = returned_type
    elif not isinstance(existing.get("token_type"), str) or not existing["token_type"]:
        updated["token_type"] = "Bearer"
    updated["expires_at"] = _expires_at(token_payload, existing.get("expires_at"))
    try:
        row.encrypted_credentials = encrypt_credentials(updated, settings.ENCRYPTION_KEY)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="The Google token could not be stored securely",
        ) from exc
    row.status = "connected"
    row.checked_at = utcnow()
    event(db, site, "oauth_connected", f"{kind} Google OAuth connected", {"kind": kind})
    db.commit()


async def _callback(
    request: Request,
    db: Session,
    *,
    callback_site_id: str | None = None,
    callback_kind: str | None = None,
):
    # Authenticate before decoding state so a callback copied to another
    # browser cannot be used, even if the state was valid in its original one.
    session, user, _team, membership = _authenticated_session(
        db, request, check_csrf=False
    )
    context = {
        "user_id": membership.user_id,
        "team_id": membership.team_id,
        "role": membership.role,
    }
    require_role(context, "owner")
    state = _validated_state(request.query_params.get("state", ""))
    kind = _assert_kind(str(state["kind"]))
    if callback_site_id is not None and state["site_id"] != callback_site_id:
        raise HTTPException(status_code=400, detail="OAuth state does not match the requested site")
    if callback_kind is not None and state["kind"] != callback_kind:
        raise HTTPException(status_code=400, detail="OAuth state does not match the requested connection")
    if (
        state["session_id"] != session.id
        or state["user_id"] != user.id
        or state["team_id"] != membership.team_id
    ):
        raise HTTPException(status_code=400, detail="OAuth state does not match this session")

    site = require_site(db, context, str(state["site_id"]))
    row = find_connection(db, site.id, kind)
    locked_row, existing, client_id, client_secret = _consume_pending_state(
        db, row, state
    )
    redirect_uri = callback_url()

    provider_error = request.query_params.get("error")
    if provider_error:
        return _redirect_result(site.id, kind=kind, result="error", reason="authorization_denied")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Google did not return an authorization code")

    try:
        token_payload = await exchange_google_code(
            code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    except OAuthExchangeError:
        return _redirect_result(site.id, kind=kind, result="error", reason="token_exchange_failed")

    _persist_token(db, site, locked_row, existing, token_payload, kind)
    return _redirect_result(site.id, kind=kind, result="connected")


@router.get("/sites/{site_id}/connections/{kind}/oauth/start")
def oauth_start(
    site_id: str,
    kind: str,
    request: Request,
    db: Session = Depends(get_db),
):
    session, user, _team, membership, context = _owner_context(request, db)
    kind = _assert_kind(kind)
    site = require_site(db, context, site_id)
    row = find_connection(db, site.id, kind)
    _required_client_credentials(row)
    redirect_uri = callback_url()
    state_payload = _state_payload(
        session=session,
        user_id=user.id,
        team_id=membership.team_id,
        site_id=site.id,
        kind=kind,
    )
    state = _encode_state(state_payload)
    _locked_row, _credentials, client_id = _remember_pending_state(
        db, row, state_payload
    )
    location = f"{GOOGLE_AUTH_URL}?{urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': GOOGLE_SCOPES[kind],
        'state': state,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
    })}"
    response = RedirectResponse(location, status_code=307)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/oauth/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    return await _callback(request, db)


@router.get("/sites/{site_id}/connections/{kind}/oauth/callback")
async def site_google_callback(
    site_id: str,
    kind: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _assert_kind(kind)
    return await _callback(
        request,
        db,
        callback_site_id=site_id,
        callback_kind=kind,
    )


__all__ = [
    "GOOGLE_AUTH_URL",
    "GOOGLE_CALLBACK_PATH",
    "GOOGLE_SCOPES",
    "GOOGLE_TOKEN_URL",
    "OAuthExchangeError",
    "callback_url",
    "exchange_google_code",
    "router",
]
