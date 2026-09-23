from __future__ import annotations

import json

import httpx
import pytest

from app.intelligence.visibility import collect


@pytest.mark.asyncio
async def test_ai_sample_uses_configured_questions_for_paid_citation_sample() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["queries"] == ["Who repairs windshields?"]
        return httpx.Response(
            200,
            json={
                "provider": "Example AI",
                "model": "example-model",
                "answer": "A provider answer",
                "citations": [{"url": "https://source.example/repair"}],
                "cost_cents": 2,
            },
        )

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "questions": ["Who repairs windshields?"],
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["data"]["query"] == ["Who repairs windshields?"]
    assert result["data"]["citations"][0]["url"] == "https://source.example/repair"
    assert result["cost_basis"] == "provider_actual"
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_ai_sample_rejects_oversized_questions_before_http() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "questions": [f"Question {index}" for index in range(21)],
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_setting"
    assert calls == 0


@pytest.mark.asyncio
async def test_ai_sample_request_body_cannot_bypass_bounded_question_contract() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "request_body": {"queries": [f"Question {index}" for index in range(21)]},
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_setting"
    assert calls == 0


@pytest.mark.asyncio
async def test_ai_sample_pauses_without_credentials_before_http() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "answer": "This must never become a measurement",
                "citations": [{"url": "https://source.example/repair"}],
                "cost_cents": 1,
            },
        )

    result = await collect(
        "ai_sample",
        {"api_key": "   "},
        {
            "endpoint": "https://ai.example/sample",
            "query": "Who repairs windshields?",
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "missing_connection"
    assert result["data"] == {}
    assert result["cost_cents"] == 0
    assert result["cost_basis"] == "unknown"
    assert calls == 0


@pytest.mark.asyncio
async def test_google_visibility_rejects_blank_oauth_credentials_before_http() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"rows": []})

    result = await collect(
        "gsc",
        {"access_token": "   ", "refresh_token": "\t"},
        {
            "site_url": "https://example.test",
            "endpoint": "https://gsc.example/query",
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "missing_connection"
    assert result["data"] == {}
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials",
    [
        {"login": "   ", "password": "password"},
        {"login": "login", "password": "\t"},
    ],
)
async def test_dataforseo_rejects_blank_credentials_before_http(credentials: dict[str, str]) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status_code": 20000, "cost": 0.01})

    result = await collect(
        "dataforseo",
        credentials,
        {
            "endpoint": "https://seo.example/data",
            "keyword": "window repair",
            "estimated_cost_cents": 2,
            "max_cost_cents": 3,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "missing_connection"
    assert result["data"] == {}
    assert calls == 0
