from __future__ import annotations

import json

import httpx
import pytest

from app.intelligence.audit import audit_page, crawl
from app.intelligence.content import check_article, check_metadata, generate_article, plan_topics
from app.browser import _chromium_navigation_provenance
from app.intelligence.visibility import collect, validate_citation_import, validate_measurement_import


def test_audit_separates_source_and_browser_and_accepts_decorative_empty_alt():
    result = audit_page(
        "https://example.test/page",
        """
        <html><head><title>What Really</title>
        <script type="application/ld+json">{bad json}</script></head>
        <body><main><h1>Services</h1><img src="/rule.svg" alt="">
        <p>Useful service information for visitors.</p></main></body></html>
        """,
        source="browser",
    )

    assert result["signals"]["browser"] is True
    assert result["signals"]["source_html"] is False
    assert "title_truncated_clause" in {item["code"] for item in result["findings"]}
    assert "schema_parse_error" in {item["code"] for item in result["findings"]}
    assert "missing_image_alt" not in {item["code"] for item in result["findings"]}
    assert result["signals"]["images"][0]["decorative"] is True


@pytest.mark.asyncio
async def test_crawl_reads_robots_sitemap_and_same_origin_links_without_offsite_requests():
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private\nSitemap: https://example.test/sitemap.xml\n",
                headers={"content-type": "text/plain"},
            )
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text="<urlset><url><loc>https://example.test/</loc></url><url><loc>https://example.test/about</loc></url></urlset>",
                headers={"content-type": "application/xml"},
            )
        if path == "/about":
            return httpx.Response(200, text="<title>About</title><main><h1>About</h1></main>", headers={"content-type": "text/html"})
        if path == "/":
            return httpx.Response(
                200,
                text='<title>Home</title><main><h1>Home</h1><a href="/about">About</a><a href="/private">No</a><a href="https://offsite.test/x">Offsite</a></main>',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404, text="not found")

    result = await crawl("https://example.test", max_pages=5, transport=httpx.MockTransport(handler))

    assert result["complete"] is True
    assert {item["url"] for item in result["pages"]} == {"https://example.test/", "https://example.test/about"}
    assert "https://offsite.test/x" not in requested
    assert "https://example.test/private" not in requested
    assert result["pending_urls"] == []


def test_topic_planning_is_bounded_and_uses_supplied_evidence_only():
    facts = {
        "business_name": "Northwind Repair",
        "audience": "Homeowners",
        "locations": ["Austin"],
        "services": ["Window repair", "Door repair"],
        "confirmed_sources": [{"url": "https://northwind.example/facts", "title": "Supplied facts"}],
    }
    briefs = plan_topics(facts, [], keywords=["window repair Austin", "unprovided research term"])

    assert 1 <= len(briefs) <= 8
    assert {brief["week"] for brief in briefs} <= {1, 2, 3, 4}
    assert all(brief["status"] == "planned" for brief in briefs)
    assert all(brief["sources"][0]["url"] == "https://northwind.example/facts" for brief in briefs)
    assert all("research result" not in json.dumps(brief).casefold() for brief in briefs)
    assert all("research_evidence" not in brief for brief in briefs)


def test_topic_planning_skips_existing_intent_and_only_refreshes_enrolled_pages():
    facts = {
        "business_name": "Northwind Repair",
        "services": ["Window repair"],
        "locations": ["Austin"],
        "confirmed_sources": ["https://northwind.example/facts"],
    }
    briefs = plan_topics(
        facts,
        [
            {"title": "Window repair in Austin", "url": "https://northwind.example/window", "enrolled": False},
            {"title": "Door repair in Austin", "url": "https://northwind.example/door", "enrolled": True},
        ],
        keywords=["window repair Austin", "door repair Austin"],
    )
    titles = {brief["title"] for brief in briefs}
    assert "Window repair in Austin" not in titles
    assert "Door repair in Austin" in titles
    assert all(brief["purpose"] != "refresh_existing" or brief["title"] == "Door repair in Austin" for brief in briefs)


