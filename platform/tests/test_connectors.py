from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

from app.connectors import (
    AmbiguousOutcome,
    ConnectorError,
    ProtectedField,
    SSRFError,
    SourceConflict,
    UnsupportedField,
    WooCommerceClient,
    WordPressClient,
    decrypt_credentials,
    encrypt_credentials,
    safe_get,
)
from app.network import PublicTransport, fetch


def _response(request: httpx.Request, status: int, value=None, *, headers=None) -> httpx.Response:
    if value is None:
        return httpx.Response(status, request=request, headers=headers)
    return httpx.Response(status, json=value, request=request, headers=headers)


def _wp_routes(*, forgeseo: bool = False) -> dict:
    routes = {
        "/wp/v2/posts": {"methods": ["GET", "POST"]},
        "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/pages": {"methods": ["GET", "POST"]},
        "/wp/v2/pages/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/media": {"methods": ["GET", "POST"]},
        "/wp/v2/users": {"methods": ["GET"]},
        "/wp/v2/statuses": {"methods": ["GET"]},
    }
    if forgeseo:
        routes["/forgeseo/v1/posts/(?P<id>[\\d]+)/seo"] = {
            "methods": ["GET", "POST"],
            "endpoints": [
                {"methods": ["GET"]},
                {
                    "methods": ["POST"],
                    "args": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "focus_keyword": {"type": "string"},
                        "canonical_url": {"type": "string"},
                    },
                },
            ],
        }
        routes["/forgeseo/v1/operations/(?P<operation_key>[A-Za-z0-9._:-]+)"] = {
            "methods": ["GET"]
        }
    return routes


def _wp_index(*, forgeseo: bool = False, seo_plugins: bool = False) -> dict:
    namespaces = ["wp/v2"]
    if seo_plugins:
        namespaces.extend(["yoast/v1", "rankmath/v1", "unsupported-seo/v1"])
    if forgeseo:
        namespaces.append("forgeseo/v1")
    return {"name": "Mock WordPress", "namespaces": namespaces, "routes": _wp_routes(forgeseo=forgeseo)}


def _wp_post(*, body: str = "<p>Body</p>", title: str = "Hello", modified: str = "2026-09-14T00:00:00") -> dict:
    return {
        "id": 7,
        "date": "2026-01-01T00:00:00",
        "modified": modified,
        "slug": "hello",
        "status": "draft",
        "type": "post",
        "link": "https://example.test/hello",
        "title": {"raw": title, "rendered": f"<h1>{title}</h1>"},
        "content": {"raw": body, "rendered": body},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 11,
        "categories": [2],
        "tags": [4],
        "meta": {"_yoast_wpseo_title": "Read-only SEO title"},
        "_embedded": {
            "author": [{"id": 3, "name": "Editor", "link": "https://example.test/author/editor"}],
            "wp:featuredmedia": [{"id": 11, "source_url": "https://example.test/image.jpg", "alt_text": ""}],
        },
    }


def _wp_user() -> dict:
    return {
        "id": 3,
        "name": "Fixture editor",
        "capabilities": {"edit_posts": True, "publish_posts": True},
    }


@pytest.mark.asyncio
async def test_wordpress_incremental_inventory_filters_users_and_passes_modified_after() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/posts":
            return _response(request, 200, [_wp_post()])
        if request.url.path == "/wp-json/wp/v2/pages":
            return _response(request, 200, [])
        pytest.fail(f"unexpected incremental inventory request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        records = await client.inventory(modified_after="2026-09-16T15:50:00Z")

    collection_requests = [request for request in requests if request.url.path.endswith("/posts") or request.url.path.endswith("/pages")]
    assert [request.url.path for request in collection_requests] == [
        "/wp-json/wp/v2/posts",
        "/wp-json/wp/v2/pages",
    ]
    assert all(request.url.params["modified_after"] == "2026-09-16T15:50:00Z" for request in collection_requests)
    assert len(records) == 1
    assert records[0]["resource_key"] == "post:7"


@pytest.mark.asyncio
async def test_encrypt_decrypt_positional_key_and_public_url_guard() -> None:
    key = b"k" * 32
    payload = {"username": "editor", "application_password": "not logged"}
    ciphertext = encrypt_credentials(payload, key)
    assert decrypt_credentials(ciphertext, key) == payload
    with pytest.raises(ValueError):
        encrypt_credentials(payload, b"short")
    with pytest.raises(SSRFError):
        WordPressClient("http://127.0.0.1", {}, httpx.MockTransport(lambda request: _response(request, 200, {})))


@pytest.mark.asyncio
async def test_wordpress_discovery_detects_seo_plugins_but_fails_closed_for_writes() -> None:
    post = _wp_post()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index(seo_plugins=True))
        if request.url.path == "/wp-json/wp/v2/posts/7":
            return _response(request, 200, post)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with WordPressClient("https://example.test", {"token": "secret"}, transport) as client:
        capabilities = await client.discover()
        assert capabilities["yoast"] is True
        assert capabilities["rank_math"] is True
        assert capabilities["seo"]["write"] is False
        record = await client.read("post:7")
        with pytest.raises(UnsupportedField):
            await client.update("post:7", {"seo": {"title": "must not write"}}, record["source_hash"])


