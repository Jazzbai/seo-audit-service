"""Source-grounded topic planning, article checks, and provider generation.

Research is a separate bounded helper.  This module consumes only caller-
supplied facts and evidence; provider responses remain untrusted drafts and are
never treated as approved, publishable, or authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .audit import _host_is_public, _normalized_text, _title_issue


_ALLOWED_MARKUP = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_SAFE_LINK_SCHEMES = {"http", "https", "mailto", "tel"}


def _dedupe(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_text(value)
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _string_items(value: Any) -> list[str]:
    output: list[str] = []
    for item in _items(value):
        if isinstance(item, dict):
            candidate = item.get("name") or item.get("title") or item.get("value") or item.get("label")
        else:
            candidate = item
        if isinstance(candidate, (str, int, float)) and _normalized_text(candidate):
            output.append(_normalized_text(candidate))
    return _dedupe(output)


def _flatten_values(value: Any, path: str = "") -> list[dict[str, Any]]:
    """Flatten caller-supplied facts while retaining their source paths."""

    output: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            output.extend(_flatten_values(child, child_path))
    elif isinstance(value, (list, tuple, set)):
        for index, child in enumerate(value):
            output.extend(_flatten_values(child, f"{path}[{index}]"))
    elif isinstance(value, (str, int, float, bool)):
        text = _normalized_text(value)
        if text:
            output.append({"path": path, "value": text})
    return output


def _source_url(entry: Any) -> str | None:
    if isinstance(entry, str):
        candidate = entry.strip()
    elif isinstance(entry, dict):
        candidate = _normalized_text(entry.get("url") or entry.get("source_url") or entry.get("link"))
    else:
        return None
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"} or not host:
        return None
    if parts.username is not None or parts.password is not None or not _host_is_public(host):
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def _source_entry(entry: Any) -> dict[str, Any] | None:
    url = _source_url(entry)
    if not url:
        return None
    if isinstance(entry, dict):
        output: dict[str, Any] = {"url": url}
        for key in (
            "title",
            "publisher",
            "published_at",
            "quote",
            "snippet",
            "source_kind",
            "purpose",
            "page_purpose",
            "page_title",
            "fetched_at",
            "fetched_time",
            "fetched_url",
            "content_hash",
            "contenthash",
            "extract",
            "excerpt",
            "extracts",
            "status",
            "status_code",
            "content_type",
            "source_authority",
            "authority",
            "error",
        ):
            if entry.get(key) is not None:
                output[key] = entry[key]
        return output
    return {"url": url}


def _source_key(entry: Any) -> str | None:
    url = _source_url(entry)
    return url.casefold() if url else None


def _is_research_record(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("sources"), list) and any(
        key in value for key in ("research_notes", "complete", "blockers")
    )


def _collect_sources(brief: Any, facts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    values: list[Any] = []
    if isinstance(brief, dict):
        for key in (
            "sources",
            "source_urls",
            "required_sources",
            "confirmed_sources",
            "research_sources",
            "references",
            "reference_urls",
        ):
            values.extend(_items(brief.get(key)))
        evidence = brief.get("evidence")
        for item in _items(evidence):
            if isinstance(item, dict) and item.get("url"):
                values.append(item)
        for research_key in ("research", "research_brief"):
            research = brief.get(research_key)
            if isinstance(research, dict):
                values.extend(_items(research.get("sources")))
        # Main persists generation provenance inside brief.generation.  Input
        # and research sources are retained; provider-returned sources are not
        # treated as authoritative evidence.
        generation = brief.get("generation")
        if isinstance(generation, dict):
            values.extend(_items(generation.get("input_sources")))
            values.extend(_items(generation.get("research_sources")))
            research = generation.get("research")
            if isinstance(research, dict):
                values.extend(_items(research.get("sources")))
            if _normalized_text(generation.get("kind")).casefold() in {
                "research",
                "research_brief",
                "source_research",
            } or _is_research_record(generation):
                values.extend(_items(generation.get("sources")))
    if isinstance(facts, dict):
        values.extend(_items(facts.get("confirmed_sources")))
        values.extend(_items(facts.get("research_sources")))
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        entry = _source_entry(value)
        key = _source_key(entry)
        if entry and key and key not in seen:
            seen.add(key)
            output.append(entry)
    return output


def _page_record(page: Any) -> dict[str, Any]:
    if isinstance(page, dict):
        return page
    return {}


def _page_title(page: dict[str, Any]) -> str:
    signals = page.get("signals") if isinstance(page.get("signals"), dict) else {}
    return _normalized_text(page.get("title") or signals.get("title"))


def _page_url(page: dict[str, Any]) -> str:
    return _normalized_text(page.get("url") or page.get("canonical") or page.get("source_url"))


def _topic_id(title: str, evidence: list[dict[str, Any]]) -> str:
    payload = json.dumps({"title": title, "evidence": evidence}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_RESEARCH_INPUT_MAX_RECORDS = 32
_RESEARCH_INPUT_MAX_FIELDS = 24
_RESEARCH_INPUT_MAX_TOPIC_LENGTH = 200
_RESEARCH_INPUT_MAX_SOURCE_LENGTH = 256
_RESEARCH_INPUT_MAX_PROVIDER_LENGTH = 120
_RESEARCH_INPUT_MAX_DATE_LENGTH = 64
_RESEARCH_INPUT_KINDS = {"search_observation", "competitor_observation"}
_RESEARCH_INPUT_CREDENTIAL_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}


def _credential_shaped(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> bool:
    """Reject untrusted input containing credential-shaped fields.

    Research inputs are deliberately a small evidence envelope.  A bounded
    recursive scan prevents a provider response or an accidentally pasted
    connection object from reaching a brief, while the node/depth limits keep
    malformed input from becoming a planner resource sink.
    """

    if budget is None:
        budget = [256]
    if budget[0] <= 0 or depth > 5:
        return True
    budget[0] -= 1
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                return True
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if normalized_key in _RESEARCH_INPUT_CREDENTIAL_KEYS:
                return True
            if _credential_shaped(child, depth=depth + 1, budget=budget):
                return True
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            if _credential_shaped(child, depth=depth + 1, budget=budget):
                return True
    return False


def _bounded_research_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalized_text(value)
    if not normalized or len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        return None
    return normalized


def _research_date(value: Any) -> str | None:
    """Accept an explicit ISO date or timezone-aware timestamp only."""

    normalized = _bounded_research_text(value, limit=_RESEARCH_INPUT_MAX_DATE_LENGTH)
    if not normalized:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        try:
            date.fromisoformat(normalized)
        except ValueError:
            return None
        return normalized
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return normalized


def _research_field(raw: dict[str, Any], data: dict[str, Any], key: str) -> Any:
    if key in raw:
        return raw[key]
    return data.get(key)


def _domain_like_topic(value: str) -> bool:
    """Identify URLs/domains that must not become article topics."""

    normalized = value.casefold().strip()
    if "://" in normalized or normalized.startswith(("www.", "http:", "https:")):
        return True
    for token in normalized.split():
        token = token.strip("()[]{}<>,.;:!?\"'")
        if re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+", token):
            return True
    return False


def _normalize_research_inputs(value: Any) -> list[dict[str, Any]]:
    """Return safe, bounded research evidence records.

    The returned records contain only fields from the planner contract.  Raw
    provider payloads, ranking details, and unknown nested values are never
    copied into a brief.
    """

    if not isinstance(value, list):
        return []
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value[:_RESEARCH_INPUT_MAX_RECORDS]:
        if not isinstance(raw, dict) or len(raw) > _RESEARCH_INPUT_MAX_FIELDS:
            continue
        if _credential_shaped(raw):
            continue
        if any(
            key != "data" and isinstance(child, (dict, list, tuple, set))
            for key, child in raw.items()
        ):
            # A top-level response/payload is not part of the planner
            # contract. Reject the record rather than risk treating it as
            # evidence or copying a provider response into a brief.
            continue
        data = raw.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            continue

        raw_kind = _research_field(raw, data, "kind")
        if raw_kind is None:
            kind = "search_observation"
        else:
            kind = _bounded_research_text(raw_kind, limit=64)
            if kind is None:
                continue
            kind = kind.casefold()
        if kind not in _RESEARCH_INPUT_KINDS:
            continue

        source = _bounded_research_text(
            _research_field(raw, data, "source"),
            limit=_RESEARCH_INPUT_MAX_SOURCE_LENGTH,
        )
        if source is None:
            continue
        if source.casefold().startswith(("http://", "https://")):
            source_url = _source_url(source)
            if source_url is None:
                continue
            source = source_url

        provider_value = _research_field(raw, data, "provider")
        if provider_value is None:
            provider = None
        else:
            provider = _bounded_research_text(provider_value, limit=_RESEARCH_INPUT_MAX_PROVIDER_LENGTH)
            if provider is None:
                continue

        query_value = _research_field(raw, data, "query")
        topic_value = _research_field(raw, data, "topic")
        if query_value is None:
            query = None
        else:
            query = _bounded_research_text(query_value, limit=_RESEARCH_INPUT_MAX_TOPIC_LENGTH)
            if query is None:
                continue
        if topic_value is None:
            topic = None
        else:
            topic = _bounded_research_text(topic_value, limit=_RESEARCH_INPUT_MAX_TOPIC_LENGTH)
            if topic is None:
                continue

        observed_value = _research_field(raw, data, "observed_at")
        if observed_value is None:
            observed_at = None
        else:
            observed_at = _research_date(observed_value)
            if observed_at is None:
                continue

        source_url_value = _research_field(raw, data, "source_url")
        if source_url_value is None:
            source_url = None
        else:
            source_url = _source_url(source_url_value)
            if source_url is None:
                continue

        competitor_url_value = _research_field(raw, data, "competitor_url")
        if competitor_url_value is None:
            competitor_url = None
        else:
            competitor_url = _source_url(competitor_url_value)
            if competitor_url is None:
                continue

        target_value = _research_field(raw, data, "target")
        if target_value is None:
            target = None
        else:
            target = _bounded_research_text(target_value, limit=253)
            if target is None or "://" in target or "/" in target:
                continue

        position_value = _research_field(raw, data, "position")
        if position_value is None:
            position = None
        elif (
            isinstance(position_value, bool)
            or not isinstance(position_value, int)
            or position_value < 1
            or position_value > 1000
        ):
            continue
        else:
            position = position_value

        verified = raw.get("verified", data.get("verified", None))
        if verified is not None and verified is not True:
            continue

        record: dict[str, Any] = {"kind": kind, "source": source}
        if query is not None:
            record["query"] = query
        if topic is not None:
            record["topic"] = topic
        if provider is not None:
            record["provider"] = provider
        if observed_at is not None:
            record["observed_at"] = observed_at
        if source_url is not None:
            record["source_url"] = source_url
        if competitor_url is not None:
            record["competitor_url"] = competitor_url
        if target is not None:
            record["target"] = target
        if position is not None:
            record["position"] = position
        if verified is True:
            record["verified"] = True

        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        accepted.append(record)

    return accepted


def _research_evidence(record: dict[str, Any]) -> dict[str, Any]:
    """Copy only the bounded planner provenance into a brief evidence item."""

    evidence: dict[str, Any] = {
        "kind": record["kind"],
        "source": record["source"],
    }
    for key in (
        "query",
        "topic",
        "provider",
        "observed_at",
        "source_url",
        "competitor_url",
        "target",
        "position",
        "verified",
    ):
        if key in record:
            evidence[key] = record[key]
    return evidence


def _research_candidates(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group duplicate research topics while retaining each provenance item."""

    groups: dict[str, dict[str, Any]] = {}
    planning_sources: list[dict[str, Any]] = []
    for record in records:
        topic = record.get("topic") or record.get("query")
        if not isinstance(topic, str) or _domain_like_topic(topic):
            if record.get("kind") == "competitor_observation":
                planning_sources.append(_research_evidence(record))
            continue
        key = " ".join(topic.casefold().split())
        group = groups.get(key)
        if group is None:
            group = {
                "title": f"{topic} guide",
                "topic": topic,
                "purpose": "research_observed",
                "evidence": [],
                "keywords": [record["query"]] if record.get("query") else [],
                "products": [],
            }
            groups[key] = group
        evidence = _research_evidence(record)
        group["evidence"].append(evidence)
        if record.get("query") and record["query"] not in group["keywords"]:
            group["keywords"].append(record["query"])
    return list(groups.values()), planning_sources


