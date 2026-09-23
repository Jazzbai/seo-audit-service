from __future__ import annotations

import httpx
import pytest

import app.intelligence.audit as audit_module


@pytest.mark.asyncio
async def test_crawl_marks_discovery_limit_as_incomplete(monkeypatch):
    monkeypatch.setattr(audit_module, "_MAX_DISCOVERED_URLS", 1)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/robots.txt", "/sitemap.xml"}:
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(
                200,
                text=(
                    '<html><body><main><h1>Home</h1>'
                    '<a href="/one">One</a><a href="/two">Two</a>'
                    '</main></body></html>'
                ),
                headers={"content-type": "text/html"},
            )
        raise AssertionError(request.url)

    result = await audit_module.crawl(
        "https://example.test",
        max_pages=1,
        transport=httpx.MockTransport(handler),
    )

    assert result["complete"] is False
    assert result["pending_urls"] == []
    assert result["errors"] == ["crawl discovery limit reached"]


@pytest.mark.asyncio
async def test_crawl_caps_oversized_continuation_seeds(monkeypatch):
    monkeypatch.setattr(audit_module, "_MAX_DISCOVERED_URLS", 2)
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path in {"/robots.txt", "/sitemap.xml"}:
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<html><body><main><h1>Page</h1></main></body></html>",
            headers={"content-type": "text/html"},
        )

    result = await audit_module.crawl(
        "https://example.test",
        max_pages=1,
        transport=httpx.MockTransport(handler),
        seed_urls=[
            "https://example.test/one",
            "https://example.test/two",
            "https://example.test/three",
        ],
    )

    assert result["complete"] is False
    assert result["errors"] == ["crawl discovery limit reached"]
    assert result["pending_urls"] == ["https://example.test/two"]
    assert result["visited_urls"] == ["https://example.test/one"]
    assert {page["url"] for page in result["pages"]} == {
        "https://example.test/one",
    }
    assert "/three" not in requested


@pytest.mark.asyncio
async def test_crawl_marks_nested_sitemap_limit_as_incomplete(monkeypatch):
    monkeypatch.setattr(audit_module, "_MAX_SITEMAPS", 1)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nSitemap: https://example.test/index.xml\n",
            )
        if request.url.path == "/index.xml":
            return httpx.Response(
                200,
                text=(
                    "<sitemapindex>"
                    "<sitemap><loc>https://example.test/one.xml</loc></sitemap>"
                    "<sitemap><loc>https://example.test/two.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
            )
        if request.url.path == "/":
            return httpx.Response(
                200,
                text="<html><body><main><h1>Home</h1></main></body></html>",
                headers={"content-type": "text/html"},
            )
        raise AssertionError(request.url)

    result = await audit_module.crawl(
        "https://example.test",
        max_pages=1,
        transport=httpx.MockTransport(handler),
    )

    assert result["complete"] is False
    assert result["pending_urls"] == []
    assert result["errors"] == ["sitemap discovery limit reached"]
