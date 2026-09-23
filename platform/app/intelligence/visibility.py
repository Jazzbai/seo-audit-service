"""Actual visibility measurements with bounded, credential-safe HTTP calls.

The functions in this module return measurements or explicit errors; they never
fill missing metrics with estimates.  Paid DataForSEO requests require a known
cost before the request is made.  OAuth refresh tokens are used only in memory
and are never returned in a result.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from .audit import _host_is_public, _validate_http_url, _normalized_text


_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GSC_ENDPOINT = "https://searchconsole.googleapis.com/webmasters/v3/sites"
_GA4_ENDPOINT = "https://analyticsdata.googleapis.com/v1beta/properties"
_DATAFORSEO_SERP_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
_DATAFORSEO_KEYWORD_ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/task_post"
_DATAFORSEO_COMPETITOR_ENDPOINT = "https://api.dataforseo.com/v3/dataforseo_labs/google/competitors_domain/live"
_PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_DATAFORSEO_SUCCESS_TASK_CODES = frozenset({20000, 20100})
_DATAFORSEO_MAX_KEYWORDS = 25
_DATAFORSEO_MAX_KEYWORD_LENGTH = 200
_AI_MAX_QUESTIONS = 20
_AI_MAX_QUESTION_LENGTH = 512
_AI_MAX_ANSWER_LENGTH = 16_000
_OPENAI_RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
_OPENAI_RESPONSES_WEB_SEARCH_FORMATS = frozenset({
    "openai_responses_web_search",
    "responses_web_search",
    "openai_responses",
})
_PAGESPEED_CONTEXTS = frozenset({"lab", "field_or_origin", "both", "unknown"})
_PAGESPEED_ALLOWED_AUDITS = frozenset({
    "cumulative-layout-shift",
    "first-contentful-paint",
    "largest-contentful-paint",
    "speed-index",
    "total-blocking-time",
    "interaction-to-next-paint",
})
_PAGESPEED_ALLOWED_FIELD_METRICS = frozenset({
    "CUMULATIVE_LAYOUT_SHIFT_SCORE",
    "EXPERIMENTAL_INTERACTION_TO_NEXT_PAINT",
    "FIRST_CONTENTFUL_PAINT_MS",
    "FIRST_INPUT_DELAY_MS",
    "LARGEST_CONTENTFUL_PAINT_MS",
    "INTERACTION_TO_NEXT_PAINT",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _observation_timestamp(*sources: Any) -> str | None:
    """Return a valid provider/settings observation timestamp, if supplied.

    A supplied but malformed timestamp must not silently turn into the local
    collection time.  That would make a stale or otherwise untrustworthy
    provider observation look current to downstream reporting.
    """

    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("observed_at", "observedAt", "observation_timestamp", "timestamp"):
            if key not in source or source[key] is None:
                continue
            value = source.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("observation timestamp is invalid")
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("observation timestamp is invalid")
            if parsed.tzinfo is None:
                raise ValueError("observation timestamp must include a timezone")
            return parsed.astimezone(timezone.utc).isoformat()
    return None


def _result(
    kind: str,
    source: str,
    *,
    data: dict[str, Any] | list[Any] | None = None,
    cost_cents: int = 0,
    metadata: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "status": "error" if error is not None else "ok",
        "kind": kind,
        "source": source,
        "observed_at": observed_at or _now(),
        "data": data if data is not None else {},
        "cost_cents": cost_cents,
        "metadata": dict(metadata or {}),
    }
    output["cost_basis"] = output["metadata"].get("cost_basis", "unknown")
    output["usage"] = output["metadata"].get("usage")
    if error is not None:
        output["error"] = error
    return output


def _error(
    kind: str,
    code: str,
    message: str,
    *,
    cost_cents: int = 0,
    metadata: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return _result(
        kind,
        source or kind,
        cost_cents=cost_cents,
        metadata=metadata,
        error={"code": code, "message": message},
    )


def _integer_cost(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or not number.is_integer():
        return None
    return int(number)


def _pricing(kind: str, settings: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    pricing = settings.get("pricing") if isinstance(settings.get("pricing"), dict) else {}
    estimate_value = settings.get("estimated_cost_cents")
    if estimate_value is None:
        estimate_value = settings.get("cost_cents")
    if estimate_value is None:
        estimate_value = pricing.get("estimated_cost_cents")
    if estimate_value is None:
        estimate_value = pricing.get("cost_cents")
    estimate = _integer_cost(estimate_value)
    max_value = settings.get("max_cost_cents")
    if max_value is None:
        max_value = pricing.get("max_cost_cents")
    max_cost = _integer_cost(max_value) if max_value is not None else None
    if estimate_value is not None and estimate is None:
        return None, max_cost, "estimated_cost_cents must be a non-negative integer"
    if max_value is not None and max_cost is None:
        return estimate, None, "max_cost_cents must be a non-negative integer"
    if max_cost is not None and estimate is not None and estimate > max_cost:
        return estimate, max_cost, "estimated cost exceeds max_cost_cents"
    requires_known = (
        kind in {"dataforseo", "ai_sample"}
        or settings.get("paid") is True
        or settings.get("pricing_required") is True
    )
    if requires_known and estimate is None:
        return None, max_cost, "known estimated_cost_cents is required before a paid request"
    return estimate if estimate is not None else 0, max_cost, None


def _metadata(kind: str, estimate: int | None, max_cost: int | None, **extra: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "status": "ok",
        "pricing_known": estimate is not None,
        "estimated_cost_cents": estimate,
        "max_cost_cents": max_cost,
        "cost_basis": "unknown",
        "usage": None,
    }
    output.update(extra)
    return output


def _validated_endpoint(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _validate_http_url(value.strip())
    except ValueError:
        return None


def _credential_text(value: Any) -> str | None:
    """Return a non-blank credential without allowing whitespace-only values."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _bounded_ai_questions(value: Any, *, singular: bool) -> list[str] | None:
    """Return deduplicated, bounded questions without trusting provider input."""

    if singular:
        if not isinstance(value, str):
            return None
        values: Any = [value]
    else:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not values or len(values) > _AI_MAX_QUESTIONS:
            return None
    output: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            return None
        item = item.strip()
        if not item or len(item) > _AI_MAX_QUESTION_LENGTH or any(ord(char) < 32 for char in item):
            return None
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output or None


def _default_transport() -> httpx.AsyncBaseTransport | None:
    """Use the foundation DNS-pinned transport for real provider requests."""

    try:
        from app.network import PublicTransport
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - minimal package fallback
        return None
    return PublicTransport()


def _auth_headers(credentials: dict[str, Any], *, access_token: str | None = None) -> dict[str, str]:
    token = (
        _credential_text(access_token)
        or _credential_text(credentials.get("access_token"))
        or _credential_text(credentials.get("token"))
    )
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_key = _credential_text(credentials.get("api_key"))
    if api_key:
        header_name = _normalized_text(credentials.get("api_key_header") or "X-API-Key")
        headers[header_name] = api_key
    return headers


def _has_auth_credential(credentials: dict[str, Any]) -> bool:
    """Return whether a provider request has a non-blank supported credential."""

    for key in ("api_key", "access_token", "token"):
        if _credential_text(credentials.get(key)):
            return True
    return False


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


