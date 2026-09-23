"""HTML-only auditing and a bounded public crawl.

Auditing is intentionally separate from browser rendering.  Callers pass the
HTML they want checked and may label it ``source_html`` or ``browser``.  The
crawler only performs safe GET requests and never writes to a remote site.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from collections import deque
from dataclasses import dataclass
from html import unescape
from typing import Any, Iterable
from urllib.parse import (
    parse_qsl,
    urlencode,
    urldefrag,
    urljoin,
    unquote,
    urlsplit,
    urlunsplit,
)

import httpx
from bs4 import BeautifulSoup

try:  # defusedxml is part of the supported stack, but keep a safe fallback.
    from defusedxml import ElementTree as SafeET
except ImportError:  # pragma: no cover - only for minimal local environments
    SafeET = None  # type: ignore[assignment]


_MAX_CRAWL_PAGES = 100
_MAX_SITEMAPS = 10
_MAX_DISCOVERED_URLS = 1_000
_MAX_CRAWL_RESPONSE_BYTES = 5_000_000
_MAX_ROBOTS_RESPONSE_BYTES = 1_000_000
_MAX_SITEMAP_RESPONSE_BYTES = 2_000_000
_DEFAULT_CRAWL_DELAY = 0.01
_GENERIC_ALT = {"image", "photo", "picture", "graphic", "img", "photo image"}
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "dclid", "msclkid"}
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_source(source: Any) -> str:
    """Return the audit input kind without making source/browser assumptions."""

    if isinstance(source, dict):
        source_dict = source
        source = (
            source_dict.get("kind")
            or source_dict.get("source")
            or source_dict.get("observation_type")
        )
        if source is None:
            type_label = _normalized_text(source_dict.get("type")).lower().replace("-", "_").replace(" ", "_")
            if type_label in {
                "browser",
                "rendered",
                "rendered_html",
                "browser_html",
                "browser_rendered",
                "rendered_dom",
                "dom",
                "source",
                "source_html",
                "view_source",
                "html",
            }:
                source = type_label
        if source is None and (
            source_dict.get("browser") is True
            or source_dict.get("rendered") is True
            or source_dict.get("rendered_html") is True
        ):
            source = "browser"
    if source is None:
        return "source_html"
    value = _normalized_text(source).lower().replace("-", "_").replace(" ", "_")
    if value in {
        "browser",
        "rendered",
        "rendered_html",
        "browser_html",
        "browser_rendered",
        "rendered_dom",
        "dom",
    }:
        return "browser"
    if value in {"source", "source_html", "view_source", "html"}:
        return "source_html"
    # Unknown labels are retained as a source label but do not claim rendering.
    return value or "source_html"


def _title_issue(title: str | None) -> str | None:
    """Return a stable editorial issue code for an incomplete title."""

    value = _normalized_text(title)
    if not value:
        return "missing_title"
    lower = value.casefold()
    if re.search(r"\bwhat\s+really\b", lower):
        return "title_truncated_clause"
    if re.search(r"\bor\s+pay\b", lower):
        return "title_truncated_clause"
    if "…" in value:
        return "title_truncated_clause"
    # These are common cut-off endings from templating or title-length clamps.
    if value.endswith(("...", "…", "—", "–", "-", ":", ",", "/")):
        return "title_truncated_clause"
    if re.search(
        r"\b(?:and|or|but|nor|to|for|with|from|of|in|on|at|by|the|a|an|how|what|why|when|where|because|if)\s*$",
        lower,
    ):
        return "title_truncated_clause"
    return None


def _host_is_public(host: str) -> bool:
    host = host.strip("[]").rstrip(".").casefold()
    if not host or host in _BLOCKED_HOSTNAMES:
        return False
    if host.endswith((".local", ".internal", ".lan", ".home", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Avoid DNS resolution here: it is blocking, environment-dependent, and
        # would make a mocked crawl unexpectedly perform network lookups.  The
        # connector security helper can add resolver-level checks in production.
        return True
    return address.is_global


def _validate_http_url(url: str, *, allowed_origin: str | None = None) -> str:
    """Validate a public HTTP(S) URL and return a fragment-free URL.

    This local check intentionally mirrors the boundary expected from
    ``app.connectors.security``.  It does not log credentials or resolve DNS.
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL is required")
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs with embedded credentials are not allowed")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    if not host or not _host_is_public(host):
        raise ValueError("private or local URLs are not allowed")
    if allowed_origin is not None and not _same_origin(value, allowed_origin):
        raise ValueError("URL is outside the allowed public origin")
    return urldefrag(value)[0]


def _effective_port(parts: Any) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.casefold() == "https" else 80


def _same_origin(first: str, second: str) -> bool:
    left = urlsplit(first)
    right = urlsplit(second)
    return (
        left.scheme.casefold() == right.scheme.casefold()
        and (left.hostname or "").casefold().rstrip(".")
        == (right.hostname or "").casefold().rstrip(".")
        and _effective_port(left) == _effective_port(right)
    )


def _normalize_origin(origin: str) -> str:
    value = _validate_http_url(origin)
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc, "", "", ""))


def _default_transport(origin: str | None = None) -> httpx.AsyncBaseTransport:
    """Use the foundation public DNS-pinned transport for real requests."""

    from app.network import PublicTransport

    return PublicTransport(origin)


def _canonical_link(url: str, origin: str) -> str | None:
    try:
        absolute = _validate_http_url(urljoin(origin + "/", url), allowed_origin=origin)
    except ValueError:
        return None
    parts = urlsplit(absolute)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), ""))


def _template_pattern(url: str) -> str:
    """Return a bounded URL-shape hint for representative sampling.

    This is deliberately not presented as a CMS template name.  It is only a
    conservative shape derived from the public URL, with obvious identifiers
    and the final slug generalized so that a crawl can choose representatives
    without pretending to understand a site's private rendering system.
    """

    path = urlsplit(url).path or "/"
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "/"
    normalized: list[str] = []
    for index, segment in enumerate(segments):
        value = segment.casefold()
        if (
            re.fullmatch(r"\d+", value)
            or re.fullmatch(r"[0-9a-f]{8,}", value)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                value,
            )
        ):
            normalized.append("{id}")
        elif index == len(segments) - 1:
            normalized.append("{slug}")
        else:
            normalized.append(value)
    return "/" + "/".join(normalized)


def representative_template_key(
    url: str,
    *,
    resource_type: str | None = None,
    page_purpose: str | None = None,
) -> str:
    """Identify a representative URL shape without claiming CMS semantics."""

    resource = _normalized_text(resource_type) or "discovered_page"
    purpose = _normalized_text(page_purpose) or "unknown"
    return f"{resource}:{purpose}:{_template_pattern(url)}"


