"""Browser-facing, team-scoped platform API."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_role, require_site, require_user
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import (Article, BudgetAccount, Candidate, Connection, Event, Finding,
                        Heartbeat, Incident, Job, Measurement, Page, Publication, Revision, Site)
from app.models import CostReservation, Team
from app.operations import (connection_view, enqueue, enqueue_article_publish, event, find_connection, global_controls,
                            iso, monitoring_status, now, record)
from app.policies import DEFAULT_POLICY, create_policy, current_policy

router = APIRouter(prefix="/api/v1")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteInput(Input):
    name: str = Field(min_length=1, max_length=120)
    origin: str = Field(max_length=2048)
    timezone: str = "America/Chicago"
    language: str = "en"
    facts: dict = Field(default_factory=dict)


class SitePatch(Input):
    name: str | None = Field(None, min_length=1, max_length=120)
    timezone: str | None = None
    language: str | None = None
    facts: dict | None = None
    paused: bool | None = None


class ConnectionInput(Input):
    credentials: dict[str, Any]
    settings: dict = Field(default_factory=dict)


_GA4_LIST_SETTING_LIMITS = {
    'conversion_event_names': (12, 40),
    'dimensions': (8, 64),
    'metrics': (10, 64),
}


def _validated_ga4_settings(settings_payload: dict) -> dict:
    """Normalize the small, non-secret GA4 report configuration surface.

    These settings select what the read-only Analytics Data API reports. They
    never contain credentials and never authorize event creation or mutation.
    Rejecting malformed values at the API boundary keeps direct API callers
    subject to the same bounds as the browser UI.
    """

    normalized = dict(settings_payload)
    if 'property_id' in normalized:
        property_id = normalized['property_id']
        if isinstance(property_id, bool) or not isinstance(property_id, (str, int)):
            raise HTTPException(422, 'GA4 property_id must be a short text value')
        property_id = str(property_id).strip()
        if not property_id or len(property_id) > 64 or any(ord(char) < 32 for char in property_id):
            raise HTTPException(422, 'GA4 property_id must be 1-64 printable characters')
        normalized['property_id'] = property_id

    for field, (max_items, max_item_length) in _GA4_LIST_SETTING_LIMITS.items():
        if field not in normalized:
            continue
        value = normalized[field]
        if not isinstance(value, list) or len(value) > max_items:
            raise HTTPException(422, f'GA4 {field} must contain at most {max_items} values')
        values: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise HTTPException(422, f'GA4 {field} values must be text')
            item = item.strip()
            if not item or len(item) > max_item_length or any(ord(char) < 32 for char in item):
                raise HTTPException(422, f'GA4 {field} values must be 1-{max_item_length} printable characters')
            if item.casefold() in seen:
                continue
            seen.add(item.casefold())
            values.append(item)
        normalized[field] = values
    return normalized


def _validated_smtp_settings(settings_payload: dict) -> dict:
    """Keep the optional digest switch strictly boolean at the API boundary."""

    normalized = dict(settings_payload)
    if 'digest_enabled' in normalized and not isinstance(normalized['digest_enabled'], bool):
        raise HTTPException(422, 'SMTP digest_enabled must be a boolean')
    return normalized


class PolicyInput(Input):
    settings: dict


class JobInput(Input):
    kind: str
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(None, min_length=8, max_length=150)


class ArticleInput(Input):
    title: str = Field(min_length=1, max_length=250)
    brief: dict = Field(default_factory=dict)
    sources: list = Field(default_factory=list)
    author_id: str | None = None


class ArticlePatch(Input):
    title: str | None = Field(None, min_length=1, max_length=250)
    body: str | None = Field(None, max_length=200000)
    brief: dict | None = None
    sources: list | None = None
    author_id: str | None = None
    scheduled_at: datetime | None = None


class Enrollment(Input):
    enrolled: bool


class SourceReviewInput(Input):
    url: str = Field(min_length=8, max_length=2048)
    notes: str = Field(min_length=30, max_length=2000)
    expected_updated_at: datetime
    confirms_claim_support: bool = Field(strict=True)


class Decision(Input):
    decision: str


class Schedule(Input):
    scheduled_at: datetime


class Controls(Input):
    global_pause: bool


class CitationImport(Input):
    # Kept under the historical route/model name for client compatibility.
    # Items may be bare citations or full, provenance-preserving measurement
    # envelopes (AI sample, referral, or technical eligibility).
    items: list[Any] = Field(max_length=1000)


class CostSettlement(Input):
    actual_cents: int = Field(ge=0,strict=True)
    evidence: str = Field(min_length=12,max_length=2000)


def guard(db, ctx, site_id, write=False, owner=False):
    require_role(ctx, *(('owner',) if owner else ('owner', 'editor') if write else ('owner', 'editor', 'viewer')))
    return require_site(db, ctx, site_id)


def own(db, cls, row_id, site_id):
    row = db.scalar(select(cls).where(cls.id == row_id, cls.site_id == site_id))
    if row is None:
        raise HTTPException(404, "Item not found")
    return row


def candidate_for_site(db, candidate_id: str, site_id: str):
    """Resolve a candidate and its page inside the same site boundary."""

    row = own(db, Candidate, candidate_id, site_id)
    if db.scalar(select(Page.id).where(Page.id == row.page_id, Page.site_id == site_id)) is None:
        # An imported or manually corrupted cross-site reference must never be
        # actionable through a site-scoped API.
        raise HTTPException(404, "Item not found")
    return row


def candidate_review_only_reasons(row: Candidate) -> list[str]:
    details = row.details if isinstance(row.details, dict) else {}
    reasons = details.get('review_only_reasons', [])
    if not isinstance(reasons, list):
        return []
    return [str(reason) for reason in reasons if str(reason).strip()]


def current_candidate_review_only_reasons(db, site, row: Candidate) -> list[str]:
    reasons = candidate_review_only_reasons(row)
    page = db.get(Page, row.page_id)
    if page is not None:
        # Import lazily to keep API module loading independent from workflow
        # registration while still rechecking connector capability at the
        # approval/execution boundary.
        from app.workflows import metadata_candidate_readiness

        readiness = metadata_candidate_readiness(db, site, page, row.field)
        if readiness is not None:
            reasons.extend(readiness['blockers'])
    return list(dict.fromkeys(reasons))


def paginated(db, cls, filters, limit=50, offset=0, transform=record, order_by=None):
    if order_by is None:
        if hasattr(cls, 'created_at'):
            order_by = (cls.created_at.desc(), cls.id.desc())
        else:
            order_by = (cls.id,)
    elif not isinstance(order_by, (tuple, list)):
        order_by = (order_by,)
    rows = db.scalars(select(cls).where(*filters).order_by(*order_by).limit(limit).offset(offset)).all()
    total = db.scalar(select(func.count()).select_from(cls).where(*filters))
    return {"items": [transform(r) for r in rows], "total": total}


_JOB_SENSITIVE_KEY_PARTS = (
    "credential",
    "password",
    "secret",
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "consumer_key",
    "consumer_secret",
    "webhook_secret",
    "bearer_token",
    "token",
    "authorization",
    "cookie",
)


_USAGE_COUNT_FIELDS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens",
    "completion_tokens", "cached_tokens", "reasoning_tokens", "audio_tokens",
})
_USAGE_DETAIL_FIELDS = frozenset({
    "input_tokens_details", "output_tokens_details", "prompt_tokens_details",
    "completion_tokens_details",
})


def _safe_usage_value(value: Any, *, depth: int) -> Any:
    """Allow exact numeric metering fields only within a usage document.

    Authentication tokens are still secrets, even if a provider puts them in
    usage. Strings, bools, negative/unsafe counts and arbitrary token-like keys
    never qualify for this exception to the normal browser redaction.
    """
    if depth > 16:
        return "[redacted]"
    if isinstance(value, list):
        return [_safe_usage_value(item, depth=depth + 1) for item in value]
    output = _safe_job_value(value, depth=depth)
    if not isinstance(value, dict):
        return output
    for field in _USAGE_COUNT_FIELDS:
        count = value.get(field)
        if type(count) is int and 0 <= count <= 2**53 - 1:
            output[field] = count
    for field in _USAGE_DETAIL_FIELDS:
        if isinstance(value.get(field), dict):
            output[field] = _safe_usage_value(value[field], depth=depth + 1)
    return output


def _safe_job_value(value: Any, *, depth: int = 0) -> Any:
    """Keep job progress useful without reflecting credential-shaped JSON."""

    if depth > 16:
        return "[redacted]"
    if isinstance(value, dict):
        output: dict[Any, Any] = {}
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            compact = normalized.replace("_", "")
            if normalized == "usage" and isinstance(nested, (dict, list)):
                output[key] = _safe_usage_value(nested, depth=depth + 1)
            elif any(part in normalized or part.replace("_", "") in compact
                   for part in _JOB_SENSITIVE_KEY_PARTS):
                output[key] = "[redacted]"
            else:
                output[key] = _safe_job_value(nested, depth=depth + 1)
        return output
    if isinstance(value, list):
        return [_safe_job_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_safe_job_value(item, depth=depth + 1) for item in value]
    return value


def _job_view(value: Job, *, include_idempotency_key: bool) -> dict:
    excluded = ("encrypted_credentials",) if include_idempotency_key else (
        "encrypted_credentials",
        "idempotency_key",
    )
    output = record(value, excluded)
    for field in ("payload", "result"):
        output[field] = _safe_job_value(output.get(field, {}))
    return output


def _browser_record(value: Any, *json_fields: str) -> dict:
    """Serialize a record while redacting nested provider-shaped values."""

    output = record(value)
    for field in json_fields:
        output[field] = _safe_job_value(output.get(field))
    return output


def page_view(value: Page) -> dict:
    return _browser_record(value, "source", "signals")


def finding_view(value: Finding) -> dict:
    return _browser_record(value, "details")


def candidate_view(value: Candidate) -> dict:
    return _browser_record(value, "details")


def article_view(value: Article) -> dict:
    from app.source_reviews import reviewed_source_urls
    result = _browser_record(value, "brief", "sources")
    result['source_review_state'] = {'accepted_urls': sorted(reviewed_source_urls(record(value))),
                                     'valid_for_days': 7}
    return result


def incident_view(value: Incident) -> dict:
    return _browser_record(value, "details")


def publication_view(value: Publication) -> dict:
    return _browser_record(value, "snapshot", "result")


def measurement_view(value: Measurement) -> dict:
    """Return a visibility observation without reflecting nested credentials."""

    return _browser_record(value, "data")


def job_history_view(value: Job) -> dict:
    """Return a collection-safe job shape without exposing nested secrets."""

    return _job_view(value, include_idempotency_key=False)


_ACTIVITY_DATA_FIELDS = frozenset({
    "site_id", "job_id", "job_kind", "status", "phase", "stage",
    "stage_index", "stage_count", "percent", "message", "candidate_id",
    "article_id", "publication_id", "page_id", "enrolled", "complete",
    "count", "checked_pages", "pending_url_count", "error_count", "seen",
    "missing", "reason", "remote_outcome", "retryable", "cost_basis",
})
_ACTIVITY_TEXT_LIMIT = 240


def _safe_activity_data(value: Event) -> dict[str, Any]:
    """Return an allowlisted event summary for browser-facing activity APIs.

    Event rows are also used as an internal audit ledger, so their data may
    contain provider results or future workflow fields that must not become a
    browser response by accident.  Keep the useful scalar operational summary
    while dropping arbitrary nested payloads, credentials, and raw responses.
    """

    source = value.data if isinstance(value.data, dict) else {}
    output: dict[str, Any] = {}
    for field in _ACTIVITY_DATA_FIELDS:
        item = source.get(field)
        if item is None:
            continue
        if isinstance(item, bool):
            output[field] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            output[field] = item
        elif isinstance(item, str):
            output[field] = item[:_ACTIVITY_TEXT_LIMIT]
        # Lists/dicts are intentionally omitted.  They can contain URLs,
        # provider responses, or credential-shaped values.
    return output


def activity_view(value: Event) -> dict:
    """Return a browser-safe activity row without exposing raw event data."""

    output = record(value, ("data",))
    output["data"] = _safe_activity_data(value)
    return output


def enqueue_response(db, site, kind, payload=None, key=None):
    try:
        return record(enqueue(db, site, kind, payload, key))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value


@router.get("/sites")
def sites(ctx=Depends(require_user), db=Depends(get_db), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return paginated(db, Site, [Site.team_id == ctx['team_id']], limit, offset)


@router.post("/sites", status_code=201)
def add_site(payload: SiteInput, ctx=Depends(require_user), db=Depends(get_db)):
    require_role(ctx, 'owner')
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(payload.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "Choose a valid timezone")
    url = urlsplit(payload.origin)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise HTTPException(422, "Enter the site's HTTPS origin without credentials or query parameters")
    if url.path not in ('', '/'):
        raise HTTPException(422, "Enter the site origin, not an individual page")
    origin = f'https://{url.netloc.lower()}'
    if db.scalar(select(Site).where(Site.team_id == ctx['team_id'], Site.origin == origin)):
        raise HTTPException(409, "This site is already registered")
    site = Site(team_id=ctx['team_id'], name=payload.name, origin=origin,
                timezone=payload.timezone, language=payload.language, facts=payload.facts, paused=True)
    db.add(site)
    db.flush()
    create_policy(db, site, ctx['user_id'], dict(DEFAULT_POLICY))
    event(db, site, 'site_created', 'Site registered; complete connection and automation settings')
    db.commit()
    return record(site)


@router.get("/sites/{site_id}")
def get_site(site_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    return record(guard(db, ctx, site_id))


@router.patch("/sites/{site_id}")
def patch_site(site_id: str, payload: SitePatch, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    if payload.timezone is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, 'Choose a valid timezone')
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(site, key, value)
    event(db, site, 'site_updated', 'Site settings updated')
    db.commit()
    return record(site)


@router.get("/sites/{site_id}/overview")
def overview(site_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id)
    def count(cls, *filters):
        return db.scalar(select(func.count()).select_from(cls).where(cls.site_id == site_id, *filters))
    audit = db.scalar(select(Job).where(Job.site_id == site_id, Job.kind == 'audit', Job.status == 'complete').order_by(Job.updated_at.desc()))
    account = db.scalar(select(BudgetAccount).where(BudgetAccount.site_id == site_id, BudgetAccount.period == now().strftime('%Y-%m')))
    policy = current_policy(db, site_id)
    budget = {"limit_cents": (policy.settings if policy else DEFAULT_POLICY).get('monthly_budget_cents', 30000),
              "spent_cents": account.spent_cents if account else 0, "reserved_cents": account.reserved_cents if account else 0}
    audit_result = audit.result if audit and isinstance(audit.result, dict) else {}
    audit_errors = audit_result.get('errors', [])
    if not isinstance(audit_errors, list):
        audit_errors = []
    pending_urls = audit_result.get('pending_urls', [])
    if not isinstance(pending_urls, list):
        pending_urls = []
    if audit is None:
        coverage_status = 'not_checked'
    elif not audit_result.get('complete'):
        coverage_status = 'partial'
    elif audit_errors:
        coverage_status = 'complete_with_errors'
    else:
        coverage_status = 'complete'
    return {"site": record(site), "counts": {"pages": count(Page), "open_findings": count(Finding, Finding.status == 'open'),
            "pending_candidates": count(Candidate, Candidate.status.in_(['pending', 'approved'])),
            "published_articles": count(Article, Article.status == 'published'), "open_incidents": count(Incident, Incident.status == 'open')},
            "monitoring": {**monitoring_status(db, site_id=site_id), "site_paused": site.paused}, "budget": budget,
            "global_pause": global_controls(db)['global_pause'],
            "recent_events": paginated(db, Event, [Event.site_id == site_id], 10,
                                        transform=activity_view)['items'],
            "coverage": {"status": coverage_status,
                         "last_audit_at": iso(audit.updated_at) if audit else None,
                         "error_count": len(audit_errors),
                         "pending_url_count": len(pending_urls)},
            "connections": [connection_view(c) for c in db.scalars(select(Connection).where(Connection.site_id == site_id))]}


@router.get("/sites/{site_id}/connections")
def connections(site_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    guard(db, ctx, site_id)
    return paginated(db, Connection, [Connection.site_id == site_id], transform=connection_view)


@router.put("/sites/{site_id}/connections/{kind}")
def save_connection(site_id: str, kind: str, payload: ConnectionInput, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    if kind not in {'wordpress','woocommerce','gsc','ga4','dataforseo','ai','pagespeed','smtp'}:
        raise HTTPException(422, 'Unsupported connection type')
    allowed = {'base_url','endpoint','provider','request_format','model','estimated_cost_cents','max_cost_cents','site_url','property_id','conversion_event_names','dimensions','metrics','keywords','questions','url','strategy','categories',
               'language_code','locale','search_context_size','location_code','host','port','sender','recipients','ssl','starttls','digest_enabled',
               'price_per_input_million','price_per_output_million','max_output_tokens','country','device'}
    if set(payload.settings) - allowed:
        raise HTTPException(422, 'Unknown connection settings; place secrets in credentials')
    connection_settings = dict(payload.settings)
    if kind == 'ga4':
        connection_settings = _validated_ga4_settings(connection_settings)
    elif kind == 'smtp':
        connection_settings = _validated_smtp_settings(connection_settings)
    from app.connectors.security import encrypt_credentials, decrypt_credentials
    row = find_connection(db, site_id, kind)
    incoming = {key:value for key,value in payload.credentials.items() if value not in ('',None)}
    # Revocation is an explicit security boundary.  A settings-only save must
    # not silently turn an intentionally revoked connection back into a
    # connector-ready row; the owner must provide a new credential to restore
    # access.
    if row is not None and row.status == 'revoked' and not incoming:
        row.capabilities = {'settings': {**(row.capabilities or {}).get('settings',{}),**connection_settings}}
        row.checked_at = None
        event(db, site, 'connection_updated', f'{kind} settings saved; connection remains revoked')
        db.commit()
        return connection_view(row)
    try:
        existing = decrypt_credentials(row.encrypted_credentials,settings.ENCRYPTION_KEY) if row and row.encrypted_credentials and row.status != 'revoked' else {}
        ciphertext = encrypt_credentials({**existing,**incoming}, settings.ENCRYPTION_KEY)
    except ValueError as exc:
        raise HTTPException(503, 'Credential encryption is not configured correctly') from exc
    if row is None:
        row = Connection(site_id=site_id, kind=kind)
        db.add(row)
    row.encrypted_credentials = ciphertext
    row.status = 'needs_test'
    row.capabilities = {'settings': {**(row.capabilities or {}).get('settings',{}),**connection_settings}}
    row.checked_at = None
    event(db, site, 'connection_updated', f'{kind} connection saved; credentials are encrypted')
    db.commit()
    return connection_view(row)


@router.post("/sites/{site_id}/connections/{kind}/test", status_code=202)
def test_connection(site_id: str, kind: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    return enqueue_response(db, site, 'connection_test', {'kind': kind})


@router.delete("/sites/{site_id}/connections/{kind}")
def revoke_connection(site_id: str, kind: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    row = find_connection(db, site_id, kind)
    if row:
        row.encrypted_credentials = ''
        row.status = 'revoked'
        row.capabilities = {}
        event(db, site, 'connection_revoked', f'{kind} connection revoked')
        db.commit()
    return {'status': 'revoked'}


@router.get("/sites/{site_id}/policy")
def policy(site_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    guard(db, ctx, site_id)
    row = current_policy(db, site_id)
    return record(row) if row else {'version': 0, 'settings': DEFAULT_POLICY}


@router.put("/sites/{site_id}/policy")
def save_policy(site_id: str, payload: PolicyInput, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    scope = payload.settings.get('publication_article_ids')
    if isinstance(scope, list) and all(isinstance(value, str) for value in scope):
        existing = set(db.scalars(select(Article.id).where(Article.site_id == site_id, Article.id.in_(scope))))
        if set(scope) - existing:
            raise HTTPException(422, 'Every selected article must belong to this site')
    try:
        row = create_policy(db, site, ctx['user_id'], payload.settings)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'The policy version changed; reload and try again') from exc
    event(db, site, 'policy_changed', f'Automation policy version {row.version} saved')
    db.commit()
    return record(row)


# All collection routes share the same ownership and pagination rules.
def collection_route(cls, extra_filter=None, transform=record, order_by=None):
    def endpoint(site_id: str, ctx=Depends(require_user), db=Depends(get_db),
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        guard(db, ctx, site_id)
        filters = [cls.site_id == site_id]
        if extra_filter is not None:
            filters.append(extra_filter)
        return paginated(db, cls, filters, limit, offset, transform=transform, order_by=order_by)
    return endpoint


for path, cls in [('pages', Page), ('findings', Finding), ('candidates', Candidate), ('articles', Article),
                  ('incidents', Incident), ('activity', Event), ('measurements', Measurement),
                  ('publications', Publication)]:
    transform = {
        Page: page_view,
        Finding: finding_view,
        Candidate: candidate_view,
        Article: article_view,
        Incident: incident_view,
        Event: activity_view,
        Measurement: measurement_view,
        Publication: publication_view,
    }[cls]
    router.add_api_route(
        '/sites/{site_id}/' + path,
        collection_route(cls, transform=transform),
        methods=['GET'],
        name='list_' + path,
    )
router.add_api_route(
    '/sites/{site_id}/jobs',
    collection_route(
        Job,
        transform=job_history_view,
        order_by=(Job.created_at.desc(), Job.id.desc()),
    ),
    methods=['GET'],
    name='list_jobs',
)
router.add_api_route('/sites/{site_id}/products', collection_route(
    Page,
    Page.resource_type.in_(['products', 'categories', 'product_categories']),
    transform=page_view,
), methods=['GET'])


@router.patch('/sites/{site_id}/pages/{page_id}')
def enroll(site_id: str, page_id: str, payload: Enrollment, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    row = own(db, Page, page_id, site_id)
    row.enrolled = payload.enrolled
    event(db, site, 'enrollment_changed', f'Editorial enrollment changed for {row.title}', {'page_id': row.id, 'enrolled': row.enrolled})
    db.commit()
    return page_view(row)


@router.get('/sites/{site_id}/pages/{page_id}/evidence')
def download_page_evidence(site_id:str,page_id:str,ctx=Depends(require_user),db=Depends(get_db)):
    from pathlib import Path
    guard(db,ctx,site_id)
    page=own(db,Page,page_id,site_id)
    manifest=page.signals.get('evidence',{})
    name=manifest.get('artifact')
    if not isinstance(name,str):
        raise HTTPException(404,'No source HTML evidence has been captured')
    root=Path(settings.ARTIFACT_ROOT).resolve()
    path=(root/name).resolve()
    if not path.is_relative_to(root/site_id/'evidence') or not path.is_file():
        raise HTTPException(404,'Evidence artifact is unavailable')
    if sha256(path.read_bytes()).hexdigest()!=manifest.get('sha256'):
        raise HTTPException(409,'Evidence checksum does not match the recorded snapshot')
    return FileResponse(path,media_type='text/plain',filename=f'page-{page_id}-evidence.html',
                        headers={'X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox; default-src 'none'"})


@router.post('/sites/{site_id}/candidates/{candidate_id}/decision')
def decide(site_id: str, candidate_id: str, payload: Decision, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    row = candidate_for_site(db, candidate_id, site_id)
    if payload.decision not in ('approve','reject') or row.status != 'pending':
        raise HTTPException(409, 'This candidate cannot accept that decision')
    if payload.decision == 'approve' and current_candidate_review_only_reasons(db, site, row):
        raise HTTPException(409, 'This candidate is review-only until its required connector is available')

    next_status = 'approved' if payload.decision == 'approve' else 'rejected'
    authorization = {
        'type': 'person',
        'user_id': ctx['user_id'],
        'decision': payload.decision,
        'at': iso(now()),
    }
    changed = db.execute(
        update(Candidate)
        .where(
            Candidate.id == row.id,
            Candidate.site_id == site_id,
            Candidate.status == 'pending',
        )
        .values(status=next_status, details={**(row.details or {}), 'authorization': authorization})
    )
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, 'This candidate was decided by another request')
    db.refresh(row)
    event(db, site, 'candidate_decided', f'Candidate {row.status}', {'candidate_id': row.id})
    db.commit()
    return candidate_view(row)


@router.post('/sites/{site_id}/candidates/{candidate_id}/execute', status_code=202)
def execute_candidate(site_id: str, candidate_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    row = candidate_for_site(db, candidate_id, site_id)
    if current_candidate_review_only_reasons(db, site, row):
        raise HTTPException(409, 'This candidate is review-only until its required connector is available')
    if row.status != 'approved':
        raise HTTPException(409, 'Approve this candidate before execution')
    return enqueue_response(db, site, 'candidate', {'candidate_id': candidate_id}, f'candidate:{candidate_id}')


@router.post('/sites/{site_id}/jobs', status_code=202)
def add_job(site_id: str, payload: JobInput, ctx=Depends(require_user), db=Depends(get_db)):
    if payload.kind not in {'audit','inventory','poll_changes','plan','generate','publish','availability','visibility','refresh','content_autopilot','full_cycle'}:
        raise HTTPException(422, 'Unsupported requested job')
    full_cycle_mode_value = None
    if payload.kind == 'full_cycle':
        from app.workflows import full_cycle_mode

        try:
            full_cycle_mode_value = full_cycle_mode(payload.payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    site = guard(
        db,
        ctx,
        site_id,
        owner=full_cycle_mode_value == 'autopilot',
        write=full_cycle_mode_value != 'autopilot',
    )
    return enqueue_response(db, site, payload.kind, payload.payload, payload.idempotency_key)


@router.get('/sites/{site_id}/jobs/{job_id}')
def get_job(site_id: str, job_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    guard(db, ctx, site_id)
    return _job_view(own(db, Job, job_id, site_id), include_idempotency_key=True)


@router.post('/sites/{site_id}/articles', status_code=201)
def add_article(site_id: str, payload: ArticleInput, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    if 'source_reviews' in payload.brief:
        raise HTTPException(422, 'Source reviews must be recorded through the source-review workflow')
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', payload.title.lower()).strip('-')[:120]
    article = Article(site_id=site_id, title=payload.title, slug=slug, brief=payload.brief,
                      sources=payload.sources, author_id=payload.author_id, status='planned', updated_at=now())
    db.add(article)
    event(db, site, 'article_planned', f'Article planned: {payload.title}')
    db.commit()
    return article_view(article)


@router.get('/sites/{site_id}/articles/{article_id}')
def get_article(site_id: str, article_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    guard(db, ctx, site_id)
    return article_view(own(db, Article, article_id, site_id))


@router.patch('/sites/{site_id}/articles/{article_id}')
def edit_article(site_id: str, article_id: str, payload: ArticlePatch, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    article = own(db, Article, article_id, site_id)
    if article.status in ('publishing','verifying','published'):
        raise HTTPException(409, 'Create a refresh workflow for a published or in-flight article')
    db.add(Revision(site_id=site_id, article_id=article.id, body=article.body, title=article.title, reason='editor_save'))
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == 'brief':
            # This audit ledger is server-owned. A browser round trip can carry
            # it, but cannot create, replace, erase or revive an attestation.
            value = {k: v for k, v in (value or {}).items() if k != 'source_reviews'}
            if 'source_reviews' in (article.brief or {}):
                value['source_reviews'] = article.brief['source_reviews']
            generation = (article.brief or {}).get('generation')
            if isinstance(generation, dict) and generation.get('kind') in ('provider_generation', 'provider_draft'):
                # Browser responses redact token-shaped fields. An editor save
                # must not persist those placeholders over provider metering,
                # or let an edited brief rewrite the original source history.
                # Only the generation workflow replaces provider provenance.
                value = {**(value or {}), 'generation': generation}
        setattr(article, key, utc(value) if key == 'scheduled_at' else value)
    if 'body' in payload.model_fields_set:
        # Record who supplied an editorial draft, not a claim that it is true
        # or approved. Source, author, markup, and policy checks still apply.
        brief = dict(article.brief or {})
        editor = {'kind': 'authenticated_editor', 'user_id': ctx['user_id'],
                  'recorded_at': iso(now())}
        if not brief.get('generation'):
            brief['generation'] = editor
        brief['last_editor_revision'] = editor
        article.brief = brief
    article.status = 'checking'
    article.checks = {}
    article.updated_at = now()
    event(db, site, 'article_edited', f'Draft updated: {article.title}')
    db.commit()
    return article_view(article)


@router.get('/sites/{site_id}/articles/{article_id}/revisions')
def revisions(site_id: str, article_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    guard(db, ctx, site_id)
    own(db, Article, article_id, site_id)
    return paginated(db, Revision, [Revision.site_id == site_id, Revision.article_id == article_id])


def check(db, site, article):
    from app.intelligence.content import check_article
    pages = [record(p) for p in db.scalars(select(Page).where(Page.site_id == site.id))]
    result = check_article(record(article), site.facts, pages)
    article.checks = result
    article.status = 'checked' if result['passed'] else 'review_needed'
    article.updated_at = now()
    return result


@router.post('/sites/{site_id}/articles/{article_id}/check')
def check_draft(site_id: str, article_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    article = own(db, Article, article_id, site_id)
    if article.status in ('publishing','verifying','published'):
        raise HTTPException(409, 'Article is already in publication workflow')
    result = check(db, site, article)
    db.commit()
    return result


@router.post('/sites/{site_id}/articles/{article_id}/source-reviews')
async def review_article_source(site_id: str, article_id: str, payload: SourceReviewInput,
                                ctx=Depends(require_user), db=Depends(get_db)):
    from types import SimpleNamespace
    from uuid import uuid4
    from app import network
    from app.source_reviews import review_fingerprint, source_url
    from app.workflows import capture_html

    site = guard(db, ctx, site_id, write=True)
    article = own(db, Article, article_id, site_id)
    if article.status in ('publishing', 'verifying', 'published', 'scheduled'):
        raise HTTPException(409, 'Source review requires an unscheduled draft')
    if utc(payload.expected_updated_at) != article.updated_at:
        raise HTTPException(409, 'The draft changed; reload before reviewing its sources')
    if payload.confirms_claim_support is not True or len(payload.notes.strip()) < 30:
        raise HTTPException(422, 'Read the source and explain which claims or link purpose it supports')
    brief = article.brief or {}
    generation = brief.get('generation') or {}
    candidates = [*(article.sources or []), *(generation.get('unverified_sources') or []),
                  *(brief.get('sources') or [])]
    url = source_url(payload.url)
    if not url or url not in {source_url(value) for value in candidates}:
        raise HTTPException(422, 'Select an existing article source or a flagged source')
    before = review_fingerprint(record(article))
    try:
        observation = await asyncio.wait_for(network.fetch(url), timeout=25)
    except Exception:
        raise HTTPException(422, 'Source could not be safely fetched; no review was recorded') from None
    if observation['status_code'] != 200 or 'text/html' not in observation.get('headers', {}).get('content-type', '').lower():
        raise HTTPException(422, 'Source must return successful HTML; no review was recorded')

    # Fetching is read-only but can take time. Recheck the current locked row,
    # not the stale identity-map instance, before attaching editorial authority.
    db.expire(article)
    article = db.scalar(select(Article).where(Article.id == article_id, Article.site_id == site_id)
                        .with_for_update().execution_options(populate_existing=True))
    if (article is None or utc(payload.expected_updated_at) != article.updated_at
            or review_fingerprint(record(article)) != before
            or article.status in ('publishing', 'verifying', 'published', 'scheduled')):
        raise HTTPException(409, 'The draft changed during verification; review the latest version')
    review_id = uuid4().hex
    evidence = capture_html(site, SimpleNamespace(id=review_id), observation['url'], observation['html'], 'source_review')
    evidence.pop('job_id', None)
    evidence['review_id'] = review_id
    review = {'id': review_id, 'kind': 'authenticated_source_review',
              'url': url, 'fetched_url': observation['url'], 'http_status': 200,
              'content_sha256': evidence['sha256'], 'evidence': evidence,
              'article_fingerprint': before, 'decision': 'accepted_for_this_revision',
              'notes': payload.notes.strip(), 'reviewer_id': ctx['user_id'], 'reviewed_at': iso(now())}
    ledger = list((article.brief or {}).get('source_reviews', []))
    if len(ledger) >= 100:
        raise HTTPException(409, 'Source-review history limit reached; retain history and request support')
    article.brief = {**(article.brief or {}), 'source_reviews': [*ledger, review]}
    article.checks = {}
    article.status = 'checking'
    article.updated_at = now()
    event(db, site, 'article_source_reviewed', 'Source reviewed for the saved article revision',
          {'article_id': article.id, 'review_id': review_id})
    check(db, site, article)
    db.commit()
    return article_view(article)


@router.post('/sites/{site_id}/articles/{article_id}/schedule')
def schedule(site_id: str, article_id: str, payload: Schedule, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    article = own(db, Article, article_id, site_id)
    if article.status in ('publishing','verifying','published'):
        raise HTTPException(409, 'Article already in publication workflow')
    result = check(db, site, article)
    if not result['passed']:
        db.commit()
        raise HTTPException(409, {'message': 'Editorial checks need attention', 'checks': result})
    scheduled = utc(payload.scheduled_at)
    if scheduled <= now():
        raise HTTPException(422, 'Choose a future publication time')
    article.scheduled_at = scheduled
    article.status = 'scheduled'
    db.commit()
    return article_view(article)


@router.post('/sites/{site_id}/articles/{article_id}/publish', status_code=202)
def publish_article(site_id: str, article_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    article = own(db, Article, article_id, site_id)
    if article.status == 'rolled_back':
        raise HTTPException(409, 'This publication was rolled back. Review a new article operation before publishing again.')
    try:
        return _job_view(enqueue_article_publish(db, site, article_id, requested_by=ctx['user_id']), include_idempotency_key=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/sites/{site_id}/articles/{article_id}/rollback', status_code=202)
def rollback_article(site_id: str, article_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, owner=True)
    own(db, Article, article_id, site_id)
    return enqueue_response(db, site, 'rollback', {'article_id': article_id}, f'rollback:{article_id}')


@router.post('/sites/{site_id}/publications/{publication_id}/reconcile', status_code=202)
def reconcile_publication_action(site_id: str, publication_id: str, ctx=Depends(require_user), db=Depends(get_db)):
    """Queue a read-only check for an uncertain remote publication outcome."""

    site = guard(db, ctx, site_id, owner=True)
    publication = own(db, Publication, publication_id, site_id)
    from app.workflows import _publication_requires_reconciliation

    if not _publication_requires_reconciliation(publication):
        raise HTTPException(409, 'This publication does not require reconciliation')
    # A failed/held read is not a permanent answer. Reuse an active check, then
    # permit a new read after the publication's last recorded observation.
    for active in db.scalars(select(Job).where(
        Job.site_id == site_id, Job.kind == 'reconcile_publication',
        Job.status.in_(('queued', 'running', 'retry')),
    )):
        if active.payload == {'publication_id': publication.id}:
            return _job_view(active, include_idempotency_key=True)
    return enqueue_response(
        db,
        site,
        'reconcile_publication',
        {'publication_id': publication.id},
        f'reconcile-publication:{publication.id}:{iso(publication.updated_at)}',
    )


@router.post('/sites/{site_id}/measurements/import', status_code=201)
def import_citations(site_id: str, payload: CitationImport, ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id, write=True)
    from app.intelligence.visibility import validate_measurement_import
    try:
        items = validate_measurement_import(payload.items)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    imported_at = now()
    imported_iso = iso(imported_at)
    for item in items:
        data = dict(item['data'])
        provenance = data.get('_import') if isinstance(data.get('_import'), dict) else {}
        data['_import'] = {
            **provenance,
            'imported_at': imported_iso,
            'observed_at_basis': 'provided' if item['observed_at'] is not None else 'imported_at',
        }
        db.add(Measurement(
            site_id=site_id,
            kind=item['kind'],
            source=item['source'],
            data=data,
            observed_at=item['observed_at'] or imported_at,
        ))
    event(db, site, 'measurement_imported', f'{len(items)} visibility observations imported')
    db.commit()
    return {'imported': len(items)}


@router.get('/settings')
def controls(ctx=Depends(require_user), db=Depends(get_db)):
    return global_controls(db)


@router.patch('/settings')
def set_controls(payload: Controls, ctx=Depends(require_user), db=Depends(get_db)):
    require_role(ctx, 'owner')
    installation_team=db.scalar(select(Team.id).order_by(Team.created_at,Team.id).limit(1))
    if ctx['team_id']!=installation_team:
        raise HTTPException(403,'Only the installation-owning team may change global controls')
    row = db.get(Heartbeat, 'platform_controls')
    if row is None:
        row = Heartbeat(name='platform_controls', last_seen_at=now(), details={})
        db.add(row)
    row.details = {**row.details, 'global_pause': payload.global_pause}
    row.last_seen_at = now()
    db.add(Event(team_id=ctx['team_id'], kind='global_control_changed', message='Global emergency control changed', data=payload.model_dump()))
    db.commit()
    return global_controls(db)


@router.get('/sites/{site_id}/budgets')
def budget_ledger(site_id:str,ctx=Depends(require_user),db=Depends(get_db)):
    guard(db,ctx,site_id)
    return {'accounts':paginated(db,BudgetAccount,[BudgetAccount.site_id==site_id]),
            'reservations':paginated(db,CostReservation,[CostReservation.site_id==site_id])}


@router.post('/sites/{site_id}/budgets/reservations/{reservation_id}/settle')
def settle_cost(site_id:str,reservation_id:str,payload:CostSettlement,ctx=Depends(require_user),db=Depends(get_db)):
    from app.budgets import settle
    site=guard(db,ctx,site_id,owner=True)
    reservation=own(db,CostReservation,reservation_id,site_id)
    # A reservation carries both a site boundary and an account foreign key.
    # The database constraint verifies that the referenced account exists, but
    # it does not express that both rows belong to the same site.  Treat a
    # mismatched/imported row as corrupted instead of allowing this endpoint
    # to settle usage against another site's account.
    account = db.scalar(
        select(BudgetAccount).where(
            BudgetAccount.id == reservation.account_id,
            BudgetAccount.site_id == site_id,
        )
    )
    if account is None:
        raise HTTPException(409, 'Budget reservation accounting is inconsistent')
    if reservation.status!='reserved':
        raise HTTPException(409,'Reservation has already been reconciled')
    try:
        settle(db,reservation.id,payload.actual_cents)
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    event(db,site,'cost_reconciled','Owner reconciled a provider charge',
          {'reservation_id':reservation_id,'actual_cents':payload.actual_cents,'evidence':payload.evidence,'user_id':ctx['user_id']})
    db.commit()
    return record(reservation)


@router.get('/sites/{site_id}/reports/weekly')
def weekly_report(site_id: str, format: str = 'json', ctx=Depends(require_user), db=Depends(get_db)):
    site = guard(db, ctx, site_id)
    from datetime import timedelta
    generated_at = now()
    start = generated_at - timedelta(days=7)
    measurements = paginated(
        db,
        Measurement,
        [Measurement.site_id == site_id, Measurement.observed_at >= start],
        200,
        transform=measurement_view,
    )
    period_incidents = paginated(
        db,
        Incident,
        [Incident.site_id == site_id, Incident.last_seen_at >= start,
         Incident.last_seen_at < generated_at],
        200,
        transform=incident_view,
    )
    period_reservations = paginated(
        db,
        CostReservation,
        [CostReservation.site_id == site_id, CostReservation.created_at >= start,
         CostReservation.created_at < generated_at],
        200,
    )
    budget_account = db.scalar(select(BudgetAccount).where(
        BudgetAccount.site_id == site_id,
        BudgetAccount.period == generated_at.strftime('%Y-%m'),
    ))
    report = {'site': record(site), 'generated_at': iso(generated_at),
              'period_start': iso(start), 'period_end': iso(generated_at),
              'overview': overview(site_id, ctx, db), 'measurements': measurements,
              'events': paginated(db, Event, [Event.site_id == site_id, Event.created_at >= start], 200,
                                  transform=activity_view),
              'publications': paginated(db, Publication, [Publication.site_id == site_id,
                                                          Publication.created_at >= start], 200,
                                        transform=publication_view),
              'incidents': period_incidents,
              'spending': {
                  # This is the current monthly ledger context; the reservation
                  # list below is bounded to the report period and is the
                  # evidence for work attempted during that period.
                  'budget_account': record(budget_account) if budget_account else None,
                  'reservations': period_reservations,
              },
              'note': 'Observed changes and measurements; not proof of ranking causality. Lists are paginated.'}
    if format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time','type','message'])
        for e in report['events']['items']:
            safe = lambda s: "'" + s if str(s).startswith(('=','+','-','@')) else s
            writer.writerow([safe(str(e[k])) for k in ['created_at','kind','message']])
        return Response(output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="forgeseo-weekly.csv"'})
    return report


_PROGRESS_EVENT_KINDS = frozenset({'job_progress', 'progress'})
_PROGRESS_TEXT_LIMITS = {
    'site_id': 64,
    'job_id': 64,
    'job_kind': 64,
    'status': 32,
    'phase': 64,
    'stage': 64,
    'message': 240,
}
_PROGRESS_DATA_FIELDS = frozenset(_PROGRESS_TEXT_LIMITS) | {
    'stage_index', 'stage_count', 'percent',
}


def _is_progress_event(row: Event) -> bool:
    return row.kind in _PROGRESS_EVENT_KINDS


def _bounded_progress_text(value, field):
    value = str(value) if value is not None else ''
    return value[:_PROGRESS_TEXT_LIMITS[field]]


def _bounded_progress_int(value, *, minimum, maximum):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(minimum, min(maximum, value))


def _safe_progress_payload(row: Event) -> dict[str, Any]:
    """Return only the bounded fields allowed on a progress SSE message."""

    source = row.data if isinstance(row.data, dict) else {}
    payload = {
        'site_id': _bounded_progress_text(row.site_id, 'site_id'),
    }
    for field in ('job_id', 'job_kind', 'status', 'phase', 'stage'):
        if field in source and source[field] is not None:
            payload[field] = _bounded_progress_text(source[field], field)
    payload['message'] = _bounded_progress_text(
        source.get('message', row.message), 'message'
    )
    for field in ('stage_index', 'stage_count'):
        value = _bounded_progress_int(source.get(field), minimum=1, maximum=100)
        if value is not None:
            payload[field] = value
    value = _bounded_progress_int(source.get('percent'), minimum=0, maximum=100)
    if value is not None:
        payload['percent'] = value
    return {field: value for field, value in payload.items() if field in _PROGRESS_DATA_FIELDS}


def _sse_payload(row: Event) -> dict[str, Any]:
    payload = activity_view(row)
    if _is_progress_event(row):
        payload['message'] = _bounded_progress_text(row.message, 'message')
        payload['data'] = _safe_progress_payload(row)
    return payload


@router.get('/sites/{site_id}/events')
async def events(site_id: str, request: Request, ctx=Depends(require_user), db=Depends(get_db)):
    guard(db, ctx, site_id)
    try:
        cursor = max(0, int(request.headers.get('last-event-id', '0')))
    except ValueError:
        raise HTTPException(422, 'Invalid event cursor')
    async def stream():
        nonlocal cursor
        for _ in range(30):
            if await request.is_disconnected():
                return
            with SessionLocal() as stream_db:
                # The response may stay open after the dependency checks that
                # ran before StreamingResponse was created. Revalidate the
                # current session and team/site boundary before every poll so
                # membership revocation or a site transfer cannot leave an
                # already-open stream authorized for stale access.
                try:
                    stream_context = require_user(request, stream_db)
                    guard(stream_db, stream_context, site_id)
                except HTTPException:
                    return
                rows = stream_db.scalars(select(Event).where(Event.site_id == site_id, Event.id > cursor).order_by(Event.id).limit(100)).all()
                payloads = [
                    (e.id, 'progress' if _is_progress_event(e) else 'activity',
                     json.dumps(_sse_payload(e), default=str))
                    for e in rows
                ]
            for event_id, event_name, body in payloads:
                cursor = event_id
                yield f'id: {event_id}\nevent: {event_name}\ndata: {body}\n\n'
            yield ': heartbeat\n\n'
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache','X-Accel-Buffering':'no'})
