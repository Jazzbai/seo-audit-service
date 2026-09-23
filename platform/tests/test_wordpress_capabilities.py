from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.connectors import ConnectorError
from app.connectors.wordpress import WordPressClient


def _response(request: httpx.Request, value: object) -> httpx.Response:
    return httpx.Response(200, json=value, request=request)


def _native_routes(*, builder: bool = False) -> dict[str, Any]:
    post_create_args: dict[str, Any] = {
        "title": {"type": "object"},
        "content": {"type": "object"},
    }
    post_update_args: dict[str, Any] = {
        "title": {"type": "object"},
        "content": {"type": "object"},
        "status": {"type": "string"},
    }
    if builder:
        builder_meta = {
            "type": "object",
            "properties": {
                "_elementor_data": {"type": "string"},
            },
        }
        post_create_args["meta"] = builder_meta
        post_update_args["meta"] = builder_meta

    return {
        "/wp/v2/posts": {
            "methods": ["GET", "POST"],
            "endpoints": [
                {"methods": ["GET"]},
                {"methods": ["POST"], "args": post_create_args},
            ],
        },
        "/wp/v2/posts/(?P<id>[\\d]+)": {
            "methods": ["GET", "POST"],
            "endpoints": [{"methods": ["POST"], "args": post_update_args}],
        },
        "/wp/v2/pages": {"methods": ["GET", "POST"]},
        "/wp/v2/media": {"methods": ["GET"]},
        "/wp/v2/users": {"methods": ["GET"]},
        "/wp/v2/statuses": {"methods": ["GET"]},
    }


