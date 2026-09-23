from __future__ import annotations

import httpx
import pytest

import app.intelligence.audit as audit_module
from app.intelligence.audit import audit_page, crawl
from app.intelligence.content import check_metadata


def _html(title: str, body: str, *, links: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body><main>{body}{links}</main></body></html>"


@pytest.mark.asyncio
async def test_crawl_continuation_bounds_each_batch_and_carries_cursor_state():
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if path == "/sitemap.xml":
            return httpx.Response(404)
        pages = {
            "/": _html("Home", "Home", links='<a href="/one">One</a><a href="/two">Two</a>'),
            "/one": _html("One", "One", links='<a href="/two">Two</a>'),
            "/two": _html("Two", "Two"),
        }
        if path in pages:
            return httpx.Response(200, text=pages[path], headers={"content-type": "text/html"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    first = await crawl("https://example.test", max_pages=1, transport=transport)

    assert len(first["pages"]) == 1
    assert first["complete"] is False
    assert first["pending_urls"] == ["https://example.test/one", "https://example.test/two"]
    assert first["visited_urls"] == ["https://example.test/"]

    second = await crawl(
        "https://example.test",
        max_pages=1,
        transport=transport,
        seed_urls=first["pending_urls"],
        visited_urls=first["visited_urls"],
    )
    assert len(second["pages"]) == 1
    assert second["pages"][0]["url"] == "https://example.test/one"
    assert second["complete"] is False
    assert second["pending_urls"] == ["https://example.test/two"]
    assert second["visited_urls"] == ["https://example.test/", "https://example.test/one"]

    third = await crawl(
        "https://example.test",
        max_pages=1,
        transport=transport,
        seed_urls=second["pending_urls"],
        visited_urls=second["visited_urls"],
    )
    assert len(third["pages"]) == 1
    assert third["pages"][0]["url"] == "https://example.test/two"
    assert third["complete"] is True
    assert third["pending_urls"] == []
    assert third["visited_urls"] == [
        "https://example.test/",
        "https://example.test/one",
        "https://example.test/two",
    ]
    assert requested.count("/") == 1
    assert requested.count("/one") == 1
    assert requested.count("/two") == 1


@pytest.mark.asyncio
async def test_crawl_reports_http_and_missing_location_errors_without_false_page_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(404)
        if request.url.path == "/missing":
            return httpx.Response(404, text="not found")
        if request.url.path == "/redirect":
            return httpx.Response(302)
        raise AssertionError(request.url)

    result = await crawl(
        "https://example.test",
        max_pages=2,
        transport=httpx.MockTransport(handler),
        seed_urls=["https://example.test/missing", "https://example.test/redirect"],
    )

    assert result["complete"] is True
    assert result["pending_urls"] == []
    assert {page["status_code"] for page in result["pages"]} == {302, 404}
    assert all(page["html"] == "" and page["error"] for page in result["pages"])
    assert any("HTTP 404" in error for error in result["errors"])
    assert any("missing Location" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_crawl_does_not_truncate_a_body_when_content_length_is_missing(monkeypatch):
    body = "<html><body><main><h1>" + ("complete " * 20) + "</h1></main></body></html>"
    monkeypatch.setattr(audit_module, "_MAX_CRAWL_RESPONSE_BYTES", len(body.encode()) + 1)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" or request.url.path == "/sitemap.xml":
            return httpx.Response(404)
        if request.url.path == "/":
            # Deliberately omit Content-Length and Content-Type.
            return httpx.Response(200, content=body.encode(), headers={})
        raise AssertionError(request.url)

    result = await crawl("https://example.test", max_pages=1, transport=httpx.MockTransport(handler))

    assert result["complete"] is True
    assert result["pages"] == [
        {"url": "https://example.test/", "html": body, "status_code": 200, "error": None}
    ]
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_crawl_applies_robots_and_sitemap_bounds_and_never_fetches_offsite_or_private_urls():
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            sitemap_lines = "\n".join(
                f"Sitemap: https://example.test/sitemap-{index}.xml" for index in range(12)
            )
            return httpx.Response(200, text=f"User-agent: *\nDisallow: /private\n{sitemap_lines}")
        if path.startswith("/sitemap-"):
            return httpx.Response(404)
        if path == "/":
            return httpx.Response(
                200,
                text=_html(
                    "Home",
                    "Home",
                    links='<a href="/private">Private</a><a href="https://offsite.test/no">Offsite</a>',
                ),
                headers={"content-type": "text/html"},
            )
        raise AssertionError(request.url)

    result = await crawl(
        "https://example.test",
        max_pages=1,
        transport=httpx.MockTransport(handler),
        seed_urls=["https://example.test/", "http://127.0.0.1/private"],
    )

    sitemap_requests = [url for url in requested if "/sitemap-" in url]
    assert len(sitemap_requests) == 10
    assert "https://offsite.test/no" not in requested
    assert "http://127.0.0.1/private" not in requested
    assert "https://example.test/private" not in requested
    assert result["pages"][0]["url"] == "https://example.test/"
    assert result["complete"] is False
    assert "sitemap discovery limit reached" in result["errors"]
    assert any("private or offsite" in error for error in result["errors"])


def test_default_crawl_transport_is_the_dns_pinned_public_transport(monkeypatch):
    class FakePublicTransport:
        def __init__(self, origin=None):
            self.origin = origin

    monkeypatch.setattr("app.network.PublicTransport", FakePublicTransport)
    transport = audit_module._default_transport("https://example.test")

    assert isinstance(transport, FakePublicTransport)
    assert transport.origin == "https://example.test"


def test_audit_reports_indexing_canonical_status_and_source_browser_labels():
    html = """
    <html><head>
      <title>Service details</title>
      <meta name="robots" content="noindex, nofollow">
      <link rel="canonical" href="https://example.test/service#section">
    </head><body><main><h1>Service details</h1>
      <form><label>Email<input name="email"></label></form>
      <img src="/decorative.svg" alt="">
      <img src="/informative.svg">
    </main></body></html>
    """
    result = audit_page(
        "https://example.test/service",
        html,
        source={
            "observation_type": "browser_rendered",
            "status_code": 200,
            "headers": {"X-Robots-Tag": "noindex"},
        },
    )
    codes = {finding["code"] for finding in result["findings"]}

    assert result["signals"]["source"] == "browser"
    assert result["signals"]["browser"] is True
    assert result["signals"]["source_html"] is False
    assert result["signals"]["indexable"] is False
    assert result["signals"]["indexing"]["followable"] is False
    assert result["signals"]["metadata"]["canonical"] == "https://example.test/service"
    assert {"robots_noindex", "robots_nofollow", "missing_image_alt"} <= codes
    assert "http_status_error" not in codes
    assert result["signals"]["images"][0]["decorative"] is True
    assert result["signals"]["page_purpose"] == "contact_or_conversion"

    source_result = audit_page(
        "https://example.test/service",
        html,
        source={"kind": "source_html", "status_code": 404},
    )
    source_codes = {finding["code"] for finding in source_result["findings"]}
    assert source_result["signals"]["source_html"] is True
    assert source_result["signals"]["browser"] is False
    assert source_result["signals"]["status_ok"] is False
    assert "http_status_error" in source_codes


def test_audit_reports_missing_open_graph_image_without_creating_a_write_candidate():
    missing = audit_page(
        "https://example.test/service",
        """
        <html><head>
          <title>Service details</title>
          <meta property="og:title" content="Service details">
          <meta property="og:description" content="Useful service details for visitors.">
        </head><body><main><h1>Service details</h1>
          <p>Complete service information for visitors who need help.</p>
        </main></body></html>
        """,
        source="source_html",
    )
    missing_codes = {finding["code"] for finding in missing["findings"]}

    assert "missing_og_image" in missing_codes
    assert missing["signals"]["metadata"]["og_image"] == ""
    assert all(candidate["field"] != "og_image" for candidate in missing["candidates"])

    present = audit_page(
        "https://example.test/service",
        """
        <html><head>
          <title>Service details</title>
          <meta property="og:title" content="Service details">
          <meta property="og:description" content="Useful service details for visitors.">
          <meta property="og:image" content="https://example.test/social/service.jpg">
        </head><body><main><h1>Service details</h1>
          <p>Complete service information for visitors who need help.</p>
        </main></body></html>
        """,
        source="source_html",
    )

    assert "missing_og_image" not in {finding["code"] for finding in present["findings"]}
    assert present["signals"]["metadata"]["og_image"] == (
        "https://example.test/social/service.jpg"
    )


def test_audit_flattens_json_ld_graph_items_and_counts_schema_objects():
    result = audit_page(
        "https://example.test/article",
        """
        <html><head>
          <title>Repair guidance</title>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@graph": [
              {"@type": "WebSite", "@id": "https://example.test/#site"},
              {"@type": ["Article", "NewsArticle"], "headline": "Repair guidance"}
            ]
          }
          </script>
        </head><body><main><h1>Repair guidance</h1>
          <p>Complete repair guidance for visitors who need practical help.</p>
        </main></body></html>
        """,
        source="source_html",
    )

    schema = result["signals"]["schema"]
    assert schema["count"] == 1
    assert schema["item_count"] == 2
    assert {"WebSite", "Article", "NewsArticle"} <= set(schema["types"])
    assert "schema_shape_invalid" not in {finding["code"] for finding in result["findings"]}


def test_audit_reports_mobile_and_language_metadata_without_creating_candidates():
    missing = audit_page(
        "https://example.test/service",
        "<html><head><title>Service</title></head><body><main><h1>Service</h1></main></body></html>",
        source="source_html",
    )
    missing_codes = {finding["code"] for finding in missing["findings"]}
    assert {"missing_html_lang", "missing_viewport"} <= missing_codes
    assert missing["signals"]["metadata"]["html_lang"] == ""
    assert missing["signals"]["metadata"]["viewport"] == ""
    assert missing["candidates"] == []

    present = audit_page(
        "https://example.test/service",
        """
        <html lang="en-US"><head><title>Service</title>
          <meta name="viewport" content="width=device-width, initial-scale=1">
        </head><body><main><h1>Service</h1></main></body></html>
        """,
        source="source_html",
    )
    present_codes = {finding["code"] for finding in present["findings"]}
    assert "missing_html_lang" not in present_codes
    assert "missing_viewport" not in present_codes
    assert present["signals"]["metadata"]["html_lang"] == "en-US"
    assert present["signals"]["metadata"]["viewport"] == (
        "width=device-width, initial-scale=1"
    )


def test_audit_treats_contact_links_as_valid_actions_and_unknown_schemes_as_invalid():
    result = audit_page(
        "https://example.test/contact",
        """
        <html><head><title>Contact</title></head><body><main><h1>Contact</h1>
          <a href="tel:+18325550123">Call us</a>
          <a href="mailto:hello@example.test">Email us</a>
          <a href="javascript:void(0)">Unsupported action</a>
        </main></body></html>
        """,
        source="source_html",
    )
    codes = {finding["code"] for finding in result["findings"]}
    links = result["signals"]["links"]

    assert "invalid_link" in codes
    assert links["contact"] == 2
    assert [item["kind"] for item in links["items"][:2]] == ["contact", "contact"]


def test_audit_reports_canonical_conflict_and_unknown_page_purpose():
    result = audit_page(
        "https://example.test/page",
        """
        <html><head>
          <title>Page</title>
          <link rel="canonical" href="https://example.test/one">
          <link rel="canonical" href="https://other.example/two">
        </head><body></body></html>
        """,
        source="source_html",
    )
    codes = {finding["code"] for finding in result["findings"]}

    assert "multiple_canonicals" in codes
    assert "page_purpose_missing" in codes
    assert result["signals"]["page_purpose"] is None
    assert result["signals"]["source"] == "source_html"


def test_title_meaning_is_checked_beyond_length_and_never_becomes_an_executable_draft():
    result = audit_page(
        "https://example.test/page",
        "<html><head><title>What Really</title></head><body><main><h1>Service</h1></main></body></html>",
        source="source_html",
    )
    codes = {finding["code"] for finding in result["findings"]}

    assert "title_truncated_clause" in codes
    assert result["signals"]["title_complete"] is False
    assert result["signals"]["metadata"]["drafting"]["execution_ready"] is False
    assert result["candidates"] == []
    assert check_metadata("seo_title", "What Really")["passed"] is False
    assert check_metadata("meta_description", "What Really")["passed"] is False

    long_but_complete = audit_page(
        "https://example.test/long",
        "<html><head><title>" + ("A meaningful title " * 5) + "</title></head>"
        "<body><main><h1>Meaningful page</h1></main></body></html>",
    )
    assert "title_too_long" in {finding["code"] for finding in long_but_complete["findings"]}
    assert long_but_complete["candidates"] == []
    assert long_but_complete["signals"]["metadata"]["drafting"]["execution_ready"] is False


def test_audit_creates_only_complete_grounded_metadata_drafts():
    result = audit_page(
        "https://example.test/repair",
        """
        <html><head></head><body><main>
          <h1>Emergency Auto Repair in Houston</h1>
          <p>Auto1StopShop helps Houston drivers recover from an accident with clear repair guidance and a free estimate.</p>
        </main></body></html>
        """,
        source="source_html",
    )

    drafts = {item["field"]: item for item in result["candidates"]}
    assert set(drafts) == {"seo_title", "meta_description"}
    assert drafts["seo_title"]["after_value"] == "Emergency Auto Repair in Houston"
    assert drafts["meta_description"]["after_value"].endswith("estimate.")
    assert drafts["meta_description"]["after_value"] == (
        "Auto1StopShop helps Houston drivers recover from an accident with clear repair guidance and a free estimate."
    )
    assert drafts["meta_description"]["details"]["draft"]["grounded"] is True
    assert drafts["meta_description"]["details"]["draft"]["basis"] == "complete_page_sentence"


def test_audit_uses_rendered_title_only_when_connector_exposes_an_empty_seo_field():
    result = audit_page(
        "https://example.test/service",
        """
        <html><head><title>Collision Repair Services</title></head><body><main>
          <h1>Collision Repair Services</h1>
          <p>Our collision repair team explains the next steps after an accident and helps drivers arrange a careful estimate.</p>
        </main></body></html>
        """,
        source={
            "metadata": {
                "seo": {
                    "forgeseo": {"title": "", "description": ""},
                },
            },
        },
    )

    title_drafts = [item for item in result["candidates"] if item["field"] == "seo_title"]
    assert len(title_drafts) == 1
    assert title_drafts[0]["before_value"] == ""
    assert title_drafts[0]["after_value"] == "Collision Repair Services"
    assert title_drafts[0]["details"]["reason"] == "missing_seo_title"
    assert title_drafts[0]["details"]["draft"]["basis"] == "rendered_title"