async def _refresh_oauth(
    client: httpx.AsyncClient,
    credentials: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[str | None, str | None]:
    refresh_token = _credential_text(credentials.get("refresh_token"))
    if not refresh_token:
        return None, "OAuth access token and refresh token are not configured"
    token_url = credentials.get("token_url") or settings.get("token_url") or _GOOGLE_TOKEN_URL
    token_url = _validated_endpoint(token_url)
    if not token_url:
        return None, "OAuth token endpoint is invalid"
    form: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    for key in ("client_id", "client_secret"):
        value = _credential_text(credentials.get(key))
        if value:
            form[key] = value
    try:
        response = await client.post(token_url, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        return None, f"OAuth refresh failed: {type(exc).__name__}"
    payload = _safe_json(response)
    if response.status_code < 200 or response.status_code >= 300:
        return None, f"OAuth refresh returned HTTP {response.status_code}"
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        return None, "OAuth refresh response did not include an access token"
    return access_token, None


async def _oauth_access(
    client: httpx.AsyncClient,
    credentials: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[str | None, bool, str | None]:
    access_token = _credential_text(credentials.get("access_token"))
    if access_token:
        return access_token, False, None
    refreshed, error = await _refresh_oauth(client, credentials, settings)
    return refreshed, refreshed is not None, error


def _date_range(settings: dict[str, Any]) -> tuple[str, str]:
    today = date.today()
    start = _normalized_text(settings.get("start_date"))
    end = _normalized_text(settings.get("end_date"))
    if not start:
        start = (today - timedelta(days=28)).isoformat()
    if not end:
        end = today.isoformat()
    return start, end


def _ga4_values(
    settings: dict[str, Any],
    key: str,
    default: list[str],
    *,
    max_items: int,
    max_item_length: int,
) -> list[str] | None:
    """Read a bounded GA4 list setting without trusting caller shape."""

    raw = settings.get(key, default)
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list) or len(raw) > max_items:
        return None
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return None
        item = item.strip()
        if not item or len(item) > max_item_length or any(ord(char) < 32 for char in item):
            return None
        if item.casefold() in seen:
            continue
        seen.add(item.casefold())
        values.append(item)
    return values


def _actual_cost(payload: Any, fallback: int) -> int:
    """Read an explicitly returned cost without treating metrics as cost."""

    actual, _basis, _usage = _cost_info(payload, fallback)
    return actual


def _cost_info(payload: Any, fallback: int) -> tuple[int, str, Any]:
    candidates = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("cost_cents", "price_cents"):
            value = _integer_cost(candidate.get(key))
            if value is not None:
                return value, "provider_actual", _usage(payload)
        for key in ("cost", "price"):
            value = candidate.get(key)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                try:
                    amount = float(value)
                except (TypeError, ValueError):
                    continue
                if amount >= 0:
                    return int(round(amount * 100)), "provider_actual", _usage(payload)
    return fallback, "estimated", _usage(payload)


def _usage(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("usage", "billing", "token_usage"):
            if key in payload:
                return payload[key]
        if isinstance(payload.get("data"), dict):
            for key in ("usage", "billing", "token_usage"):
                if key in payload["data"]:
                    return payload["data"][key]
    return None


def _pagespeed_scalar(value: Any, *, max_length: int = 256) -> int | float | str | None:
    """Return a bounded scalar from a known PageSpeed field."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if abs(value) > 1_000_000_000_000:
            return None
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > max_length or any(ord(char) < 32 for char in value):
            return None
        return value
    return None


def _pagespeed_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return _validate_http_url(value.strip())
    except ValueError:
        return None


def _normalize_pagespeed_metric(value: Any) -> dict[str, int | float | str] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, int | float | str] = {}
    percentile = _pagespeed_scalar(value.get("percentile"))
    if percentile is not None:
        normalized["percentile"] = percentile
    category = _pagespeed_scalar(value.get("category"), max_length=32)
    if isinstance(category, str):
        normalized["category"] = category
    return normalized or None


def _normalize_pagespeed_loading(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {}
    identifier = _pagespeed_scalar(value.get("id"), max_length=2048)
    if isinstance(identifier, str):
        normalized["id"] = identifier
    initial_url = _pagespeed_url(value.get("initial_url"))
    if initial_url:
        normalized["initial_url"] = initial_url
    overall_category = _pagespeed_scalar(value.get("overall_category"), max_length=32)
    if isinstance(overall_category, str):
        normalized["overall_category"] = overall_category
    metrics = value.get("metrics")
    if isinstance(metrics, dict):
        normalized_metrics = {
            key: metric
            for key in _PAGESPEED_ALLOWED_FIELD_METRICS
            if (metric := _normalize_pagespeed_metric(metrics.get(key))) is not None
        }
        if normalized_metrics:
            normalized["metrics"] = normalized_metrics
    return normalized or None


def _normalize_pagespeed_lab(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {}
    requested_url = _pagespeed_url(value.get("requestedUrl"))
    if requested_url:
        normalized["requested_url"] = requested_url
    final_url = _pagespeed_url(value.get("finalUrl"))
    if final_url:
        normalized["final_url"] = final_url
    lighthouse_version = _pagespeed_scalar(value.get("lighthouseVersion"), max_length=64)
    if isinstance(lighthouse_version, str):
        normalized["lighthouse_version"] = lighthouse_version
    categories = value.get("categories")
    if isinstance(categories, dict):
        performance = categories.get("performance")
        if isinstance(performance, dict):
            score = performance.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                if math.isfinite(score) and 0 <= score <= 1:
                    normalized["performance_score"] = score
    audits = value.get("audits")
    if isinstance(audits, dict):
        normalized_audits: dict[str, dict[str, int | float | str]] = {}
        for key in _PAGESPEED_ALLOWED_AUDITS:
            audit = audits.get(key)
            if not isinstance(audit, dict):
                continue
            metric: dict[str, int | float | str] = {}
            numeric_value = _pagespeed_scalar(audit.get("numericValue"))
            if isinstance(numeric_value, (int, float)):
                metric["numeric_value"] = numeric_value
            display_value = _pagespeed_scalar(audit.get("displayValue"), max_length=128)
            if isinstance(display_value, str):
                metric["display_value"] = display_value
            if metric:
                normalized_audits[key] = metric
        if normalized_audits:
            normalized["audits"] = normalized_audits
    analysis_timestamp = _pagespeed_scalar(value.get("analysisUTCTimestamp"), max_length=64)
    if isinstance(analysis_timestamp, str):
        normalized["analysis_utc_timestamp"] = analysis_timestamp
    return normalized or None


def _normalize_pagespeed_result(
    payload: dict[str, Any],
    *,
    page_url: str,
    strategy: str,
) -> dict[str, Any]:
    """Keep only useful, named PageSpeed fields and classify their provenance."""

    lab = _normalize_pagespeed_lab(payload.get("lighthouseResult"))
    loading = _normalize_pagespeed_loading(payload.get("loadingExperience"))
    origin_loading = _normalize_pagespeed_loading(payload.get("originLoadingExperience"))
    has_lab = isinstance(payload.get("lighthouseResult"), dict)
    has_field_or_origin = isinstance(payload.get("loadingExperience"), dict) or isinstance(
        payload.get("originLoadingExperience"), dict
    )
    if has_lab and has_field_or_origin:
        context = "both"
    elif has_lab:
        context = "lab"
    elif has_field_or_origin:
        context = "field_or_origin"
    else:
        context = "unknown"
    assert context in _PAGESPEED_CONTEXTS

    normalized: dict[str, Any] = {
        "measurement_context": context,
        "url": page_url,
        "strategy": strategy,
    }
    if lab is not None:
        normalized["lab"] = lab
    if loading is not None:
        normalized["loading_experience"] = loading
    if origin_loading is not None:
        normalized["origin_loading_experience"] = origin_loading
    return normalized


def _api_error_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status_code")
    if isinstance(status, int) and status not in {20000, 200, 0}:
        return f"remote API status {status}"
    status_message = payload.get("status_message")
    if isinstance(status_message, str) and status_message and status not in {None, 20000, 200, 0}:
        return "remote API returned an error"
    tasks_error = payload.get("tasks_error")
    if isinstance(tasks_error, int) and not isinstance(tasks_error, bool) and tasks_error > 0:
        return "remote API returned task errors"
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_status = task.get("status_code")
            if (
                isinstance(task_status, int)
                and not isinstance(task_status, bool)
                and task_status not in _DATAFORSEO_SUCCESS_TASK_CODES
            ):
                return "remote API returned a task error"
    return None


def validate_citation_import(payload: Any) -> list[dict[str, Any]]:
    """Validate citation records from either ``payload`` or ``payload.items``.

    The function accepts a direct list because the measurements import route
    passes ``payload.items``.  Invalid records raise ``ValueError`` so callers
    can reject the complete import atomically instead of silently dropping
    citations.
    """

    if isinstance(payload, dict):
        items = payload.get("items")
        if items is None:
            items = payload.get("citations")
        if items is None:
            items = payload.get("sources")
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("citation import must be a list or an object containing items")
    if not isinstance(items, list):
        raise ValueError("citation items must be a list")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if isinstance(item, str):
            raw_url = item
            item_dict: dict[str, Any] = {"url": item}
        elif isinstance(item, dict):
            item_dict = item
            raw_url = item.get("url") or item.get("source_url") or item.get("link")
        else:
            raise ValueError(f"citation {index} must be an object or URL string")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ValueError(f"citation {index} is missing a URL")
        try:
            url = _validate_http_url(raw_url.strip())
        except ValueError as exc:
            raise ValueError(f"citation {index} has an invalid URL") from exc
        key = url.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized: dict[str, Any] = {"url": url}
        for field in ("title", "publisher", "published_at", "quote", "snippet", "source_kind"):
            value = item_dict.get(field)
            if value is not None:
                if not isinstance(value, (str, int, float, bool)):
                    raise ValueError(f"citation {index} field {field} is not scalar")
                normalized[field] = value
        output.append(normalized)
    return output


_IMPORT_KIND_ALIASES = {
    "ai": "ai_sample",
    "ai_answer": "ai_sample",
    "ai_sample": "ai_sample",
    "citation": "citation",
    "citations": "citation",
    "citation_report": "citation",
    "citation_import": "citation_import",
    "referral": "referral",
    "referral_traffic": "referral",
    "technical": "technical_eligibility",
    "technical_eligibility": "technical_eligibility",
    "backlink": "backlink_observation",
    "backlinks": "backlink_observation",
    "backlink_observation": "backlink_observation",
    "backlink_observations": "backlink_observation",
    "listing": "business_listing",
    "listings": "business_listing",
    "business_listing": "business_listing",
    "business_listings": "business_listing",
    "listing_observation": "business_listing",
    "business_listing_observation": "business_listing",
    "competitor": "competitor_observation",
    "competitors": "competitor_observation",
    "competitor_observation": "competitor_observation",
    "competitor_observations": "competitor_observation",
    "competitor_report": "competitor_observation",
    "competitor_research": "competitor_observation",
}
_IMPORT_KINDS = set(_IMPORT_KIND_ALIASES.values())

_BOUNDED_OBSERVATION_IMPORT_KINDS = frozenset({
    "backlink_observation",
    "business_listing",
    "competitor_observation",
})
_IMPORT_MAX_INSPECTION_DEPTH = 12
_IMPORT_MAX_INSPECTION_NODES = 2_048
_IMPORT_SENSITIVE_EXACT_KEYS = frozenset({
    "api_key",
    "application_password",
    "authorization",
    "bearer",
    "client_secret",
    "consumer_key",
    "consumer_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "password_hash",
    "private_key",
    "refresh_token",
    "secret",
    "token",
    "webhook_secret",
})
_OBSERVATION_MAX_FIELDS = 64
_OBSERVATION_MAX_KEY_LENGTH = 64
_OBSERVATION_MAX_VALUE_LENGTH = 512
_OBSERVATION_MAX_URL_LENGTH = 2048
_OBSERVATION_URL_KEYS = frozenset({
    "url",
    "backlink_url",
    "business_url",
    "competitor_url",
    "listing_url",
    "page_url",
    "profile_url",
    "referrer_url",
    "referring_url",
    "source_url",
    "target_url",
    "website_url",
})
_OBSERVATION_REQUIRED_URL_KEYS = {
    "backlink_observation": frozenset({
        "backlink_url",
        "page_url",
        "referrer_url",
        "referring_url",
        "source_url",
        "target_url",
        "url",
    }),
    "business_listing": frozenset({
        "business_url",
        "listing_url",
        "page_url",
        "profile_url",
        "url",
        "website_url",
    }),
    "competitor_observation": frozenset({
        "competitor_url",
        "page_url",
        "url",
        "website_url",
    }),
}
_OBSERVATION_RANKING_KEYS = frozenset({
    "consumer_ranking",
    "consumer_rankings",
    "is_consumer_ranking",
    "organic_rank",
    "ranking",
    "ranking_claim",
    "ranking_type",
    "rank",
    "search_position",
    "position",
})


def _import_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observed_at must be an ISO-8601 timestamp")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _is_import_sensitive_key(value: str) -> bool:
    normalized = value.casefold().strip().replace("-", "_").replace(" ", "_")
    if normalized in _IMPORT_SENSITIVE_EXACT_KEYS:
        return True
    return normalized.endswith((
        "_api_key",
        "_cookie",
        "_credential",
        "_credentials",
        "_password",
        "_private_key",
        "_secret",
        "_token",
    ))


def _reject_import_credentials(value: Any, index: int, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Reject credential-shaped keys before imported data reaches storage."""

    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > _IMPORT_MAX_INSPECTION_NODES:
        raise ValueError(f"measurement {index} data is too large to inspect")
    if depth > _IMPORT_MAX_INSPECTION_DEPTH:
        raise ValueError(f"measurement {index} data is nested too deeply")
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"measurement {index} data contains an invalid field name")
            if _is_import_sensitive_key(key):
                raise ValueError(f"measurement {index} contains credential-shaped field {key}")
            _reject_import_credentials(nested, index, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_import_credentials(nested, index, depth=depth + 1, nodes=nodes)


def _validate_bounded_observation(
    index: int,
    kind: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Validate an imported bounded backlink, listing, or competitor observation.

    These records are observations, not search-position reports.  Keep their
    provider-supplied scalar fields for provenance, while rejecting nested or
    unbounded values that could turn the measurement import into an arbitrary
    document store.
    """

    if len(data) > _OBSERVATION_MAX_FIELDS:
        raise ValueError(f"measurement {index} observation has too many fields")

    normalized: dict[str, Any] = {}
    public_urls: set[str] = set()
    for field, value in data.items():
        if not isinstance(field, str) or not field.strip() or len(field) > _OBSERVATION_MAX_KEY_LENGTH:
            raise ValueError(f"measurement {index} observation has an invalid field name")
        field = field.strip()
        field_key = field.casefold().replace("-", "_").replace(" ", "_")

        if field_key in _OBSERVATION_RANKING_KEYS:
            if kind == "competitor_observation" and field_key in {
                "organic_rank",
                "position",
                "rank",
                "search_position",
            }:
                pass
            elif field_key == "consumer_rankings" and value is False:
                normalized[field] = False
                continue
            else:
                raise ValueError(f"measurement {index} {kind} cannot claim consumer rankings")

        if value is None:
            normalized[field] = None
            continue
        if isinstance(value, bool):
            normalized[field] = value
            continue
        if isinstance(value, str):
            max_length = (
                _OBSERVATION_MAX_URL_LENGTH
                if field_key in _OBSERVATION_URL_KEYS
                else _OBSERVATION_MAX_VALUE_LENGTH
            )
            if len(value) > max_length:
                raise ValueError(f"measurement {index} observation field {field} is too long")
            if any(ord(char) < 32 for char in value):
                raise ValueError(f"measurement {index} observation field {field} contains control characters")
            if field_key in _OBSERVATION_URL_KEYS:
                try:
                    value = _validate_http_url(value)
                except ValueError as exc:
                    raise ValueError(f"measurement {index} observation field {field} must be a public HTTP URL") from exc
                public_urls.add(field_key)
            normalized[field] = value
            continue
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"measurement {index} observation field {field} must be a finite scalar")
            if abs(value) > 1_000_000_000_000:
                raise ValueError(f"measurement {index} observation field {field} is out of bounds")
            normalized[field] = value
            continue
        raise ValueError(f"measurement {index} observation field {field} must be scalar")

    required_urls = _OBSERVATION_REQUIRED_URL_KEYS[kind]
    if not public_urls.intersection(required_urls):
        raise ValueError(f"measurement {index} {kind} requires a public HTTP URL")
    if kind == "competitor_observation":
        normalized["observation_scope"] = "competitor"
        normalized["ranking_type"] = "observed_competitor"
        normalized["consumer_rankings"] = False
    else:
        normalized.setdefault("consumer_rankings", False)
    return normalized


def validate_measurement_import(payload: Any) -> list[dict[str, Any]]:
    """Normalize owner-supplied visibility observations without inventing data.

    Older callers may submit a bare citation object.  It remains accepted as a
    ``citation_import`` record.  Newer imports can preserve an observation's
    kind, source, timestamp, and provider-specific data.  AI samples require an
    answer and explicit citations and are always labelled as provider samples,
    never as consumer rankings.
    """

    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list):
        raise ValueError("measurement import must be a list")
    output: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"measurement {index} must be an object")
        _reject_import_credentials(item, index)

        # Preserve the original citation-only import contract.
        is_envelope = any(key in item for key in ("kind", "source", "data", "observed_at"))
        if not is_envelope:
            citation = validate_citation_import([item])[0]
            output.append({
                "kind": "citation_import",
                "source": "owner_import",
                "data": citation,
                "observed_at": None,
            })
            continue

        raw_kind = item.get("kind")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            raise ValueError(f"measurement {index} is missing kind")
        kind_key = raw_kind.strip().casefold().replace("-", "_").replace(" ", "_")
        kind = _IMPORT_KIND_ALIASES.get(kind_key)
        if kind not in _IMPORT_KINDS:
            raise ValueError(f"measurement {index} has unsupported kind")

        source = item.get("source", "owner_import")
        if not isinstance(source, str) or not source.strip() or len(source.strip()) > 128:
            raise ValueError(f"measurement {index} has an invalid source")
        source = source.strip()
        data = item.get("data")
        if data is None and kind in _BOUNDED_OBSERVATION_IMPORT_KINDS:
            data = {
                key: value
                for key, value in item.items()
                if key not in {"kind", "source", "observed_at", "data"}
            }
        elif data is None:
            # Accept flat exports for provenance fields while keeping the
            # persisted Measurement shape consistent.
            data = {
                key: item[key]
                for key in (
                    "provider", "model", "question", "query", "locale",
                    "answer", "sample", "samples", "citations", "sources",
                    "url", "title", "publisher", "sessions", "referrals",
                    "status", "details",
                )
                if key in item
            }
        if not isinstance(data, dict) or not data:
            raise ValueError(f"measurement {index} data must be a non-empty object")
        data = dict(data)

        if kind in _BOUNDED_OBSERVATION_IMPORT_KINDS:
            data = _validate_bounded_observation(index, kind, data)

        if kind in {"citation", "citation_import"}:
            raw_citations = data.get("citations") or data.get("sources")
            if raw_citations is not None:
                citations = validate_citation_import(raw_citations)
                if not citations:
                    raise ValueError(f"measurement {index} must contain at least one citation")
                data["citations"] = citations
                data.pop("sources", None)
            else:
                data = validate_citation_import([data])[0]

        if kind == "ai_sample":
            answer = data.get("answer")
            if answer is None:
                answer = data.get("sample")
            if answer is None:
                answer = data.get("samples")
            if answer is None:
                raise ValueError(f"measurement {index} AI sample is missing an answer")
            citations = data.get("citations") or data.get("sources")
            if citations is None:
                raise ValueError(f"measurement {index} AI sample is missing citations")
            citations = validate_citation_import(citations)
            if not citations:
                raise ValueError(f"measurement {index} AI sample must contain citations")
            question = data.get("question") or data.get("query")
            if not isinstance(question, (str, list)) or not question:
                raise ValueError(f"measurement {index} AI sample is missing a question")
            provider = data.get("provider")
            if provider is None and source != "owner_import":
                provider = source
            if provider is not None and (not isinstance(provider, str) or not provider.strip()):
                raise ValueError(f"measurement {index} has an invalid provider")
            data["answer"] = answer
            data["question"] = question
            data["citations"] = citations
            data.pop("sources", None)
            # This classification is part of the contract, not a ranking
            # claim.  An import cannot assert that a provider sample is a
            # universal consumer ranking.
            if data.get("consumer_rankings") not in (None, False):
                raise ValueError(f"measurement {index} AI sample cannot be marked as consumer rankings")
            data["consumer_rankings"] = False
            data["ranking_type"] = "ai_answer"

        try:
            json.dumps(data)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"measurement {index} data must be JSON serializable") from exc
        output.append({
            "kind": kind,
            "source": source,
            "data": data,
            "observed_at": _import_timestamp(item.get("observed_at")),
        })
    return output


def _dataforseo_auth(credentials: dict[str, Any]) -> str | None:
    login = _credential_text(credentials.get("login")) or _credential_text(credentials.get("username"))
    password = _credential_text(credentials.get("password"))
    if not login or not password:
        return None
    raw = f"{login}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _dataforseo_body(settings: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
    def keyword_values(value: Any) -> list[str] | None:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value or len(value) > _DATAFORSEO_MAX_KEYWORDS:
            return None
        output: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                return None
            item = item.strip()
            if not item or len(item) > _DATAFORSEO_MAX_KEYWORD_LENGTH:
                return None
            if item.casefold() in seen:
                continue
            seen.add(item.casefold())
            output.append(item)
        return output or None

    mode = _normalized_text(settings.get("mode") or settings.get("task_type") or "serp").casefold()
    if mode in {"competitor", "competitors", "competitor_observation", "competitor_research", "competitors_domain"}:
        def domain_value(value: Any) -> str | None:
            if not isinstance(value, str) or not value.strip():
                return None
            raw = value.strip()
            candidate = raw if "://" in raw else f"https://{raw}"
            try:
                normalized = _validate_http_url(candidate)
            except ValueError:
                return None
            parsed = urlsplit(normalized)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
                or not _host_is_public(parsed.hostname)
            ):
                return None
            host = parsed.hostname.casefold().rstrip(".")
            return host[4:] if host.startswith("www.") else host

        target = domain_value(settings.get("target") or settings.get("site_url") or settings.get("origin"))
        if target is None:
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "a public target site domain is required"
        raw_competitors = settings.get("competitors")
        if raw_competitors is None:
            raw_competitors = settings.get("tracked_competitors")
        if isinstance(raw_competitors, str):
            raw_competitors = raw_competitors.split(",")
        if not isinstance(raw_competitors, list) or not raw_competitors or len(raw_competitors) > 3:
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "1-3 competitor domains are required"
        competitors: list[str] = []
        for value in raw_competitors:
            domain = domain_value(value)
            if domain is None:
                return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "competitors must be public domains or HTTP URLs"
            if domain not in competitors:
                competitors.append(domain)
        if not competitors:
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "1-3 competitor domains are required"

        location_code = settings.get("location_code")
        if isinstance(location_code, str) and location_code.strip().isdigit():
            location_code = int(location_code.strip())
        location_name = settings.get("location_name")
        if location_code is not None and (isinstance(location_code, bool) or not isinstance(location_code, int) or location_code <= 0):
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "location_code must be a positive integer"
        if location_name is not None and (not isinstance(location_name, str) or not location_name.strip() or len(location_name.strip()) > 128):
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "location_name must be a bounded string"
        if location_code is None and location_name is None:
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "location_code or location_name is required"
        language_code = settings.get("language_code") or "en"
        if not isinstance(language_code, str) or not language_code.strip() or len(language_code.strip()) > 32:
            return str(settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT), [], "language_code must be a bounded string"
        task: dict[str, Any] = {
            "target": target,
            "intersecting_domains": competitors,
            "language_code": language_code.strip(),
            "location_code": location_code,
            "location_name": location_name.strip() if isinstance(location_name, str) else None,
            "limit": len(competitors),
        }
        task = {key: value for key, value in task.items() if value is not None}
        endpoint = settings.get("endpoint") or _DATAFORSEO_COMPETITOR_ENDPOINT
        return str(endpoint), [task], None
    if mode in {"keyword", "keywords", "keyword_data", "search_volume"}:
        endpoint = settings.get("endpoint") or settings.get("base_url") or _DATAFORSEO_KEYWORD_ENDPOINT
        keywords = settings.get("keywords")
        if keywords is None and settings.get("keyword") is not None:
            keywords = [settings.get("keyword")]
        keywords = keyword_values(keywords)
        if keywords is None:
            return str(endpoint), [], f"1-{_DATAFORSEO_MAX_KEYWORDS} keywords are required"
        task: dict[str, Any] = {
            "keywords": keywords,
            "language_code": settings.get("language_code", "en"),
            "location_code": settings.get("location_code"),
        }
        task = {key: value for key, value in task.items() if value is not None}
        return str(endpoint), [task], None
    endpoint = settings.get("endpoint") or settings.get("base_url") or _DATAFORSEO_SERP_ENDPOINT
    raw_keywords = settings.get("keyword")
    if raw_keywords is None:
        raw_keywords = settings.get("keywords")
    keywords = keyword_values(raw_keywords)
    if keywords is None:
        return str(endpoint), [], f"1-{_DATAFORSEO_MAX_KEYWORDS} SERP keywords are required"
    tasks = []
    for keyword in keywords:
        task = {
            "keyword": keyword,
            "language_code": settings.get("language_code", "en"),
            "location_code": settings.get("location_code"),
            "location_name": settings.get("location_name"),
            "device": settings.get("device", "desktop"),
            "os": settings.get("os", "windows"),
            "depth": settings.get("depth", 100),
        }
        tasks.append({key: value for key, value in task.items() if value is not None})
    return str(endpoint), tasks, None


async def _collect_google(
    kind: str,
    credentials: dict[str, Any],
    settings: dict[str, Any],
    client: httpx.AsyncClient,
    estimate: int,
    max_cost: int | None,
) -> dict[str, Any]:
    access_token, refreshed, oauth_error = await _oauth_access(client, credentials, settings)
    if oauth_error or not access_token:
        return _error(
            kind,
            "missing_connection" if "not configured" in (oauth_error or "") else "oauth_refresh_failed",
            oauth_error or "OAuth access token is unavailable",
            metadata=_metadata(kind, estimate, max_cost, token_refreshed=refreshed, status="error"),
        )
    start_date, end_date = _date_range(settings)
    headers = _auth_headers(credentials, access_token=access_token)
    refreshed_once = refreshed
    while True:
        if kind == "gsc":
            site_url = settings.get("site_url") or credentials.get("site_url")
            if not isinstance(site_url, str) or not site_url.strip():
                return _error(kind, "missing_setting", "site_url is required", metadata=_metadata(kind, estimate, max_cost))
            endpoint = settings.get("endpoint") or settings.get("base_url")
            if not endpoint:
                endpoint = f"{_GSC_ENDPOINT}/{quote(site_url, safe='')}/searchAnalytics/query"
            endpoint = _validated_endpoint(endpoint)
            request_body: dict[str, Any] = {
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": settings.get("dimensions", ["query", "page"]),
                "rowLimit": settings.get("row_limit", 1_000),
            }
            if settings.get("search_type"):
                request_body["type"] = settings["search_type"]
            method = client.post
            request_args = {"json": request_body, "headers": headers}
        else:
            property_id = _normalized_text(settings.get("property") or settings.get("property_id") or credentials.get("property"))
            if not property_id:
                return _error(kind, "missing_setting", "property is required", metadata=_metadata(kind, estimate, max_cost))
            if property_id.startswith("properties/"):
                property_id = property_id.split("/", 1)[1]
            endpoint = settings.get("endpoint") or settings.get("base_url") or f"{_GA4_ENDPOINT}/{quote(property_id, safe='')}:runReport"
            endpoint = _validated_endpoint(endpoint)
            dimensions = _ga4_values(settings, "dimensions", ["date"], max_items=8, max_item_length=64)
            metrics = _ga4_values(settings, "metrics", ["sessions", "totalUsers"], max_items=10, max_item_length=64)
            conversion_event_names = _ga4_values(
                settings,
                "conversion_event_names",
                [],
                max_items=12,
                max_item_length=40,
            )
            if dimensions is None or metrics is None or conversion_event_names is None:
                return _error(
                    kind,
                    "invalid_setting",
                    "GA4 report settings contain an invalid or oversized list",
                    metadata=_metadata(kind, estimate, max_cost, status="error"),
                )
            request_body = {
                "dateRanges": [{"startDate": start_date, "endDate": end_date}],
                "dimensions": [{"name": item} for item in dimensions],
                "metrics": [{"name": item} for item in metrics],
            }
            if conversion_event_names:
                request_body["dimensionFilter"] = {
                    "filter": {
                        "fieldName": "eventName",
                        "inListFilter": {
                            "values": conversion_event_names,
                            "caseSensitive": False,
                        },
                    },
                }
            method = client.post
            request_args = {"json": request_body, "headers": headers}
        if not endpoint:
            return _error(kind, "invalid_endpoint", "Google API endpoint is invalid", metadata=_metadata(kind, estimate, max_cost))
        try:
            response = await method(endpoint, **request_args)
        except httpx.HTTPError as exc:
            return _error(
                kind,
                "unavailable",
                f"visibility request failed: {type(exc).__name__}",
                metadata=_metadata(kind, estimate, max_cost, token_refreshed=refreshed_once, status="error"),
            )
        if response.status_code == 401 and not refreshed_once and credentials.get("refresh_token"):
            refreshed_token, refresh_error = await _refresh_oauth(client, credentials, settings)
            if refreshed_token:
                headers = _auth_headers(credentials, access_token=refreshed_token)
                refreshed_once = True
                continue
            return _error(
                kind,
                "oauth_refresh_failed",
                refresh_error or "OAuth refresh failed",
                metadata=_metadata(kind, estimate, max_cost, token_refreshed=False, status="error"),
            )
        payload = _safe_json(response)
        if response.status_code < 200 or response.status_code >= 300:
            return _error(
                kind,
                "remote_error",
                f"visibility API returned HTTP {response.status_code}",
                metadata=_metadata(kind, estimate, max_cost, token_refreshed=refreshed_once, status="error"),
            )
        if not isinstance(payload, dict):
            return _error(
                kind,
                "invalid_response",
                "visibility API returned non-JSON data",
                metadata=_metadata(kind, estimate, max_cost, token_refreshed=refreshed_once, status="error"),
            )
        actual, cost_basis, usage = _cost_info(payload, estimate)
        return _result(
            kind,
            kind,
            data=payload,
            cost_cents=actual,
            metadata=_metadata(
                kind,
                estimate,
                max_cost,
                token_refreshed=refreshed_once,
                status="ok",
                cost_basis=cost_basis,
                usage=usage,
                **({
                    "report_dimensions": dimensions,
                    "report_metrics": metrics,
                    "conversion_event_names": conversion_event_names,
                } if kind == "ga4" else {}),
            ),
        )


async def _collect_dataforseo(
    credentials: dict[str, Any],
    settings: dict[str, Any],
    client: httpx.AsyncClient,
    estimate: int,
    max_cost: int | None,
) -> dict[str, Any]:
    authorization = _dataforseo_auth(credentials)
    if not authorization:
        return _error("dataforseo", "missing_connection", "DataForSEO login and password are required", metadata=_metadata("dataforseo", estimate, max_cost, status="error"))
    endpoint, body, body_error = _dataforseo_body(settings)
    if body_error:
        return _error("dataforseo", "missing_setting", body_error, metadata=_metadata("dataforseo", estimate, max_cost, status="error"))
    endpoint = _validated_endpoint(endpoint)
    if not endpoint:
        return _error("dataforseo", "invalid_endpoint", "DataForSEO endpoint is invalid", metadata=_metadata("dataforseo", estimate, max_cost, status="error"))
    try:
        response = await client.post(endpoint, json=body, headers={"Accept": "application/json", "Authorization": authorization})
    except httpx.HTTPError as exc:
        return _error("dataforseo", "unavailable", f"DataForSEO request failed: {type(exc).__name__}", metadata=_metadata("dataforseo", estimate, max_cost, status="error"))
    payload = _safe_json(response)
    if response.status_code < 200 or response.status_code >= 300:
        return _error("dataforseo", "remote_error", f"DataForSEO returned HTTP {response.status_code}", metadata=_metadata("dataforseo", estimate, max_cost, status="error"))
    if not isinstance(payload, dict):
        return _error("dataforseo", "invalid_response", "DataForSEO returned non-JSON data", metadata=_metadata("dataforseo", estimate, max_cost, status="error"))
    api_error = _api_error_payload(payload)
    if api_error:
        actual, cost_basis, usage = _cost_info(payload, estimate)
        metadata = _metadata(
            "dataforseo",
            estimate,
            max_cost,
            status="error",
            cost_basis=cost_basis,
            usage=usage,
        )
        if max_cost is not None and actual > max_cost:
            metadata["over_max_cost"] = True
        return _error(
            "dataforseo",
            "remote_error",
            api_error,
            cost_cents=actual,
            metadata=metadata,
        )
    actual, cost_basis, usage = _cost_info(payload, estimate)
    mode = _normalized_text(settings.get("mode") or settings.get("task_type") or "serp").casefold()
    metadata = _metadata(
        "dataforseo",
        estimate,
        max_cost,
        status="ok",
        mode=mode or "serp",
        cost_basis=cost_basis,
        usage=usage,
    )
    if max_cost is not None and actual > max_cost:
        metadata["over_max_cost"] = True
    if mode in {"competitor", "competitors", "competitor_observation", "competitor_research", "competitors_domain"}:
        request = body[0] if body else {}
        return _result(
            "competitor_observation",
            "dataforseo",
            data={
                "provider": "DataForSEO",
                "observation_scope": "competitor",
                "ranking_type": "observed_competitor",
                "consumer_rankings": False,
                "target": request.get("target"),
                "competitors": request.get("intersecting_domains", []),
                "response": payload,
            },
            cost_cents=actual,
            metadata=metadata,
        )
    return _result("dataforseo", "dataforseo", data=payload, cost_cents=actual, metadata=metadata)


def _openai_responses_web_search_format(settings: dict[str, Any]) -> bool:
    request_format = _normalized_text(settings.get("request_format") or "").casefold()
    return request_format in _OPENAI_RESPONSES_WEB_SEARCH_FORMATS


def paid_visibility_preflight_error(
    kind: str,
    credentials: dict[str, Any],
    settings: dict[str, Any],
) -> str | None:
    """Return a safe pre-reservation error for missing paid credentials.

    The collection functions also validate these values, but paid workflow
    code must perform this pure check before reserving a monthly budget. It
    prevents a blank credential from creating a reservation that can never
    reach a provider request.
    """

    if kind == "dataforseo" and not _dataforseo_auth(credentials):
        return "DataForSEO login and password are required before paid work"
    if kind == "ai_sample":
        if _openai_responses_web_search_format(settings):
            if not any(_credential_text(credentials.get(key)) for key in ("api_key", "access_token", "token")):
                return "OpenAI API key is required before paid work"
        elif not _has_auth_credential(credentials):
            return "AI sample credentials are required before paid work"
    return None


def _openai_models_endpoint(endpoint: str) -> str | None:
    """Derive the same-origin OpenAI models endpoint from ``/responses``."""

    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/")
    if not path.endswith("/responses"):
        return None
    model_path = f"{path[:-len('/responses')]}/models"
    if not model_path.startswith("/"):
        model_path = f"/{model_path}"
    return urlunsplit((parts.scheme, parts.netloc, model_path, "", ""))


async def verify_ai_connection(
    credentials: dict[str, Any],
    settings: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Verify an explicit OpenAI Responses connection without paid sampling.

    The model-list request is read-only.  This deliberately does not call the
    Responses endpoint or the web-search tool, so a connection test cannot
    consume the configured AI observation budget.
    """

    settings = settings if isinstance(settings, dict) else {}
    if not _openai_responses_web_search_format(settings):
        return {
            "status": "unsupported",
            "kind": "ai",
            "read_only": True,
            "error": {
                "code": "unsupported_format",
                "message": "Select an explicit OpenAI Responses format to verify the model connection",
            },
        }

    api_key = (
        _credential_text(credentials.get("api_key"))
        or _credential_text(credentials.get("access_token"))
        or _credential_text(credentials.get("token"))
    )
    if not api_key:
        return {
            "status": "error",
            "kind": "ai",
            "error": {"code": "missing_connection", "message": "OpenAI API key is required"},
        }

    endpoint = _validated_endpoint(
        settings.get("endpoint") or settings.get("base_url") or _OPENAI_RESPONSES_ENDPOINT
    )
    if not endpoint:
        return {
            "status": "error",
            "kind": "ai",
            "error": {"code": "invalid_endpoint", "message": "OpenAI Responses endpoint is invalid"},
        }
    models_endpoint = _openai_models_endpoint(endpoint)
    if not models_endpoint:
        return {
            "status": "error",
            "kind": "ai",
            "error": {
                "code": "invalid_endpoint",
                "message": "OpenAI Responses endpoint must end in /responses",
            },
        }

    model = settings.get("model")
    if (
        not isinstance(model, str)
        or not model.strip()
        or len(model.strip()) > 128
        or any(ord(char) < 32 for char in model)
    ):
        return {
            "status": "error",
            "kind": "ai",
            "error": {"code": "missing_setting", "message": "OpenAI Responses model is required"},
        }
    model = model.strip()

    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport if transport is not None else _default_transport(),
        ) as provider_client:
            response = await provider_client.get(models_endpoint, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "status": "error",
            "kind": "ai",
            "error": {"code": "unavailable", "message": f"OpenAI model check failed: {type(exc).__name__}"},
        }

    if response.status_code < 200 or response.status_code >= 300:
        return {
            "status": "error",
            "kind": "ai",
            "error": {
                "code": "remote_error",
                "message": f"OpenAI model check returned HTTP {response.status_code}",
            },
        }
    payload = _safe_json(response)
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return {
            "status": "error",
            "kind": "ai",
            "error": {"code": "invalid_response", "message": "OpenAI model check returned an invalid model list"},
        }
    available_ids = {
        item.get("id")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if model not in available_ids:
        return {
            "status": "error",
            "kind": "ai",
            "error": {"code": "model_unavailable", "message": "Configured OpenAI model is not available"},
        }
    return {
        "status": "verified",
        "kind": "ai",
        "provider": "OpenAI",
        "model": model,
        "model_available": True,
        "read_only": True,
    }


def _openai_response_output(
    payload: Any,
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """Extract only completed, cited answer content from a Responses payload."""

    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return None, [], "incomplete_response"
    output = payload.get("output")
    if not isinstance(output, list):
        return None, [], "invalid_response"

    text_parts: list[str] = []
    raw_citations: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("status") not in (None, "completed"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str) and _normalized_text(text):
                text_parts.append(text.strip())
            annotations = part.get("annotations")
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                citation = annotation.get("url_citation")
                if not isinstance(citation, dict):
                    citation = annotation
                url = citation.get("url")
                if not isinstance(url, str) or not url.strip():
                    # An incomplete citation is not silently turned into a
                    # source record; the complete response will fail closed.
                    return None, [], "invalid_citations"
                record: dict[str, Any] = {"url": url.strip(), "source_kind": "openai_web_search"}
                title = citation.get("title")
                if title is not None:
                    if not isinstance(title, str):
                        return None, [], "invalid_citations"
                    record["title"] = title.strip()
                raw_citations.append(record)

    answer = "\n\n".join(text_parts).strip()
    if not answer:
        return None, [], "answer_required"
    if len(answer) > _AI_MAX_ANSWER_LENGTH:
        return None, [], "answer_too_large"
    if not raw_citations:
        return None, [], "citations_required"
    try:
        citations = validate_citation_import(raw_citations)
    except ValueError:
        return None, [], "invalid_citations"
    if not citations:
        return None, [], "citations_required"
    return answer, citations, None


def _openai_usage_summary(payload: Any) -> dict[str, int] | None:
    """Keep token usage bounded to numeric counters; never persist raw output."""

    usage = _usage(payload)
    if not isinstance(usage, dict):
        return None
    summary: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000:
            summary[key] = value
    return summary or None


async def _collect_openai_responses_web_search(
    credentials: dict[str, Any],
    settings: dict[str, Any],
    client: httpx.AsyncClient,
    estimate: int,
    max_cost: int | None,
) -> dict[str, Any]:
    api_key = (
        _credential_text(credentials.get("api_key"))
        or _credential_text(credentials.get("access_token"))
        or _credential_text(credentials.get("token"))
    )
    if not api_key:
        return _error(
            "ai_sample",
            "missing_connection",
            "OpenAI API key is required",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )

    endpoint = _validated_endpoint(
        settings.get("endpoint") or settings.get("base_url") or _OPENAI_RESPONSES_ENDPOINT
    )
    if not endpoint:
        return _error(
            "ai_sample",
            "invalid_endpoint",
            "OpenAI Responses endpoint is invalid",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )

    model = settings.get("model")
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > 128 or any(ord(char) < 32 for char in model):
        return _error(
            "ai_sample",
            "missing_setting",
            "OpenAI Responses model is required",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )
    model = model.strip()

    if settings.get("request_body") is not None:
        return _error(
            "ai_sample",
            "invalid_setting",
            "request_body is not supported for the OpenAI Responses web-search format",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )
    question_keys = ("query", "queries", "questions")
    configured_keys = [key for key in question_keys if key in settings]
    if len(configured_keys) != 1:
        return _error(
            "ai_sample",
            "missing_setting" if not configured_keys else "invalid_setting",
            "OpenAI Responses sampling requires exactly one query field",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )
    question_key = configured_keys[0]
    questions = _bounded_ai_questions(settings[question_key], singular=question_key == "query")
    if questions is None:
        return _error(
            "ai_sample",
            "invalid_setting",
            f"AI sample {question_key} must contain 1-{_AI_MAX_QUESTIONS} bounded questions",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )

    total_estimate = estimate * len(questions)
    total_max = max_cost * len(questions) if max_cost is not None else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    context_size = _normalized_text(settings.get("search_context_size") or "low").casefold()
    if context_size not in {"low", "medium", "high"}:
        context_size = "low"
    output_limit = settings.get("max_output_tokens", 1200)
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 128 <= output_limit <= 4_096:
        output_limit = 1_200
    locale = _credential_text(settings.get("locale") or settings.get("language_code"))
    instructions = (
        "Use web search to answer the question with current, factual information. "
        "Keep the answer concise, cite the sources used, and do not claim universal "
        "search rankings or invent a source. This is a provider observation, not a "
        "guarantee of what every user sees."
    )
    samples: list[dict[str, Any]] = []
    usage_records: list[dict[str, int]] = []
    total_cost = 0
    all_actual = True
    observed_at = _now()

    for question in questions:
        body: dict[str, Any] = {
            "model": model,
            "input": question,
            "instructions": instructions,
            "tools": [{"type": "web_search", "search_context_size": context_size}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "store": False,
            "max_output_tokens": output_limit,
        }
        try:
            response = await client.post(endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return _error(
                "ai_sample",
                "unavailable",
                f"OpenAI Responses request failed: {type(exc).__name__}",
                cost_cents=total_cost,
                metadata=_metadata(
                    "ai_sample", total_estimate, total_max, status="error",
                    completed_questions=len(samples), question_count=len(questions),
                    cost_basis="estimated", usage=usage_records or None,
                ),
            )
        payload = _safe_json(response)
        if response.status_code < 200 or response.status_code >= 300:
            return _error(
                "ai_sample",
                "remote_error",
                f"OpenAI Responses API returned HTTP {response.status_code}",
                cost_cents=total_cost,
                metadata=_metadata(
                    "ai_sample", total_estimate, total_max, status="error",
                    completed_questions=len(samples), question_count=len(questions),
                    cost_basis="estimated", usage=usage_records or None,
                ),
            )
        if not isinstance(payload, dict):
            return _error(
                "ai_sample",
                "invalid_response",
                "OpenAI Responses API returned non-JSON data",
                cost_cents=total_cost,
                metadata=_metadata(
                    "ai_sample", total_estimate, total_max, status="error",
                    completed_questions=len(samples), question_count=len(questions),
                    cost_basis="estimated", usage=usage_records or None,
                ),
            )

        answer, citations, parse_error = _openai_response_output(payload)
        if parse_error:
            message = {
                "incomplete_response": "OpenAI Responses API did not complete the answer",
                "invalid_response": "OpenAI Responses API returned an invalid output shape",
                "answer_required": "OpenAI Responses API did not include an answer",
                "answer_too_large": "OpenAI Responses answer exceeded the safety limit",
                "citations_required": "OpenAI Responses answer did not include explicit citations",
                "invalid_citations": "OpenAI Responses citations failed validation",
            }.get(parse_error, "OpenAI Responses answer could not be validated")
            return _error(
                "ai_sample",
                parse_error,
                message,
                cost_cents=total_cost,
                metadata=_metadata(
                    "ai_sample", total_estimate, total_max, status="error",
                    completed_questions=len(samples), question_count=len(questions),
                    cost_basis="estimated", usage=usage_records or None,
                ),
            )

        actual, cost_basis, _provider_usage = _cost_info(payload, estimate)
        total_cost += actual
        all_actual = all_actual and cost_basis == "provider_actual"
        usage = _openai_usage_summary(payload)
        if usage is not None:
            usage_records.append(usage)
        sample: dict[str, Any] = {
            "provider": "OpenAI",
            "model": model,
            "question": question,
            "answer": answer,
            "citations": citations,
            "observed_at": observed_at,
            "ranking_type": "ai_answer",
            "consumer_rankings": False,
        }
        if locale:
            sample["locale"] = locale
        samples.append(sample)
        if total_max is not None and total_cost > total_max:
            return _error(
                "ai_sample",
                "cost_exceeded",
                "OpenAI Responses provider cost exceeded max_cost_cents",
                cost_cents=total_cost,
                metadata=_metadata(
                    "ai_sample", total_estimate, total_max, status="error",
                    completed_questions=len(samples), question_count=len(questions),
                    cost_basis="provider_actual" if all_actual else "estimated",
                    usage=usage_records or None, over_max_cost=True,
                ),
            )

    flattened_citations = validate_citation_import(
        [citation for sample in samples for citation in sample["citations"]]
    )
    data: dict[str, Any] = {
        "provider": "OpenAI",
        "model": model,
        "question": questions[0] if len(questions) == 1 else questions,
        "query": questions[0] if len(questions) == 1 else questions,
        "answer": samples[0]["answer"] if len(samples) == 1 else [sample["answer"] for sample in samples],
        "samples": samples,
        "citations": flattened_citations,
        "ranking_type": "ai_answer",
        "consumer_rankings": False,
    }
    if locale:
        data["locale"] = locale
    return _result(
        "ai_sample",
        "ai_sample",
        data=data,
        cost_cents=total_cost,
        metadata=_metadata(
            "ai_sample", total_estimate, total_max, status="ok",
            question_count=len(questions),
            per_question_estimated_cost_cents=estimate,
            per_question_max_cost_cents=max_cost,
            ranking_type="ai_answer",
            consumer_rankings=False,
            cost_basis="provider_actual" if all_actual else "estimated",
            usage=usage_records or None,
        ),
        observed_at=observed_at,
    )


async def _collect_ai_sample(
    credentials: dict[str, Any],
    settings: dict[str, Any],
    client: httpx.AsyncClient,
    estimate: int,
    max_cost: int | None,
) -> dict[str, Any]:
    if _openai_responses_web_search_format(settings):
        return await _collect_openai_responses_web_search(
            credentials, settings, client, estimate, max_cost
        )
    if not _has_auth_credential(credentials):
        return _error(
            "ai_sample",
            "missing_connection",
            "AI sample credentials are required",
            metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
        )
    endpoint = _validated_endpoint(
        settings.get("endpoint") or settings.get("base_url") or credentials.get("endpoint") or credentials.get("base_url")
    )
    if not endpoint:
        return _error("ai_sample", "missing_connection", "AI sample endpoint is not configured", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    question_keys = ("query", "queries", "questions")
    configured_keys = [key for key in question_keys if key in settings]
    raw_body = settings.get("request_body")
    if raw_body is not None:
        if not isinstance(raw_body, dict):
            return _error(
                "ai_sample",
                "invalid_setting",
                "AI sample request_body must be an object",
                metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
            )
        body = dict(raw_body)
        body_keys = [key for key in question_keys if key in body]
        if len(body_keys) != 1:
            return _error(
                "ai_sample",
                "invalid_setting",
                "AI sample request_body must contain exactly one bounded query field",
                metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
            )
        question_key = body_keys[0]
        question_values = _bounded_ai_questions(body[question_key], singular=question_key == "query")
        if question_values is None:
            return _error(
                "ai_sample",
                "invalid_setting",
                f"AI sample {question_key} must contain 1-{_AI_MAX_QUESTIONS} bounded questions",
                metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
            )
        request_question: str | list[str] = (
            question_values[0] if question_key == "query" else question_values
        )
    else:
        if len(configured_keys) > 1:
            return _error(
                "ai_sample",
                "invalid_setting",
                "AI sample settings must contain only one query field",
                metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
            )
        if not configured_keys:
            return _error("ai_sample", "missing_setting", "AI sample query is required", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
        question_key = configured_keys[0]
        question_values = _bounded_ai_questions(settings[question_key], singular=question_key == "query")
        if question_values is None:
            return _error(
                "ai_sample",
                "invalid_setting",
                f"AI sample {question_key} must contain 1-{_AI_MAX_QUESTIONS} bounded questions",
                metadata=_metadata("ai_sample", estimate, max_cost, status="error"),
            )
        request_question = question_values[0] if question_key == "query" else question_values
        body = {
            "query" if question_key == "query" else "queries": request_question,
            "model": settings.get("model"),
        }
        body = {key: value for key, value in body.items() if value is not None}
    headers = _auth_headers(credentials, access_token=credentials.get("access_token"))
    try:
        response = await client.post(endpoint, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return _error("ai_sample", "unavailable", f"AI sample request failed: {type(exc).__name__}", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    payload = _safe_json(response)
    if response.status_code < 200 or response.status_code >= 300:
        return _error("ai_sample", "remote_error", f"AI sample API returned HTTP {response.status_code}", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    if not isinstance(payload, dict):
        return _error("ai_sample", "invalid_response", "AI sample API returned non-JSON data", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_citations = nested.get("citations") or nested.get("sources")
    if not raw_citations:
        return _error("ai_sample", "citations_required", "AI sample response did not include explicit citations", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    try:
        citations = validate_citation_import(raw_citations)
    except ValueError:
        return _error("ai_sample", "invalid_citations", "AI sample citations failed validation", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    if not citations:
        return _error("ai_sample", "citations_required", "AI sample response contained no valid citations", metadata=_metadata("ai_sample", estimate, max_cost, status="error"))
    sample = nested.get("samples") or nested.get("answers") or nested.get("sample")
    if sample is None:
        sample = nested.get("answer") or nested.get("text")
    actual, cost_basis, usage = _cost_info(payload, estimate)

    def has_answer_content(value: Any) -> bool:
        if isinstance(value, str):
            return bool(_normalized_text(value))
        if isinstance(value, list):
            return any(has_answer_content(item) for item in value)
        if isinstance(value, dict):
            return any(has_answer_content(item) for item in value.values())
        return False

    if not has_answer_content(sample):
        return _error(
            "ai_sample",
            "answer_required",
            "AI sample response did not include an answer",
            cost_cents=actual,
            metadata=_metadata(
                "ai_sample",
                estimate,
                max_cost,
                status="error",
                cost_basis=cost_basis,
                usage=usage,
            ),
        )
    body_settings = body if isinstance(body, dict) else {}

    def provenance_text(key: str) -> str | None:
        for source in (nested, settings, body_settings, credentials):
            value = source.get(key)
            if isinstance(value, str) and _normalized_text(value):
                return value.strip()
        return None

    question = nested.get("question") or nested.get("query") or request_question
    optional_provenance = {
        "provider": provenance_text("provider"),
        "model": provenance_text("model"),
        "locale": provenance_text("locale") or provenance_text("language_code"),
    }
    data = {
        "samples": sample if sample is not None else [],
        "answer": sample,
        "citations": citations,
        "question": question,
        "query": question,
        "ranking_type": "ai_answer",
        "consumer_rankings": False,
    }
    data.update({key: value for key, value in optional_provenance.items() if value is not None})
    try:
        observed_at = _observation_timestamp(nested, payload, settings)
    except ValueError as exc:
        return _error(
            "ai_sample",
            "invalid_observation_timestamp",
            str(exc),
            cost_cents=actual,
            metadata=_metadata(
                "ai_sample",
                estimate,
                max_cost,
                status="error",
                cost_basis=cost_basis,
                usage=usage,
            ),
        )
    return _result(
        "ai_sample",
        "ai_sample",
        data=data,
        cost_cents=actual,
        metadata=_metadata(
            "ai_sample",
            estimate,
            max_cost,
            status="ok",
            ranking_type="ai_answer",
            consumer_rankings=False,
            cost_basis=cost_basis,
            usage=usage,
        ),
        observed_at=observed_at,
    )


async def _collect_pagespeed(
    credentials: dict[str, Any],
    settings: dict[str, Any],
    client: httpx.AsyncClient,
    estimate: int,
    max_cost: int | None,
) -> dict[str, Any]:
    page_url = settings.get("url") or settings.get("page_url")
    if not isinstance(page_url, str) or not page_url.strip():
        return _error("pagespeed", "missing_setting", "url is required", metadata=_metadata("pagespeed", estimate, max_cost, status="error"))
    try:
        page_url = _validate_http_url(page_url.strip())
    except ValueError:
        return _error("pagespeed", "invalid_url", "PageSpeed URL must be a public HTTP URL", metadata=_metadata("pagespeed", estimate, max_cost, status="error"))
    endpoint = _validated_endpoint(settings.get("endpoint") or settings.get("base_url") or _PAGESPEED_ENDPOINT)
    if not endpoint:
        return _error("pagespeed", "invalid_endpoint", "PageSpeed endpoint is invalid", metadata=_metadata("pagespeed", estimate, max_cost, status="error"))
    strategy = settings.get("strategy", "mobile")
    if not isinstance(strategy, str) or strategy not in {"mobile", "desktop"}:
        strategy = "mobile"
    params: dict[str, Any] = {"url": page_url, "strategy": strategy}
    categories = settings.get("categories") or settings.get("category")
    if isinstance(categories, str):
        categories = [categories]
    if isinstance(categories, list):
        params["category"] = categories
    api_key = credentials.get("api_key") or settings.get("api_key")
    if isinstance(api_key, str) and api_key:
        params["key"] = api_key
    try:
        response = await client.get(endpoint, params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        return _error("pagespeed", "unavailable", f"PageSpeed request failed: {type(exc).__name__}", metadata=_metadata("pagespeed", estimate, max_cost, status="error"))
    payload = _safe_json(response)
    if response.status_code < 200 or response.status_code >= 300:
        return _error("pagespeed", "remote_error", f"PageSpeed returned HTTP {response.status_code}", metadata=_metadata("pagespeed", estimate, max_cost, status="error"))
    if not isinstance(payload, dict):
        return _error("pagespeed", "invalid_response", "PageSpeed returned non-JSON data", metadata=_metadata("pagespeed", estimate, max_cost, status="error"))
    actual, cost_basis, _usage_value = _cost_info(payload, estimate)
    return _result(
        "pagespeed",
        "pagespeed",
        data=_normalize_pagespeed_result(payload, page_url=page_url, strategy=strategy),
        cost_cents=actual,
        metadata=_metadata("pagespeed", estimate, max_cost, status="ok", cost_basis=cost_basis, usage=None),
    )


async def collect(
    kind: str,
    credentials: dict[str, Any],
    settings_dict: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Collect one actual visibility source using its documented HTTP API."""

    normalized_kind = _normalized_text(kind).casefold()
    supported = {"gsc", "ga4", "dataforseo", "ai_sample", "pagespeed"}
    if normalized_kind not in supported:
        return _error(normalized_kind or "unknown", "unsupported_kind", "unsupported visibility source")
    if not isinstance(credentials, dict) or not isinstance(settings_dict, dict):
        return _error(normalized_kind, "invalid_configuration", "credentials and settings must be objects")
    estimate, max_cost, pricing_error = _pricing(normalized_kind, settings_dict)
    if pricing_error:
        return _error(
            normalized_kind,
            "pricing_denied",
            pricing_error,
            metadata={
                "status": "denied",
                "pricing_known": estimate is not None,
                "estimated_cost_cents": estimate,
                "max_cost_cents": max_cost,
            },
        )
    assert estimate is not None
    timeout_value = settings_dict.get("timeout_seconds", 30)
    try:
        timeout_value = min(max(float(timeout_value), 1.0), 120.0)
    except (TypeError, ValueError):
        timeout_value = 30.0
    async with httpx.AsyncClient(
        transport=transport if transport is not None else _default_transport(),
        trust_env=False,
        timeout=httpx.Timeout(timeout_value),
    ) as client:
        if normalized_kind in {"gsc", "ga4"}:
            return await _collect_google(normalized_kind, credentials, settings_dict, client, estimate, max_cost)
        if normalized_kind == "dataforseo":
            return await _collect_dataforseo(credentials, settings_dict, client, estimate, max_cost)
        if normalized_kind == "ai_sample":
            return await _collect_ai_sample(credentials, settings_dict, client, estimate, max_cost)
        return await _collect_pagespeed(credentials, settings_dict, client, estimate, max_cost)


__all__ = ["collect", "validate_citation_import", "verify_ai_connection"]