def test_topic_planning_records_internal_links_and_review_only_schema_prerequisites():
    facts = {
        "business_name": "Northwind Repair",
        "audience": "Homeowners",
        "services": ["Window repair"],
        "authors": [{"name": "Alex Morgan", "id": "wp-7", "verified": True}],
        "publisher": {"name": "Northwind Repair", "verified": True},
    }
    briefs = plan_topics(
        facts,
        [
            {"title": "Window repair services", "url": "https://northwind.example/services/window", "enrolled": False},
            {"title": "Door repair services", "url": "https://northwind.example/services/door", "enrolled": False},
            {"title": "Offsite window repair", "url": "https://other.example/window", "enrolled": False},
        ],
        keywords=["window repair"],
        origin="https://northwind.example",
    )
    brief = next(item for item in briefs if item["title"] == "Window repair overview")
    assert brief["internal_link_opportunities"]
    assert all(
        item["target_url"].startswith("https://northwind.example/")
        for item in brief["internal_link_opportunities"]
    )
    recommendation = brief["structured_data_recommendation"]
    assert recommendation["schema_type"] == "Article"
    assert recommendation["status"] == "review_required"
    assert recommendation["eligible"] is True
    assert recommendation["external_urls"] == []


def test_topic_planning_uses_bounded_search_evidence_and_keeps_competitor_context_scoped():
    facts = {
        "business_name": "Northwind Repair",
        "services": ["Windshield repair"],
    }
    briefs = plan_topics(
        facts,
        [],
        keywords=["windshield repair Houston"],
        research_inputs=[
            {
                "kind": "search_observation",
                "query": "windshield repair Houston",
                "source": "gsc",
                "provider": "Google Search Console",
                "observed_at": "2026-09-18T12:00:00Z",
            },
            {
                "kind": "search_observation",
                "query": "windshield repair Houston cost",
                "source": "gsc",
                "provider": "Google Search Console",
            },
            {
                "kind": "competitor_observation",
                "competitor_url": "https://rival.example/",
                "target": "northwind.example",
                "source": "dataforseo",
                "provider": "DataForSEO",
                "position": 4,
            },
            {
                "kind": "search_observation",
                "query": "must not enter planning",
                "source": "gsc",
                "api_key": "secret-value",
            },
            {
                "kind": "search_observation",
                "query": "provider response must not enter planning",
                "source": "gsc",
                "response": {"rows": ["untrusted"]},
            },
        ],
    )

    titles = {brief["title"] for brief in briefs}
    assert "windshield repair Houston guide" in titles
    assert "windshield repair Houston cost guide" not in titles
    matching = next(brief for brief in briefs if brief["title"] == "windshield repair Houston guide")
    assert matching["evidence"][0]["kind"] == "search_observation"
    assert matching["evidence"][0]["source"] == "gsc"
    competitor_context = [
        item for item in matching["research_evidence"]
        if item["kind"] == "competitor_observation"
    ]
    assert competitor_context == [
        {
            "kind": "competitor_observation",
            "source": "dataforseo",
            "provider": "DataForSEO",
            "competitor_url": "https://rival.example/",
            "target": "northwind.example",
            "position": 4,
        }
    ]
    assert "secret-value" not in json.dumps(briefs)
    assert "provider response must not enter planning" not in json.dumps(briefs)
    assert not any("rival.example" in brief["title"] for brief in briefs)
    assert matching["planning_source_summary"] == matching["research_evidence"][1:]


def test_topic_planning_accepts_topic_inputs_and_preserves_only_scalar_provenance():
    briefs = plan_topics(
        {"business_name": "Northwind Repair"},
        [],
        research_inputs=[
            {
                "topic": "Brake repair Houston",
                "source": "search-console",
                "provider": "Google Search Console",
                "observed_at": "2026-09-18",
                "kind": "search_observation",
                "data": {"response": {"rows": ["provider-only payload"]}},
            }
        ],
    )

    brief = next(item for item in briefs if item["title"] == "Brake repair Houston guide")
    assert brief["research_evidence"] == [
        {
            "kind": "search_observation",
            "source": "search-console",
            "topic": "Brake repair Houston",
            "provider": "Google Search Console",
            "observed_at": "2026-09-18",
        }
    ]
    assert "provider-only payload" not in json.dumps(brief)
    assert "response" not in brief["research_evidence"][0]


