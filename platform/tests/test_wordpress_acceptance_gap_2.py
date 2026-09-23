from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.connectors import AmbiguousOutcome, ConnectorError
from app.connectors.wordpress import WordPressClient


def _response(request: httpx.Request, value: object) -> httpx.Response:
    return httpx.Response(200, json=value, request=request)


@pytest.mark.asyncio
async def test_missing_collection_write_route_does_not_authorize_draft_creation() -> None:
    requests: list[httpx.Request] = []
    index: dict[str, Any] = {
        "namespaces": ["wp/v2"],
        "routes": {
            # The item route is present, so the general write capability is
            # available, but the collection route has no documented schema.
            "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/wp-json/":
            return _response(request, index)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://route-capability.fixture.test",
        {"token": "fixture-token"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()

        assert capabilities["native"]["update"] is True
        assert capabilities["native"]["create"] is False
        with pytest.raises(ConnectorError, match="post creation capability"):
            await client.create_draft(
                {"title": "Should remain local", "body": "No remote write"},
                "create:fixture",
            )

    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_publish_timeout_keeps_operation_key_when_reconciliation_read_fails() -> None:
    post = {
        "id": 7,
        "slug": "fixture-post",
        "status": "draft",
        "type": "post",
        "link": "https://fixture.test/fixture-post",
        "title": {"raw": "Fixture post", "rendered": "Fixture post"},
        "content": {"raw": "<p>Approved body</p>", "rendered": "<p>Approved body</p>"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
    }
    post_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_reads
        if request.url.path == "/wp-json/":
            return _response(
                request,
                {
                    "namespaces": ["wp/v2"],
                    "routes": {
                        "/wp/v2/posts": {"methods": ["GET", "POST"]},
                        "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
                        "/wp/v2/users": {"methods": ["GET"]},
                    },
                },
            )
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(
                request,
                {
                    "id": 3,
                    "name": "Fixture editor",
                    "capabilities": {"edit_posts": True, "publish_posts": True},
                },
            )
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            post_reads += 1
            if post_reads > 2:
                raise httpx.ReadTimeout("reconciliation read lost")
            return _response(request, post)
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            raise httpx.ReadTimeout("publish response lost")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    operation_key = "publish:fixture:timeout"
    async with WordPressClient(
        "https://fixture.test",
        {"token": "fixture-token"},
        transport=httpx.MockTransport(handler),
    ) as client:
        draft = await client.read("post:7")
        with pytest.raises(AmbiguousOutcome) as caught:
            await client.publish(
                "post:7",
                expected_hash=draft["source_hash"],
                operation_key=operation_key,
            )

    assert caught.value.operation_key == operation_key


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_applies", [True, False])
async def test_native_update_timeout_is_reconciled_without_retry(remote_applies: bool) -> None:
    post = {
        "id": 8,
        "slug": "fixture-update",
        "status": "draft",
        "type": "post",
        "link": "https://fixture.test/fixture-update",
        "title": {"raw": "Original title", "rendered": "Original title"},
        "content": {"raw": "<p>Original body</p>", "rendered": "<p>Original body</p>"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
    }
    update_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal update_requests
        if request.url.path == "/wp-json/":
            return _response(
                request,
                {
                    "namespaces": ["wp/v2"],
                    "routes": {
                        "/wp/v2/posts": {"methods": ["GET", "POST"]},
                        "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
                        "/wp/v2/users": {"methods": ["GET"]},
                    },
                },
            )
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(
                request,
                {
                    "id": 3,
                    "name": "Fixture editor",
                    "capabilities": {"edit_posts": True, "publish_posts": True},
                },
            )
        if request.url.path == "/wp-json/wp/v2/posts/8" and request.method == "GET":
            return _response(request, post)
        if request.url.path == "/wp-json/wp/v2/posts/8" and request.method == "POST":
            update_requests += 1
            if remote_applies:
                post["content"] = {
                    "raw": "<p>Updated body</p>",
                    "rendered": "<p>Updated body</p>",
                }
            raise httpx.ReadTimeout("update response lost")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://fixture.test",
        {"token": "fixture-token"},
        transport=httpx.MockTransport(handler),
    ) as client:
        current = await client.read("post:8")
        if remote_applies:
            result = await client.update(
                "post:8",
                {"body": "<p>Updated body</p>"},
                current["source_hash"],
                operation_key="update:fixture:timeout",
            )
            assert result["body"] == "<p>Updated body</p>"
            assert result["title"] == "Original title"
        else:
            with pytest.raises(AmbiguousOutcome) as caught:
                await client.update(
                    "post:8",
                    {"body": "<p>Updated body</p>"},
                    current["source_hash"],
                    operation_key="update:fixture:timeout",
                )
            assert caught.value.operation_key == "update:fixture:timeout"

    assert update_requests == 1
