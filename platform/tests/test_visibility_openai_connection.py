from __future__ import annotations

import json

import httpx
import pytest

from app.intelligence.visibility import verify_ai_connection


@pytest.mark.asyncio
async def test_openai_connection_test_uses_read_only_models_endpoint_without_secret():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.4-mini", "object": "model"},
                    {"id": "another-model", "object": "model"},
                ]
            },
        )

    result = await verify_ai_connection(
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://api.openai.example/v1/responses",
            "request_format": "openai_responses_web_search",
            "model": "gpt-5.4-mini",
        },
        transport=httpx.MockTransport(handler),
    )

    assert result == {
        "status": "verified",
        "kind": "ai",
        "provider": "OpenAI",
        "model": "gpt-5.4-mini",
        "model_available": True,
        "read_only": True,
    }
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.openai.example/v1/models"
    assert requests[0].headers["authorization"] == "Bearer fixture-secret"
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_openai_connection_test_rejects_unavailable_model_without_persisting_provider_body():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "different-model"}], "secret_body": "must-not-persist"},
        )

    result = await verify_ai_connection(
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://api.openai.example/v1/responses",
            "request_format": "openai_responses_web_search",
            "model": "gpt-5.4-mini",
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"] == {
        "code": "model_unavailable",
        "message": "Configured OpenAI model is not available",
    }
    assert "must-not-persist" not in json.dumps(result)
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_openai_connection_test_requires_model_before_provider_call():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": []})

    result = await verify_ai_connection(
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://api.openai.example/v1/responses",
            "request_format": "openai_responses_web_search",
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "missing_setting"
    assert calls == 0