def test_topic_planning_deduplicates_research_topics_and_skips_overlapping_intent():
    briefs = plan_topics(
        {},
        [{"title": "Door repair Austin services", "url": "https://northwind.example/door"}],
        research_inputs=[
            {
                "query": "Window repair Austin",
                "source": "gsc",
                "provider": "Google Search Console",
            },
            {
                "query": "window repair Austin",
                "source": "dataforseo",
                "provider": "DataForSEO",
            },
            {
                "query": "Window repair Austin emergency",
                "source": "gsc",
            },
            {
                "query": "Door repair Austin",
                "source": "gsc",
            },
        ],
    )

    assert [brief["title"] for brief in briefs] == ["Window repair Austin guide"]
    assert [item["source"] for item in briefs[0]["research_evidence"]] == ["gsc", "dataforseo"]


def test_topic_planning_ignores_malformed_and_credential_shaped_research_inputs():
    briefs = plan_topics(
        {},
        [],
        research_inputs=[
            {"query": "safe planning topic", "source": "gsc"},
            {"query": "secret planning topic", "source": "gsc", "api_key": "do-not-copy"},
            {"query": ["not a scalar"], "source": "gsc"},
            {"query": "bad date", "source": "gsc", "observed_at": "not-a-date"},
            {"query": "missing source"},
            {"query": "too long", "source": "gsc", "provider": {"name": "not scalar"}},
        ],
    )

    assert [brief["title"] for brief in briefs] == ["safe planning topic guide"]
    serialized = json.dumps(briefs)
    assert "do-not-copy" not in serialized
    assert "secret planning topic" not in serialized


def test_article_check_accepts_foundation_record_shape_and_rejects_markup_duplicates_and_incomplete_titles():
    facts = {
        "business_name": "Northwind Repair",
        "services": ["Window repair"],
        "confirmed_sources": [{"url": "https://northwind.example/facts", "title": "Facts"}],
    }
    article = {
        "title": "Window repair overview",
        "body": "<p>Northwind Repair provides Window repair.</p>",
        "author_id": "wp-author-1",
        "sources": facts["confirmed_sources"],
        "brief": {"generation": {"kind": "provider_generation", "provider": "test", "approval_required": True}},
    }
    assert check_article(article, facts)["passed"] is True

    bad = {**article, "title": "What Really", "body": "<script>alert(1)</script>"}
    checked = check_article(bad, facts, pages=[{"title": "Existing", "body": "<p>Existing</p>"}])
    assert checked["passed"] is False
    assert {"title_truncated_clause", "disallowed_markup"} <= set(checked["blockers"])

    duplicate = {**article, "body": "<p>Same</p>"}
    duplicate_check = check_article(duplicate, facts, pages=[{"body": "<p>Same</p>"}])
    assert "duplicate_content" in duplicate_check["blockers"]


def test_metadata_helper_matches_workflow_contract():
    assert check_metadata("seo_title", "A complete title") == {"passed": True, "blockers": []}
    failed = check_metadata("seo_title", "What Really")
    assert failed["passed"] is False
    assert "title_truncated_clause" in failed["blockers"]


@pytest.mark.asyncio
async def test_provider_generation_requires_bounded_pricing_and_preserves_sources():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "title": "Window repair overview",
                "body": "<p>Draft grounded in supplied facts.</p>",
                "sources": [{"url": "https://northwind.example/facts", "title": "Facts"}],
                "cost_cents": 4,
            },
        )

    brief = {"title": "Window repair overview", "sources": [{"url": "https://northwind.example/facts"}]}
    facts = {"business_name": "Northwind Repair"}
    missing_pricing = await generate_article(
        brief,
        facts,
        {"provider": "test", "endpoint": "https://provider.example/draft", "model": "test-model"},
        transport=httpx.MockTransport(handler),
    )
    assert missing_pricing["status"] == "error"
    assert missing_pricing["error"]["code"] == "pricing_denied"
    assert calls == 0

    generated = await generate_article(
        brief,
        facts,
        {
            "provider": "test",
            "endpoint": "https://provider.example/draft",
            "model": "test-model",
            "estimated_cost_cents": 5,
            "max_cost_cents": 10,
        },
        transport=httpx.MockTransport(handler),
    )
    assert generated["status"] == "generated"
    assert generated["approved"] is False
    assert generated["publishable"] is False
    assert generated["cost_cents"] == 4
    assert generated["sources"][0]["url"] == "https://northwind.example/facts"
    assert calls == 1


