"""Authentication, sessions, CSRF, and team membership APIs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from .config import settings
from .db import get_db
from .models import Membership, Session as AuthSession, Site, Team, User, utcnow

SESSION_COOKIE_NAME = "forge_session"
CSRF_COOKIE_NAME = "forge_csrf"
SESSION_TTL = timedelta(days=7)
_AUTH_FAILURE = "Invalid email or password"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BOOTSTRAP_TOKEN_HEADER = "x-forgeseo-bootstrap-token"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SCRYPT_SALT_BYTES = 16


class _AuthThrottle:
    """Small process-local failure window for login/bootstrap abuse.

    A shared rate limiter service can replace this in production. Keeping the
    fallback local makes the standalone pilot safe by default without adding a
    new persistence table to the contract.
    """

    limit = 5
    window_seconds = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[key]
            while failures and now - failures[0] >= self.window_seconds:
                failures.popleft()
            return len(failures) < self.limit

    def failed(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[key]
            while failures and now - failures[0] >= self.window_seconds:
                failures.popleft()
            failures.append(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


_throttle = _AuthThrottle()


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str
    password: str = Field(min_length=8, max_length=1024)
    name: str
    team_name: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str
    password: str = Field(min_length=1, max_length=1024)
    # Optional for backwards compatibility. When supplied, authentication is
    # only successful if the user is a member of that exact team.
    team_id: str | None = Field(default=None, min_length=1, max_length=64)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str
    name: str
    password: str = Field(min_length=8, max_length=1024)
    role: Literal["owner", "editor", "viewer"] = "viewer"


class UpdateMemberRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["owner", "editor", "viewer"]


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    return password.encode("utf-8")


def hash_password(password: str) -> str:
    """Hash a password with stdlib scrypt and a per-password random salt."""

    raw_password = _password_bytes(password)
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        raw_password,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    encode = base64.b64encode
    return "$".join(
        (
            "scrypt",
            "1",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            encode(salt).decode("ascii"),
            encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify scrypt output with constant-time digest comparison."""

    try:
        parts = encoded_hash.split("$")
        if len(parts) != 7 or parts[0] != "scrypt" or parts[1] != "1":
            return False
        n, r, p = (int(parts[2]), int(parts[3]), int(parts[4]))
        salt = base64.b64decode(parts[5], validate=True)
        expected = base64.b64decode(parts[6], validate=True)
        if n <= 1 or r <= 0 or p <= 0 or not salt or not expected:
            return False
        actual = hashlib.scrypt(
            _password_bytes(password),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, OverflowError):
        return False


# A valid fixed-cost hash makes the unknown-email branch perform the same
# password KDF as the known-user branch. It is never used as a user credential.
_DUMMY_PASSWORD_HASH = hash_password("forge-sealed-dummy-password")


def _normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise HTTPException(status_code=422, detail="A valid email is required")
    normalized = email.strip().casefold()
    if not normalized or "@" not in normalized or any(char.isspace() for char in normalized):
        raise HTTPException(status_code=422, detail="A valid email is required")
    return normalized


def _nonempty(value: str, label: str) -> str:
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise HTTPException(status_code=422, detail=f"{label} is required")
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}".lower().rstrip("/")


