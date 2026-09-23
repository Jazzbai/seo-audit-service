from __future__ import annotations

import httpx
import pytest

from app.intelligence.visibility import collect


@pytest.mark.asyncio
async def test_ai_sample_preserves_provider_observation_timestamp() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Example AI",
                "model": "example-model",
                "observed_at": "2026-09-17T12:00:00-05:00",
                "question": "Who repairs windshields?",
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
            "query": "Who repairs windshields?",
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["observed_at"] == "2026-09-17T17:00:00+00:00"


@pytest.mark.asyncio
async def test_ai_sample_rejects_invalid_provider_timestamp_without_losing_charge() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Example AI",
                "answer": "A provider answer",
                "citations": [{"url": "https://source.example/repair"}],
                "observed_at": "2026-09-17T12:00:00",
                "cost_cents": 2,
            },
        )

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://ai.example/sample",
            "query": "Who repairs windshields?",
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_observation_timestamp"
    assert result["data"] == {}
    assert result["cost_cents"] == 2
    assert result["cost_basis"] == "provider_actual"