def reconcile_site_audit(
    pages: Iterable[dict[str, Any]],
    origin: str,
    *,
    browser_sample_urls: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Reconcile bounded crawl observations at site scope.

    Page findings continue to be emitted by :func:`audit_page` in their
    ``source_html`` or ``browser`` namespaces.  This helper emits a separate
    ``site_reconciliation`` namespace for relationships that cannot be proven
    by inspecting one document: redirect targets/chains, URL convergence,
    canonical collisions, and representative URL-shape coverage.
    """

    normalized_origin = _normalize_origin(origin)
    sample_urls: set[str] = set()
    for raw_url in browser_sample_urls or []:
        if isinstance(raw_url, str):
            normalized = _canonical_link(raw_url, normalized_origin)
            if normalized:
                sample_urls.add(normalized)

    observed: list[dict[str, Any]] = []
    redirects: list[dict[str, Any]] = []
    template_groups: dict[str, dict[str, Any]] = {}
    final_groups: dict[str, list[dict[str, Any]]] = {}
    canonical_groups: dict[str, set[str]] = {}

    for raw_item in pages:
        if not isinstance(raw_item, dict):
            continue
        raw_url = raw_item.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        requested_raw = raw_item.get("requested_url") or raw_url
        if not isinstance(requested_raw, str) or not requested_raw.strip():
            requested_raw = raw_url
        requested_url = _canonical_link(requested_raw, normalized_origin)
        final_url = _canonical_link(raw_url, normalized_origin)
        if requested_url is None and final_url is None:
            continue
        requested_url = requested_url or final_url
        final_url = final_url or requested_url

        raw_chain = raw_item.get("redirect_chain")
        chain: list[str] = []
        if isinstance(raw_chain, list):
            for value in raw_chain:
                if not isinstance(value, str):
                    continue
                normalized = _canonical_link(value, normalized_origin)
                if normalized and (not chain or chain[-1] != normalized):
                    chain.append(normalized)
        if not chain:
            chain = [requested_url]
            if final_url != requested_url:
                chain.append(final_url)
        elif chain[0] != requested_url:
            chain.insert(0, requested_url)
        if chain[-1] != final_url:
            chain.append(final_url)

        status_code = raw_item.get("status_code")
        try:
            status_code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        error = raw_item.get("error")
        record = {
            "requested_url": requested_url,
            "final_url": final_url,
            "redirect_chain": chain,
            "status_code": status_code,
            "error": str(error) if error else None,
            "resource_type": _normalized_text(raw_item.get("resource_type")) or "discovered_page",
            "page_purpose": _normalized_text(raw_item.get("page_purpose")),
            "canonical": None,
            "source_html_checked": False,
            "browser_observed": bool(raw_item.get("browser_observed")),
        }
        raw_signals = raw_item.get("signals")
        if isinstance(raw_signals, dict):
            record["page_purpose"] = (
                _normalized_text(raw_signals.get("page_purpose"))
                or record["page_purpose"]
            )
            metadata = raw_signals.get("metadata")
            if isinstance(metadata, dict):
                canonical = metadata.get("canonical")
                if isinstance(canonical, str):
                    record["canonical"] = _canonical_link(canonical, normalized_origin)
            record["source_html_checked"] = raw_signals.get("source") == "source_html" or bool(
                raw_signals.get("source_html")
            )
        if raw_item.get("source_html_checked") is True:
            record["source_html_checked"] = True
        observed.append(record)

        if len(chain) > 1:
            redirect = {
                "source_url": chain[0],
                "target_url": chain[-1],
                "chain": chain,
                "hop_count": len(chain) - 1,
                "status_code": status_code,
                "error": record["error"],
            }
            if redirect not in redirects:
                redirects.append(redirect)

        is_success = (
            record["error"] is None
            and status_code is not None
            and 200 <= status_code < 300
        )
        if not is_success:
            continue
        final_groups.setdefault(final_url, []).append(record)
        if record["canonical"]:
            canonical_groups.setdefault(record["canonical"], set()).add(final_url)

        template_key = representative_template_key(
            final_url,
            resource_type=record["resource_type"],
            page_purpose=record["page_purpose"],
        )
        group = template_groups.setdefault(
            template_key,
            {
                "template_key": template_key,
                "pattern": _template_pattern(final_url),
                "resource_type": record["resource_type"],
                "page_purpose": record["page_purpose"] or None,
                "sample_url": final_url,
                "sample_urls": [],
                "pages_seen": 0,
                "source_html_checked": 0,
                "browser_observed": 0,
            },
        )
        group["pages_seen"] += 1
        if len(group["sample_urls"]) < 5 and final_url not in group["sample_urls"]:
            group["sample_urls"].append(final_url)
        if record["source_html_checked"]:
            group["source_html_checked"] += 1
        if record["browser_observed"]:
            group["browser_observed"] += 1

    duplicate_urls: list[dict[str, Any]] = []
    for target_url, entries in sorted(final_groups.items()):
        source_urls = sorted({entry["requested_url"] for entry in entries})
        if len(entries) < 2:
            continue
        duplicate_urls.append({
            "target_url": target_url,
            "source_urls": source_urls,
            "distinct_source_count": len(source_urls),
            "observation_count": len(entries),
            "kind": "redirect_convergence" if len(source_urls) > 1 else "repeated_observation",
        })

    canonical_collisions = [
        {
            "canonical_url": canonical_url,
            "page_urls": sorted(page_urls),
            "page_count": len(page_urls),
        }
        for canonical_url, page_urls in sorted(canonical_groups.items())
        if len(page_urls) > 1
    ]

    browser_by_template: dict[str, list[str]] = {}
    for item in observed:
        if item["final_url"] not in sample_urls:
            continue
        key = representative_template_key(
            item["final_url"],
            resource_type=item["resource_type"],
            page_purpose=item["page_purpose"],
        )
        browser_by_template.setdefault(key, []).append(item["final_url"])
    for key, group in template_groups.items():
        queued = sorted(set(browser_by_template.get(key, [])))
        group["browser_sample_queued"] = bool(queued)
        group["browser_sample_urls"] = queued[:5]
        group["coverage_status"] = (
            "source_and_browser" if queued else "source_only"
        )

    findings: list[dict[str, Any]] = []

    def add_finding(code: str, severity: str, title: str, details: dict[str, Any]) -> None:
        identity = details.get("identity") or details
        findings.append({
            "code": code,
            "severity": severity,
            "title": title,
            "source": "site_reconciliation",
            "namespace": "site_reconciliation",
            "details": {
                "namespace": "site_reconciliation",
                "observation_type": "site_reconciliation",
                "identity": identity,
                **details,
            },
        })

    for group in duplicate_urls:
        if group["distinct_source_count"] > 1:
            add_finding(
                "duplicate_url_target",
                "warning",
                "Multiple crawled URLs resolve to one target",
                {**group, "identity": {"target_url": group["target_url"], "source_urls": group["source_urls"]}},
            )
    for group in canonical_collisions:
        add_finding(
            "duplicate_canonical_target",
            "warning",
            "Multiple pages declare the same canonical target",
            {**group, "identity": {"canonical_url": group["canonical_url"], "page_urls": group["page_urls"]}},
        )
    for redirect in redirects:
        if redirect["hop_count"] > 1:
            add_finding(
                "redirect_chain",
                "warning",
                "A URL requires multiple redirect hops",
                {**redirect, "identity": {"source_url": redirect["source_url"], "chain": redirect["chain"]}},
            )

    return {
        "namespace": "site_reconciliation",
        "evidence_version": 1,
        "pages_considered": len(observed),
        "duplicate_urls": duplicate_urls,
        "canonical_collisions": canonical_collisions,
        "redirects": redirects,
        "redirect_chains": [item for item in redirects if item["hop_count"] > 1],
        "template_coverage": {
            "basis": "bounded_url_shape",
            "templates": sorted(template_groups.values(), key=lambda item: item["template_key"]),
            "template_count": len(template_groups),
            "browser_sample_urls": sorted(sample_urls),
            "browser_covered_count": sum(
                1 for item in template_groups.values() if item.get("browser_sample_queued")
            ),
            "browser_pending_count": sum(
                1 for item in template_groups.values() if not item.get("browser_sample_queued")
            ),
        },
        "findings": findings,
    }


def _finding(
    url: str,
    source_kind: str,
    code: str,
    severity: str,
    title: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail = dict(details or {})
    detail.setdefault("url", url)
    detail.setdefault("source", source_kind)
    return {
        "key": f"{code}:{url}",
        "code": code,
        "severity": severity,
        "title": title,
        "details": detail,
    }


def _meta_values(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        content = tag.get("content")
        if name and content is not None:
            values[str(name).strip().casefold()] = _normalized_text(content)
    return values


def _meta_directives(soup: BeautifulSoup, *names: str) -> list[str]:
    wanted = {name.casefold() for name in names}
    directives: list[str] = []
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        if not name or str(name).strip().casefold() not in wanted:
            continue
        content = tag.get("content")
        if content is None:
            continue
        for item in re.split(r"[,;\s]+", str(content)):
            normalized = _normalized_text(item).casefold()
            if normalized and normalized not in directives:
                directives.append(normalized)
    return directives


def _source_headers(source: Any) -> dict[str, str]:
    if not isinstance(source, dict):
        return {}
    headers = source.get("headers") or source.get("response_headers")
    if not isinstance(headers, dict):
        return {}
    return {
        str(key).casefold(): _normalized_text(value)
        for key, value in headers.items()
        if value is not None
    }


def _jsonld_items(value: Any) -> tuple[list[Any], bool]:
    """Flatten JSON-LD item containers without treating ``@graph`` as an item.

    WordPress SEO plugins commonly emit one script whose root contains an
    ``@graph`` array. The graph is a container, so its child objects are the
    useful audit units. Return a validity flag for malformed graph shapes so
    callers can preserve an explicit finding instead of silently ignoring
    them.
    """

    roots = value if isinstance(value, list) else [value]
    items: list[Any] = []
    valid = True
    pending = list(roots)
    while pending:
        current = pending.pop(0)
        if isinstance(current, dict) and "@graph" in current:
            graph = current.get("@graph")
            if isinstance(graph, list):
                pending[0:0] = graph
            elif isinstance(graph, dict):
                pending.insert(0, graph)
            else:
                valid = False
            continue
        items.append(current)
    return items, valid


def _source_seo_values(source: Any) -> dict[str, str]:
    """Read explicitly exposed SEO values without treating HTML as authority."""

    if not isinstance(source, dict):
        return {}
    metadata = source.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    seo = metadata.get("seo")
    if not isinstance(seo, dict):
        return {}
    values: dict[str, str] = {}
    for provider in ("forgeseo", "yoast", "rank_math"):
        record = seo.get(provider)
        if not isinstance(record, dict):
            continue
        for field in ("title", "description"):
            if field not in values and record.get(field) is not None:
                values[field] = _normalized_text(record.get(field))
    return values


def _header_directives(headers: dict[str, str], name: str) -> list[str]:
    value = headers.get(name.casefold(), "")
    # X-Robots-Tag may contain an optional user-agent prefix, e.g.
    # ``googlebot: noindex, nofollow``.  The audit only needs the directives.
    if ":" in value:
        value = value.split(":", 1)[1]
    directives: list[str] = []
    for item in re.split(r"[,;\s]+", value):
        normalized = _normalized_text(item).casefold()
        if normalized and normalized not in directives:
            directives.append(normalized)
    return directives


def _visible_text(soup: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone.find_all(["head", "script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return _normalized_text(clone.get_text(" ", strip=True))


_CONFIRMATION_PATH_MARKERS = {
    "confirmation",
    "confirmed",
    "thank you",
    "thankyou",
    "thanks",
    "submitted",
    "submission",
    "success",
}
_CONFIRMATION_HEADING_RE = re.compile(
    r"^(?:thank\s+you|thanks)(?:\s+for\s+.+)?|"
    r"(?:confirmation|confirmed|submitted|submission(?:\s+(?:received|successful))?|"
    r"success(?:fully)?(?:\s+(?:submitted|completed|received|sent))?)$",
    flags=re.IGNORECASE,
)
_CONFIRMATION_VISIBLE_RE = re.compile(
    r"\b(?:"
    r"(?:thank\s+you|thanks)\s+for\s+(?:your\s+)?(?:submission|message|request|order|contacting|submitting)|"
    r"(?:your\s+)?(?:form|message|request|submission|order)\s+"
    r"(?:has\s+been|was|is)?\s*(?:successfully\s+)?"
    r"(?:submitted|received|confirmed|sent|processed)|"
    r"(?:form|submission|message|request|order)\s+"
    r"(?:submitted|received|confirmed|successful|successfully\s+processed)"
    r")\b",
    flags=re.IGNORECASE,
)


def _has_confirmation_path_signal(url: str) -> bool:
    """Return whether the URL contains a dedicated confirmation-like segment.

    Match complete path segments (and a small set of clearly composed forms)
    so an ordinary page such as ``/success-stories`` is not reclassified merely
    because its slug begins with a marker word.
    """

    for raw_segment in urlsplit(url).path.split("/"):
        segment = _normalized_text(
            re.sub(r"[-_]+", " ", unquote(raw_segment))
        ).casefold()
        if not segment:
            continue
        if segment in _CONFIRMATION_PATH_MARKERS:
            return True
        if re.fullmatch(
            r"(?:confirmation|confirmed|submitted|submission|success)(?: page| form| message)?",
            segment,
        ):
            return True
        if re.fullmatch(r"(?:thank you|thanks)(?: page| for .+)?", segment):
            return True
        if re.fullmatch(
            r"(?:form|order|request|message|submission|contact) "
            r"(?:confirmation|submitted|success)",
            segment,
        ):
            return True
    return False


def _has_confirmation_text_signal(soup: BeautifulSoup, visible_text: str) -> bool:
    headings = _normalized_text(
        " ".join(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"]))
    )
    if headings and _CONFIRMATION_HEADING_RE.search(headings.rstrip(".!?…")):
        return True
    # A standalone visible label is sufficient evidence even when a template
    # does not use a heading element.  Requiring the entire visible text to be
    # the confirmation label avoids classifying an ordinary article that merely
    # mentions a successful result.
    compact_visible = _normalized_text(visible_text.rstrip(".!?…"))
    if compact_visible and _CONFIRMATION_HEADING_RE.fullmatch(compact_visible):
        return True
    return bool(_CONFIRMATION_VISIBLE_RE.search(visible_text))


def _infer_page_purpose(
    soup: BeautifulSoup,
    visible_text: str,
    url: str | None = None,
) -> tuple[str | None, float]:
    has_confirmation_path = bool(url and _has_confirmation_path_signal(url))
    has_confirmation_text = _has_confirmation_text_signal(soup, visible_text)
    if has_confirmation_path or has_confirmation_text:
        return "confirmation", 0.95 if has_confirmation_path and has_confirmation_text else 0.85

    h1_text = _normalized_text(" ".join(node.get_text(" ", strip=True) for node in soup.find_all("h1")))
    semantic = bool(soup.find(["main", "article"]))
    lower = visible_text.casefold()
    if not visible_text or (not h1_text and not semantic and len(visible_text) < 40):
        return None, 0.0
    if soup.find("form") or any(word in lower for word in ("contact us", "get in touch", "request a quote")):
        return "contact_or_conversion", 0.75
    if any(word in lower for word in ("add to cart", "buy now", "product details", "sku")):
        return "product", 0.8
    if soup.find("article") or soup.find("time"):
        return "article", 0.8
    if soup.find("main") and (soup.find("button") or soup.find("a")):
        return "landing_or_navigation", 0.65
    return "informational", 0.6


def _complete_description_draft(soup: BeautifulSoup, visible_text: str) -> str:
    """Return one complete, source-grounded sentence suitable for review."""

    containers = soup.find_all(["main", "article"])
    roots = containers if containers else [soup]
    paragraphs: list[str] = []
    for root in roots:
        for node in root.find_all(["p", "li", "blockquote"]):
            text = _normalized_text(node.get_text(" ", strip=True))
            if text and text not in paragraphs:
                paragraphs.append(text)

    candidates = [*paragraphs, visible_text]
    for text in candidates:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            value = _normalized_text(sentence)
            if not 50 <= len(value) <= 160:
                continue
            if "<" in value or ">" in value:
                continue
            if value[-1:] not in ".!?":
                continue
            if len(re.findall(r"\b\w+[\w'-]*\b", value)) < 8:
                continue
            return value
    return ""


def _grounded_metadata_drafts(
    checked_url: str,
    source_kind: str,
    source: Any,
    soup: BeautifulSoup,
    title: str,
    title_issue: str | None,
    description: str,
    visible_text: str,
) -> list[dict[str, Any]]:
    """Build conservative metadata drafts from complete published text only.

    These are drafts, not model claims. A title comes from a complete H1 and a
    description comes from one complete sentence already present on the page.
    No character clipping, invented business fact, or automatic authority is
    introduced here; connector capability and policy checks happen later.
    """

    values = _source_seo_values(source)
    drafts: list[dict[str, Any]] = []
    h1 = _normalized_text(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    seo_title_missing = "title" in values and not values.get("title")
    title_basis = ""
    proposed_title = ""
    if title_issue == "missing_title" and not values.get("title"):
        proposed_title, title_basis = h1, "h1"
    elif seo_title_missing and title_issue is None and len(title) <= 60:
        proposed_title, title_basis = title, "rendered_title"
    if proposed_title and _title_issue(proposed_title) is None and len(proposed_title) <= 60:
        drafts.append({
            "field": "seo_title",
            "before_value": values.get("title", ""),
            "after_value": proposed_title,
            "details": {
                "reason": "missing_seo_title" if seo_title_missing else "missing_title",
                "draft": {
                    "method": "grounded_page_content",
                    "basis": title_basis,
                    "source_kind": source_kind,
                    "source_url": checked_url,
                    "grounded": True,
                },
            },
        })

    if not description and not values.get("description"):
        proposed = _complete_description_draft(soup, visible_text)
        if proposed:
            drafts.append({
                "field": "meta_description",
                "before_value": "",
                "after_value": proposed,
                "details": {
                    "reason": "missing_meta_description",
                    "draft": {
                        "method": "grounded_page_content",
                        "basis": "complete_page_sentence",
                        "source_kind": source_kind,
                        "source_url": checked_url,
                        "grounded": True,
                    },
                },
            })
    return drafts


def audit_page(url: str, html: str, source: Any = None) -> dict[str, Any]:
    """Audit one supplied HTML document without fetching or rendering it."""

    if not isinstance(html, str):
        raise TypeError("html must be a string")
    checked_url = _validate_http_url(url)
    source_kind = _normalize_source(source)
    source_headers = _source_headers(source)
    status_code: int | None = None
    if isinstance(source, dict):
        raw_status = source.get("status_code")
        if raw_status is None:
            raw_status = source.get("status")
        try:
            if raw_status is not None:
                status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = None
    soup = BeautifulSoup(html, "html.parser")
    findings: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    title_tag = soup.find("title")
    title = _normalized_text(title_tag.get_text(" ", strip=True) if title_tag else "")
    title_issue = _title_issue(title)
    if title_issue == "missing_title":
        findings.append(
            _finding(checked_url, source_kind, "missing_title", "error", "Page title is missing")
        )
    elif title_issue:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                title_issue,
                "error",
                "Page title appears truncated or incomplete",
                {"title": title},
            )
        )
    elif len(title) > 60:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "title_too_long",
                "warning",
                "Page title may be truncated in search results",
                {"length": len(title), "title": title},
            )
        )

    meta = _meta_values(soup)
    html_tag = soup.find("html")
    html_lang = _normalized_text(html_tag.get("lang") if html_tag else "")
    if not html_lang:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_html_lang",
                "warning",
                "Document language is missing",
            )
        )
    viewport = meta.get("viewport", "")
    if not viewport:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_viewport",
                "warning",
                "Mobile viewport declaration is missing",
            )
        )
    description = meta.get("description", "")
    if not description:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_meta_description",
                "warning",
                "Meta description is missing",
            )
        )
    elif len(description) < 50:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "meta_description_short",
                "warning",
                "Meta description is unusually short",
                {"length": len(description)},
            )
        )
    elif len(description) > 160:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "meta_description_long",
                "warning",
                "Meta description may be truncated in search results",
                {"length": len(description)},
            )
        )
    if not meta.get("og:title"):
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_og_title",
                "warning",
                "Open Graph title is missing",
            )
        )
    if not meta.get("og:description"):
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_og_description",
                "warning",
                "Open Graph description is missing",
            )
        )
    if not meta.get("og:image"):
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_og_image",
                "warning",
                "Open Graph image is missing",
            )
        )

    canonical_tags = [tag for tag in soup.find_all("link") if "canonical" in [
        token.casefold() for token in (tag.get("rel") or [])
    ]]
    canonical_urls: list[str] = []
    for tag in canonical_tags:
        href = _normalized_text(tag.get("href"))
        if href:
            canonical_urls.append(urljoin(checked_url, href))
    if not canonical_urls:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "missing_canonical",
                "warning",
                "Canonical link is missing",
            )
        )
    elif len(canonical_urls) > 1:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "multiple_canonicals",
                "error",
                "Multiple canonical links are present",
                {"canonical_urls": canonical_urls},
            )
        )
    else:
        canonical_valid = True
        canonical_value = canonical_urls[0]
        try:
            canonical_value = _validate_http_url(canonical_urls[0])
        except ValueError:
            canonical_valid = False
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "invalid_canonical",
                    "error",
                    "Canonical link is not a public HTTP URL",
                    {"canonical": canonical_urls[0]},
                )
            )
        if canonical_valid and not _same_origin(canonical_value, checked_url):
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "cross_origin_canonical",
                    "warning",
                    "Canonical points to another origin",
                    {"canonical": canonical_value},
                )
            )

    robots = meta.get("robots", "")
    meta_robot_directives = _meta_directives(
        soup,
        "robots",
        "googlebot",
        "googlebot-news",
    )
    header_robot_directives = _header_directives(source_headers, "x-robots-tag")
    indexing_directives: list[str] = []
    for directive in meta_robot_directives + header_robot_directives:
        if directive not in indexing_directives:
            indexing_directives.append(directive)
    noindex = "noindex" in indexing_directives or bool(
        re.search(r"(?:^|[,\s])noindex(?:$|[,\s])", robots.casefold())
    )
    nofollow = "nofollow" in indexing_directives
    if noindex:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "robots_noindex",
                "warning",
                "Page is marked noindex",
                {"robots": robots},
            )
        )
    if nofollow:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "robots_nofollow",
                "warning",
                "Page is marked nofollow",
                {"directives": indexing_directives},
            )
        )

    if status_code is not None and not 200 <= status_code < 300:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "http_status_error",
                "error",
                "Page returned a non-success HTTP status",
                {"status_code": status_code},
            )
        )

    schema_types: list[str] = []
    schema_count = 0
    schema_item_count = 0
    for script in soup.find_all("script"):
        if _normalized_text(script.get("type")).casefold() != "application/ld+json":
            continue
        schema_count += 1
        raw = script.string if script.string is not None else script.get_text()
        try:
            parsed = json.loads(raw or "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "schema_parse_error",
                    "error",
                    "Structured data JSON-LD cannot be parsed",
                    {"error": str(exc)[:160]},
                )
            )
            continue
        entries, graph_valid = _jsonld_items(parsed)
        if not graph_valid:
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "schema_shape_invalid",
                    "warning",
                    "Structured data @graph should contain an object or object list",
                )
            )
        for entry in entries:
            if isinstance(entry, dict):
                schema_item_count += 1
                value = entry.get("@type")
                if isinstance(value, list):
                    schema_types.extend(_normalized_text(item) for item in value if item)
                elif value:
                    schema_types.append(_normalized_text(value))
            else:
                findings.append(
                    _finding(
                        checked_url,
                        source_kind,
                        "schema_shape_invalid",
                        "warning",
                        "Structured data should contain an object or object list",
                    )
                )

    images: list[dict[str, Any]] = []
    for index, image in enumerate(soup.find_all("img")):
        has_alt = image.has_attr("alt")
        raw_alt = image.get("alt") if has_alt else None
        alt = _normalized_text(raw_alt)
        role = _normalized_text(image.get("role")).casefold()
        aria_hidden = _normalized_text(image.get("aria-hidden")).casefold() == "true"
        decorative = has_alt and not alt or role in {"presentation", "none"} or aria_hidden
        image_signal = {
            "index": index,
            "src": _normalized_text(image.get("src")),
            "alt": alt,
            "has_alt": has_alt,
            "decorative": decorative,
        }
        images.append(image_signal)
        if not has_alt and role not in {"presentation", "none"} and not aria_hidden:
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "missing_image_alt",
                    "warning",
                    "Informative image has no alt attribute",
                    {"index": index, "src": image_signal["src"]},
                )
            )
        elif alt.casefold() in _GENERIC_ALT:
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "generic_image_alt",
                    "warning",
                    "Image alt text is generic",
                    {"index": index, "alt": alt},
                )
            )

    links: list[dict[str, Any]] = []
    internal_count = 0
    external_count = 0
    contact_count = 0
    for index, anchor in enumerate(soup.find_all("a")):
        href = _normalized_text(anchor.get("href"))
        absolute = urljoin(checked_url, href) if href else ""
        link_kind = "empty"
        if href:
            scheme = urlsplit(href).scheme.casefold()
            if scheme in {"tel", "mailto", "sms"}:
                # These are valid business/contact actions, not navigational
                # HTTP URLs. Keep them visible without treating them as broken
                # links or attempting a network request.
                link_kind = "contact"
            else:
                try:
                    _validate_http_url(absolute)
                    link_kind = "internal" if _same_origin(absolute, checked_url) else "external"
                except ValueError:
                    if href.startswith("#"):
                        link_kind = "fragment"
                    else:
                        link_kind = "invalid"
        if link_kind == "internal":
            internal_count += 1
        elif link_kind == "external":
            external_count += 1
        elif link_kind == "contact":
            contact_count += 1
        link_text = _normalized_text(anchor.get_text(" ", strip=True))
        accessible_name = link_text or _normalized_text(anchor.get("aria-label")) or _normalized_text(anchor.get("title"))
        if not accessible_name:
            nested_alt = _normalized_text(" ".join(
                _normalized_text(node.get("alt")) for node in anchor.find_all("img") if node.get("alt")
            ))
            accessible_name = nested_alt
        if link_kind in {"empty", "invalid"}:
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "invalid_link" if link_kind == "invalid" else "empty_link",
                    "warning",
                    "Link target is missing or invalid",
                    {"index": index, "href": href},
                )
            )
        elif not accessible_name:
            findings.append(
                _finding(
                    checked_url,
                    source_kind,
                    "link_text_missing",
                    "warning",
                    "Link has no accessible name",
                    {"index": index, "href": href},
                )
            )
        links.append({"index": index, "href": href, "kind": link_kind, "text": accessible_name})

    visible_text = _visible_text(soup)
    page_purpose, purpose_confidence = _infer_page_purpose(soup, visible_text, checked_url)
    if page_purpose is None:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "page_purpose_missing",
                "error",
                "Page purpose cannot be determined from visible content",
                {"text_characters": len(visible_text)},
            )
        )
    if len(soup.find_all("h1")) > 1:
        findings.append(
            _finding(
                checked_url,
                source_kind,
                "multiple_h1",
                "warning",
                "Page has multiple H1 headings",
                {"count": len(soup.find_all("h1"))},
            )
        )

    candidates.extend(_grounded_metadata_drafts(
        checked_url,
        source_kind,
        source,
        soup,
        title,
        title_issue,
        description,
        visible_text,
    ))

    signals: dict[str, Any] = {
        "url": checked_url,
        "source": source_kind,
        "source_kind": source_kind,
        "source_html": source_kind == "source_html",
        "browser": source_kind == "browser",
        "rendered": source_kind == "browser",
        "title": title,
        "title_complete": title_issue is None,
        "meta_description": description,
        "status_code": status_code,
        "status_ok": status_code is None or 200 <= status_code < 300,
        "indexable": not noindex,
        "indexing": {
            "indexable": not noindex,
            "followable": not nofollow,
            "directives": indexing_directives,
            "source": "html_meta_and_headers" if source_headers else "html_meta",
        },
        "metadata": {
            "description": description,
            "html_lang": html_lang,
            "viewport": viewport,
            "robots": robots,
            "x_robots_tag": source_headers.get("x-robots-tag", ""),
            "og_title": meta.get("og:title", ""),
            "og_description": meta.get("og:description", ""),
            "og_image": meta.get("og:image", ""),
            "canonical": canonical_value if len(canonical_urls) == 1 and canonical_valid else None,
            "canonical_count": len(canonical_urls),
            # Length-only checks are editorial review signals, never an
            # executable semantic draft.  The workflow must receive a
            # reviewer-approved value from a separate source before writing.
            "drafting": {
                "execution_ready": False,
                "requires_meaning_review": bool(title_issue or len(title) > 60 or not description),
            },
        },
        "schema": {
            "count": schema_count,
            "item_count": schema_item_count,
            "types": schema_types,
        },
        "images": images,
        "links": {
            "items": links,
            "internal": internal_count,
            "external": external_count,
            "contact": contact_count,
            "total": len(links),
        },
        "headings": {
            "h1": [_normalized_text(node.get_text(" ", strip=True)) for node in soup.find_all("h1")],
            "h2": [_normalized_text(node.get_text(" ", strip=True)) for node in soup.find_all("h2")],
        },
        "word_count": len(re.findall(r"\b\w+[\w'’-]*\b", visible_text)),
        "page_purpose": page_purpose,
        "page_purpose_confidence": purpose_confidence,
    }
    return {"signals": signals, "findings": findings, "candidates": candidates}


@dataclass
class _RobotsRules:
    disallow: list[str]
    sitemaps: list[str]
    crawl_delay: float | None = None
    allow: list[str] | None = None

    def allows(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        query = urlsplit(url).query
        target = f"{path}?{query}" if query else path
        disallow_match = max(
            (rule for rule in self.disallow if _robots_rule_matches(rule, target)),
            key=len,
            default="",
        )
        allow_match = max(
            (rule for rule in (self.allow or []) if _robots_rule_matches(rule, target)),
            key=len,
            default="",
        )
        # Robots uses the longest matching rule; an equally long Allow wins.
        return not disallow_match or len(allow_match) >= len(disallow_match)


def _robots_rule_matches(rule: str, target: str) -> bool:
    if not rule:
        return False
    pattern = re.escape(rule).replace(r"\*", ".*")
    if rule.endswith("$"):
        pattern = pattern[:-2] + r"$"
    return re.match(r"^" + pattern, target) is not None


def _parse_robots(body: str) -> _RobotsRules:
    sitemaps: list[str] = []
    groups: list[tuple[list[str], list[str], list[str], float | None]] = []
    agents: list[str] = []
    disallow: list[str] = []
    allow: list[str] = []
    crawl_delay: float | None = None
    saw_rule = False

    def flush() -> None:
        nonlocal agents, disallow, allow, crawl_delay, saw_rule
        if agents or disallow or allow or crawl_delay is not None:
            groups.append((agents, disallow, allow, crawl_delay))
        agents, disallow, allow, crawl_delay, saw_rule = [], [], [], None, False

    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().casefold()
        value = value.strip()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue
        if key == "user-agent":
            if saw_rule:
                flush()
            agents.append(value.casefold())
            continue
        if key == "disallow":
            saw_rule = True
            if value:
                disallow.append(value)
            continue
        if key == "allow":
            saw_rule = True
            if value:
                allow.append(value)
            continue
        if key == "crawl-delay":
            saw_rule = True
            try:
                crawl_delay = max(0.0, min(float(value), 2.0))
            except ValueError:
                pass
    flush()
    selected: tuple[list[str], list[str], list[str], float | None] | None = None
    for group in groups:
        if "*" in group[0]:
            selected = group
            break
    return _RobotsRules(
        disallow=list(selected[1] if selected else []),
        sitemaps=sitemaps,
        crawl_delay=selected[3] if selected else None,
        allow=list(selected[2] if selected else []),
    )


def _sitemap_locations(body: str) -> list[str]:
    values: list[str] = []
    if SafeET is not None:
        try:
            root = SafeET.fromstring(body.encode("utf-8", errors="ignore"))
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1].casefold() == "loc" and node.text:
                    values.append(_normalized_text(unescape(node.text)))
            if values:
                return values
        except Exception:
            pass
    # A constrained fallback handles simple mocked sitemap XML without trying to
    # interpret arbitrary markup.
    values.extend(
        _normalized_text(unescape(match))
        for match in re.findall(r"<loc[^>]*>(.*?)</loc>", body, flags=re.I | re.S)
        if _normalized_text(match)
    )
    return values


class _RateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self.last = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.delay <= 0:
            return
        async with self.lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            remaining = self.delay - (now - self.last)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.last = loop.time()


async def crawl(
    origin: str,
    max_pages: int = 100,
    transport: httpx.AsyncBaseTransport | None = None,
    seed_urls: list[str] | None = None,
    visited_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Crawl one bounded batch of public, same-origin HTML.

    A continuation passes the previous ``pending_urls`` as ``seed_urls`` and
    the previous ``visited_urls`` back here.  The original three positional
    arguments remain in their original order for existing callers.
    """

    normalized_origin = _normalize_origin(origin)
    try:
        requested_max = int(max_pages)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_pages must be an integer") from exc
    if requested_max < 1:
        raise ValueError("max_pages must be at least 1")
    page_limit = min(requested_max, _MAX_CRAWL_PAGES)

    if seed_urls is not None and isinstance(seed_urls, (str, bytes)):
        raise TypeError("seed_urls must be a list of URLs")
    if visited_urls is not None and isinstance(visited_urls, (str, bytes)):
        raise TypeError("visited_urls must be a list of URLs")
    try:
        raw_visited = list(visited_urls or [])
        raw_seeds = list(seed_urls) if seed_urls is not None else [normalized_origin + "/"]
    except TypeError as exc:
        raise TypeError("seed_urls and visited_urls must be iterable URL lists") from exc

    errors: list[str] = []
    visited_order: list[str] = []
    visited: set[str] = set()

    def add_visited(value: str) -> None:
        if value not in visited:
            visited.add(value)
            visited_order.append(value)

    for raw_url in raw_visited:
        if not isinstance(raw_url, str) or not raw_url.strip():
            errors.append("visited_urls contained an invalid URL")
            continue
        canonical = _canonical_link(raw_url, normalized_origin)
        if canonical is None:
            errors.append("visited_urls contained a private or offsite URL")
            continue
        add_visited(canonical)

    # An explicit empty seed list is a useful no-op for a continuation caller;
    # it must not silently restart the crawl at the origin.
    if seed_urls is not None and not raw_seeds:
        return {
            "pages": [],
            "complete": not errors,
            "pending_urls": [],
            "visited_urls": visited_order,
            "errors": errors,
        }

    queue: deque[str] = deque()
    queued: set[str] = set()
    seed_discovery_limit_reached = False
    for raw_url in raw_seeds:
        if not isinstance(raw_url, str) or not raw_url.strip():
            errors.append("seed_urls contained an invalid URL")
            continue
        canonical = _canonical_link(raw_url, normalized_origin)
        if canonical is None:
            errors.append("seed_urls contained a private or offsite URL")
            continue
        if canonical not in visited and canonical not in queued:
            if len(queued) >= _MAX_DISCOVERED_URLS:
                # Continuation state is untrusted input.  Keep the same
                # discovery bound for supplied seeds that applies to links
                # found in documents and sitemaps; otherwise a caller can
                # bypass the bound with an oversized pending queue.
                seed_discovery_limit_reached = True
                continue
            queue.append(canonical)
            queued.add(canonical)
    if seed_discovery_limit_reached:
        errors.append("crawl discovery limit reached")

    # An already-exhausted continuation should not make an unnecessary
    # robots/sitemap request or rediscover the origin.
    if not queue:
        return {
            "pages": [],
            "complete": not errors,
            "pending_urls": [],
            "visited_urls": visited_order,
            "errors": errors,
        }

    try:
        configured_delay = float(os.environ.get("FORGSEO_CRAWL_DELAY_SECONDS", _DEFAULT_CRAWL_DELAY))
    except ValueError:
        configured_delay = _DEFAULT_CRAWL_DELAY
    limiter = _RateLimiter(min(max(configured_delay, 0.0), 2.0))
    rules = _RobotsRules([], [])
    pages: list[dict[str, Any]] = []
    discovered_count = len(queued)
    discovery_incomplete = seed_discovery_limit_reached
    robots_blocked = False
    discovery_limit_reported = seed_discovery_limit_reached
    sitemap_limit_reported = False

    def mark_sitemap_limit() -> None:
        nonlocal discovery_incomplete, sitemap_limit_reported
        discovery_incomplete = True
        if not sitemap_limit_reported:
            errors.append("sitemap discovery limit reached")
            sitemap_limit_reported = True

    async with httpx.AsyncClient(
        transport=transport if transport is not None else _default_transport(normalized_origin),
        trust_env=False,
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
        headers={"User-Agent": "ForgeSEO-intelligence/1.0", "Accept": "text/html,application/xhtml+xml,*/*;q=0.1"},
    ) as client:
        async def fetch(
            url: str,
            max_bytes: int = _MAX_CRAWL_RESPONSE_BYTES,
        ) -> tuple[str, int | None, str, str | None, str | None, list[str]]:
            current = url
            redirect_chain = [url]
            for _ in range(4):
                try:
                    _validate_http_url(current, allowed_origin=normalized_origin)
                    await limiter.wait()
                    async with client.stream("GET", current, follow_redirects=False) as response:
                        status = response.status_code
                        content_type = response.headers.get("content-type")
                        location = response.headers.get("location")
                        if 300 <= status < 400:
                            if not location:
                                return current, status, "", content_type, "redirect response missing Location header", redirect_chain
                            target = urljoin(current, location)
                            try:
                                _validate_http_url(target, allowed_origin=normalized_origin)
                            except ValueError as exc:
                                return current, status, "", content_type, str(exc), redirect_chain
                            current = target
                            normalized_target = _canonical_link(target, normalized_origin) or target
                            if normalized_target != redirect_chain[-1]:
                                redirect_chain.append(normalized_target)
                            continue

                        declared_length = response.headers.get("content-length")
                        if declared_length:
                            try:
                                if int(declared_length) > max_bytes:
                                    return current, status, "", content_type, (
                                        f"response exceeded {max_bytes} byte crawl limit"
                                    ), redirect_chain
                            except ValueError:
                                # A malformed or missing size header must not
                                # cause the body to be treated as empty.
                                pass

                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > max_bytes:
                                return current, status, "", content_type, (
                                    f"response exceeded {max_bytes} byte crawl limit"
                                ), redirect_chain
                            body.extend(chunk)
                        encoding = response.encoding or "utf-8"
                        return current, status, bytes(body).decode(encoding, errors="replace"), content_type, None, redirect_chain
                except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                    return current, None, "", None, str(exc), redirect_chain
            return current, None, "", None, "redirect limit exceeded", redirect_chain

        def add_redirect_evidence(
            record: dict[str, Any],
            requested_url: str,
            redirect_chain: list[str],
        ) -> dict[str, Any]:
            # Preserve the historic compact page shape when no redirect was
            # observed.  Redirect metadata is included only when there is a
            # concrete hop to reconcile at site scope.
            if len(redirect_chain) > 1:
                record["requested_url"] = requested_url
                record["redirect_chain"] = list(redirect_chain)
            return record

        def enqueue(link: str | None) -> None:
            nonlocal discovered_count, discovery_incomplete, discovery_limit_reported
            if not link or link in queued or link in visited or not rules.allows(link):
                return
            if discovered_count >= _MAX_DISCOVERED_URLS:
                if not discovery_limit_reported:
                    errors.append("crawl discovery limit reached")
                    discovery_limit_reported = True
                discovery_incomplete = True
                return
            queue.append(link)
            queued.add(link)
            discovered_count += 1

        # robots.txt is advisory but always checked before page requests.
        robots_url = normalized_origin + "/robots.txt"
        _, robots_status, robots_body, _, robots_error, _ = await fetch(
            robots_url,
            max_bytes=_MAX_ROBOTS_RESPONSE_BYTES,
        )
        if robots_error:
            errors.append(f"robots.txt: {robots_error}")
            robots_blocked = True
            discovery_incomplete = True
        elif robots_status is not None and 200 <= robots_status < 300:
            rules = _parse_robots(robots_body)
        elif robots_status == 404:
            pass
        elif robots_status is not None:
            errors.append(f"robots.txt returned HTTP {robots_status}")
            # A server/authentication failure is not permission to ignore
            # robots policy.  Keep the seeds pending for a safe retry.
            robots_blocked = True
            discovery_incomplete = True

        if rules.crawl_delay is not None:
            limiter.delay = max(limiter.delay, rules.crawl_delay)

        if not robots_blocked:
            sitemap_candidates = list(rules.sitemaps) or [normalized_origin + "/sitemap.xml"]
            sitemap_seen: set[str] = set()
            sitemap_queue: deque[str] = deque()
            for candidate in sitemap_candidates:
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                sitemap_url = _canonical_link(candidate, normalized_origin)
                if sitemap_url is None:
                    errors.append("sitemap URL was outside the allowed public origin")
                    discovery_incomplete = True
                    continue
                if sitemap_url in sitemap_seen or sitemap_url in sitemap_queue:
                    continue
                if len(sitemap_queue) >= _MAX_SITEMAPS:
                    mark_sitemap_limit()
                    continue
                sitemap_queue.append(sitemap_url)

            while sitemap_queue and len(sitemap_seen) < _MAX_SITEMAPS:
                sitemap_url = sitemap_queue.popleft()
                if sitemap_url in sitemap_seen:
                    continue
                sitemap_seen.add(sitemap_url)
                _, sitemap_status, sitemap_body, _, sitemap_error, _ = await fetch(
                    sitemap_url,
                    max_bytes=_MAX_SITEMAP_RESPONSE_BYTES,
                )
                if sitemap_error:
                    # A missing default sitemap is normal; other failures are
                    # retained so the scheduler can retry discovery.
                    if sitemap_status is None or sitemap_status != 404:
                        errors.append(f"{sitemap_url}: {sitemap_error}")
                        discovery_incomplete = True
                    continue
                if sitemap_status is None or sitemap_status == 404:
                    continue
                if not 200 <= sitemap_status < 300:
                    errors.append(f"{sitemap_url} returned HTTP {sitemap_status}")
                    discovery_incomplete = True
                    continue
                locations = _sitemap_locations(sitemap_body)
                is_sitemap_index = bool(re.search(r"<sitemapindex(?:\s|>)", sitemap_body, flags=re.I))
                for location in locations:
                    if is_sitemap_index:
                        nested = _canonical_link(location, normalized_origin)
                        if nested is None:
                            errors.append("nested sitemap URL was outside the allowed public origin")
                            discovery_incomplete = True
                            continue
                        if nested in sitemap_seen or nested in sitemap_queue:
                            continue
                        if len(sitemap_seen) + len(sitemap_queue) >= _MAX_SITEMAPS:
                            mark_sitemap_limit()
                            continue
                        sitemap_queue.append(nested)
                        continue
                    link = _canonical_link(location, normalized_origin)
                    if link is not None:
                        enqueue(link)

        while queue and len(pages) < page_limit and not robots_blocked:
            requested_url = queue.popleft()
            if requested_url in visited:
                continue
            if not rules.allows(requested_url):
                continue
            add_visited(requested_url)
            final_url, status, body, content_type, fetch_error, redirect_chain = await fetch(requested_url)
            if fetch_error:
                pages.append(add_redirect_evidence(
                    {"url": requested_url, "html": "", "status_code": status, "error": fetch_error},
                    requested_url,
                    redirect_chain,
                ))
                errors.append(f"{requested_url}: {fetch_error}")
                continue
            if status is None:
                pages.append(add_redirect_evidence(
                    {"url": requested_url, "html": "", "status_code": None, "error": "request failed"},
                    requested_url,
                    redirect_chain,
                ))
                errors.append(f"{requested_url}: request failed")
                continue
            is_html = (
                not content_type
                or "text/html" in content_type.casefold()
                or "application/xhtml+xml" in content_type.casefold()
            )
            if not 200 <= status < 300:
                error = f"HTTP {status}"
                pages.append(add_redirect_evidence(
                    {"url": final_url, "html": "", "status_code": status, "error": error},
                    requested_url,
                    redirect_chain,
                ))
                errors.append(f"{final_url}: {error}")
                continue
            if not is_html:
                error = "non-html response"
                pages.append(add_redirect_evidence(
                    {"url": final_url, "html": "", "status_code": status, "error": error},
                    requested_url,
                    redirect_chain,
                ))
                errors.append(f"{final_url}: {error}")
                continue
            final_canonical = _canonical_link(final_url, normalized_origin)
            if final_canonical:
                add_visited(final_canonical)
            pages.append(add_redirect_evidence(
                {"url": final_url, "html": body, "status_code": status, "error": None},
                requested_url,
                redirect_chain,
            ))
            page_soup = BeautifulSoup(body, "html.parser")
            for anchor in page_soup.find_all("a", href=True):
                link = _canonical_link(str(anchor.get("href")), normalized_origin)
                enqueue(link)

    pending_urls = [
        value for value in queue
        if value not in visited and rules.allows(value)
    ]
    complete = not pending_urls and not robots_blocked and not discovery_incomplete
    return {
        "pages": pages,
        "complete": complete,
        "pending_urls": pending_urls,
        "visited_urls": visited_order,
        "errors": errors,
    }


__all__ = ["audit_page", "crawl", "reconcile_site_audit", "representative_template_key"]
