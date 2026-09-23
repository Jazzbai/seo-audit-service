from __future__ import annotations

import httpx
import pytest

from app.intelligence.content import check_article, generate_article
from app.intelligence.research import research_brief


def _facts() -> dict:
    return {
        "business_name": "Northwind Repair",
        "services": ["Window repair"],
        "confirmed_sources": [{"url": "https://facts.example/about", "title": "Confirmed facts"}],
    }


def _research_facts() -> dict:
    return {"business_name": "Northwind Repair", "services": ["Window repair"]}


@pytest.mark.asyncio
async def test_research_fetches_only_supplied_public_reference_and_preserves_evidence_provenance():
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            text="<html><title>Repair details</title><main><p>Window repair is available by appointment.</p></main></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    result = await research_brief(
        {
            "title": "Window repair overview",
            "sources": [{"url": "https://source.example/repair", "purpose": "service details"}],
        },
        _research_facts(),
        transport=httpx.MockTransport(handler),
    )

    assert requested == ["https://source.example/repair"]
    assert result["complete"] is True
    assert result["blockers"] == []
    source = result["sources"][0]
    assert source["url"] == "https://source.example/repair"
    assert source["purpose"] == "service details"
    assert source["fetched_at"]
    assert source["content_hash"]
    assert source["extracts"]
    assert source["source_authority"] == "untrusted"


@pytest.mark.asyncio
async def test_research_marks_unavailable_sources_incomplete_without_inventing_evidence():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="temporarily unavailable")

    result = await research_brief(
        {"sources": ["https://source.example/unavailable"]},
        _research_facts(),
        transport=httpx.MockTransport(handler),
    )

    assert result["sources"] == []
    assert result["complete"] is False
    assert "source_unavailable" in result["blockers"]
    assert any(note["status"] == "unavailable" for note in result["research_notes"] if note.get("kind") == "source")


@pytest.mark.asyncio
async def test_research_rejects_private_urls_before_transport_call():
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="should not be requested")

    result = await research_brief(
        {"sources": ["http://127.0.0.1/admin"]},
        _research_facts(),
        transport=httpx.MockTransport(handler),
    )

    assert called is False
    assert result["sources"] == []
    assert result["complete"] is False
    assert "private_url" in result["blockers"]


@pytest.mark.asyncio
async def test_research_caps_pages_and_marks_missing_or_disputed_facts_for_review():
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="<p>Reference evidence.</p>", headers={"content-type": "text/html"})

    result = await research_brief(
        {
            "sources": [
                "https://source.example/one",
                "https://source.example/two",
                "https://source.example/three",
            ],
            "research_limits": {"max_pages": 2},
            "required_facts": ["audience"],
        },
        {"disputed_facts": ["services"]},
        transport=httpx.MockTransport(handler),
    )

    assert len(requested) == 2
    assert "research_page_limit" in result["blockers"]
    assert {"missing_facts", "disputed_facts"} <= set(result["blockers"])
    assert result["complete"] is False


@pytest.mark.asyncio
async def test_research_blocks_disputed_fact_records_nested_in_lists():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<p>Reference evidence.</p>",
            headers={"content-type": "text/html"},
        )

    result = await research_brief(
        {"sources": ["https://source.example/reference"]},
        {"services": [{"name": "Window repair", "status": "disputed"}]},
        transport=httpx.MockTransport(handler),
    )

    assert result["complete"] is False
    assert "disputed_facts" in result["blockers"]
    assert any(
        note.get("disputed") == ["services[0]"]
        for note in result["research_notes"]
        if note.get("kind") == "fact_review"
    )


@pytest.mark.asyncio
async def test_research_byte_limit_is_enforced_by_public_fetch():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<p>0123456789</p>", headers={"content-type": "text/html"})

    result = await research_brief(
        {"sources": ["https://source.example/large"], "research_limits": {"max_bytes": 8}},
        _research_facts(),
        transport=httpx.MockTransport(handler),
    )

    assert result["sources"] == []
    assert result["complete"] is False
    assert "research_byte_limit" in result["blockers"]


@pytest.mark.asyncio
async def test_research_does_not_treat_document_title_as_source_evidence():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><head><title>Repair details</title></head><body></body></html>",
            headers={"content-type": "text/html"},
        )

    result = await research_brief(
        {"sources": ["https://source.example/title-only"]},
        _research_facts(),
        transport=httpx.MockTransport(handler),
    )

    assert result["sources"][0]["page_title"] == "Repair details"
    assert result["sources"][0]["extracts"] == []
    assert "source_empty" in result["blockers"]
    assert result["complete"] is False


def test_article_check_rejects_unsupported_claims_and_preserves_research_provenance_rules():
    article = {
        "title": "Window repair overview",
        "body": "<p>Northwind Repair provides Window repair.</p>",
        "author_id": "wp-author-1",
        "sources": _facts()["confirmed_sources"],
        "claims": [
            {
                "value": "Northwind won a national award",
                "supported_by": "https://facts.example/about",
            }
        ],
        "brief": {
            "generation": {
                "complete": True,
                "sources": _facts()["confirmed_sources"],
                "research_notes": [],
                "blockers": [],
            }
        },
    }

    checked = check_article(article, _facts())

    assert checked["passed"] is False
    assert "unsupported_claim" in checked["blockers"]