@pytest.mark.asyncio
async def test_provider_http_failure_is_not_a_successful_fallback():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="temporary provider failure")

    result = await generate_article(
        {"title": "Topic", "sources": []},
        {"business_name": "Supplied"},
        {
            "provider": "test",
            "endpoint": "https://provider.example/draft",
            "model": "test-model",
            "estimated_cost_cents": 5,
            "max_cost_cents": 10,
        },
        transport=httpx.MockTransport(handler),
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "provider_http_error"
    assert "body" not in result


@pytest.mark.asyncio
async def test_gsc_refreshes_oauth_and_returns_actual_rows_without_tokens():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            assert request.method == "POST"
            return httpx.Response(200, json={"access_token": "refreshed-secret"})
        assert request.headers.get("authorization") == "Bearer refreshed-secret"
        return httpx.Response(200, json={"rows": [{"keys": ["term"], "clicks": 3}]})

    result = await collect(
        "gsc",
        {
            "refresh_token": "refresh-secret",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token_url": "https://oauth.example/token",
        },
        {
            "site_url": "https://northwind.example",
            "endpoint": "https://gsc.example/query",
        },
        transport=httpx.MockTransport(handler),
    )
    serialized = json.dumps(result)
    assert result["data"]["rows"][0]["clicks"] == 3
    assert result["metadata"]["token_refreshed"] is True
    assert "refreshed-secret" not in serialized
    assert "refresh-secret" not in serialized


@pytest.mark.asyncio
async def test_ga4_uses_bounded_report_settings_and_read_only_conversion_filter():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer access-secret"
        body = json.loads(request.content)
        assert body["dimensions"] == [{"name": "date"}, {"name": "eventName"}]
        assert body["metrics"] == [{"name": "sessions"}, {"name": "conversions"}]
        assert body["dimensionFilter"] == {
            "filter": {
                "fieldName": "eventName",
                "inListFilter": {"values": ["generate_lead", "purchase"], "caseSensitive": False},
            },
        }
        return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": "generate_lead"}]}]})

    result = await collect(
        "ga4",
        {"access_token": "access-secret"},
        {
            "property_id": "123456789",
            "endpoint": "https://ga4.example/report",
            "dimensions": ["date", "eventName"],
            "metrics": ["sessions", "conversions"],
            "conversion_event_names": ["generate_lead", "purchase"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["metadata"]["report_dimensions"] == ["date", "eventName"]
    assert result["metadata"]["report_metrics"] == ["sessions", "conversions"]
    assert result["metadata"]["conversion_event_names"] == ["generate_lead", "purchase"]
    assert "access-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_ga4_rejects_oversized_report_settings_before_http():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"rows": []})

    result = await collect(
        "ga4",
        {"access_token": "access-secret"},
        {
            "property_id": "123456789",
            "endpoint": "https://ga4.example/report",
            "metrics": [f"metric_{index}" for index in range(11)],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_setting"
    assert calls == 0


@pytest.mark.asyncio
async def test_dataforseo_denies_unknown_pricing_before_paid_http_and_ai_requires_citations():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.startswith("/data"):
            return httpx.Response(200, json={"status_code": 20000, "cost": 0.08, "tasks": [{"rank": 2}]})
        return httpx.Response(
            200,
            json={"samples": ["answer"], "citations": [{"url": "https://source.example/page", "title": "Source"}]},
        )

    denied = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {"endpoint": "https://seo.example/data", "keyword": "window repair"},
        transport=httpx.MockTransport(handler),
    )
    assert denied["error"]["code"] == "pricing_denied"
    assert calls == 0

    serp = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {
            "endpoint": "https://seo.example/data",
            "keyword": "window repair",
            "estimated_cost_cents": 10,
            "max_cost_cents": 20,
        },
        transport=httpx.MockTransport(handler),
    )
    assert serp["data"]["tasks"][0]["rank"] == 2
    assert serp["cost_cents"] == 8

    ai = await collect(
        "ai_sample",
        {"api_key": "ai-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "query": "window repair",
            "estimated_cost_cents": 1,
            "max_cost_cents": 2,
        },
        transport=httpx.MockTransport(handler),
    )
    assert ai["data"]["ranking_type"] == "ai_answer"
    assert ai["data"]["consumer_rankings"] is False
    assert ai["data"]["citations"][0]["url"] == "https://source.example/page"
    assert "ai-secret" not in json.dumps(ai)


@pytest.mark.asyncio
async def test_ai_sample_preserves_provider_model_question_locale_answer_and_citations():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Example AI",
                "model": "provider-model",
                "locale": "en-US",
                "question": "Which shop helps?",
                "answer": "A provider answer",
                "citations": [{"url": "https://source.example/shop"}],
                "cost": 0.02,
            },
        )

    result = await collect(
        "ai_sample",
        {"api_key": "ai-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "query": "Which shop helps?",
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["data"]["provider"] == "Example AI"
    assert result["data"]["model"] == "provider-model"
    assert result["data"]["question"] == "Which shop helps?"
    assert result["data"]["locale"] == "en-US"
    assert result["data"]["answer"] == "A provider answer"
    assert result["data"]["citations"][0]["url"] == "https://source.example/shop"
    assert result["observed_at"]
    assert "ai-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_dataforseo_batches_bounded_tracked_serp_keywords_and_rejects_overflow():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "cost": 0.20,
                "tasks": [{"status_code": 20000, "rank": 4}],
            },
        )

    keywords = [f"repair topic {index}" for index in range(25)]
    result = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {
            "endpoint": "https://seo.example/data",
            "keywords": keywords,
            "estimated_cost_cents": 25,
            "max_cost_cents": 100,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert len(requests) == 1
    assert [task["keyword"] for task in requests[0]] == keywords
    assert result["cost_cents"] == 20

    overflow = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {
            "endpoint": "https://seo.example/data",
            "keywords": [f"repair topic {index}" for index in range(26)],
            "estimated_cost_cents": 25,
            "max_cost_cents": 100,
        },
        transport=httpx.MockTransport(handler),
    )
    assert overflow["status"] == "error"
    assert overflow["error"]["code"] == "missing_setting"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_dataforseo_competitor_mode_is_bounded_and_preserves_scoped_observation_context():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "cost": 0.12,
                "tasks": [{"status_code": 20000, "result": [{"items": [{"domain": "rival.example"}]}]}],
            },
        )

    result = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {
            "mode": "competitors",
            "endpoint": "https://seo.example/competitors",
            "site_url": "https://www.example.test/",
            "competitors": ["https://rival.example/", "second.example"],
            "location_code": 2840,
            "language_code": "en",
            "estimated_cost_cents": 20,
            "max_cost_cents": 30,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["kind"] == "competitor_observation"
    assert result["source"] == "dataforseo"
    assert result["data"]["observation_scope"] == "competitor"
    assert result["data"]["target"] == "example.test"
    assert result["data"]["competitors"] == ["rival.example", "second.example"]
    assert result["data"]["consumer_rankings"] is False
    assert result["cost_cents"] == 12
    assert requests[0][0]["target"] == "example.test"
    assert requests[0][0]["intersecting_domains"] == ["rival.example", "second.example"]
    assert requests[0][0]["location_code"] == 2840

    invalid = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {
            "mode": "competitor_observation",
            "site_url": "https://example.test",
            "competitors": ["one.example", "two.example", "three.example", "four.example"],
            "location_code": 2840,
            "estimated_cost_cents": 20,
            "max_cost_cents": 30,
        },
        transport=httpx.MockTransport(handler),
    )
    assert invalid["status"] == "error"
    assert invalid["error"]["code"] == "missing_setting"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_dataforseo_task_errors_are_not_successful_visibility_and_keep_actual_cost():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "status_message": "Ok.",
                "cost": 0.03,
                "tasks_error": 1,
                "tasks": [{"status_code": 40100, "status_message": "Invalid task", "cost": 0.03}],
            },
        )

    result = await collect(
        "dataforseo",
        {"login": "login", "password": "password"},
        {
            "endpoint": "https://seo.example/data",
            "keyword": "window repair",
            "estimated_cost_cents": 10,
            "max_cost_cents": 20,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "remote_error"
    assert result["data"] == {}
    assert result["cost_cents"] == 3
    assert result["cost_basis"] == "provider_actual"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, "   "])
