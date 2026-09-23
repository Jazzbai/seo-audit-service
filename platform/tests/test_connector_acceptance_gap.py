from __future__ import annotations

import json

import httpx
import pytest

from app.connectors import ConnectorError, SourceConflict
from app.connectors.wordpress import WordPressClient


def _response(request: httpx.Request, value: object) -> httpx.Response:
    return httpx.Response(200, json=value, request=request)


def _post(*, body: str, status: str) -> dict[str, object]:
    return {
        "id": 7,
        "slug": "fixture-post",
        "status": status,
        "type": "post",
        "link": "https://fixture.test/fixture-post",
        "title": {"raw": "Fixture post", "rendered": "Fixture post"},
        "content": {"raw": body, "rendered": body},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
    }


@pytest.mark.asyncio
async def test_publish_timeout_reconciliation_rejects_external_source_change() -> None:
    state = {"post": _post(body="<p>Approved body</p>", status="draft")}
    publish_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal publish_calls
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
            return _response(request, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            publish_calls += 1
            assert json.loads(request.content) == {"status": "publish"}
            state["post"] = _post(body="<p>External replacement</p>", status="publish")
            raise httpx.ReadTimeout("publish response lost")
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://fixture.test",
        {"token": "fixture-token"},
        transport=httpx.MockTransport(handler),
    ) as client:
        draft = await client.read("post:7")
        with pytest.raises(SourceConflict):
            await client.publish(
                "post:7",
                expected_hash=draft["source_hash"],
                operation_key="publish:fixture",
            )

    assert publish_calls == 1
    assert state["post"]["status"] == "publish"
    assert state["post"]["content"]["raw"] == "<p>External replacement</p>"


@pytest.mark.asyncio
async def test_publish_rechecks_current_permission_before_mutation() -> None:
    state = {"post": _post(body="<p>Approved body</p>", status="draft")}
    permissions = {"edit_posts": True, "publish_posts": True}
    publish_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal publish_calls
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
                {"id": 3, "name": "Fixture editor", "capabilities": dict(permissions)},
            )
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            publish_calls += 1
            pytest.fail("publish must not be sent after the permission was revoked")
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture-editor", "application_password": "fixture-password"},
        transport=httpx.MockTransport(handler),
    ) as client:
        draft = await client.read("post:7")
        permissions["publish_posts"] = False
        with pytest.raises(ConnectorError, match="publication capability"):
            await client.publish(
                "post:7",
                expected_hash=draft["source_hash"],
                operation_key="publish:permission-revoked",
            )

    assert publish_calls == 0
    assert state["post"]["status"] == "draft"
