from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest

from app.connectors import AuthenticationError, ConnectorError
from app.connectors.wordpress import WordPressClient


def _response(
    request: httpx.Request,
    value: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, json=value, headers=headers, request=request)


def _user(
    user_id: int,
    name: str,
    *,
    edit_posts: bool = True,
    roles: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": user_id,
        "name": name,
        "roles": roles or [],
        "capabilities": {"edit_posts": edit_posts},
    }


def _me(
    *,
    user_id: int = 1,
    edit_posts: bool = True,
    publish_posts: bool = True,
    edit_others_posts: bool = True,
) -> dict[str, Any]:
    return {
        "id": user_id,
        "name": "Connected user",
        "capabilities": {
            "edit_posts": edit_posts,
            "publish_posts": publish_posts,
            "edit_others_posts": edit_others_posts,
        },
    }


def _client(handler) -> WordPressClient:
    return WordPressClient(
        "https://authors.fixture.test",
        {"username": "fixture", "application_password": "fixture-password"},
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_discovery_returns_the_only_assignable_user_for_single_user_site() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/me"):
            return _response(request, _me(edit_others_posts=False))
        if request.url.path.endswith("/users"):
            return _response(
                request,
                [
                    _user(1, "Connected user"),
                    _user(2, "Other editor"),
                    _user(3, "Subscriber", edit_posts=False),
                ],
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == [{"id": "1", "name": "Connected user"}]
    assert result["complete"] is True
    assert result["authenticated_user_id"] == "1"
    assert result["blockers"] == []
    assert result["warnings"] == ["connection_can_only_assign_self"]
    assert datetime.fromisoformat(result["checked_at"]).utcoffset().total_seconds() == 0
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.params["context"] == "edit" for request in requests)


@pytest.mark.asyncio
async def test_repeated_discovery_refetches_me_and_does_not_keep_deleted_authors() -> None:
    counts = {"me": 0, "users": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            counts["me"] += 1
            return _response(request, _me())
        if request.url.path.endswith("/users"):
            counts["users"] += 1
            rows = [_user(1, "Connected user")]
            if counts["users"] == 1:
                rows.append(_user(2, "Removed author"))
            return _response(request, rows)
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        first = await client.discover_authors()
        second = await client.discover_authors()

    assert [item["id"] for item in first["items"]] == ["1", "2"]
    assert second["items"] == [{"id": "1", "name": "Connected user"}]
    assert second["complete"] is True
    assert counts == {"me": 2, "users": 2}


@pytest.mark.asyncio
async def test_capabilities_include_custom_editors_and_exclude_subscribers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return _response(request, _me())
        if request.url.path.endswith("/users"):
            return _response(
                request,
                [
                    _user(1, "Connected user", roles=["administrator"]),
                    _user(7, "Custom role editor", roles=["seo_contributor"]),
                    _user(8, "Subscriber", edit_posts=False, roles=["subscriber"]),
                ],
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == [
        {"id": "1", "name": "Connected user"},
        {"id": "7", "name": "Custom role editor"},
    ]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_connection_without_edit_others_posts_can_only_assign_self() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return _response(request, _me(edit_others_posts=False))
        if request.url.path.endswith("/users"):
            return _response(
                request,
                [_user(1, "Connected user"), _user(2, "Another editor")],
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == [{"id": "1", "name": "Connected user"}]
    assert result["blockers"] == []
    assert result["warnings"] == ["connection_can_only_assign_self"]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_missing_capability_is_false_not_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            user = _me()
            del user["capabilities"]["edit_others_posts"]
            return _response(request, user)
        if request.url.path.endswith("/users"):
            return _response(request, [_user(1, "Connected user")])
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == [{"id": "1", "name": "Connected user"}]
    assert result["complete"] is True
    assert result["blockers"] == []
    assert result["warnings"] == ["connection_can_only_assign_self"]


@pytest.mark.asyncio
async def test_unauthorized_me_raises_safe_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {"message": "private upstream diagnostic"},
            status=401,
        )

    async with _client(handler) as client:
        with pytest.raises(AuthenticationError) as raised:
            await client.discover_authors()

    assert "private upstream diagnostic" not in str(raised.value)
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_forbidden_user_listing_returns_incomplete_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return _response(request, _me())
        if request.url.path.endswith("/users"):
            return _response(
                request,
                {"message": "private upstream diagnostic"},
                status=403,
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == []
    assert result["complete"] is False
    assert result["blockers"] == ["author_listing_denied"]
    assert "private upstream diagnostic" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "headers", "expected_blocker"),
    [
        (
            [{"id": 2, "name": "Missing capability map"}],
            {},
            "author_records_incomplete",
        ),
        (
            [{"id": "2", "name": "Malformed id", "capabilities": {"edit_posts": True}}],
            {},
            "author_records_incomplete",
        ),
        (
            [_user(2, "Duplicate one"), _user(2, "Duplicate two")],
            {"X-WP-TotalPages": "1", "X-WP-Total": "2"},
            "author_listing_incomplete",
        ),
        (
            [],
            {"X-WP-TotalPages": "not-a-number"},
            "author_listing_incomplete",
        ),
    ],
)
async def test_malformed_or_incomplete_listing_never_claims_complete(
    rows: list[dict[str, Any]],
    headers: dict[str, str],
    expected_blocker: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return _response(request, _me())
        if request.url.path.endswith("/users"):
            return _response(request, rows, headers=headers)
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == []
    assert result["complete"] is False
    assert result["blockers"] == [expected_blocker]


@pytest.mark.asyncio
async def test_discovery_fetches_every_reported_user_page() -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return _response(request, _me())
        if request.url.path.endswith("/users"):
            page = request.url.params["page"]
            requested_pages.append(page)
            rows = {
                "1": [_user(1, "Connected user"), _user(2, "Second editor")],
                "2": [_user(3, "Third editor")],
            }[page]
            return _response(
                request,
                rows,
                headers={"X-WP-TotalPages": "2", "X-WP-Total": "3"},
            )
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert requested_pages == ["1", "2"]
    assert result["items"] == [
        {"id": "1", "name": "Connected user"},
        {"id": "2", "name": "Second editor"},
        {"id": "3", "name": "Third editor"},
    ]
    assert result["complete"] is True
    assert result["blockers"] == []


@pytest.mark.asyncio
async def test_connection_without_publish_permissions_returns_no_authors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return _response(request, _me(publish_posts=False))
        if request.url.path.endswith("/users"):
            return _response(request, [_user(1, "Connected user"), _user(2, "Other editor")])
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with _client(handler) as client:
        result = await client.discover_authors()

    assert result["items"] == []
    assert result["complete"] is True
    assert result["blockers"] == ["connection_cannot_publish"]


@pytest.mark.asyncio
async def test_other_user_read_errors_are_safe_connector_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {"message": "private upstream diagnostic"},
            status=403,
        )

    async with _client(handler) as client:
        with pytest.raises(ConnectorError) as raised:
            await client.discover_authors()

    assert "private upstream diagnostic" not in str(raised.value)


@pytest.mark.asyncio
async def test_absent_capability_is_false_not_an_incomplete_user_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/users/me'):
            me = _me()
            me['capabilities'].pop('edit_others_posts')
            return _response(request, me)
        return _response(request, [
            _user(1, 'Connected user'),
            {'id': 2, 'name': 'Subscriber', 'capabilities': {'read': True}},
        ])

    async with _client(handler) as client:
        result = await client.discover_authors()
    assert result['complete'] is True
    assert result['blockers'] == []
    assert result['items'] == [{'id': '1', 'name': 'Connected user'}]
    assert result['warnings'] == ['connection_can_only_assign_self']