_TOPIC_STOPWORDS = {"a", "an", "and", "for", "guide", "how", "in", "of", "the", "to", "what"}


def _topic_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _normalized_text(value).casefold())
        if token not in _TOPIC_STOPWORDS and len(token) > 1
    }


def _overlapping_intent(left: Any, right: Any) -> bool:
    left_tokens, right_tokens = _topic_tokens(left), _topic_tokens(right)
    if len(left_tokens) < 2 or len(right_tokens) < 2:
        return False
    shared = len(left_tokens & right_tokens)
    return shared >= 2 and shared / min(len(left_tokens), len(right_tokens)) >= 0.75


_INTERNAL_LINK_LIMIT = 3
_REFRESHABLE_RESOURCE_TYPES = {"post", "posts", "page", "pages"}


def _origin_key(value: Any) -> tuple[str, str, int | None] | None:
    normalized = _source_url(value)
    if not normalized:
        return None
    parts = urlsplit(normalized)
    try:
        port = parts.port
    except ValueError:
        return None
    return parts.scheme.casefold(), (parts.hostname or "").casefold(), port


def _stored_page_link_records(
    pages: list[dict[str, Any]],
    *,
    origin: str | None = None,
) -> list[dict[str, str]]:
    """Return only usable URL/title pairs already present in the inventory.

    The planner must not turn a topic, slug, or model output into a URL.  A
    page is therefore eligible only when both fields are present in the
    stored record and the URL passes the same public HTTP(S) boundary used for
    source evidence.  The title is retained verbatim after normalisation so
    it can be used as an honest anchor suggestion and evidence label.
    """

    expected_origin = _origin_key(origin) if origin else None
    output: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for page in pages:
        record = _page_record(page)
        title = _page_title(record)
        url = _source_url(_page_url(record))
        if not title or not url:
            continue
        if expected_origin is not None and _origin_key(url) != expected_origin:
            continue
        key = url.casefold()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        output.append({"url": url, "title": title})
    return output