async def test_ai_sample_without_answer_fails_closed_and_preserves_actual_cost(answer):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "cost": 0.04,
                "citations": [{"url": "https://source.example/page", "title": "Source"}],
                "answer": answer,
            },
        )

    result = await collect(
        "ai_sample",
        {"api_key": "ai-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "query": "window repair",
            "estimated_cost_cents": 5,
            "max_cost_cents": 10,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "answer_required"
    assert result["data"] == {}
    assert result["cost_cents"] == 4
    assert result["cost_basis"] == "provider_actual"
    assert "ai-secret" not in json.dumps(result)


def test_citation_import_accepts_workflow_list_and_rejects_private_urls():
    citations = validate_citation_import([{"url": "https://source.example/a", "title": "A"}])
    assert citations == [{"url": "https://source.example/a", "title": "A"}]
    assert validate_citation_import({"items": citations}) == citations
    with pytest.raises(ValueError):
        validate_citation_import([{"url": "http://127.0.0.1/private"}])


@pytest.mark.asyncio
async def test_pagespeed_normalizes_provenance_context_and_drops_unallowlisted_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "lighthouseResult": {
                "requestedUrl": "https://example.test/",
                "finalUrl": "https://example.test/home",
                "lighthouseVersion": "12.0.0",
                "categories": {"performance": {"score": 0.92}, "secret": {"score": 1}},
                "audits": {
                    "largest-contentful-paint": {"numericValue": 2300, "displayValue": "2.3 s"},
                    "unlisted-audit": {"numericValue": 999},
                },
            },
            "loadingExperience": {
                "id": "https://example.test/",
                "overall_category": "FAST",
                "metrics": {
                    "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2400, "category": "FAST"},
                    "UNLISTED_METRIC": {"percentile": "secret"},
                },
            },
            "originLoadingExperience": {
                "overall_category": "AVERAGE",
                "metrics": {"FIRST_CONTENTFUL_PAINT_MS": {"percentile": 1800}},
            },
            "arbitrary_provider_payload": {"token": "should-not-persist"},
            "usage": {"secret": "also-not-persisted"},
        })

    result = await collect(
        "pagespeed",
        {},
        {"url": "https://example.test/", "endpoint": "https://pagespeed.example/run", "strategy": "desktop"},
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["data"]["measurement_context"] == "both"
    assert result["data"]["strategy"] == "desktop"
    assert result["data"]["lab"]["performance_score"] == 0.92
    assert result["data"]["lab"]["audits"]["largest-contentful-paint"]["numeric_value"] == 2300
    assert result["data"]["loading_experience"]["metrics"]["LARGEST_CONTENTFUL_PAINT_MS"]["percentile"] == 2400
    assert "secret" not in json.dumps(result)
    assert "arbitrary_provider_payload" not in result["data"]
    assert result["usage"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_context"),
    [
        ({"lighthouseResult": {"categories": {"performance": {"score": 0.8}}}}, "lab"),
        ({"loadingExperience": {"overall_category": "FAST"}}, "field_or_origin"),
        ({"provider_extension": {"anything": "ignored"}}, "unknown"),
    ],
)
async def test_pagespeed_context_distinguishes_lab_field_and_unknown(payload, expected_context):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    result = await collect(
        "pagespeed",
        {},
        {"url": "https://example.test/", "endpoint": "https://pagespeed.example/run"},
        transport=httpx.MockTransport(handler),
    )

    assert result["data"]["measurement_context"] == expected_context


def test_chromium_navigation_provenance_is_lab_only():
    assert _chromium_navigation_provenance() == {
        "measurement_context": "lab",
        "core_web_vitals": False,
        "field_data": False,
    }


def test_measurement_import_preserves_ai_provenance_and_distinguishes_observation_kinds():
    records = validate_measurement_import([
        {
            "kind": "ai_sample",
            "source": "provider-export",
            "observed_at": "2026-09-15T12:30:00-05:00",
            "data": {
                "provider": "Example AI",
                "model": "example-model",
                "question": "Who repairs windshields?",
                "locale": "en-US",
                "answer": "A provider answer",
                "citations": [{"url": "https://source.example/repair", "title": "Source"}],
            },
        },
        {"kind": "referral_traffic", "source": "ga4-export", "data": {"sessions": 4}},
        {"url": "https://source.example/legacy", "title": "Legacy citation"},
    ])

    assert records[0]["kind"] == "ai_sample"
    assert records[0]["source"] == "provider-export"
    assert records[0]["data"]["provider"] == "Example AI"
    assert records[0]["data"]["model"] == "example-model"
    assert records[0]["data"]["question"] == "Who repairs windshields?"
    assert records[0]["data"]["citations"][0]["url"] == "https://source.example/repair"
    assert records[0]["data"]["consumer_rankings"] is False
    assert records[0]["observed_at"].isoformat() == "2026-09-15T17:30:00"
    assert records[1]["kind"] == "referral"
    assert records[2]["kind"] == "citation_import"

    wrapped = validate_measurement_import({"items": [{
        "kind": "technical",
        "source": "pagespeed-export",
        "data": {"performance_score": 92},
    }]})
    assert wrapped[0]["kind"] == "technical_eligibility"
    assert wrapped[0]["source"] == "pagespeed-export"

    with pytest.raises(ValueError, match="consumer rankings"):
        validate_measurement_import([{
            "kind": "ai_sample",
            "source": "provider-export",
            "data": {
                "question": "Question",
                "answer": "Answer",
                "consumer_rankings": True,
                "citations": ["https://source.example/a"],
            },
        }])


def test_measurement_import_preserves_backlink_and_business_listing_observations():
    records = validate_measurement_import([
        {
            "kind": "backlinks",
            "source": "link-export",
            "observed_at": "2026-09-16T10:00:00Z",
            "data": {
                "source_url": "https://publisher.example/story",
                "target_url": "https://northwind.example/services",
                "anchor_text": "Northwind Repair",
                "rel": "nofollow",
                "status_code": 200,
            },
        },
        {
            "kind": "listing",
            "source": "directory-export",
            "data": {
                "listing_url": "https://directory.example/northwind-repair",
                "platform": "Example Directory",
                "name": "Northwind Repair",
                "phone": "+1 512 555 0100",
                "claimed": True,
            },
        },
    ])

    assert records[0]["kind"] == "backlink_observation"
    assert records[0]["source"] == "link-export"
    assert records[0]["data"]["source_url"] == "https://publisher.example/story"
    assert records[0]["data"]["target_url"] == "https://northwind.example/services"
    assert records[0]["data"]["consumer_rankings"] is False
    assert records[0]["observed_at"].isoformat() == "2026-09-16T10:00:00"
    assert records[1]["kind"] == "business_listing"
    assert records[1]["source"] == "directory-export"
    assert records[1]["data"]["listing_url"] == "https://directory.example/northwind-repair"
    assert records[1]["data"]["name"] == "Northwind Repair"
    assert records[1]["data"]["consumer_rankings"] is False


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("backlink", "backlink_observation"),
        ("backlinks", "backlink_observation"),
        ("backlink-observation", "backlink_observation"),
        ("listing", "business_listing"),
        ("business_listing", "business_listing"),
        ("business-listing-observation", "business_listing"),
    ],
)
def test_measurement_import_accepts_observation_kind_aliases(alias, canonical):
    records = validate_measurement_import([{
        "kind": alias,
        "source": "owner-export",
        "data": {"url": "https://source.example/observation", "label": "Observed"},
    }])

    assert records[0]["kind"] == canonical
    assert records[0]["source"] == "owner-export"
    assert records[0]["data"]["url"] == "https://source.example/observation"