@pytest.mark.asyncio
async def test_wordpress_hash_ignores_volatile_hints_and_conflict_blocks_write() -> None:
    state = {"post": _wp_post()}
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            changes = json.loads(request.content)
            writes.append(changes)
            state["post"].update(
                {
                    "title": {"raw": changes["title"], "rendered": changes["title"]},
                    "modified": "2026-09-14T02:00:00",
                }
            )
            return _response(request, 200, state["post"])
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient("https://example.test", {"token": "secret"}, httpx.MockTransport(handler)) as client:
        first = await client.read("post:7")
        state["post"]["modified"] = "2026-09-14T03:00:00"
        state["post"]["link"] = "https://example.test/changed-render-hint"
        second = await client.read("post:7")
        assert first["source_hash"] == second["source_hash"]
        with pytest.raises(SourceConflict):
            await client.update("post:7", {"title": "blocked"}, "0" * 64)
        updated = await client.update("post:7", {"title": "Updated"}, second["source_hash"])
        assert updated["title"] == "Updated"
        assert writes == [{"title": "Updated"}]


@pytest.mark.asyncio
async def test_wordpress_native_update_requires_read_after_write_verification() -> None:
    state = {"post": _wp_post()}
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            changes = json.loads(request.content)
            writes.append(changes)
            # Simulate an endpoint that returns the requested value but did
            # not actually persist it.  The authenticated read is the source
            # of truth, not the mutation response body.
            response = deepcopy(state["post"])
            response["title"] = {"raw": changes["title"], "rendered": changes["title"]}
            return _response(request, 200, response)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        with pytest.raises(AmbiguousOutcome, match="read-after-write verification"):
            await client.update(
                "post:7",
                {"title": "Not actually persisted"},
                original["source_hash"],
                operation_key="update:verify",
            )

    assert writes == [{"title": "Not actually persisted"}]
    assert state["post"]["title"]["raw"] == "Hello"


