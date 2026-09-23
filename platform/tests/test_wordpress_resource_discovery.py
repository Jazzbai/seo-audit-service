from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.connectors.errors import ConnectorError
from app.connectors.wordpress import WordPressClient


def _response(request: httpx.Request, value: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=value, request=request)


def _route_index(*, include_types_route: bool = True, route_viewable: bool = False) -> dict[str, Any]:
    routes: dict[str, Any] = {
        "/wp/v2/posts": {"methods": ["GET", "POST"]},
        "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/pages": {"methods": ["GET"]},
        "/wp/v2/users": {"methods": ["GET"]},
        "/wp/v2/users/(?P<id>[\\d]+)": {"methods": ["GET"]},
        "/wp/v2/media": {"methods": ["GET"]},
        "/wp/v2/media/(?P<id>[\\d]+)": {"methods": ["GET"]},
        "/wp/v2/categories": {"methods": ["GET"]},
        "/wp/v2/categories/(?P<id>[\\d]+)": {"methods": ["GET"]},
        "/wp/v2/menus": {"methods": ["GET"]},
        "/wp/v2/menus/(?P<id>[\\d]+)": {"methods": ["GET"]},
        "/wp/v2/portfolio": {
            "methods": ["GET", "POST"],
            **({"viewable": True} if route_viewable else {}),
        },
        "/wp/v2/portfolio/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/portfolio/(?P<id>[\\d]+)/revisions": {"methods": ["GET"]},
        "/wp/v2/elementor_library": {"methods": ["GET", "POST"]},
        "/wp/v2/elementor_library/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/templates": {"methods": ["GET", "POST"]},
        "/wp/v2/templates/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/font-families": {"methods": ["GET", "POST"]},
        "/wp/v2/font-families/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
    }
    if include_types_route:
        routes["/wp/v2/types"] = {"methods": ["GET"]}
    return {"name": "Resource fixture", "namespaces": ["wp/v2"], "routes": routes}


def _types_payload(*, portfolio_viewable: bool = True) -> dict[str, Any]:
    return {
        "post": {
            "name": "post",
            "slug": "post",
            "rest_base": "posts",
            "viewable": True,
            "labels": {"name": "Posts"},
        },
        "page": {
            "name": "page",
            "slug": "page",
            "rest_base": "pages",
            "viewable": True,
            "labels": {"name": "Pages"},
        },
        "portfolio": {
            "name": "portfolio",
            "slug": "portfolio",
            "rest_base": "portfolio",
            "viewable": portfolio_viewable,
            "labels": {"name": "Portfolio"},
        },
        "elementor_library": {
            "name": "elementor_library",
            "slug": "elementor_library",
            "rest_base": "elementor_library",
            "viewable": True,
            "labels": {"name": "Elementor Library"},
        },
        "wp_template": {
            "name": "wp_template",
            "slug": "wp_template",
            "rest_base": "templates",
            "viewable": True,
            "labels": {"name": "Templates"},
        },
        "wp_font_family": {
            "name": "wp_font_family",
            "slug": "wp_font_family",
            "rest_base": "font-families",
            "viewable": True,
            "labels": {"name": "Font Families"},
        },
        "attachment": {
            "name": "attachment",
            "slug": "attachment",
            "rest_base": "media",
            "viewable": True,
            "labels": {"name": "Media"},
        },
    }