@pytest.mark.parametrize(
    "data",
    [
        {"label": "No URL"},
        {"url": "ftp://source.example/page"},
        {"url": "https://127.0.0.1/private"},
        {"url": "https://"},
        {
            "source_url": "https://publisher.example/story",
            "target_url": "http://localhost/private",
        },
    ],
)
def test_measurement_import_rejects_missing_private_and_malformed_observation_urls(data):
    with pytest.raises(ValueError, match="public HTTP URL"):
        validate_measurement_import([{
            "kind": "business_listing",
            "source": "owner-export",
            "data": data,
        }])


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {"url": "https://source.example/page", "anchor_text": "x" * 513},
            "too long",
        ),
        (
            {"url": "https://source.example/page", "platform": ["directory"]},
            "scalar",
        ),
        (
            {"url": "https://source.example/page", "details": {"nested": True}},
            "scalar",
        ),
        (
            {"url": "https://source.example/page", "consumer_rankings": True},
            "consumer rankings",
        ),
    ],
)
def test_measurement_import_rejects_unbounded_non_scalar_and_ranking_claims(data, message):
    with pytest.raises(ValueError, match=message):
        validate_measurement_import([{
            "kind": "backlink_observation",
            "source": "owner-export",
            "data": data,
        }])


def test_measurement_import_rejects_flat_and_nested_credentials_but_keeps_ordinary_metadata():
    for data in (
        {"url": "https://source.example/page", "api_key": "secret-fixture"},
        {"url": "https://source.example/page", "provider": {"credentials": {"token": "secret-fixture"}}},
    ):
        with pytest.raises(ValueError, match="credential-shaped"):
            validate_measurement_import([{
                "kind": "business_listing",
                "source": "owner-export",
                "data": data,
            }])

    records = validate_measurement_import([{
        "kind": "business_listing",
        "source": "owner-export",
        "data": {
            "url": "https://directory.example/profile",
            "token_count": 3,
            "authorization_class": "review-only",
        },
    }])
    assert records[0]["data"]["token_count"] == 3
    assert records[0]["data"]["authorization_class"] == "review-only"