@pytest.mark.asyncio
async def test_wordpress_native_update_rejects_unrelated_post_write_source_change() -> None:
    state = {"post": _wp_post()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            changes = json.loads(request.content)
            state["post"]["title"] = {"raw": changes["title"], "rendered": changes["title"]}
            # A separate editor changed body content during the mutation.
            state["post"]["content"] = {
                "raw": "<p>External edit during the write.</p>",
                "rendered": "<p>External edit during the write.</p>",
            }
            return _response(request, 200, state["post"])
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        with pytest.raises(AmbiguousOutcome, match="outside the requested native fields"):
            await client.update(
                "post:7",
                {"title": "Requested title"},
                original["source_hash"],
            )

    assert state["post"]["title"]["raw"] == "Requested title"
    assert state["post"]["content"]["raw"] == "<p>External edit during the write.</p>"


@pytest.mark.asyncio
async def test_wordpress_update_rechecks_source_before_mutation() -> None:
    state = {"post": _wp_post()}
    post_reads = 0
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_reads
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            post_reads += 1
            if post_reads == 2:
                state["post"]["content"] = {
                    "raw": "<p>External edit before the mutation.</p>",
                    "rendered": "<p>External edit before the mutation.</p>",
                }
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            writes.append(json.loads(request.content))
            pytest.fail("WordPress mutation was sent after the source changed")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        with pytest.raises(SourceConflict):
            await client.update("post:7", {"title": "must not overwrite"}, original["source_hash"])

    assert writes == []
    assert state["post"]["content"]["raw"] == "<p>External edit before the mutation.</p>"


@pytest.mark.asyncio
async def test_wordpress_publish_requires_read_after_write_verification() -> None:
    state = {"post": _wp_post()}
    publish_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal publish_calls
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            publish_calls += 1
            response = deepcopy(state["post"])
            response["status"] = "publish"
            # The response claims success, while the subsequent read still
            # observes a draft.  Publishing must remain unreconciled.
            return _response(request, 200, response)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        with pytest.raises(AmbiguousOutcome, match="did not confirm publish"):
            await client.publish(
                "post:7",
                expected_hash=original["source_hash"],
                operation_key="publish:verify",
            )

    assert publish_calls == 1
    assert state["post"]["status"] == "draft"


@pytest.mark.asyncio
async def test_wordpress_mutations_propagate_operation_key_headers() -> None:
    state = {"post": _wp_post()}
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            seen.append(request.headers.get("X-ForgeSEO-Operation-Key"))
            changes = json.loads(request.content)
            if "title" in changes:
                state["post"]["title"] = {"raw": changes["title"], "rendered": changes["title"]}
            if "content" in changes:
                state["post"]["content"] = {"raw": changes["content"], "rendered": changes["content"]}
            if "status" in changes:
                state["post"]["status"] = changes["status"]
            state["post"]["modified"] = "2026-09-14T04:00:00"
            return _response(request, 200, state["post"])
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        changed = await client.update(
            "post:7",
            {"title": "Platform title"},
            original["source_hash"],
            operation_key="refresh:site:article",
        )
        published = await client.publish(
            "7",
            expected_hash=changed["source_hash"],
            operation_key="publish:site:article",
        )
        await client.restore(
            "post:7",
            original,
            published["source_hash"],
            operation_key="rollback:site:article",
        )

    assert seen == [
        "refresh:site:article",
        "publish:site:article",
        "rollback:site:article",
    ]


@pytest.mark.asyncio
async def test_wordpress_mixed_native_and_seo_failure_compensates_native_write() -> None:
    post = {**_wp_post(), "forgeseo_seo": {"title": "", "description": ""}}
    state = {"post": post}
    native_writes: list[dict] = []
    seo_writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seo_writes
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index(forgeseo=True))
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            changes = json.loads(request.content)
            native_writes.append(changes)
            if "title" in changes:
                state["post"]["title"] = {"raw": changes["title"], "rendered": changes["title"]}
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/forgeseo/v1/posts/7/seo" and request.method == "POST":
            seo_writes += 1
            return _response(request, 500, {"code": "fixture_seo_failure"})
        pytest.fail(f"unexpected mixed-write request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        with pytest.raises(ConnectorError):
            await client.update(
                "post:7",
                {"title": "New title", "seo": {"title": "New SEO title"}},
                original["source_hash"],
            )
        observed = await client.read("post:7")

    assert native_writes == [{"title": "New title"}, {"title": "Hello"}]
    assert seo_writes == 1
    assert observed["title"] == original["title"]
    assert observed["metadata"]["seo"]["forgeseo"] == {"title": "", "description": ""}


@pytest.mark.asyncio
async def test_wordpress_seo_write_uses_documented_put_method() -> None:
    post = {**_wp_post(), "forgeseo_seo": {"title": "", "description": ""}}
    state = {"post": post}
    seo_methods: list[str] = []

    index = _wp_index(forgeseo=True)
    index["routes"]["/forgeseo/v1/posts/(?P<id>[\\d]+)/seo"] = {
        "methods": ["GET", "PUT"],
        "endpoints": [
            {"methods": ["GET"]},
            {
                "methods": ["PUT"],
                "args": {"title": {"type": "string"}, "description": {"type": "string"}},
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, index)
        if request.url.path == "/wp-json/forgeseo/v1/capabilities":
            return _response(
                request,
                200,
                {
                    "seo_fields": ["title", "description"],
                    "seo_write_supported": True,
                },
            )
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/forgeseo/v1/posts/7/seo":
            if request.method != "PUT":
                pytest.fail(f"unexpected SEO method: {request.method}")
            seo_methods.append(request.method)
            state["post"]["forgeseo_seo"].update(json.loads(request.content))
            return _response(request, 200, {"updated": True})
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://put-seo.example.test",
        {"username": "fixture", "application_password": "fixture-password"},
        httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["seo"]["write"] is True
        original = await client.read("post:7")
        changed = await client.update(
            "post:7",
            {"seo": {"title": "PUT-managed title"}},
            original["source_hash"],
        )

    assert seo_methods == ["PUT"]
    assert changed["metadata"]["seo"]["forgeseo"]["title"] == "PUT-managed title"


@pytest.mark.asyncio
async def test_wordpress_metadata_seo_alias_uses_verified_route_and_protects_other_metadata() -> None:
    post = {**_wp_post(), "forgeseo_seo": {"title": "", "description": ""}}
    state = {"post": post}
    seo_writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index(forgeseo=True))
        if request.url.path == "/wp-json/forgeseo/v1/capabilities":
            return _response(
                request,
                200,
                {"seo_fields": ["title", "description"], "seo_write_supported": True},
            )
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/forgeseo/v1/posts/7/seo" and request.method == "POST":
            payload = json.loads(request.content)
            seo_writes.append(payload)
            state["post"]["forgeseo_seo"].update(payload)
            return _response(request, 200, {"updated": True})
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://metadata-alias.example.test",
        {"username": "fixture", "application_password": "fixture-password"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("post:7")
        changed = await client.update(
            "post:7",
            {"metadata": {"seo": {"title": "Metadata-managed title"}}},
            original["source_hash"],
        )
        assert changed["metadata"]["seo"]["forgeseo"]["title"] == "Metadata-managed title"

        with pytest.raises(ProtectedField):
            await client.update(
                "post:7",
                {"metadata": {"price": "0.01"}},
                changed["source_hash"],
            )

    assert seo_writes == [{"title": "Metadata-managed title"}]


@pytest.mark.asyncio
async def test_wordpress_lost_create_reconciles_without_duplicate_post() -> None:
    created = _wp_post(title="New article", body="<p>New body</p>")
    created["slug"] = "new-article"
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts" and request.method == "POST":
            posts += 1
            raise httpx.ReadTimeout("response lost")
        if request.url.path == "/wp-json/wp/v2/posts" and request.method == "GET":
            assert request.url.params["slug"] == "new-article"
            return _response(request, 200, [created])
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient("https://example.test", {"token": "secret"}, httpx.MockTransport(handler)) as client:
        record = await client.create_draft(
            {"title": "New article", "body": "<p>New body</p>", "slug": "new-article"},
            "operation-123",
        )
        assert record["outcome"] == "reconciled"
        assert record["resource_key"] == "post:7"
        assert posts == 1


@pytest.mark.asyncio
async def test_wordpress_builder_controls_are_protected() -> None:
    post = _wp_post()
    post["meta"] = {"_elementor_data": "serialized builder document"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/posts/7":
            return _response(request, 200, post)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient("https://example.test", {"token": "secret"}, httpx.MockTransport(handler)) as client:
        record = await client.read("post:7")
        with pytest.raises(ProtectedField):
            await client.update("post:7", {"body": "replace builder"}, record["source_hash"])


@pytest.mark.asyncio
async def test_wordpress_restore_builder_post_with_unchanged_body_restores_status() -> None:
    body = "<p>Builder body</p>"
    post = _wp_post(body=body, modified="2026-09-14T00:00:00")
    post["meta"] = {"_elementor_data": "serialized builder document"}
    state = {"post": post}
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _response(request, 200, _wp_user())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, state["post"])
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            changes = json.loads(request.content)
            writes.append(changes)
            state["post"].update(changes)
            state["post"]["modified"] = "2026-09-14T04:00:00"
            return _response(request, 200, state["post"])
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        snapshot = await client.read("post:7")
        state["post"]["status"] = "publish"
        published = await client.read("post:7")
        restored = await client.restore(
            "post:7",
            snapshot,
            expected_hash=published["source_hash"],
        )

    assert len(writes) == 1
    assert writes[0]["status"] == "draft"
    assert "content" not in writes[0]
    assert restored["status"] == "draft"
    assert restored["body"] == body


@pytest.mark.asyncio
async def test_wordpress_restore_rejects_changed_builder_body() -> None:
    snapshot_post = _wp_post(body="<p>Snapshot body</p>")
    snapshot_post["meta"] = {"_elementor_data": "serialized builder document"}
    current_post = deepcopy(snapshot_post)
    current_post["content"] = {"raw": "<p>Changed builder body</p>", "rendered": "<p>Changed builder body</p>"}
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _wp_index())
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _response(request, 200, current_post)
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "POST":
            writes.append(json.loads(request.content))
            return _response(request, 200, current_post)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WordPressClient(
        "https://example.test",
        {"token": "secret"},
        httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProtectedField):
            await client.restore("post:7", snapshot_post)

    assert writes == []


def _woo_index() -> dict:
    return {
        "name": "Mock Woo",
        "namespaces": ["wp/v2", "wc/v3"],
        "routes": {
            "/wc/v3/products": {"methods": ["GET"]},
            "/wc/v3/products/(?P<id>[\\d]+)": {"methods": ["GET", "PUT"]},
            "/wc/v3/products/categories": {"methods": ["GET"]},
            "/wc/v3/products/categories/(?P<id>[\\d]+)": {"methods": ["GET", "PUT"]},
        },
    }


def _woo_product(description: str = "Old description") -> dict:
    return {
        "id": 5,
        "name": "Product",
        "slug": "product",
        "permalink": "https://example.test/product",
        "status": "publish",
        "description": description,
        "short_description": "Short",
        "categories": [{"id": 2, "name": "Category"}],
        "tags": [],
        "price": "99.00",
        "regular_price": "99.00",
        "stock_quantity": 7,
        "sku": "SKU-5",
        "meta_data": [{"id": 1, "key": "_private_store_flag", "value": "keep"}],
        "date_modified": "2026-09-14T00:00:00",
    }


@pytest.mark.asyncio
async def test_woocommerce_update_and_restore_preserve_store_data() -> None:
    state = {"product": _woo_product()}
    put_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _woo_index())
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, 200, state["product"])
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            body = json.loads(request.content)
            put_bodies.append(body)
            state["product"].update(body)
            return _response(request, 200, state["product"])
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://example.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        changed = await client.update("product:5", {"description": "New description"}, original["source_hash"])
        assert changed["body"] == "New description"
        assert put_bodies == [{"description": "New description"}]
        restored = await client.restore("product:5", original, expected_hash=changed["source_hash"])
        assert restored["body"] == "Old description"
        assert put_bodies[-1] == {"name": "Product", "description": "Old description", "short_description": "Short", "categories": [{"id": 2}], "tags": []}
        assert "price" not in put_bodies[-1]
        assert "stock_quantity" not in put_bodies[-1]
        assert "sku" not in put_bodies[-1]
        assert state["product"]["price"] == "99.00"
        assert state["product"]["meta_data"] == [{"id": 1, "key": "_private_store_flag", "value": "keep"}]