def _portfolio_record() -> dict[str, Any]:
    return {
        "id": 17,
        "slug": "sample-project",
        "status": "publish",
        "link": "https://fixture.test/sample-project",
        "title": {"raw": "Sample project", "rendered": "Sample project"},
        "content": {"raw": "Project details", "rendered": "Project details"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 1,
        "featured_media": 0,
    }


def _transport(
    index: dict[str, Any],
    *,
    types_payload: object = None,
    types_status: int = 200,
    include_inventory: bool = False,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/wp-json/":
            return _response(request, index)
        if path == "/wp-json/wp/v2/types":
            return _response(request, types_payload, types_status)
        if include_inventory and request.method == "GET" and path.startswith("/wp-json/wp/v2/"):
            if path.endswith("/portfolio"):
                return _response(request, [_portfolio_record()])
            return _response(request, [])
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    return httpx.MockTransport(handler), requests


@pytest.mark.asyncio
async def test_discovery_reports_viewable_custom_type_and_safe_route_capabilities() -> None:
    transport, requests = _transport(
        _route_index(),
        types_payload=_types_payload(),
    )

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        capabilities = await client.discover()
        cached = await client.discover()

    resource_types = {item["key"]: item for item in capabilities["resource_types"]}
    assert set(resource_types) == {"page", "post", "portfolio"}
    assert capabilities["resource_types"] == cached["resource_types"]

    portfolio = resource_types["portfolio"]
    assert portfolio["name"] == "portfolio"
    assert portfolio["label"] == "Portfolio"
    assert portfolio["rest_base"] == "portfolio"
    assert portfolio["viewable"] is True
    assert portfolio["inventoryable"] is True
    assert portfolio["collection"] == {
        "route": "/wp/v2/portfolio",
        "read": True,
        "write": True,
        "read_methods": ["GET"],
        "write_methods": ["POST"],
    }
    assert portfolio["item"] == {
        "route": "/wp/v2/portfolio/(?P<id>[\\d]+)",
        "read": True,
        "write": True,
        "read_methods": ["GET"],
        "write_methods": ["POST"],
    }
    assert portfolio["editorial_write"] == {
        "supported": False,
        "automatic": False,
        "policy": "inventory_only",
        "reason": "Custom post types require an explicit connector write contract",
    }
    assert sum(request.url.path.endswith("/types") for request in requests) == 1


@pytest.mark.asyncio
async def test_inventory_includes_viewable_custom_type_but_never_uses_mutation_methods() -> None:
    transport, requests = _transport(
        _route_index(),
        types_payload=_types_payload(),
        include_inventory=True,
    )

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        records = await client.inventory()

    assert [record["resource_key"] for record in records if record["resource_type"] == "portfolio"] == [
        "portfolio:17"
    ]
    assert {request.method for request in requests} == {"GET"}
    assert sum(request.url.path.endswith("/types") for request in requests) == 1


@pytest.mark.asyncio
async def test_discovery_excludes_internal_media_users_and_nested_routes() -> None:
    transport, _ = _transport(
        _route_index(),
        types_payload=_types_payload(),
    )

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        capabilities = await client.discover()

    keys = {item["key"] for item in capabilities["resource_types"]}
    assert "elementor_library" not in keys
    assert "e-floating-buttons" not in keys
    assert "wp_template" not in keys
    assert "wp_font_family" not in keys
    assert "attachment" not in keys
    assert "media" not in keys
    assert "users" not in keys
    assert "categories" not in keys
    assert "menus" not in keys
    assert "portfolio/(?P<id>" not in keys


@pytest.mark.asyncio
async def test_authenticated_types_probe_prefers_edit_context_for_viewable_flags() -> None:
    transport, requests = _transport(
        _route_index(),
        types_payload=_types_payload(),
    )

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        capabilities = await client.discover()

    type_requests = [request for request in requests if request.url.path.endswith("/types")]
    assert len(type_requests) == 1
    assert type_requests[0].url.params["context"] == "edit"
    assert next(item for item in capabilities["resource_types"] if item["key"] == "portfolio")["viewable"] is True


@pytest.mark.asyncio
async def test_nested_type_rest_base_is_not_reported_as_a_resource() -> None:
    index = _route_index()
    payload = _types_payload()
    payload["font_face"] = {
        "name": "font_face",
        "slug": "font_face",
        "rest_base": "font-families/(?P<font_family_id>[\\d]+)/font-faces",
        "viewable": True,
        "labels": {"name": "Font Faces"},
    }
    transport, _ = _transport(index, types_payload=payload)

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        capabilities = await client.discover()

    assert "font_face" not in {item["key"] for item in capabilities["resource_types"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("types_mode", ["absent", "malformed", "not_found"])
async def test_route_fallback_preserves_standard_types_and_accepts_explicit_viewable_marker(
    types_mode: str,
) -> None:
    include_types_route = types_mode != "absent"
    transport, requests = _transport(
        _route_index(include_types_route=include_types_route, route_viewable=True),
        types_payload=[] if types_mode == "malformed" else None,
        types_status=404 if types_mode == "not_found" else 200,
    )

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        capabilities = await client.discover()

    resource_types = {item["key"]: item for item in capabilities["resource_types"]}
    assert {"post", "page", "portfolio"} <= set(resource_types)
    assert resource_types["portfolio"]["source"] == "routes"
    assert resource_types["portfolio"]["inventoryable"] is True
    if types_mode == "absent":
        assert not any(request.url.path.endswith("/types") for request in requests)
    else:
        type_requests = [request for request in requests if request.url.path.endswith("/types")]
        assert len(type_requests) == 2
        assert {request.url.params["context"] for request in type_requests} == {"edit", "view"}


@pytest.mark.asyncio
async def test_non_viewable_custom_type_is_not_inventoryable() -> None:
    transport, requests = _transport(
        _route_index(),
        types_payload=_types_payload(portfolio_viewable=False),
        include_inventory=True,
    )

    async with WordPressClient(
        "https://fixture.test",
        {"username": "fixture", "application_password": "not-a-live-secret"},
        transport=transport,
    ) as client:
        capabilities = await client.discover()
        records = await client.inventory()

    portfolio = next(item for item in capabilities["resource_types"] if item["key"] == "portfolio")
    assert portfolio["viewable"] is False
    assert portfolio["inventoryable"] is False
    assert not any(record["resource_type"] == "portfolio" for record in records)
    assert not any(request.url.path.endswith("/portfolio") for request in requests if request.method == "GET")


@pytest.mark.asyncio
async def test_configured_custom_type_uses_the_same_safe_route_validation() -> None:
    transport, _ = _transport(
        _route_index(include_types_route=False, route_viewable=False),
        types_payload=None,
    )

    async with WordPressClient(
        "https://fixture.test",
        {
            "username": "fixture",
            "application_password": "not-a-live-secret",
            "post_types": ["portfolio", "portfolio/(?P<id>[\\d]+)"],
        },
        transport=transport,
    ) as client:
        capabilities = await client.discover()

    resource_types = {item["key"]: item for item in capabilities["resource_types"]}
    assert resource_types["portfolio"]["source"] == "configured"
    assert resource_types["portfolio"]["inventoryable"] is True
    assert all("/" not in key for key in resource_types)


@pytest.mark.asyncio
async def test_collection_rejects_total_pages_above_the_page_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[{"id": page}],
            headers={"X-WP-TotalPages": "3"},
            request=request,
        )

    async with WordPressClient(
        "https://fixture.test",
        {"token": "secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ConnectorError, match="exceeds the pagination limit"):
            await client._fetch_collection("posts", max_pages=2)

    assert [request.url.params["page"] for request in requests] == ["1"]
    assert {request.method for request in requests} == {"GET"}


@pytest.mark.asyncio
async def test_collection_stops_after_the_reported_last_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[{"id": page}],
            headers={"X-WP-TotalPages": "2"},
            request=request,
        )

    async with WordPressClient(
        "https://fixture.test",
        {"token": "secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        items = await client._fetch_collection("posts", max_pages=3)

    assert [item["id"] for item in items] == [1, 2]
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert {request.method for request in requests} == {"GET"}


@pytest.mark.asyncio
async def test_collection_accepts_a_short_page_when_total_pages_is_absent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"id": 1}], request=request)

    async with WordPressClient(
        "https://fixture.test",
        {"token": "secret"},
        transport=httpx.MockTransport(handler),
    ) as client:
        items = await client._fetch_collection("posts", max_pages=2)

    assert items == [{"id": 1}]
    assert [request.url.params["page"] for request in requests] == ["1"]
    assert {request.method for request in requests} == {"GET"}