def test_article_check_rejects_duplicate_content_and_incomplete_or_duplicate_titles():
    article = {
        "title": "What Really",
        "body": "<p>Same editorial body.</p>",
        "author_id": "wp-author-1",
        "sources": _facts()["confirmed_sources"],
        "brief": {"generation": {"kind": "human_draft", "approval_required": True}},
    }

    checked = check_article(
        article,
        _facts(),
        pages=[{"title": "What Really", "body": "<p>Same editorial body.</p>"}],
    )

    assert checked["passed"] is False
    assert {"title_truncated_clause", "duplicate_content", "duplicate_title"} <= set(checked["blockers"])


def test_article_check_requires_author_to_match_authenticated_inventory():
    article = {
        "title": "Complete useful title",
        "body": "<p>Useful explanation.</p>",
        "author_id": "999",
        "sources": _facts()["confirmed_sources"],
        "brief": {"generation": {"kind": "editor", "approval_required": True}},
    }
    pages = [{
        "resource_type": "authors",
        "resource_key": "authors:7",
        "id": "local-page-id",
        "source": {"id": 7, "name": "Alex Morgan"},
    }]

    rejected = check_article(article, _facts(), pages=pages)
    assert rejected["passed"] is False
    assert "author_not_verified" in rejected["blockers"]

    accepted = check_article({**article, "author_id": "7"}, _facts(), pages=pages)
    assert accepted["passed"] is True


def test_article_check_requires_meaningful_alt_text_but_allows_decorative_images():
    base = {
        "title": "Complete useful title",
        "body": '<p>Useful explanation.</p><img src="https://cdn.example/image.jpg" alt="">',
        "author_id": "author-1",
        "sources": _facts()["confirmed_sources"],
        "brief": {"generation": {"kind": "editor", "approval_required": True}},
    }
    checked = check_article(base, _facts())
    assert "empty_image_alt" in checked["blockers"]

    decorative = {**base, "body": '<p>Useful explanation.</p><img src="https://cdn.example/image.jpg" alt="" role="presentation">'}
    checked_decorative = check_article(decorative, _facts())
    assert "empty_image_alt" not in checked_decorative["blockers"]


def test_article_check_requires_image_provenance_and_safe_generated_disclosure():
    body = '<p>Useful explanation.</p><img src="https://cdn.example/image.jpg" alt="Repair example">'
    base = {
        "title": "Complete useful title",
        "body": body,
        "author_id": "author-1",
        "sources": _facts()["confirmed_sources"],
        "brief": {"generation": {"kind": "editor", "approval_required": True}},
    }

    missing = check_article(base, _facts())
    assert "missing_image_provenance" in missing["blockers"]

    licensed = {
        **base,
        "brief": {
            **base["brief"],
            "image_sources": [{
                "url": "https://cdn.example/image.jpg",
                "kind": "licensed",
                "license": "CC BY 4.0",
            }],
        },
    }
    incomplete_license = check_article(licensed, _facts())
    assert {"missing_image_attribution"} <= set(incomplete_license["blockers"])

    owner = {
        **base,
        "brief": {
            **base["brief"],
            "image_sources": [{
                "url": "https://cdn.example/image.jpg",
                "kind": "owner_provided",
                "owner_confirmed": True,
            }],
        },
    }
    checked_owner = check_article(owner, _facts())
    assert "missing_image_provenance" not in checked_owner["blockers"]
    assert checked_owner["details"]["image_source_count"] == 1

    generated = {
        **base,
        "brief": {
            **base["brief"],
            "image_sources": [{
                "url": "https://cdn.example/image.jpg",
                "kind": "generated_illustration",
                "disclosure": "Illustration; not a photo of a customer or completed repair.",
                "not_real": True,
            }],
        },
    }
    checked_generated = check_article(generated, _facts())
    assert "generated_image_disclosure_required" not in checked_generated["blockers"]

    credentialed = {
        **owner,
        "brief": {
            **owner["brief"],
            "image_sources": [{
                "url": "https://cdn.example/image.jpg",
                "kind": "owner_provided",
                "owner_confirmed": True,
                "api_key": "do-not-store",
            }],
        },
    }
    checked_credentialed = check_article(credentialed, _facts())
    assert "invalid_image_provenance" in checked_credentialed["blockers"]


@pytest.mark.asyncio
async def test_provider_output_cannot_authorize_invented_sources_or_claims():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "title": "Window repair overview",
                "body": "<p>Northwind Repair provides Window repair.</p>",
                "sources": [{"url": "https://invented.example/authority", "title": "Model source"}],
                "claims": [{"value": "Northwind won a national award", "supported_by": "https://invented.example/authority"}],
                "approved": True,
                "publishable": True,
            },
        )

    brief = {
        "title": "Window repair overview",
        "author_id": "wp-author-1",
        "sources": _facts()["confirmed_sources"],
    }
    generated = await generate_article(
        brief,
        _facts(),
        {
            "provider": "test",
            "endpoint": "https://provider.example/draft",
            "model": "test-model",
            "estimated_cost_cents": 1,
            "max_cost_cents": 2,
        },
        transport=httpx.MockTransport(handler),
    )

    assert generated["status"] == "generated"
    assert generated["approved"] is False
    assert generated["publishable"] is False
    assert [source["url"] for source in generated["sources"]] == ["https://facts.example/about"]
    assert generated["provenance"]["unverified_sources"]

    checked = check_article(generated, _facts())
    assert checked["passed"] is False
    assert {"unverified_sources", "unsupported_claim"} <= set(checked["blockers"])