def test_measurement_import_preserves_competitor_observation_scope_and_measured_position():
    records = validate_measurement_import([{
        "kind": "competitor-report",
        "source": "serp-provider-export",
        "observed_at": "2026-09-17T12:00:00Z",
        "data": {
            "competitor_url": "https://competitor.example/",
            "query": "auto glass repair houston",
            "position": 4,
            "provider": "Example SERP provider",
            "source_date": "2026-09-17",
        },
    }])

    assert records[0]["kind"] == "competitor_observation"
    assert records[0]["data"]["observation_scope"] == "competitor"
    assert records[0]["data"]["ranking_type"] == "observed_competitor"
    assert records[0]["data"]["consumer_rankings"] is False
    assert records[0]["data"]["position"] == 4
    assert records[0]["observed_at"].isoformat() == "2026-09-17T12:00:00"


@pytest.mark.parametrize(
    "data",
    [
        {"query": "auto glass repair"},
        {"competitor_url": "http://127.0.0.1/private"},
        {"competitor_url": "ftp://competitor.example/"},
        {"competitor_url": "https://competitor.example/", "consumer_rankings": True},
        {"competitor_url": "https://competitor.example/", "ranking_claim": "number one"},
    ],
)
def test_measurement_import_rejects_invalid_competitor_observations(data):
    with pytest.raises(ValueError):
        validate_measurement_import([{
            "kind": "competitor_observation",
            "source": "provider-export",
            "data": data,
        }])