@pytest.mark.asyncio
async def test_woocommerce_update_rechecks_source_before_mutation() -> None:
    state = {"product": _woo_product()}
    product_reads = 0
    put_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal product_reads
        if request.url.path == "/wp-json/":
            return _response(request, 200, _woo_index())
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            product_reads += 1
            if product_reads == 2:
                state["product"]["description"] = "External catalog editor change"
            return _response(request, 200, state["product"])
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            put_bodies.append(json.loads(request.content))
            pytest.fail("WooCommerce mutation was sent after the source changed")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://example.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        with pytest.raises(SourceConflict):
            await client.update("product:5", {"description": "must not overwrite"}, original["source_hash"])

    assert put_bodies == []
    assert state["product"]["description"] == "External catalog editor change"
    assert state["product"]["price"] == "99.00"
    assert state["product"]["stock_quantity"] == 7


@pytest.mark.asyncio
async def test_woocommerce_catalog_timeout_reconciles_but_remains_ambiguous_without_retry() -> None:
    state = {"product": _woo_product()}
    put_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _woo_index())
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, 200, state["product"])
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            body = json.loads(request.content)
            put_bodies.append(body)
            state["product"].update(body)
            raise httpx.ReadTimeout("response lost after WooCommerce applied the write", request=request)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://example.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        with pytest.raises(AmbiguousOutcome, match="outcome is unknown"):
            await client.update(
                "product:5",
                {"description": "Applied before the response was lost"},
                original["source_hash"],
            )

    assert put_bodies == [{"description": "Applied before the response was lost"}]
    assert state["product"]["price"] == "99.00"
    assert state["product"]["stock_quantity"] == 7
    assert state["product"]["sku"] == "SKU-5"


