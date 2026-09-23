"""Responses draft generation: no real provider requests or paid usage."""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app import workflows
from app.intelligence.content import generate_article
from app.models import Article, BudgetAccount, CostReservation, Event, Job, Site
from test_platform import platform


SOURCE = "https://evidence.example/repair"
CONFIG = {
    "provider": "OpenAI",
    "endpoint": "https://api.openai.com/v1/responses",
    "request_format": "openai_responses_web_search",
    "model": "gpt-5.4-mini",
    "api_key": "fixture-secret-never-use-live",
    "estimated_cost_cents": 5,
    "max_cost_cents": 50,
}
DRAFT = {"title": "Prepare for a repair visit", "body": "<p>Prepare your questions.</p>",
         "sources": [SOURCE, "https://invented.example/unverified"]}
USAGE = {"input_tokens": 120, "output_tokens": 60, "total_tokens": 180,
         "input_tokens_details": {"cached_tokens": 20},
         "output_tokens_details": {"reasoning_tokens": 10}}


def response_payload():
    return {
        "object": "response", "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": json.dumps(DRAFT)}]},
        ],
        "usage": USAGE,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("request_format", ["responses", "openai_responses_web_search"])
async def test_responses_draft_uses_bounded_schema_and_keeps_source_authority(request_format):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["model"] == "gpt-5.4-mini"
        assert body["max_output_tokens"] == 4096
        assert "messages" not in body and "tools" not in body
        assert "temperature" not in body
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["required"] == ["title", "body", "sources"]
        assert json.loads(body["input"])["constraints"]["use_only_supplied_facts_and_sources"] is True
        return httpx.Response(200, json=response_payload())

    result = await generate_article(
        {"title": DRAFT["title"], "sources": [{"url": SOURCE, "title": "Supplied evidence"}]},
        {}, {**CONFIG, "request_format": request_format}, transport=httpx.MockTransport(handler),
    )
    assert result["status"] == "generated"
    assert result["body"] == DRAFT["body"]
    assert not result["approved"] and not result["publishable"] and result["check_required"]
    assert result["sources"] == [{"url": SOURCE, "title": "Supplied evidence"}]
    assert result["provenance"]["unverified_sources"] == [{"url": DRAFT["sources"][1]}]
    assert result["usage"] == USAGE
    assert result["cost_basis"] == "estimated" and result["cost_cents"] == 5
    assert CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["incomplete", "failed", "missing_status", "refusal",
                                   "partial_message", "invalid_json", "no_output", "tool_only"])
async def test_responses_rejects_unfinished_output_but_retains_metering(state):
    payload = response_payload()
    if state in {"incomplete", "failed"}:
        payload["status"] = state
    elif state == "missing_status":
        payload.pop("status")
    elif state == "refusal":
        payload["output"][-1]["content"].append({"type": "refusal", "refusal": "private refusal"})
    elif state == "partial_message":
        payload["output"][-1]["status"] = "in_progress"
    elif state == "invalid_json":
        payload["output"][-1]["content"][0]["text"] = "truncated JSON: private output"
    elif state == "no_output":
        payload.pop("output")
        payload["output_text"] = json.dumps(DRAFT)
    elif state == "tool_only":
        payload["output"] = [{"type": "function_call", "arguments": json.dumps(DRAFT)}]
    payload["usage"] = {**USAGE, "debug": "private usage", "output_tokens_details": {
        "reasoning_tokens": 10, "debug": "private nested usage"}}

    result = await generate_article({}, {}, CONFIG, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_provider_response"
    assert result["usage"] == USAGE
    assert result["cost_basis"] == "estimated"
    assert "body" not in result and not result["publishable"]
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, 0, -1, 16385, "4096", None])
async def test_responses_invalid_token_bound_makes_no_request(limit):
    def handler(request):
        pytest.fail("Invalid output limit must be rejected before paid work")

    result = await generate_article({}, {}, {**CONFIG, "max_output_tokens": limit},
                                    transport=httpx.MockTransport(handler))
    assert result["error"]["code"] == "invalid_output_limit"


@pytest.mark.asyncio
async def test_responses_uses_configured_token_bound_and_sanitizes_metering():
    def handler(request):
        assert json.loads(request.content)["max_output_tokens"] == 2048
        payload = response_payload()
        payload["usage"] = {"input_tokens": True, "output_tokens": -1, "total_tokens": "180",
                            "input_tokens_details": {"cached_tokens": 1_000_000_001},
                            "output_tokens_details": {"reasoning_tokens": 8}, "debug": "private"}
        return httpx.Response(200, json=payload)

    result = await generate_article({}, {}, {**CONFIG, "max_output_tokens": 2048},
                                    transport=httpx.MockTransport(handler))
    assert result["usage"] == {"output_tokens_details": {"reasoning_tokens": 8}}


@pytest.mark.asyncio
async def test_invalid_draft_does_not_discard_an_explicit_charge():
    result = await generate_article({}, {}, CONFIG, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "incomplete", "usage": USAGE, "cost_cents": 7})))
    assert result["status"] == "error" and result["cost_basis"] == "provider_actual"
    assert result["cost_cents"] == 7 and result["usage"] == USAGE


def test_failed_generation_persists_usage_without_settling_estimate(platform, monkeypatch):
    _, factory, site_id = platform

    async def incomplete_draft(*args):
        return {"status": "error", "error": {"code": "invalid_provider_response"},
                "cost_basis": "estimated", "cost_cents": 5, "usage": USAGE}

    monkeypatch.setattr("app.intelligence.content.generate_article", incomplete_draft)
    monkeypatch.setattr(workflows, "credentials", lambda *args: ({}, CONFIG))
    with factory() as db:
        article = Article(site_id=site_id, title=DRAFT["title"], body="<p>Earlier draft.</p>",
                          brief={"research": {"complete": True, "sources": [{"url": SOURCE}]}})
        db.add(article)
        db.commit()
        job = Job(id="responses-failure", site_id=site_id, kind="generate",
                  idempotency_key="responses-failure", payload={"article_id": article.id})
        with pytest.raises(ValueError, match="invalid_provider_response"):
            asyncio.run(workflows.generate(db, db.get(Site, site_id), job))
        reservation = db.scalar(select(CostReservation).where(CostReservation.site_id == site_id))
        account = db.get(BudgetAccount, reservation.account_id)
        assert reservation.status == "reserved" and reservation.actual_cents is None
        assert account.reserved_cents == 50 and account.spent_cents == 0
        assert article.status == "review_needed" and article.body == "<p>Earlier draft.</p>"
        evidence = db.scalars(select(Event).where(Event.site_id == site_id,
                                                 Event.kind == "cost_reconciliation_needed")).all()
        assert len(evidence) == 1
        assert evidence[0].data["usage"] == USAGE
