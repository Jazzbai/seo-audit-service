"""Bounded, source-evidence collection for content briefs.

This module fetches only URLs that the caller supplied.  It records enough
provenance for a later editorial decision, but it does not turn page text into
facts, claims, confidence scores, or publication authority.  The main workflow
owns persistence and any budget reservation around this helper.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from app.network import PublicTransport, fetch

from .audit import _host_is_public, _normalized_text, _validate_http_url


MAX_RESEARCH_PAGES = 8
MAX_RESEARCH_BYTES = 1_000_000
MAX_RESEARCH_SECONDS = 20.0
MAX_EXTRACTS_PER_SOURCE = 3
MAX_EXTRACT_CHARS = 280
MAX_EXTRACT_TOTAL_CHARS = 720

_SOURCE_KEYS = (
    "sources",
    "source_urls",
    "required_sources",
    "confirmed_sources",
    "references",
    "reference_urls",
    "research_sources",
)
_SOURCE_METADATA_KEYS = (
    "title",
    "publisher",
    "published_at",
    "purpose",
    "page_purpose",
    "source_kind",
)


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _source_url(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()
    elif isinstance(value, dict):
        candidate = _normalized_text(value.get("url") or value.get("source_url") or value.get("link"))
    else:
        return None
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError:
        return None
    if not host or not _host_is_public(host):
        return None
    try:
        return _validate_http_url(candidate)
    except ValueError:
        return None


def _raw_source_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _normalized_text(value.get("url") or value.get("source_url") or value.get("link"))
    return ""


def _source_metadata(value: Any, brief: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in _SOURCE_METADATA_KEYS:
            item = value.get(key)
            if isinstance(item, (str, int, float)) and _normalized_text(item):
                output[key] = _normalized_text(item)
    purpose = output.get("page_purpose") or output.get("purpose")
    if not purpose:
        for key in ("page_purpose", "purpose"):
            candidate = brief.get(key)
            if isinstance(candidate, str) and _normalized_text(candidate):
                purpose = _normalized_text(candidate)
                break
    output["purpose"] = purpose or "reference"
    output["page_purpose"] = output["purpose"]
    return output


def _source_candidates(brief: dict[str, Any], facts: dict[str, Any]) -> list[tuple[Any, str, dict[str, Any]]]:
    brief_values: list[Any] = []
    for key in _SOURCE_KEYS:
        brief_values.extend(_items(brief.get(key)))
    evidence = brief.get("evidence")
    for item in _items(evidence):
        if isinstance(item, dict) and (item.get("url") or item.get("source_url") or item.get("link")):
            brief_values.append(item)
    # A planned brief normally already contains its explicit references.  Fall
    # back to confirmed fact sources only for a direct caller that supplied no
    # brief-level references at all.
    values = brief_values
    if not values:
        for key in ("confirmed_sources", "sources", "research_sources"):
            values.extend(_items(facts.get(key)))

    output: list[tuple[Any, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for value in values:
        raw_url = _raw_source_url(value)
        normalized = _source_url(value)
        # Keep an invalid URL candidate in the list so the caller gets a review
        # blocker, but never pass it to the network layer.
        key = (normalized or raw_url).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append((value, normalized or raw_url, _source_metadata(value, brief)))
    return output


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(candidate, minimum), maximum)


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return default
    if candidate != candidate:  # NaN
        return default
    return min(max(candidate, minimum), maximum)


def _limits(brief: dict[str, Any]) -> tuple[int, int, float]:
    settings: dict[str, Any] = {}
    for key in ("research_limits", "research"):
        value = brief.get(key)
        if isinstance(value, dict):
            settings = value
            break
    pages = _bounded_int(settings.get("max_pages"), MAX_RESEARCH_PAGES, 1, MAX_RESEARCH_PAGES)
    raw_bytes = settings.get("max_bytes", settings.get("max_source_bytes"))
    bytes_limit = _bounded_int(raw_bytes, MAX_RESEARCH_BYTES, 1, MAX_RESEARCH_BYTES)
    seconds = _bounded_float(
        settings.get("max_seconds", settings.get("timeout_seconds")),
        MAX_RESEARCH_SECONDS,
        0.001,
        MAX_RESEARCH_SECONDS,
    )
    return pages, bytes_limit, seconds


def _fact_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _empty_fact(value: Any) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _fact_review(brief: dict[str, Any], facts: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    missing: list[str] = []
    disputed: list[str] = []

    required_values: list[Any] = []
    for key in ("required_facts", "required_fact_keys", "missing_facts"):
        required_values.extend(_items(brief.get(key)))
    required_values.extend(_items(facts.get("missing_facts")))
    for item in required_values:
        if isinstance(item, dict):
            key = _normalized_text(item.get("path") or item.get("key") or item.get("name"))
        else:
            key = _normalized_text(item)
        if not key:
            continue
        value = _fact_path(facts, key)
        if _empty_fact(value):
            missing.append(key)

    disputed_values: list[Any] = []
    for container in (brief, facts):
        for key in ("disputed_facts", "facts_under_review", "disputed"):
            value = container.get(key)
            if isinstance(value, bool):
                if value:
                    disputed.append(key)
            elif isinstance(value, dict):
                for field, state in value.items():
                    if _normalized_text(state).casefold() in {"disputed", "unverified", "needs_review", "under_review"}:
                        disputed.append(_normalized_text(field))
            else:
                disputed_values.extend(_items(value))
    for item in disputed_values:
        if isinstance(item, dict):
            key = _normalized_text(item.get("path") or item.get("key") or item.get("name") or item.get("text"))
        else:
            key = _normalized_text(item)
        if key:
            disputed.append(key)

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, (list, tuple, set)):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        status = _normalized_text(value.get("status")).casefold()
        if status in {"disputed", "unverified", "needs_review", "under_review"}:
            disputed.append(path or "facts")
        for key, child in value.items():
            if key in {"confirmed_sources", "sources", "research_sources", "disputed_facts", "facts_under_review"}:
                continue
            child_path = f"{path}.{key}" if path else str(key)
            walk(child, child_path)

    walk(facts)
    blockers: list[str] = []
    notes: list[dict[str, Any]] = []
    if missing:
        blockers.append("missing_facts")
        notes.append({"kind": "fact_review", "status": "blocked", "missing": sorted(set(missing))})
    if disputed:
        blockers.append("disputed_facts")
        notes.append({"kind": "fact_review", "status": "blocked", "disputed": sorted(set(disputed))})
    return blockers, notes


def _visible_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = _normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if not title:
        heading = soup.find(["h1", "h2"])
        title = _normalized_text(heading.get_text(" ", strip=True)) if heading else ""

    # Capture the document title as metadata, but never use head content as
    # source evidence.  A title-only response must remain empty evidence so a
    # successful HTTP request cannot make research appear complete without
    # readable page content.
    head = soup.find("head")
    if head is not None:
        head.decompose()
    for tag in soup.find_all(["script", "style", "noscript", "template", "svg", "title"]):
        tag.decompose()

    nodes = soup.find_all(["p", "h1", "h2", "h3", "h4", "li", "blockquote"])
    chunks = [_normalized_text(node.get_text(" ", strip=True)) for node in nodes]
    chunks = [item for item in chunks if item]
    if not chunks:
        text = _normalized_text(soup.get_text(" ", strip=True))
    else:
        text = " ".join(chunks)
    return title, text


def _extracts(text: str) -> list[str]:
    if not text:
        return []
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
    if not sentences:
        sentences = [text]
    output: list[str] = []
    total = 0
    for sentence in sentences:
        if len(output) >= MAX_EXTRACTS_PER_SOURCE or total >= MAX_EXTRACT_TOTAL_CHARS:
            break
        value = re.sub(r"\s+", " ", sentence).strip()
        if len(value) > MAX_EXTRACT_CHARS:
            value = value[: MAX_EXTRACT_CHARS - 1].rstrip() + "…"
        if not value:
            continue
        remaining = MAX_EXTRACT_TOTAL_CHARS - total
        if len(value) > remaining:
            value = value[: max(0, remaining - 1)].rstrip() + "…"
        if value:
            output.append(value)
            total += len(value)
    return output


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _note(url: str, purpose: str, status: str, **extra: Any) -> dict[str, Any]:
    output: dict[str, Any] = {"kind": "source", "url": url, "purpose": purpose, "status": status}
    output.update(extra)
    return output


async def research_brief(
    brief: dict[str, Any],
    facts: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch caller-supplied public references and return reviewable evidence.

    The result intentionally contains no generated claims.  ``sources`` holds
    successful fetches only; rejected or unavailable URLs are described in
    ``research_notes`` and make ``complete`` false.
    """

    if not isinstance(brief, dict) or not isinstance(facts, dict):
        raise TypeError("brief and facts must be dictionaries")

    page_limit, byte_limit, time_limit = _limits(brief)
    candidates = _source_candidates(brief, facts)
    blockers, research_notes = _fact_review(brief, facts)
    sources: list[dict[str, Any]] = []

    if not candidates:
        blockers.append("missing_sources")
        research_notes.append({"kind": "research", "status": "blocked", "reason": "no supplied reference URLs"})

    if len(candidates) > page_limit:
        blockers.append("research_page_limit")
        research_notes.append(
            {
                "kind": "research",
                "status": "blocked",
                "reason": "source page limit reached",
                "page_limit": page_limit,
                "skipped": len(candidates) - page_limit,
            }
        )

    deadline = asyncio.get_running_loop().time() + time_limit
    attempted = 0
    bytes_remaining = byte_limit
    for raw_entry, requested_url, metadata in candidates[:page_limit]:
        purpose = str(metadata["purpose"])
        if not _source_url(raw_entry):
            raw_label = requested_url or "(missing URL)"
            # Avoid retaining a private or malformed URL as usable evidence.
            try:
                parts = urlsplit(raw_label)
                is_private = bool(parts.hostname) and not _host_is_public(parts.hostname)
            except ValueError:
                is_private = False
            code = "private_url" if is_private else "invalid_source_url"
            blockers.append(code)
            research_notes.append(_note(raw_label, purpose, "rejected", reason=code))
            continue

        url = _source_url(raw_entry)
        if not url:  # guarded above; keeps type checkers and future edits safe.
            blockers.append("invalid_source_url")
            continue
        attempted += 1
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            blockers.append("research_timeout")
            research_notes.append(_note(url, purpose, "skipped", reason="research time limit reached"))
            continue
        if bytes_remaining <= 0:
            blockers.append("research_byte_limit")
            research_notes.append(_note(url, purpose, "skipped", reason="research byte limit reached"))
            continue

        try:
            # A fresh PublicTransport per request is intentional: app.network.fetch
            # closes the transport-owned AsyncClient when each fetch completes.
            request_transport = transport if transport is not None else PublicTransport()
            response = await asyncio.wait_for(
                fetch(url, transport=request_transport, max_bytes=min(byte_limit, bytes_remaining)),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            blockers.append("source_unavailable")
            blockers.append("research_timeout")
            research_notes.append(_note(url, purpose, "unavailable", reason="request timed out"))
            continue
        except Exception as exc:  # public transport errors are reviewable, never claims.
            if "size limit" in str(exc).casefold() or "byte" in str(exc).casefold():
                blockers.append("research_byte_limit")
                reason = "response exceeded research byte limit"
            else:
                blockers.append("source_unavailable")
                reason = type(exc).__name__
            research_notes.append(_note(url, purpose, "unavailable", reason=reason))
            continue

        status_code = response.get("status_code") if isinstance(response, dict) else None
        if not isinstance(status_code, int) or status_code < 200 or status_code >= 300:
            blockers.append("source_unavailable")
            research_notes.append(_note(url, purpose, "unavailable", status_code=status_code, reason="non-success HTTP status"))
            continue

        headers = response.get("headers") if isinstance(response, dict) else {}
        content_type = _normalized_text(headers.get("content-type")) if isinstance(headers, dict) else ""
        if content_type and not (
            content_type.casefold().startswith("text/")
            or "html" in content_type.casefold()
            or "json" in content_type.casefold()
            or "xml" in content_type.casefold()
        ):
            blockers.append("unsupported_source_content")
            research_notes.append(_note(url, purpose, "unavailable", reason="non-text response", content_type=content_type))
            continue

        html = response.get("html") if isinstance(response, dict) else None
        if not isinstance(html, str):
            blockers.append("source_unavailable")
            research_notes.append(_note(url, purpose, "unavailable", reason="response did not contain text"))
            continue

        fetched_bytes = len(html.encode("utf-8", errors="replace"))
        if fetched_bytes > bytes_remaining:
            blockers.append("research_byte_limit")
            research_notes.append(_note(url, purpose, "unavailable", reason="research byte limit reached"))
            continue
        bytes_remaining -= fetched_bytes

        fetched_at = _now()
        content_hash = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
        page_title, visible_text = _visible_text(html)
        extracts = _extracts(visible_text)
        source: dict[str, Any] = {
            "url": url,
            "purpose": purpose,
            "page_purpose": purpose,
            "fetched_at": fetched_at,
            "fetched_time": fetched_at,
            "content_hash": content_hash,
            "contenthash": content_hash,
            "extracts": extracts,
            # Reference text is evidence to review, never a fact authority.
            "source_authority": "untrusted",
            "authority": "untrusted",
            "status": "fetched",
            "status_code": status_code,
        }
        if page_title:
            source["page_title"] = page_title
        if extracts:
            source["extract"] = extracts[0]
        if isinstance(metadata.get("title"), str) and metadata["title"]:
            source["title"] = metadata["title"]
        elif page_title:
            source["title"] = page_title
        for key in ("publisher", "published_at", "source_kind"):
            if metadata.get(key) is not None:
                source[key] = metadata[key]
        final_url = response.get("url") if isinstance(response, dict) else None
        if isinstance(final_url, str) and final_url and final_url != url:
            source["fetched_url"] = final_url
        if content_type:
            source["content_type"] = content_type
        if not extracts:
            blockers.append("source_empty")
        sources.append(source)
        research_notes.append(
            _note(
                url,
                purpose,
                "fetched",
                extract_count=len(extracts),
                content_hash=content_hash,
                authority="untrusted",
            )
        )

    if attempted == 0 and candidates and not any(note.get("status") == "rejected" for note in research_notes):
        blockers.append("missing_sources")
    blockers = list(dict.fromkeys(blockers))
    complete = bool(sources) and not blockers and len(sources) == attempted == len(candidates)
    return {
        "sources": sources,
        "research_notes": research_notes,
        "blockers": blockers,
        "complete": complete,
    }


__all__ = ["research_brief"]