@pytest.mark.asyncio
async def test_woocommerce_rejects_nested_commerce_and_unknown_fields() -> None:
    product = _woo_product()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, 200, _woo_index())
        if request.url.path == "/wp-json/wc/v3/products/5":
            return _response(request, 200, product)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient("https://example.test", {}, httpx.MockTransport(handler)) as client:
        record = await client.read("product:5")
        with pytest.raises(ProtectedField):
            await client.update("product:5", {"metadata": {"price": "0.01"}}, record["source_hash"])
        with pytest.raises(UnsupportedField):
            await client.update("product:5", {"arbitrary_meta": "no"}, record["source_hash"])


@pytest.mark.asyncio
async def test_safe_get_rejects_private_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, 302, headers={"location": "http://127.0.0.1/private"})

    with pytest.raises(SSRFError):
        await safe_get("https://example.test/page", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_public_transport_rejects_same_host_authority_port_changes() -> None:
    transport = PublicTransport("https://example.test")
    try:
        with pytest.raises(ValueError, match="authority"):
            await transport.handle_async_request(
                httpx.Request("GET", "https://example.test:80/private")
            )
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_fetch_rejects_same_host_redirect_to_different_effective_port() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.test:80/private"},
            request=request,
        )

    with pytest.raises(ValueError, match="outside the selected site"):
        await fetch("https://example.test/start", transport=httpx.MockTransport(handler))