def _index(
    *,
    builder: bool = False,
    namespaces: list[str] | None = None,
    forgeseo: bool = False,
) -> dict[str, Any]:
    route_map = _native_routes(builder=builder)
    namespace_list = list(namespaces or ["wp/v2"])
    if forgeseo:
        if "forgeseo/v1" not in namespace_list:
            namespace_list.append("forgeseo/v1")
        # The GET endpoint advertises a field that the mutating endpoint does
        # not accept. Discovery must not turn that read-only argument into a
        # writable field.
        route_map["/forgeseo/v1/posts/(?P<id>[\\d]+)/seo"] = {
            "methods": ["GET", "POST"],
            "endpoints": [
                {
                    "methods": ["GET"],
                    "args": {"focus_keyword": {"type": "string"}},
                },
                {
                    "methods": ["POST"],
                    "args": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            ],
        }
        route_map["/forgeseo/v1/capabilities"] = {"methods": ["GET"]}
        route_map["/forgeseo/v1/operations/(?P<operation_key>[A-Za-z0-9._:-]+)"] = {
            "methods": ["GET"]
        }
    return {"name": "Capability fixture", "namespaces": namespace_list, "routes": route_map}


async def _discover(index: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, index)
        if request.url.path == "/wp-json/forgeseo/v1/capabilities":
            return _response(
                request,
                extra
                or {
                    "seo_fields": ["title", "description"],
                    "seo_write_supported": True,
                    "webhooks": True,
                    "seo_provider": "yoast",
                },
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://capabilities.fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        return await client.discover()


@pytest.mark.asyncio
async def test_discovery_reports_native_editorial_capabilities() -> None:
    capabilities = await _discover(_index())

    assert capabilities["native"] == {
        "read": True,
        "create": True,
        "update": True,
        "publish": True,
        "pages": True,
        "media": True,
        "authors": True,
        "statuses": True,
    }
    assert capabilities["editorial"]["read"] is True
    assert capabilities["editorial"]["write"] is True
    assert capabilities["editorial"]["conditionally_writable_fields"] == []
    assert capabilities["editorial"]["builder_constraints"]["detected"] is False


@pytest.mark.asyncio
async def test_discovery_exposes_builder_constraints_from_rest_metadata() -> None:
    capabilities = await _discover(_index(builder=True))

    constraints = capabilities["editorial"]["builder_constraints"]
    assert constraints["detected"] is True
    assert constraints["providers"] == ["elementor"]
    assert constraints["metadata_fields"] == ["_elementor_data"]
    assert constraints["detection_source"] == "wordpress_rest_route_metadata"
    assert constraints["metadata_write"] is False
    assert constraints["body_write"]["mode"] == "protected_when_builder_metadata_present"
    assert constraints["body_write"]["allowed_for_unmanaged_target"] is True
    assert constraints["body_write"]["blocked_for_builder_target"] is True
    assert constraints["body_write"]["requires_target_check"] is True
    assert capabilities["editorial"]["conditionally_writable_fields"] == ["body"]
    assert capabilities["editorial"]["protected_fields"] == [
        "metadata",
        "body",
        "content",
    ]


@pytest.mark.asyncio
async def test_ordinary_commerce_like_metadata_does_not_look_like_divi() -> None:
    index = _index(builder=False)
    index["routes"]["/wp/v2/posts"]["endpoints"][1]["args"]["meta"] = {
        "type": "object",
        "properties": {"sold_individually": {"type": "boolean"}},
    }
    capabilities = await _discover(index)
    assert capabilities["editorial"]["builder_constraints"]["detected"] is False


@pytest.mark.asyncio
async def test_yoast_and_rank_math_are_read_only_while_forgeseo_is_narrow_write_surface() -> None:
    capabilities = await _discover(
        _index(
            namespaces=["wp/v2", "yoast/v1", "rankmath/v1"],
            forgeseo=True,
        )
    )

    assert capabilities["plugins"]["yoast"] == {
        "detected": True,
        "read": True,
        "write": False,
        "writable_fields": [],
    }
    assert capabilities["plugins"]["rank_math"] == {
        "detected": True,
        "read": True,
        "write": False,
        "writable_fields": [],
    }
    assert capabilities["plugins"]["forgeseo"]["detected"] is True
    assert capabilities["plugins"]["forgeseo"]["read"] is True
    assert capabilities["plugins"]["forgeseo"]["write"] is True
    assert capabilities["plugins"]["forgeseo"]["writable_fields"] == [
        "description",
        "title",
    ]
    assert capabilities["seo"]["provider"] == "forgeseo"
    assert capabilities["seo"]["active_provider"] == "yoast"
    assert capabilities["seo"]["write"] is True
    assert capabilities["seo"]["writable_fields"] == ["description", "title"]


@pytest.mark.asyncio
async def test_no_forgeseo_route_never_promotes_seo_plugins_to_writes() -> None:
    index = _index(namespaces=["wp/v2", "yoast/v1", "rankmath/v1"])
    # Even if a third-party plugin advertises a mutating route, it is not an
    # approved write surface for this connector.
    index["routes"]["/yoast/v1/posts/(?P<id>[\\d]+)/seo"] = {
        "methods": ["GET", "POST"],
        "endpoints": [
            {"methods": ["POST"], "args": {"title": {"type": "string"}}}
        ],
    }
    index["routes"]["/rankmath/v1/posts/(?P<id>[\\d]+)/seo"] = {
        "methods": ["GET", "POST"],
        "endpoints": [
            {"methods": ["POST"], "args": {"description": {"type": "string"}}}
        ],
    }
    capabilities = await _discover(index)

    assert capabilities["seo"]["write"] is False
    assert capabilities["seo"]["writable_fields"] == []
    assert capabilities["seo"]["provider"] is None
    for provider in ("yoast", "rank_math"):
        assert capabilities["plugins"][provider]["read"] is True
        assert capabilities["plugins"][provider]["write"] is False
        assert capabilities["plugins"][provider]["writable_fields"] == []


@pytest.mark.asyncio
async def test_forgeseo_write_requires_a_resource_scoped_seo_route() -> None:
    index = _index()
    index["namespaces"].append("forgeseo/v1")
    index["routes"]["/forgeseo/v1/capabilities"] = {"methods": ["GET"]}
    index["routes"]["/forgeseo/v1/settings/seo"] = {
        "methods": ["POST"],
        "endpoints": [
            {
                "methods": ["POST"],
                "args": {"title": {"type": "string"}},
            }
        ],
    }

    capabilities = await _discover(index)

    assert capabilities["plugins"]["forgeseo"]["detected"] is True
    assert capabilities["plugins"]["forgeseo"]["read"] is True
    assert capabilities["plugins"]["forgeseo"]["write"] is False
    assert capabilities["plugins"]["forgeseo"]["writable_fields"] == []
    assert capabilities["seo"]["write"] is False
    assert capabilities["seo"]["writable_fields"] == []


@pytest.mark.asyncio
async def test_authenticated_permissions_reduce_editorial_write_capability() -> None:
    index = _index()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, index)
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(
                request,
                {
                    "id": 7,
                    "name": "Read-only editor",
                    "capabilities": {"edit_posts": False, "publish_posts": False},
                },
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://permissions.fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.validate_connection()

    assert capabilities["native"]["create"] is False
    assert capabilities["native"]["update"] is False
    assert capabilities["native"]["publish"] is False
    assert capabilities["editorial"]["write"] is False
    assert capabilities["editorial"]["writable_fields"] == []
    assert capabilities["editorial"]["conditionally_writable_fields"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "permissions", "should_write"),
    [
        (
            "update",
            {"edit_posts": True, "edit_pages": False, "publish_posts": True, "publish_pages": False},
            False,
        ),
        (
            "publish",
            {"edit_posts": True, "edit_pages": True, "publish_posts": True, "publish_pages": False},
            False,
        ),
        (
            "update",
            {"edit_posts": False, "edit_pages": True, "publish_posts": False, "publish_pages": True},
            True,
        ),
        (
            "publish",
            {"edit_posts": False, "edit_pages": True, "publish_posts": False, "publish_pages": True},
            True,
        ),
    ],
)
async def test_page_mutations_use_page_scoped_authenticated_permissions(
    operation: str,
    permissions: dict[str, bool],
    should_write: bool,
) -> None:
    index = _index()
    index["routes"]["/wp/v2/pages/(?P<id>[\\d]+)"] = {"methods": ["GET", "POST"]}
    page = {
        "id": 9,
        "type": "page",
        "slug": "fixture-page",
        "status": "draft",
        "title": {"raw": "Fixture page", "rendered": "Fixture page"},
        "content": {"raw": "Page body", "rendered": "Page body"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
    }
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.url.path == "/wp-json/":
            return _response(request, index)
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(
                request,
                {"id": 7, "name": "Fixture page editor", "capabilities": permissions},
            )
        if request.url.path == "/wp-json/wp/v2/pages/9" and request.method == "GET":
            return _response(request, page)
        if request.url.path == "/wp-json/wp/v2/pages/9" and request.method == "POST":
            if not should_write:
                pytest.fail("page permission test attempted a forbidden write")
            writes += 1
            body = json.loads(request.content)
            if operation == "update":
                page["title"] = {"raw": body["title"], "rendered": body["title"]}
            else:
                page["status"] = body["status"]
            return _response(request, page)
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://page-permissions.fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.validate_connection()
        assert capabilities["authenticated_permissions"]["pages"]["update"] == (
            permissions["edit_pages"]
        )
        assert capabilities["authenticated_permissions"]["pages"]["publish"] == (
            permissions["publish_pages"]
        )
        record = await client.read("page:9")
        if operation == "update":
            action = client.update(
                "page:9",
                {"title": "Updated page"},
                record["source_hash"],
            )
        else:
            action = client.publish("page:9", expected_hash=record["source_hash"])

        if should_write:
            result = await action
            assert writes == 1
            if operation == "update":
                assert result["title"] == "Updated page"
            else:
                assert result["status"] == "publish"
        else:
            with pytest.raises(ConnectorError):
                await action
            assert writes == 0


@pytest.mark.asyncio
async def test_writes_require_a_documented_item_route_for_the_selected_target() -> None:
    page = {
        "id": 9,
        "type": "page",
        "slug": "fixture-page",
        "status": "draft",
        "title": {"raw": "Fixture page", "rendered": "Fixture page"},
        "content": {"raw": "Page body", "rendered": "Page body"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
    }
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, _index())
        if request.url.path == "/wp-json/wp/v2/pages/9" and request.method == "GET":
            return _response(request, page)
        if request.url.path == "/wp-json/wp/v2/pages/9" and request.method == "POST":
            writes.append(request.method)
            pytest.fail("undocumented page item route was used for a write")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://target-capability.fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await client.read("page:9")
        with pytest.raises(ConnectorError, match="item write capability"):
            await client.update("page:9", {"title": "must not write"}, record["source_hash"])
        with pytest.raises(ConnectorError, match="item write capability"):
            await client.publish("page:9")
        with pytest.raises(ConnectorError, match="item write capability"):
            await client.restore("page:9", record, record["source_hash"])

    assert writes == []


@pytest.mark.asyncio
async def test_slug_item_route_does_not_authorize_numeric_wordpress_writes() -> None:
    index = _index()
    index["routes"].pop("/wp/v2/posts/(?P<id>[\\d]+)")
    index["routes"]["/wp/v2/posts/(?P<slug>[^/]+)"] = {
        "methods": ["GET", "POST"],
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/wp-json/":
            return _response(request, index)
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://slug-route.fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["native"]["update"] is False
        assert capabilities["native"]["publish"] is False
        with pytest.raises(ConnectorError, match="content write capability"):
            await client.update("post:7", {"title": "must remain local"}, "source-hash")

    assert [request.method for request in requests] == ["GET"]