def _configured_origin() -> str | None:
    raw = (settings.PUBLIC_URL or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower().rstrip("/")


def _cookie_name(base_name: str) -> str:
    """Use host-only cookie prefixes when secure browser cookies are enabled."""

    return f"__Host-{base_name}" if settings.COOKIE_SECURE else base_name


def _origin_matches(value: str, allowed: set[str], *, referer: bool = False) -> bool:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if not referer and parsed.path not in {"", "/"}:
        return False
    return f"{parsed.scheme}://{parsed.netloc}".lower().rstrip("/") in allowed


def _validate_same_origin(request: Request) -> None:
    if request.method.upper() not in _UNSAFE_METHODS:
        return
    allowed = {_request_origin(request)}
    configured = _configured_origin()
    if configured:
        allowed.add(configured)

    origin = request.headers.get("origin")
    if origin is not None:
        valid = _origin_matches(origin, allowed)
    else:
        referer = request.headers.get("referer")
        valid = True if referer is None else _origin_matches(referer, allowed, referer=True)

    if not valid:
        raise HTTPException(status_code=403, detail="Cross-origin request blocked")


def _client_key(request: Request, email: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{host}|{email}"


def _throttled(key: str) -> None:
    if not _throttle.allowed(key):
        raise HTTPException(status_code=429, detail="Too many authentication attempts; try again later")


def _require_bootstrap_token(request: Request, throttle_key: str) -> None:
    """Require the deployment-only secret for the first-owner operation.

    The secret is intentionally carried in a header rather than the JSON
    payload. It is never reflected in an exception, response, or application
    log. An unset deployment fails closed so a fresh internet-facing instance
    cannot be initialized with only the public bootstrap form.
    """

    configured = settings.BOOTSTRAP_TOKEN
    if not isinstance(configured, str) or not configured:
        raise HTTPException(status_code=503, detail="Initial owner setup is not configured")

    supplied = request.headers.get(BOOTSTRAP_TOKEN_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, configured):
        _throttle.failed(throttle_key)
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")


def _new_session(db: DBSession, user_id: str, team_id: str) -> tuple[AuthSession, str, str]:
    if not isinstance(team_id, str) or not team_id.strip():
        raise ValueError("a team-bound session requires a team id")
    raw_session = secrets.token_urlsafe(48)
    raw_csrf = secrets.token_urlsafe(32)
    session = AuthSession(
        user_id=user_id,
        team_id=team_id,
        token_hash=_token_hash(raw_session),
        csrf_token=_token_hash(raw_csrf),
        expires_at=utcnow() + SESSION_TTL,
    )
    db.add(session)
    db.flush()
    return session, raw_session, raw_csrf


def _set_auth_cookies(response: Response, raw_session: str, raw_csrf: str) -> None:
    max_age = int(SESSION_TTL.total_seconds())
    response.set_cookie(
        _cookie_name(SESSION_COOKIE_NAME),
        raw_session,
        max_age=max_age,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        _cookie_name(CSRF_COOKIE_NAME),
        raw_csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _delete_auth_cookies(response: Response) -> None:
    """Expire auth cookies with the same prefix constraints used to set them."""

    secure = settings.COOKIE_SECURE
    response.delete_cookie(
        _cookie_name(SESSION_COOKIE_NAME),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        _cookie_name(CSRF_COOKIE_NAME),
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def _public_user(user: User) -> dict[str, str]:
    return {"id": user.id, "email": user.email, "name": user.name}


def _public_team(team: Team) -> dict[str, str]:
    return {"id": team.id, "name": team.name}


def _auth_payload(user: User, team: Team, membership: Membership, csrf_token: str) -> dict[str, Any]:
    return {
        "user": _public_user(user),
        "team": _public_team(team),
        "role": membership.role,
        "csrf_token": csrf_token,
    }


def _membership_for_user(
    db: DBSession,
    user_id: str,
    team_id: str | None = None,
) -> Membership | None:
    statement = select(Membership).where(Membership.user_id == user_id)
    if team_id is not None:
        statement = statement.where(Membership.team_id == team_id)
    return db.scalar(statement.order_by(Membership.id.asc()).limit(1))


def _authenticated_session(
    db: DBSession,
    request: Request,
    *,
    check_csrf: bool = True,
) -> tuple[AuthSession, User, Team, Membership]:
    _validate_same_origin(request)
    raw_session = request.cookies.get(_cookie_name(SESSION_COOKIE_NAME))
    if not raw_session:
        raise HTTPException(status_code=401, detail="Authentication required")

    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(raw_session)))
    # A session without a team binding is a pre-hardening/invalid session. Do
    # not fall back to the user's first membership: that can silently change
    # the tenant and role represented by an existing browser cookie.
    if session is None or session.expires_at <= utcnow() or not session.team_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = db.get(User, session.user_id)
    membership = _membership_for_user(db, session.user_id, session.team_id)
    if user is None or membership is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    team = db.get(Team, membership.team_id)
    if team is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    if check_csrf and request.method.upper() in _UNSAFE_METHODS:
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not hmac.compare_digest(_token_hash(supplied), session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF validation failed")

    return session, user, team, membership


def require_user(request: Request, db: DBSession = Depends(get_db)) -> dict[str, str]:
    """FastAPI dependency returning only the authenticated team context."""

    _session, _user, _team, membership = _authenticated_session(db, request)
    return {
        "user_id": membership.user_id,
        "team_id": membership.team_id,
        "role": membership.role,
    }


def require_role(context: Mapping[str, Any], *roles: str) -> dict[str, Any]:
    """Require one of the supplied team roles and return the context."""

    if len(roles) == 1 and isinstance(roles[0], (list, tuple, set, frozenset)):
        roles = tuple(roles[0])
    if not roles or context.get("role") not in roles:
        raise HTTPException(status_code=403, detail="Insufficient team role")
    return dict(context)


def require_site(db: DBSession, context: Mapping[str, Any], site_id: str) -> Site:
    """Resolve a site only inside the authenticated team.

    Cross-team access intentionally looks like a missing site to avoid leaking
    tenant existence.
    """

    site = db.scalar(select(Site).where(Site.id == site_id, Site.team_id == context.get("team_id")))
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    return site


def _bootstrap_lock(db: DBSession) -> None:
    if db.in_transaction():
        return
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    elif dialect == "postgresql":
        # There is no existing row to lock before the first user exists.
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": 0x464F52474553454F},
        )
    else:
        db.begin()


router = APIRouter(prefix="/api/v1")


@router.get("/auth/status")
def auth_status(db: DBSession = Depends(get_db)) -> dict[str, bool]:
    initialized = db.scalar(select(User.id).limit(1)) is not None
    return {"initialized": initialized}


@router.post("/auth/bootstrap")
def bootstrap(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_same_origin(request)
    email = _normalize_email(payload.email)
    name = _nonempty(payload.name, "name")
    team_name = _nonempty(payload.team_name, "team_name")
    throttle_key = _client_key(request, "bootstrap")
    _throttled(throttle_key)
    _require_bootstrap_token(request, throttle_key)

    try:
        _bootstrap_lock(db)
        if db.scalar(select(User.id).limit(1)) is not None:
            db.rollback()
            raise HTTPException(status_code=409, detail="Bootstrap already completed")

        user = User(email=email, name=name, password_hash=hash_password(payload.password))
        team = Team(name=team_name)
        db.add_all([user, team])
        db.flush()
        membership = Membership(team_id=team.id, user_id=user.id, role="owner")
        db.add(membership)
        _session, raw_session, raw_csrf = _new_session(db, user.id, membership.team_id)
        db.commit()
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Bootstrap already completed")
    except BaseException:
        db.rollback()
        raise

    _throttle.succeeded(throttle_key)
    _set_auth_cookies(response, raw_session, raw_csrf)
    return _auth_payload(user, team, membership, raw_csrf)


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_same_origin(request)
    email = _normalize_email(payload.email)
    throttle_key = _client_key(request, email)
    _throttled(throttle_key)

    user = db.scalar(select(User).where(User.email == email))
    candidate_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    valid = verify_password(payload.password, candidate_hash)
    requested_team_id = payload.team_id.strip() if payload.team_id else None
    membership = (
        _membership_for_user(db, user.id, requested_team_id)
        if user is not None and valid
        else None
    )

    if not valid or user is None or membership is None:
        _throttle.failed(throttle_key)
        raise HTTPException(status_code=401, detail=_AUTH_FAILURE)

    team = db.get(Team, membership.team_id)
    if team is None:
        _throttle.failed(throttle_key)
        raise HTTPException(status_code=401, detail=_AUTH_FAILURE)

    try:
        _session, raw_session, raw_csrf = _new_session(db, user.id, membership.team_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise

    _throttle.succeeded(throttle_key)
    _set_auth_cookies(response, raw_session, raw_csrf)
    return _auth_payload(user, team, membership, raw_csrf)


@router.get("/auth/me")
def me(
    request: Request,
    response: Response,
    context: dict[str, str] = Depends(require_user),
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    session, user, team, membership = _authenticated_session(db, request, check_csrf=False)
    raw_csrf = request.cookies.get(_cookie_name(CSRF_COOKIE_NAME), "")
    if not raw_csrf or not hmac.compare_digest(_token_hash(raw_csrf), session.csrf_token):
        raw_csrf = secrets.token_urlsafe(32)
        session.csrf_token = _token_hash(raw_csrf)
        db.commit()
        _set_auth_cookies(response, request.cookies.get(_cookie_name(SESSION_COOKIE_NAME), ""), raw_csrf)
    return _auth_payload(user, team, membership, raw_csrf)


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    context: dict[str, str] = Depends(require_user),
    db: DBSession = Depends(get_db),
) -> dict[str, bool]:
    _session, _user, _team, _membership = _authenticated_session(db, request)
    session_cookie_name = _cookie_name(SESSION_COOKIE_NAME)
    raw_session = request.cookies.get(session_cookie_name)
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(raw_session or "")))
    if session is not None:
        db.delete(session)
        db.commit()
    _delete_auth_cookies(response)
    return {"ok": True}


@router.get("/team")
def get_team(
    context: dict[str, str] = Depends(require_user),
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    team = db.get(Team, context["team_id"])
    if team is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.team_id == team.id)
        .order_by(Membership.id.asc())
    ).all()
    members = [
        {"id": user.id, "email": user.email, "name": user.name, "role": membership.role}
        for membership, user in rows
    ]
    return {"team": _public_team(team), "members": members}


@router.post("/team/members")
def add_member(
    payload: AddMemberRequest,
    request: Request,
    context: dict[str, str] = Depends(require_user),
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    require_role(context, "owner")
    email = _normalize_email(payload.email)
    name = _nonempty(payload.name, "name")

    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        if db.scalar(
            select(Membership).where(
                Membership.team_id == context["team_id"],
                Membership.user_id == existing.id,
            )
        ) is not None:
            raise HTTPException(status_code=409, detail="Member already belongs to this team")
        user = existing
    else:
        user = User(email=email, name=name, password_hash=hash_password(payload.password))
        db.add(user)
        db.flush()

    membership = Membership(team_id=context["team_id"], user_id=user.id, role=payload.role)
    db.add(membership)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Member already belongs to this team")

    return {
        "member": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": membership.role,
        }
    }


def _team_member(db: DBSession, team_id: str, user_id: str) -> Membership:
    membership = db.scalar(
        select(Membership).where(
            Membership.team_id == team_id,
            Membership.user_id == user_id,
        )
    )
    if membership is None:
        # Do not disclose whether the identifier belongs to another team.
        raise HTTPException(status_code=404, detail="Team member not found")
    return membership


def _owner_count(db: DBSession, team_id: str) -> int:
    return len(
        db.scalars(
            select(Membership).where(
                Membership.team_id == team_id,
                Membership.role == "owner",
            )
        ).all()
    )


@router.patch("/team/members/{user_id}")
def update_member(
    user_id: str,
    payload: UpdateMemberRequest,
    context: dict[str, str] = Depends(require_user),
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    """Change a member role without changing their user account.

    Membership is the authorization source of truth on every request, so a
    role change takes effect for already-issued sessions on their next call.
    The current owner cannot demote themselves, which keeps the last-owner
    invariant meaningful even for a team with only one owner.
    """

    require_role(context, "owner")
    membership = _team_member(db, context["team_id"], user_id)
    if user_id == context["user_id"] and payload.role != "owner":
        raise HTTPException(status_code=409, detail="The current owner cannot demote themselves")
    if membership.role == "owner" and payload.role != "owner" and _owner_count(db, context["team_id"]) <= 1:
        raise HTTPException(status_code=409, detail="The team must retain an owner")

    membership.role = payload.role
    db.commit()
    user = db.get(User, user_id)
    if user is None:  # pragma: no cover - protected by the membership foreign key
        raise HTTPException(status_code=404, detail="Team member not found")
    return {
        "member": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": membership.role,
        }
    }


@router.delete("/team/members/{user_id}")
def remove_member(
    user_id: str,
    context: dict[str, str] = Depends(require_user),
    db: DBSession = Depends(get_db),
) -> dict[str, Any]:
    """Remove only this team's membership and invalidate its browser sessions."""

    require_role(context, "owner")
    membership = _team_member(db, context["team_id"], user_id)
    if user_id == context["user_id"]:
        raise HTTPException(status_code=409, detail="The current owner cannot remove themselves")
    if membership.role == "owner" and _owner_count(db, context["team_id"]) <= 1:
        raise HTTPException(status_code=409, detail="The team must retain an owner")

    db.query(AuthSession).filter(
        AuthSession.user_id == user_id,
        AuthSession.team_id == context["team_id"],
    ).delete(synchronize_session=False)
    db.delete(membership)
    db.commit()
    return {"ok": True, "user_id": user_id}


__all__ = [
    "AddMemberRequest",
    "BootstrapRequest",
    "LoginRequest",
    "hash_password",
    "login",
    "me",
    "require_role",
    "require_site",
    "require_user",
    "router",
    "UpdateMemberRequest",
    "verify_password",
]
