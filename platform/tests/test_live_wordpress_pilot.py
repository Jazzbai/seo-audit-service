from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from scripts.live_wordpress_pilot import (
    ProbeConfigurationError,
    build_parser,
    resolve_inputs,
    run_probe,
)


def _response(request: httpx.Request, value: object) -> httpx.Response:
    return httpx.Response(200, json=value, request=request)


def _index() -> dict[str, Any]:
    return {
        "name": "Standalone pilot fixture",
        "namespaces": ["wp/v2"],
        "routes": {
            "/wp/v2/types": {"methods": ["GET"]},
            "/wp/v2/posts": {"methods": ["GET", "POST"]},
            "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
            "/wp/v2/pages": {"methods": ["GET"]},
            "/wp/v2/users": {"methods": ["GET"]},
        },
    }


def _types() -> dict[str, Any]:
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
    }


def _content_record(
    remote_id: int,
    *,
    title: str,
    status: str,
    resource_type: str = "post",
) -> dict[str, Any]:
    return {
        "id": remote_id,
        "type": resource_type,
        "slug": f"fixture-{remote_id}",
        "status": status,
        "title": {"raw": title, "rendered": title},
        "content": {"raw": "Fixture body", "rendered": "Fixture body"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
        "link": f"https://fixture.test/{remote_id}",
    }


def _read_only_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    posts = [
        _content_record(11, title="Published fixture", status="publish"),
        _content_record(12, title="Draft fixture", status="draft"),
    ]
    pages = [_content_record(21, title="Published page", status="publish", resource_type="page")]
    users = [{"id": 3, "name": "Fixture editor", "description": ""}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/wp-json/":
            return _response(request, _index())
        if path == "/wp-json/wp/v2/types":
            return _response(request, _types())
        if path == "/wp-json/wp/v2/users/me":
            return _response(
                request,
                {
                    "id": 3,
                    "name": "Fixture editor",
                    "capabilities": {"edit_posts": True, "publish_posts": True},
                },
            )
        if path == "/wp-json/wp/v2/posts":
            return _response(request, posts)
        if path == "/wp-json/wp/v2/pages":
            return _response(request, pages)
        if path == "/wp-json/wp/v2/users":
            return _response(request, users)
        pytest.fail(f"unexpected fixture request: {request.method} {request.url}")

    return httpx.MockTransport(handler), requests


@pytest.mark.asyncio
async def test_default_probe_is_read_only_and_reports_authenticated_counts() -> None:
    transport, requests = _read_only_transport()
    secret = "fixture-application-password-do-not-print"

    report = await run_probe(
        "https://fixture.test",
        {"username": "fixture-editor", "application_password": secret},
        transport=transport,
    )

    assert report["result"] == "PASS"
    assert report["mode"] == "read_only"
    checks = report["read_only_checks"]
    assert checks["status"] == "passed"
    assert checks["authenticated_capability"]["authenticated"] is True
    assert checks["authenticated_capability"]["native"]["create"] is True
    assert checks["inventory_counts"] == {
        "total": 4,
        "by_resource_type": {"author": 1, "page": 1, "post": 2},
        "by_status": {"draft": 1, "publish": 2, "unknown": 1},
    }
    assert report["live_write_evidence"]["status"] == "not_requested"
    assert all(request.method == "GET" for request in requests)
    assert secret not in json.dumps(report)


def test_inputs_use_only_explicit_cli_or_named_environment_values() -> None:
    parser = build_parser()
    environment = {
        "WORDPRESS_ORIGIN": "https://environment.test",
        "WORDPRESS_USERNAME": "environment-user",
        "WORDPRESS_APPLICATION_PASSWORD": "environment-password",
    }
    origin, credentials = resolve_inputs(
        parser.parse_args([]),
        environment=environment,
    )
    assert origin == "https://environment.test"
    assert credentials == {
        "username": "environment-user",
        "application_password": "environment-password",
    }

    cli_origin, cli_credentials = resolve_inputs(
        parser.parse_args(
            [
                "--origin",
                "https://cli.test",
                "--username",
                "cli-user",
                "--application-password",
                "cli-password",
            ]
        ),
        environment=environment,
    )
    assert cli_origin == "https://cli.test"
    assert cli_credentials == {
        "username": "cli-user",
        "application_password": "cli-password",
    }

    with pytest.raises(ProbeConfigurationError, match="origin is required"):
        resolve_inputs(parser.parse_args([]), environment={"FORGESEO_ORIGIN": "ignored"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_live_write", "confirm_live_write"),
    [(True, False), (False, True)],
)
async def test_temporary_write_requires_two_opt_ins_before_client_creation(
    allow_live_write: bool,
    confirm_live_write: bool,
) -> None:
    class NeverConstructed:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pytest.fail("the client must not be constructed for a partial opt-in")

    with pytest.raises(ProbeConfigurationError, match="both"):
        await run_probe(
            "https://fixture.test",
            {"token": "fixture-token"},
            allow_live_write=allow_live_write,
            confirm_live_write=confirm_live_write,
            client_factory=NeverConstructed,
        )


@pytest.mark.asyncio
async def test_opted_in_roundtrip_marks_one_new_post_restores_draft_and_never_deletes() -> None:
    class FakeClient:
        instances: list["FakeClient"] = []

        def __init__(self, origin: str, credentials: dict[str, object]) -> None:
            self.origin = origin
            self.credentials = credentials
            self.calls: list[tuple[str, Any]] = []
            self.created: dict[str, Any] | None = None
            self.instances.append(self)

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def validate_connection(self) -> dict[str, Any]:
            return {
                "authenticated": True,
                "rest_api": True,
                "wp_v2": True,
                "native": {"read": True, "create": True, "update": True, "publish": True},
                "editorial": {"read": True, "write": True},
                "resource_types": [],
                "plugins": {},
                "seo": {"read": False, "write": False},
                "authenticated_author": {"id": "7", "name": "Fixture operator"},
            }

        async def inventory(self) -> list[dict[str, str]]:
            return [{"resource_type": "post", "status": "draft"}]

        async def create_draft(
            self,
            article: dict[str, str],
            operation_key: str,
        ) -> dict[str, Any]:
            self.calls.append(("create_draft", article, operation_key))
            self.created = {
                "resource_key": "post:9001",
                "title": article["title"],
                "body": article["body"],
                "status": "draft",
                "source_hash": "draft-source",
            }
            return dict(self.created)

        async def publish(
            self,
            resource_key: str,
            expected_hash: str,
            *,
            operation_key: str,
        ) -> dict[str, Any]:
            self.calls.append(("publish", resource_key, expected_hash, operation_key))
            assert resource_key == "post:9001"
            assert expected_hash == "draft-source"
            return {**self.created, "status": "publish", "source_hash": "published-source"}

        async def restore(
            self,
            resource_key: str,
            snapshot: dict[str, Any],
            expected_hash: str,
            *,
            operation_key: str,
        ) -> dict[str, Any]:
            self.calls.append(("restore", resource_key, snapshot, expected_hash, operation_key))
            assert resource_key == "post:9001"
            assert snapshot["status"] == "draft"
            assert expected_hash == "published-source"
            return {**snapshot, "status": "draft", "source_hash": "restored-source"}

    secret = "fixture-token-that-must-not-be-output"
    report = await run_probe(
        "https://fixture.test",
        {"token": secret},
        allow_live_write=True,
        confirm_live_write=True,
        client_factory=FakeClient,
    )

    evidence = report["live_write_evidence"]
    assert report["result"] == "PASS"
    assert report["mode"] == "temporary_post_roundtrip"
    assert evidence["status"] == "passed"
    assert evidence["attempted"] is True
    assert evidence["created_resource_key"] == "post:9001"
    assert re.search(r"FORGESEO_LIVE_PROBE::[0-9a-f]{32}", evidence["marker"])
    assert evidence["mutations"] == ["create_draft", "publish", "restore_to_draft"]
    assert evidence["restored_to_draft"] is True
    assert evidence["deletion_attempted"] is False
    assert evidence["deletion_performed"] is False
    assert all(call[0] in {"create_draft", "publish", "restore"} for call in FakeClient.instances[-1].calls)
    create_call = FakeClient.instances[-1].calls[0]
    assert evidence["marker"] in create_call[1]["title"]
    assert evidence["marker"] in create_call[1]["body"]
    assert secret not in json.dumps(report)
