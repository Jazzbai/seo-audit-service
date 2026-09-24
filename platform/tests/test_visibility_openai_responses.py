from __future__ import annotations

import json

import httpx
import pytest

from app.intelligence.visibility import collect


def _response(question: str, *, url: str = "https://source.example/article") -> dict:
    return {
        "status": "completed",
        "usage": {"input_tokens": 11, "output_tokens": 17, "total_tokens": 28},
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"A sourced answer for {question}.",
                        "annotations": [
                            {"type": "url_citation", "url": url, "title": "Source article"}
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_openai_responses_web_search_preserves_cited_answer_without_raw_payload_or_key():
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append({
            "body": json.loads(request.content),
            "authorization": request.headers.get("authorization"),
        })
        return httpx.Response(200, json=_response("Who repairs windshields?"))

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://api.openai.example/v1/responses",
            "request_format": "openai_responses_web_search",
            "model": "configured-model",
            "query": "Who repairs windshields?",
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert result["source"] == "ai_sample"
    assert result["data"]["provider"] == "OpenAI"
    assert result["data"]["model"] == "configured-model"
    assert result["data"]["question"] == "Who repairs windshields?"
    assert result["data"]["samples"][0]["answer"].startswith("A sourced answer")
    assert result["data"]["citations"] == [
        {"url": "https://source.example/article", "title": "Source article", "source_kind": "openai_web_search"}
    ]
    assert result["data"]["consumer_rankings"] is False
    assert result["cost_cents"] == 3
    assert result["cost_basis"] == "estimated"
    assert result["metadata"]["usage"] == [{"input_tokens": 11, "output_tokens": 17, "total_tokens": 28}]
    assert requests[0]["authorization"] == "Bearer fixture-secret"
    body = requests[0]["body"]
    assert body["model"] == "configured-model"
    assert body["input"] == "Who repairs windshields?"
    assert body["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert body["max_tool_calls"] == 1
    assert body["tool_choice"] == "required"
    assert body["include"] == ["web_search_call.action.sources"]
    assert body["store"] is False
    assert "fixture-secret" not in json.dumps(result)
    assert '"output":' not in json.dumps(result)


@pytest.mark.asyncio
async def test_openai_responses_web_search_samples_each_question_and_scales_estimate():
    questions = ["Who repairs windshields?", "What services are offered?"]
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["input"])
        return httpx.Response(200, json=_response(body["input"], url=f"https://source.example/{len(calls)}"))

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "endpoint": "https://api.openai.example/v1/responses",
            "request_format": "openai_responses_web_search",
            "model": "configured-model",
            "questions": questions,
            "estimated_cost_cents": 4,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "ok"
    assert calls == questions
    assert [sample["question"] for sample in result["data"]["samples"]] == questions
    assert result["cost_cents"] == 8
    assert result["metadata"]["estimated_cost_cents"] == 8
    assert result["metadata"]["max_cost_cents"] == 10
    assert result["metadata"]["question_count"] == 2
    assert result["metadata"]["per_question_estimated_cost_cents"] == 4


@pytest.mark.asyncio
async def test_openai_responses_web_search_rejects_incomplete_or_uncited_output():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if body["input"] == "incomplete":
            return httpx.Response(200, json={"status": "incomplete", "output": []})
        return httpx.Response(200, json=_response("uncited", url="http://127.0.0.1/private"))

    for question, code in (("incomplete", "incomplete_response"), ("uncited", "invalid_citations")):
        result = await collect(
            "ai_sample",
            {"api_key": "fixture-secret"},
            {
                "endpoint": "https://api.openai.example/v1/responses",
                "request_format": "openai_responses_web_search",
                "model": "configured-model",
                "query": question,
                "estimated_cost_cents": 3,
                "max_cost_cents": 5,
            },
            transport=httpx.MockTransport(handler),
        )
        assert result["status"] == "error"
        assert result["error"]["code"] == code
        assert result["data"] == {}
        assert "fixture-secret" not in json.dumps(result)

    assert calls == 2


@pytest.mark.asyncio
async def test_openai_responses_web_search_requires_model_and_does_not_call_provider():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response("must not run"))

    result = await collect(
        "ai_sample",
        {"api_key": "fixture-secret"},
        {
            "request_format": "openai_responses_web_search",
            "query": "must not run",
            "estimated_cost_cents": 3,
            "max_cost_cents": 5,
        },
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "missing_setting"
    assert calls == 0