def _internal_link_opportunities(
    candidate: dict[str, Any],
    title: str,
    stored_pages: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Suggest relevant links without inventing targets or anchor text."""

    topic_values: list[Any] = [title, candidate.get("topic")]
    topic_values.extend(_items(candidate.get("keywords")))
    topic_values.extend(_items(candidate.get("products")))
    topic_tokens = set().union(*(_topic_tokens(value) for value in topic_values))
    if not topic_tokens:
        return []

    # A refresh brief already has one stored-page target.  Recommending that
    # same page as its own internal link is not useful, so leave it out while
    # retaining every other inventory page as a possible target.
    evidence = candidate.get("evidence") or []
    current_urls = {
        url.casefold()
        for url in (
            _source_url(item.get("url"))
            for item in evidence
            if isinstance(item, dict) and item.get("url")
        )
        if url
    }

    ranked: list[tuple[int, float, str, str, dict[str, str], list[str]]] = []
    for page in stored_pages:
        if page["url"].casefold() in current_urls:
            continue
        page_tokens = _topic_tokens(page["title"])
        shared = sorted(topic_tokens & page_tokens)
        if not shared:
            continue
        coverage = len(shared) / max(1, len(topic_tokens))
        ranked.append(
            (
                -len(shared),
                -coverage,
                page["title"].casefold(),
                page["url"].casefold(),
                page,
                shared,
            )
        )
    ranked.sort(key=lambda item: item[:4])

    opportunities: list[dict[str, Any]] = []
    for _, _, _, _, page, shared in ranked[:_INTERNAL_LINK_LIMIT]:
        opportunities.append(
            {
                "target_url": page["url"],
                "target_title": page["title"],
                # An exact stored title is safer than an invented anchor.
                "anchor_text": page["title"],
                "matched_terms": shared,
                "evidence": {
                    "kind": "existing_stored_page",
                    "url": page["url"],
                    "title": page["title"],
                },
            }
        )
    return opportunities


def _explicitly_verified(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("verified") is True:
        return True
    for key in ("verification_status", "status"):
        marker = value.get(key)
        if isinstance(marker, str) and marker.strip().casefold() == "verified":
            return True
    return False


def _identity_prerequisite(
    facts: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    """Describe an explicit author/publisher prerequisite without asserting it."""

    if kind == "author":
        raw = facts.get("authors")
        candidates = _items(raw)
        required_fields = {"id", "name"}
        fact_root = "authors"
    else:
        raw = facts.get("publisher")
        if raw is None and facts.get("publisher_name") is not None:
            raw = {"name": facts.get("publisher_name"), "verified": facts.get("publisher_verified") is True}
        candidates = _items(raw)
        required_fields = {"name"}
        fact_root = "publisher"

    if not candidates:
        return {
            "required": True,
            "status": "missing",
            "evidence": {"fact_path": fact_root, "provided_fields": []},
        }

    first_present: tuple[int, Any, set[str]] | None = None
    for index, value in enumerate(candidates):
        if isinstance(value, dict):
            fields = {
                field
                for field, raw_value in value.items()
                if field in {"id", "author_id", "name", "display_name"}
                and _normalized_text(raw_value)
            }
        elif isinstance(value, (str, int, float)) and _normalized_text(value):
            fields = {"name"}
        else:
            fields = set()
        if fields and first_present is None:
            first_present = (index, value, fields)
        if _explicitly_verified(value) and required_fields <= fields:
            return {
                "required": True,
                "status": "verified",
                "evidence": {
                    "fact_path": f"{fact_root}[{index}]" if kind == "author" else fact_root,
                    "provided_fields": sorted(fields),
                    "verification": "explicit",
                },
            }

    if first_present is None:
        status = "missing"
        evidence = {"fact_path": fact_root, "provided_fields": []}
    else:
        index, _, fields = first_present
        status = "needs_identity" if not required_fields <= fields else "needs_verification"
        evidence = {
            "fact_path": f"{fact_root}[{index}]" if kind == "author" else fact_root,
            "provided_fields": sorted(fields),
        }
    return {"required": True, "status": status, "evidence": evidence}


def _structured_data_recommendation(facts: dict[str, Any]) -> dict[str, Any]:
    """Return a review-only Article recommendation, never a schema payload."""

    author = _identity_prerequisite(facts, kind="author")
    publisher = _identity_prerequisite(facts, kind="publisher")
    eligible = author["status"] == "verified" and publisher["status"] == "verified"
    return {
        "schema_type": "Article",
        "status": "review_required",
        "review_required": True,
        "eligible": eligible,
        "prerequisites": {"author": author, "publisher": publisher},
        "metadata": {
            "headline": {"source": "brief.title", "status": "review_required"},
            "author": {"source": "facts.authors", "status": author["status"]},
            "publisher": {"source": "facts.publisher", "status": publisher["status"]},
        },
        # A later editorial check may add supplied, verified identifiers.  The
        # planner itself must not manufacture @context, @id, or URLs.
        "external_urls": [],
    }


def plan_topics(
    facts: dict[str, Any],
    pages: list[dict[str, Any]],
    keywords: list[str] | None = None,
    products: list[dict[str, Any]] | None = None,
    origin: str | None = None,
    research_inputs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build at most eight four-week briefs from supplied inputs only.

    ``research_inputs`` is an optional caller-supplied evidence envelope. It
    can influence topic selection, but it is never fetched, interpreted as a
    provider response, or treated as an authoritative claim.
    """

    if not isinstance(facts, dict):
        raise TypeError("facts must be a dictionary")
    pages = pages if isinstance(pages, list) else []
    supplied_products = _items(products) if products is not None else _items(facts.get("products"))
    product_names = _string_items(supplied_products)
    service_names = _string_items(facts.get("services"))
    location_names = _string_items(facts.get("locations"))
    keyword_names = _string_items(keywords)
    audience = facts.get("audience")
    business_name = _normalized_text(facts.get("business_name"))
    fact_values = [item["value"] for item in _flatten_values(facts)]
    confirmed_sources = _collect_sources({}, facts)
    source_urls = [item["url"] for item in confirmed_sources]
    stored_page_links = _stored_page_link_records(pages, origin=origin)
    normalized_research = _normalize_research_inputs(research_inputs)
    research_candidates, planning_source_summary = _research_candidates(normalized_research)

    candidates: list[dict[str, Any]] = []
    # Explicit observations are the highest-signal optional planning input,
    # while their bounded evidence remains separate from factual claims.
    candidates.extend(research_candidates)

    # Product and service names are topics, not claims.  The generic framing is
    # deliberately transparent so a later editorial pass must add evidence.
    for name in product_names:
        candidates.append(
            {
                "title": f"{name} overview",
                "topic": name,
                "purpose": "new_topic",
                "evidence": [{"kind": "supplied_fact", "path": "products", "value": name}],
                "keywords": [name] if name in keyword_names else [],
                "products": [name],
            }
        )
    for service in service_names:
        if location_names:
            for location in location_names:
                candidates.append(
                    {
                        "title": f"{service} in {location}",
                        "topic": service,
                        "purpose": "new_topic",
                        "evidence": [
                            {"kind": "supplied_fact", "path": "services", "value": service},
                            {"kind": "supplied_fact", "path": "locations", "value": location},
                        ],
                        "keywords": [item for item in keyword_names if service.casefold() in item.casefold()],
                        "products": [],
                    }
                )
        else:
            candidates.append(
                {
                    "title": f"{service} overview",
                    "topic": service,
                    "purpose": "new_topic",
                    "evidence": [{"kind": "supplied_fact", "path": "services", "value": service}],
                    "keywords": [item for item in keyword_names if service.casefold() in item.casefold()],
                    "products": [],
                }
            )
    for keyword in keyword_names:
        candidates.append(
            {
                "title": f"{keyword} guide",
                "topic": keyword,
                "purpose": "new_topic",
                "evidence": [{"kind": "supplied_keyword", "value": keyword}],
                "keywords": [keyword],
                "products": [],
            }
        )
    if business_name:
        candidates.append(
            {
                "title": f"About {business_name}",
                "topic": business_name,
                "purpose": "new_topic",
                "evidence": [{"kind": "supplied_fact", "path": "business_name", "value": business_name}],
                "keywords": [],
                "products": [],
            }
        )

    # Existing pages are used only for refresh planning.  Their text is never
    # treated as newly verified research.
    for page in pages:
        record = _page_record(page)
        if not record.get("enrolled", False):
            continue
        resource_type = _normalized_text(record.get("resource_type")).casefold()
        if resource_type and resource_type not in _REFRESHABLE_RESOURCE_TYPES:
            continue
        title = _page_title(record)
        url = _page_url(record)
        if title and url:
            candidates.append(
                {
                    "title": title,
                    "topic": title,
                    "purpose": "refresh_existing",
                    "evidence": [{"kind": "existing_page", "url": url, "title": title}],
                    "keywords": [],
                    "products": [],
                }
            )

    selected: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    existing_titles = [
        _page_title(_page_record(page))
        for page in pages
        if _page_title(_page_record(page))
    ]
    for candidate in candidates:
        title = _normalized_text(candidate.get("title"))
        if not title or title.casefold() in seen_topics:
            continue
        # A new brief needs a distinct search intent. Existing-page candidates
        # are retained as explicit refresh work; only new topics are skipped
        # when their substantive terms already describe an existing page.
        if candidate.get("purpose") in {"new_topic", "research_observed"} and any(
            _overlapping_intent(title, existing_title) for existing_title in existing_titles
        ):
            continue
        if candidate.get("purpose") in {"new_topic", "research_observed"} and any(
            _overlapping_intent(title, selected_item["title"])
            for selected_item in selected
            if selected_item.get("purpose") in {"new_topic", "research_observed"}
        ):
            continue
        seen_topics.add(title.casefold())
        evidence = list(candidate.get("evidence") or [])
        evidence_urls = [item["url"] for item in evidence if isinstance(item, dict) and item.get("url")]
        research_evidence = [
            item for item in evidence
            if isinstance(item, dict) and item.get("kind") in _RESEARCH_INPUT_KINDS
        ]
        for item in planning_source_summary:
            if item not in research_evidence:
                research_evidence.append(item)
        brief = {
            "id": _topic_id(title, evidence),
            "week": min(4, len(selected) // 2 + 1),
            "title": title,
            "topic": candidate.get("topic"),
            "purpose": candidate.get("purpose", "new_topic"),
            "audience": audience,
            "facts": dict(facts),
            "fact_values": fact_values,
            "keywords": list(candidate.get("keywords") or []),
            "products": list(candidate.get("products") or []),
            "evidence": evidence,
            "sources": confirmed_sources,
            "source_urls": _dedupe(source_urls + evidence_urls),
            "claims_allowed": fact_values,
            "internal_link_opportunities": _internal_link_opportunities(candidate, title, stored_page_links),
            "structured_data_recommendation": _structured_data_recommendation(facts),
            "status": "planned",
        }
        if research_evidence:
            brief["research_evidence"] = research_evidence
        if planning_source_summary:
            # Planning provenance is not an article claim or a request to
            # mention a competitor in published copy.
            brief["planning_source_summary"] = list(planning_source_summary)
        selected.append(brief)
        if len(selected) >= 8:
            break
    return selected


def _normalized_body(body: Any) -> str:
    if not isinstance(body, str):
        return ""
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip().casefold()


def _markup_issues(body: str) -> list[str]:
    issues: list[str] = []
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup.find_all(True):
        name = tag.name.casefold()
        if name not in _ALLOWED_MARKUP:
            issues.append(f"disallowed_tag:{name}")
        for raw_name, raw_value in tag.attrs.items():
            attr = str(raw_name).casefold()
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            value = _normalized_text(" ".join(str(item) for item in values))
            if attr.startswith("on") or attr in {"style", "srcdoc", "formaction"}:
                issues.append(f"disallowed_attribute:{attr}")
            if attr in {"href", "src", "action"} and value:
                scheme = urlsplit(value).scheme.casefold()
                if scheme and scheme not in _SAFE_LINK_SCHEMES:
                    issues.append(f"disallowed_url_scheme:{scheme}")
                if value.casefold().startswith("javascript:"):
                    issues.append("javascript_url")
    return _dedupe(issues)


def _article_source_entries(article: dict[str, Any]) -> list[Any]:
    value = article.get("sources")
    if value is None:
        return []
    return _items(value)


def _known_source_keys(
    article: dict[str, Any],
    facts: dict[str, Any],
    brief: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
) -> set[str]:
    values: list[Any] = []
    values.extend(_items(facts.get("confirmed_sources")))
    values.extend(_items(brief.get("sources")))
    values.extend(_items(brief.get("source_urls")))
    values.extend(_items(brief.get("required_sources")))
    provenance = article.get("provenance")
    if isinstance(provenance, dict):
        values.extend(_items(provenance.get("input_sources")))
        values.extend(_items(provenance.get("research_sources")))
        research = provenance.get("research")
        if isinstance(research, dict):
            values.extend(_items(research.get("sources")))
        if _normalized_text(provenance.get("kind")).casefold() in {
            "research",
            "research_brief",
            "source_research",
        } or _is_research_record(provenance):
            values.extend(_items(provenance.get("sources")))
    generation = brief.get("generation")
    if isinstance(generation, dict):
        values.extend(_items(generation.get("input_sources")))
        values.extend(_items(generation.get("research_sources")))
        research = generation.get("research")
        if isinstance(research, dict):
            values.extend(_items(research.get("sources")))
        if _normalized_text(generation.get("kind")).casefold() in {
            "research",
            "research_brief",
            "source_research",
        } or _is_research_record(generation):
            values.extend(_items(generation.get("sources")))
    for page in _items(brief.get("evidence")):
        if isinstance(page, dict) and page.get("url"):
            values.append(page)
    # Existing inventory/crawl records are supplied evidence too.  Their URL
    # may be the only source reference available for a refresh article.
    for page in _items(pages):
        if isinstance(page, dict) and page.get("url"):
            values.append(page)
    keys = {_source_key(value) for value in values}
    return {key for key in keys if key}


def _claim_supported(claim: Any, known_values: set[str], known_sources: set[str]) -> bool:
    """Require a claim to resolve to caller-supplied facts.

    A URL citation is useful provenance, but fetched/provider text is not an
    authority by itself.  Therefore ``supported_by`` can validate a citation
    only when the claim value is also present in the supplied fact set.
    """

    if isinstance(claim, dict):
        supported_by = claim.get("supported_by") or claim.get("source") or claim.get("source_url")
        claim_value = claim.get("value") or claim.get("text") or claim.get("claim")
        normalized_claim = _normalized_text(claim_value).casefold()
        if not normalized_claim or normalized_claim not in known_values:
            return False
        if not supported_by:
            return True
        return any(
            (_source_key(value) in known_sources if _source_key(value) else False)
            for value in _items(supported_by)
        )
    return _normalized_text(claim).casefold() in known_values


_NON_FACT_KEYS = {
    "confirmed_sources",
    "sources",
    "research_sources",
    "missing_facts",
    "disputed_facts",
    "facts_under_review",
}


def _known_fact_values(value: Any) -> set[str]:
    """Flatten facts while excluding source metadata and review bookkeeping."""

    output: set[str] = set()

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                if str(key) in _NON_FACT_KEYS:
                    continue
                walk(child)
            return
        if isinstance(current, (list, tuple, set)):
            for child in current:
                walk(child)
            return
        if isinstance(current, (str, int, float, bool)):
            text = _normalized_text(current)
            if text:
                output.add(text.casefold())

    walk(value)
    return output


def _review_fact_markers(value: Any, keys: tuple[str, ...]) -> list[str]:
    markers: list[str] = []
    if not isinstance(value, dict):
        return markers
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool):
            if item:
                markers.append(key)
            continue
        if isinstance(item, dict):
            for field, state in item.items():
                if _normalized_text(state).casefold() in {"missing", "disputed", "unverified", "needs_review", "under_review"}:
                    normalized_field = _normalized_text(field)
                    if normalized_field:
                        markers.append(normalized_field)
            continue
        for child in _items(item):
            if isinstance(child, dict):
                marker = child.get("path") or child.get("key") or child.get("name") or child.get("text")
            else:
                marker = child
            normalized = _normalized_text(marker)
            if normalized:
                markers.append(normalized)
    return list(dict.fromkeys(markers))


def _nested_fact_statuses(value: Any, statuses: set[str], path: str = "") -> list[str]:
    markers: list[str] = []
    if isinstance(value, dict):
        if _normalized_text(value.get("status")).casefold() in statuses:
            markers.append(path or "facts")
        for key, child in value.items():
            if str(key) in _NON_FACT_KEYS:
                continue
            child_path = f"{path}.{key}" if path else str(key)
            markers.extend(_nested_fact_statuses(child, statuses, child_path))
    elif isinstance(value, (list, tuple, set)):
        for index, child in enumerate(value):
            markers.extend(_nested_fact_statuses(child, statuses, f"{path}[{index}]"))
    return list(dict.fromkeys(markers))


def _author_inventory_ids(pages: list[dict[str, Any]]) -> set[str]:
    """Return remote author IDs from the authenticated inventory records.

    ``Page.id`` is ForgeSEO's local row ID, so it must not be confused with
    the WordPress author ID.  Prefer the connector's preserved source record
    and the canonical ``authors:<remote-id>`` resource key instead.
    """

    author_ids: set[str] = set()
    for page in pages:
        record = _page_record(page)
        resource_type = _normalized_text(record.get("resource_type")).casefold()
        resource_key = _normalized_text(record.get("resource_key"))
        key_type, _, key_id = resource_key.partition(":")
        if resource_type not in {"author", "authors"} and key_type.casefold() not in {"author", "authors"}:
            continue

        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        candidates = [
            source.get("id"),
            source.get("author_id"),
            record.get("author_id"),
            key_id,
        ]
        # Raw connector inventory records may not have a resource key yet.
        if not resource_key:
            candidates.append(record.get("id"))
        for candidate in candidates:
            normalized = _normalized_text(candidate)
            if normalized:
                author_ids.add(normalized.casefold())
    return author_ids


def _missing_required_facts(brief: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    required: list[Any] = []
    for key in ("required_facts", "required_fact_keys"):
        required.extend(_items(brief.get(key)))
    missing: list[str] = []
    for item in required:
        if isinstance(item, dict):
            path = _normalized_text(item.get("path") or item.get("key") or item.get("name"))
        else:
            path = _normalized_text(item)
        if not path:
            continue
        current: Any = facts
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
                break
        if current is None or current is False or (isinstance(current, (str, list, tuple, set, dict)) and not current):
            missing.append(path)
    return list(dict.fromkeys(missing))


_IMAGE_SOURCE_LIMIT = 24
_IMAGE_TEXT_LIMITS = {
    "kind": 32,
    "attribution": 500,
    "license": 200,
    "disclosure": 240,
}
_IMAGE_KINDS = {"owner_provided", "licensed", "generated_illustration"}


def _image_source_records(brief: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Normalize explicit image provenance without trusting arbitrary metadata."""

    raw_sources = brief.get("image_sources")
    if raw_sources is None:
        return [], False
    if not isinstance(raw_sources, list) or len(raw_sources) > _IMAGE_SOURCE_LIMIT:
        return [], True
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_sources:
        if not isinstance(raw, dict) or _credential_shaped(raw):
            return [], True
        source = _source_url(raw.get("url") or raw.get("source_url"))
        if not source:
            return [], True
        kind = _normalized_text(raw.get("kind")).casefold()
        if kind not in _IMAGE_KINDS:
            return [], True
        record: dict[str, Any] = {"url": source, "kind": kind}
        for field, limit in _IMAGE_TEXT_LIMITS.items():
            if field == "kind":
                continue
            value = raw.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                return [], True
            value = _normalized_text(value)
            if not value or len(value) > limit:
                return [], True
            record[field] = value
        for field in ("owner_confirmed", "not_real"):
            value = raw.get(field)
            if value is not None:
                if not isinstance(value, bool):
                    return [], True
                record[field] = value
        key = source.casefold()
        if key in seen:
            return [], True
        seen.add(key)
        records.append(record)
    return records, False


def _check_image_provenance(body: str, brief: dict[str, Any]) -> dict[str, Any]:
    """Require a safe provenance record for every article image."""

    soup = BeautifulSoup(body, "html.parser")
    images = soup.find_all("img")
    records, invalid = _image_source_records(brief)
    blockers: list[str] = ["invalid_image_provenance"] if invalid else []
    by_url = {record["url"].casefold(): record for record in records}
    for image in images:
        source = _source_url(image.get("src"))
        if not source:
            blockers.append("image_source_not_public")
            continue
        record = by_url.get(source.casefold())
        if record is None:
            blockers.append("missing_image_provenance")
            continue
        kind = record["kind"]
        if kind == "owner_provided" and record.get("owner_confirmed") is not True:
            blockers.append("unverified_image_ownership")
        elif kind == "licensed" and not record.get("license"):
            blockers.append("missing_image_license")
        if kind == "licensed" and not record.get("attribution"):
            blockers.append("missing_image_attribution")
        if kind == "generated_illustration" and (
            not record.get("disclosure") or record.get("not_real") is not True
        ):
            blockers.append("generated_image_disclosure_required")
    return {
        "blockers": _dedupe(blockers),
        "image_count": len(images),
        "image_source_count": len(records),
    }


def check_article(
    article: dict[str, Any],
    facts: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check a foundation Article-shaped record without approving it for publish."""

    if not isinstance(article, dict):
        raise TypeError("article must be a dictionary")
    if not isinstance(facts, dict):
        raise TypeError("facts must be a dictionary")
    pages = pages if isinstance(pages, list) else []
    blockers: list[str] = []
    warnings: list[str] = []

    title = _normalized_text(article.get("title"))
    title_issue = _title_issue(title)
    if title_issue:
        blockers.append(title_issue)
    body = article.get("body")
    if not isinstance(body, str):
        blockers.append("empty_body")
    else:
        markup_issues = _markup_issues(body)
        if markup_issues:
            blockers.append("disallowed_markup")
            warnings.extend(markup_issues)
        if not _normalized_body(body):
            blockers.append("empty_body")
        body_soup = BeautifulSoup(body, "html.parser")
        for image in body_soup.find_all("img"):
            if not image.has_attr("alt"):
                blockers.append("missing_image_alt")
                break
            alt = _normalized_text(image.get("alt"))
            decorative = (
                _normalized_text(image.get("role")).casefold() in {"presentation", "none"}
                or _normalized_text(image.get("aria-hidden")).casefold() == "true"
                or _normalized_text(image.get("data-decorative")).casefold() == "true"
            )
            if not alt and not decorative:
                blockers.append("empty_image_alt")
                break
        if not body_soup.find(["h2", "h3", "p", "ul", "ol", "blockquote"]):
            warnings.append("body_has_no_structured_paragraph_or_heading")

    author_id = article.get("author_id")
    if not _normalized_text(author_id):
        blockers.append("missing_author")
    else:
        author_inventory_ids = _author_inventory_ids(pages)
        if author_inventory_ids and _normalized_text(author_id).casefold() not in author_inventory_ids:
            # Once authenticated author inventory exists, presence alone is
            # not enough: an arbitrary ID must never become article attribution.
            blockers.append("author_not_verified")

    brief = article.get("brief") if isinstance(article.get("brief"), dict) else {}
    image_check = _check_image_provenance(body if isinstance(body, str) else "", brief)
    blockers.extend(image_check["blockers"])
    # The foundation Article record stores the generation provenance inside its
    # JSON ``brief`` after the main workflow persists a draft.  Direct callers
    # may also provide the same object at the top level.
    provenance = article.get("provenance")
    if not isinstance(provenance, dict) and isinstance(brief.get("generation"), dict):
        provenance = brief.get("generation")
    if not isinstance(provenance, dict) or not (
        _normalized_text(provenance.get("kind"))
        or _normalized_text(provenance.get("provider"))
        or _normalized_text(provenance.get("source"))
        or _is_research_record(provenance)
    ):
        blockers.append("missing_provenance")
    elif provenance.get("unverified_sources"):
        blockers.append("unverified_sources")

    research_records: list[dict[str, Any]] = []
    for key in ("research", "research_brief"):
        value = brief.get(key)
        if isinstance(value, dict):
            research_records.append(value)
    if isinstance(brief.get("generation"), dict):
        generation = brief["generation"]
        if isinstance(generation.get("research"), dict):
            research_records.append(generation["research"])
        elif generation.get("kind") in {"research", "research_brief", "source_research"} or _is_research_record(generation):
            research_records.append(generation)
    for research in research_records:
        if research.get("blockers"):
            blockers.append("research_review_required")
        if research.get("complete") is False:
            blockers.append("research_incomplete")

    sources = _article_source_entries(article)
    if not sources:
        blockers.append("missing_sources")
    known_source_keys = _known_source_keys(article, facts, brief, pages)
    normalized_sources: list[dict[str, Any]] = []
    for source in sources:
        normalized = _source_entry(source)
        if not normalized:
            blockers.append("invalid_source")
            continue
        normalized_sources.append(normalized)
        if _source_key(normalized) not in known_source_keys:
            blockers.append("source_not_supplied")
        source_status = _normalized_text(normalized.get("status")).casefold()
        if normalized.get("error") or source_status in {"unavailable", "error", "rejected", "skipped"}:
            blockers.append("unavailable_source")
        if source_status == "fetched" and not (
            _normalized_text(normalized.get("content_hash")) or _normalized_text(normalized.get("contenthash"))
        ):
            blockers.append("invalid_source_provenance")
        claimed_authority = normalized.get("authority") or normalized.get("source_authority")
        if claimed_authority and _normalized_text(claimed_authority).casefold() not in {
            "untrusted",
            "reference",
            "evidence",
        }:
            # A source record cannot promote itself by carrying a confidence or
            # authority label.  Keep the source for provenance and require the
            # normal editorial/fact checks below.
            warnings.append("source_authority_ignored")

    known_values = _known_fact_values(facts)
    known_values.update(_known_fact_values(brief.get("facts", {})))
    if (
        _review_fact_markers(facts, ("missing_facts",))
        or _review_fact_markers(brief, ("missing_facts",))
        or _missing_required_facts(brief, facts)
        or _nested_fact_statuses(facts, {"missing", "needs_review", "under_review"})
        or _nested_fact_statuses(brief.get("facts", {}), {"missing", "needs_review", "under_review"})
    ):
        blockers.append("missing_facts")
    if (
        _review_fact_markers(facts, ("disputed_facts", "facts_under_review", "disputed"))
        or _review_fact_markers(brief, ("disputed_facts", "facts_under_review", "disputed"))
        or _nested_fact_statuses(facts, {"disputed", "unverified"})
        or _nested_fact_statuses(brief.get("facts", {}), {"disputed", "unverified"})
    ):
        blockers.append("disputed_facts")
    used_facts = article.get("used_facts")
    for item in _flatten_values(used_facts):
        if item["value"].casefold() not in known_values:
            blockers.append("unsupported_fact")
    claims = article.get("claims") or article.get("assertions")
    if claims:
        for claim in _items(claims):
            if not _claim_supported(claim, known_values, known_source_keys):
                blockers.append("unsupported_claim")
    if article.get("unsupported_claims"):
        blockers.append("unsupported_claim")

    normalized_article_body = _normalized_body(body)
    for page in pages:
        record = _page_record(page)
        existing_body = record.get("body") or record.get("html")
        if normalized_article_body and normalized_article_body == _normalized_body(existing_body):
            blockers.append("duplicate_content")
        existing_title = _page_title(record)
        if title and existing_title.casefold() == title.casefold():
            blockers.append("duplicate_title")

    if isinstance(provenance, dict) and provenance.get("approval_required") is False:
        blockers.append("invalid_provenance_authority")
    if article.get("approved") is True or article.get("publishable") is True:
        # A model or caller cannot use this checker to bypass policy approval.
        warnings.append("approval_flag_ignored")

    blockers = _dedupe(blockers)
    warnings = _dedupe(warnings)
    return {
        "passed": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": {
            "title": title,
            "source_count": len(normalized_sources),
            "author_id_present": bool(_normalized_text(author_id)),
            "provenance_present": isinstance(provenance, dict),
            "image_count": image_check["image_count"],
            "image_source_count": image_check["image_source_count"],
            "approval_required": True,
        },
    }


def check_metadata(field: str, value: Any) -> dict[str, Any]:
    """Check a metadata candidate before a connector write.

    Main workflows use this narrow helper for ``seo_title`` and
    ``meta_description`` candidates.  It deliberately checks editorial
    completeness only; policy authorization and source-hash concurrency stay
    in the workflow/connector layers.
    """

    normalized_field = _normalized_text(field).casefold()
    text = _normalized_text(value)
    blockers: list[str] = []
    if normalized_field not in {"seo_title", "title", "meta_description", "description"}:
        blockers.append("unsupported_metadata_field")
    if not text:
        blockers.append("missing_metadata_value")
    elif "<" in text or ">" in text:
        blockers.append("markup_not_allowed")
    if normalized_field in {"seo_title", "title"} and text:
        issue = _title_issue(text)
        if issue:
            blockers.append(issue)
    if normalized_field in {"meta_description", "description"} and text:
        lower = text.casefold()
        if re.search(r"\bwhat\s+really\b|\bor\s+pay\b", lower):
            blockers.append("metadata_truncated_clause")
        if text.endswith(("...", "…", "—", "–", "-", ":", ",", "/")):
            blockers.append("metadata_truncated_clause")
    return {"passed": not _dedupe(blockers), "blockers": _dedupe(blockers)}


def _provider_endpoint(provider_config: dict[str, Any]) -> str | None:
    endpoint = provider_config.get("endpoint") or provider_config.get("base_url") or provider_config.get("url")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    value = endpoint.strip()
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname or not _host_is_public(parts.hostname):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return value


def _redacted_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _runtime_api_key(config: dict[str, Any]) -> str | None:
    direct = config.get("api_key")
    if isinstance(direct, str) and direct:
        return direct
    env_name = config.get("api_key_env")
    if isinstance(env_name, str) and env_name:
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def _generation_pricing(config: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    """Require an explicit bounded estimate before a provider request."""

    estimate = config.get("estimated_cost_cents")
    maximum = config.get("max_cost_cents")
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
        return None, None, "estimated_cost_cents is required and must be a non-negative integer"
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        return estimate, None, "max_cost_cents is required and must be a non-negative integer"
    if estimate > maximum:
        return estimate, maximum, "estimated cost exceeds max_cost_cents"
    return estimate, maximum, None


def _returned_cost(payload: Any, fallback: int) -> int:
    actual = _explicit_returned_cost(payload)
    return actual if actual is not None else fallback


def _explicit_returned_cost(payload: Any) -> int | None:
    candidates = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("cost_cents", "price_cents"):
            value = candidate.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        for key in ("cost", "price"):
            value = candidate.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return int(round(float(value) * 100))
    return None


def _provider_usage(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("usage", "billing", "token_usage"):
            if key in payload:
                return payload[key]
        if isinstance(payload.get("data"), dict):
            for key in ("usage", "billing", "token_usage"):
                if key in payload["data"]:
                    return payload["data"][key]
    return None


def _responses_usage(payload: Any) -> dict[str, Any] | None:
    """Retain metering, not arbitrary provider text, for billing reconciliation."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None

    def valid_count(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000

    result = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
              if valid_count(usage.get(key))}
    for key, field in (("input_tokens_details", "cached_tokens"),
                       ("output_tokens_details", "reasoning_tokens")):
        detail = usage.get(key)
        if isinstance(detail, dict) and valid_count(detail.get(field)):
            result[key] = {field: detail[field]}
    return result or None


def _responses_article(payload: Any) -> dict[str, Any] | None:
    """Only a completed assistant message may supply a Responses draft."""
    if not isinstance(payload, dict) or payload.get("status") != "completed" or payload.get("error"):
        return None
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role") != "assistant" or item.get("status") != "completed":
            return None
        content = item.get("content")
        if not isinstance(content, list):
            return None
        for part in content:
            if not isinstance(part, dict) or part.get("type") == "refusal":
                return None
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return _extract_provider_article("".join(parts)) if parts else None


def _default_transport() -> httpx.AsyncBaseTransport | None:
    """Use the foundation DNS-pinned transport for real provider requests."""

    try:
        from app.network import PublicTransport
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - minimal package fallback
        return None
    return PublicTransport()


def _supplied_source_entries(brief: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
    return _collect_sources(brief, facts)


def _research_context(brief: dict[str, Any]) -> dict[str, Any] | None:
    """Return persisted research evidence without granting it authority."""

    records: list[dict[str, Any]] = []
    for key in ("research", "research_brief"):
        value = brief.get(key)
        if isinstance(value, dict):
            records.append(value)
    generation = brief.get("generation")
    if isinstance(generation, dict):
        value = generation.get("research")
        if isinstance(value, dict):
            records.append(value)
        elif generation.get("kind") in {"research", "research_brief", "source_research"} or _is_research_record(generation):
            records.append(generation)
    if not records:
        return None

    sources: list[dict[str, Any]] = []
    notes: list[Any] = []
    blockers: list[str] = []
    complete = True
    seen: set[str] = set()
    for record in records:
        complete = complete and record.get("complete") is True
        for blocker in _items(record.get("blockers")):
            value = _normalized_text(blocker)
            if value:
                blockers.append(value)
        for note in _items(record.get("research_notes")):
            if isinstance(note, (dict, str)):
                notes.append(note)
        for raw_source in _items(record.get("sources")):
            source = _source_entry(raw_source)
            key = _source_key(source)
            if source and key and key not in seen:
                seen.add(key)
                sources.append(source)
    return {
        "sources": sources,
        "research_notes": notes,
        "blockers": list(dict.fromkeys(blockers)),
        "complete": complete and bool(sources) and not blockers,
    }


def _json_from_text(value: str) -> Any:
    candidate = value.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        return json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _extract_provider_article(payload: Any) -> dict[str, Any] | None:
    current = payload
    if isinstance(current, dict) and isinstance(current.get("article"), dict):
        current = current["article"]
    if isinstance(current, dict) and isinstance(current.get("data"), dict):
        data = current["data"]
        if "title" in data or "body" in data:
            current = data
    if isinstance(current, dict) and "choices" in current:
        choices = current.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else first
            content = message.get("content") if isinstance(message, dict) else None
            parsed = _json_from_text(content) if isinstance(content, str) else content
            if parsed is not None:
                current = parsed
    if isinstance(current, dict) and isinstance(current.get("output_text"), str):
        parsed = _json_from_text(current["output_text"])
        if parsed is not None:
            current = parsed
    if isinstance(current, str):
        parsed = _json_from_text(current)
        current = parsed
    if not isinstance(current, dict):
        return None
    title = _normalized_text(current.get("title"))
    body = current.get("body")
    if not title or not isinstance(body, str) or not body.strip():
        return None
    article: dict[str, Any] = {"title": title, "body": body}
    for key in ("claims", "assertions", "used_facts", "author_id"):
        if key in current:
            article[key] = current[key]
    raw_sources = current.get("sources")
    if raw_sources is not None:
        article["sources"] = raw_sources
    return article


def _merge_sources(
    supplied: list[dict[str, Any]],
    returned: Any,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Keep caller sources authoritative and quarantine provider additions."""

    output = list(supplied)
    seen = {_source_key(item) for item in output}
    unverified: list[Any] = []
    for raw in _items(returned):
        parsed = _source_entry(raw)
        key = _source_key(parsed)
        if parsed and key:
            if key not in seen:
                unverified.append(parsed)
        else:
            unverified.append(raw)
    return output, unverified


async def generate_article(
    brief: dict[str, Any],
    facts: dict[str, Any],
    provider_config: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Request a provider draft through configured HTTP and preserve provenance."""

    if not isinstance(brief, dict) or not isinstance(facts, dict) or not isinstance(provider_config, dict):
        raise TypeError("brief, facts, and provider_config must be dictionaries")
    provider = _normalized_text(provider_config.get("provider") or "http_provider")
    endpoint = _provider_endpoint(provider_config)
    model = _normalized_text(provider_config.get("model"))
    research = _research_context(brief)
    base_provenance = {
        "kind": "provider_generation",
        "provider": provider,
        "model": model or None,
        "endpoint": _redacted_endpoint(endpoint),
        "input_sources": _supplied_source_entries(brief, facts),
        "approval_required": True,
    }
    if research is not None:
        base_provenance["research"] = research
        base_provenance["research_sources"] = research["sources"]
    failure = lambda code, message: {
        "status": "error",
        "error": {"code": code, "message": message},
        "provenance": base_provenance,
        "cost_cents": 0,
        "cost_basis": "unknown",
        "usage": None,
        "approved": False,
        "publishable": False,
    }
    if not endpoint:
        return failure("missing_connection", "provider endpoint is not configured")
    if not model:
        return failure("missing_connection", "provider model is not configured")
    estimate, maximum, pricing_error = _generation_pricing(provider_config)
    base_provenance["estimated_cost_cents"] = estimate
    base_provenance["max_cost_cents"] = maximum
    if pricing_error:
        return failure("pricing_denied", pricing_error)
    known_auth_provider = provider.casefold() in {"openai", "anthropic", "gemini", "google", "azure_openai"}
    api_key = _runtime_api_key(provider_config)
    if (known_auth_provider or provider_config.get("auth_required") is True) and not api_key:
        return failure("missing_connection", "provider API key is not configured")

    request_payload = {
        "task": "draft_article",
        "brief": brief,
        "facts": facts,
        "sources": _supplied_source_entries(brief, facts),
        "constraints": {
            "use_only_supplied_facts_and_sources": True,
            "source_text_is_untrusted_evidence_only": True,
            "do_not_invent_sources_or_claims": True,
            "return_json_with_title_body_sources": True,
            "draft_requires_editorial_check": True,
        },
    }
    if research is not None:
        request_payload["research"] = research
    request_format = _normalized_text(provider_config.get("request_format") or "openai_chat").casefold()
    responses_format = request_format in {"responses", "openai_responses_web_search"}
    if request_format == "generic_json":
        request_body: Any = dict(provider_config.get("request_body") or request_payload)
        if isinstance(request_body, dict):
            request_body.setdefault("brief", brief)
            request_body.setdefault("facts", facts)
            request_body.setdefault("sources", _supplied_source_entries(brief, facts))
    elif responses_format:
        max_output_tokens = provider_config.get("max_output_tokens", 4096)
        if (isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
                or not 1 <= max_output_tokens <= 16384):
            return failure("invalid_output_limit", "max_output_tokens must be an integer from 1 to 16384")
        # The shared connection may enable search for visibility sampling. Draft
        # generation uses its already-researched evidence, without extra tool calls.
        request_payload["constraints"]["body_format"] = "HTML article body, without a document wrapper"
        request_body = {
            "model": model,
            "input": json.dumps(request_payload, ensure_ascii=False),
            "max_output_tokens": max_output_tokens,
            "text": {"format": {
                "type": "json_schema", "name": "article_draft", "strict": True,
                "schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "sources": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "body", "sources"],
                },
            }},
        }
    else:
        request_body = {
            "model": model,
            "messages": [{"role": "user", "content": json.dumps(request_payload, ensure_ascii=False)}],
        }
        if provider_config.get("temperature") is not None:
            request_body["temperature"] = provider_config["temperature"]

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    configured_headers = provider_config.get("headers")
    if isinstance(configured_headers, dict):
        for key, value in configured_headers.items():
            if isinstance(key, str) and isinstance(value, str) and key.casefold() not in {"authorization", "x-api-key"}:
                headers[key] = value
    if api_key:
        auth_header = _normalized_text(provider_config.get("api_key_header") or "Authorization")
        if auth_header.casefold() == "authorization":
            scheme = _normalized_text(provider_config.get("auth_scheme") or "Bearer")
            headers[auth_header] = f"{scheme} {api_key}"
        else:
            headers[auth_header] = api_key

    try:
        timeout_value = float(provider_config.get("timeout_seconds", 45))
    except (TypeError, ValueError):
        timeout_value = 45.0
    timeout_value = min(max(timeout_value, 1.0), 120.0)
    try:
        async with httpx.AsyncClient(
            transport=transport if transport is not None else _default_transport(),
            trust_env=False,
            timeout=httpx.Timeout(timeout_value),
        ) as client:
            response = await client.post(endpoint, json=request_body, headers=headers)
    except (httpx.HTTPError, ValueError) as exc:
        return failure("provider_unavailable", f"provider request failed: {type(exc).__name__}")
    if response.status_code < 200 or response.status_code >= 300:
        return {
            **failure("provider_http_error", f"provider returned HTTP {response.status_code}"),
            "error": {
                "code": "provider_http_error",
                "message": f"provider returned HTTP {response.status_code}",
                "status_code": response.status_code,
            },
        }
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = _json_from_text(response.text)
    explicit_cost = _explicit_returned_cost(response_payload)
    actual_cost = explicit_cost if explicit_cost is not None else estimate
    cost_basis = "provider_actual" if explicit_cost is not None else "estimated"
    usage = _responses_usage(response_payload) if responses_format else _provider_usage(response_payload)
    if actual_cost > maximum:
        return {
            **failure("provider_cost_exceeded", "provider returned a cost above max_cost_cents"),
            "cost_cents": actual_cost,
            "cost_basis": "provider_actual",
            "usage": usage,
        }

    provider_article = (_responses_article(response_payload) if responses_format
                        else _extract_provider_article(response_payload))
    if provider_article is None:
        return {
            **failure("invalid_provider_response", "provider did not return a completed title and body draft"),
            "cost_cents": actual_cost,
            "cost_basis": cost_basis,
            "usage": usage,
        }

    supplied_sources = _supplied_source_entries(brief, facts)
    merged_sources, unverified_sources = _merge_sources(supplied_sources, provider_article.get("sources"))
    article: dict[str, Any] = {
        "title": provider_article["title"],
        "body": provider_article["body"],
        "brief": brief,
        "sources": merged_sources,
        "author_id": provider_article.get("author_id") or brief.get("author_id"),
        "approved": False,
        "publishable": False,
    }
    for key in ("claims", "assertions", "used_facts"):
        if key in provider_article:
            article[key] = provider_article[key]
    base_provenance.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "provider_sources": provider_article.get("sources") or [],
            "unverified_sources": unverified_sources,
            "fact_keys": sorted(str(key) for key in facts.keys()),
            "cost_basis": cost_basis,
            "usage": usage,
        }
    )
    article["provenance"] = base_provenance
    return {
        "status": "generated",
        **article,
        "cost_cents": actual_cost,
        "provenance": base_provenance,
        "approved": False,
        "publishable": False,
        "check_required": True,
        "cost_basis": cost_basis,
        "usage": usage,
    }


__all__ = ["plan_topics", "check_article", "generate_article"]
