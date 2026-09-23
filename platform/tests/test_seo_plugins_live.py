"""Opt-in rendered SEO-plugin checks against the separate port-18092 fixture.

The fixture is deliberately independent from deploy/compose.integration.yaml.
Set FORGE_SEO_LIVE=1 to run these Docker-backed tests. The test transport only
maps the two fixture domains below to the disposable loopback service; it does
not permit requests to live sites or paid providers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from bs4 import BeautifulSoup

from app.connectors.errors import SourceConflict, UnsupportedField
from app.connectors.wordpress import WordPressClient
from app.workflows import protected_html


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose.seo-test.yaml"
CONNECTOR_FILE = ROOT / "wordpress" / "forgeseo-connector.php"
FIXTURE_FILE = ROOT / "scripts" / "seo_plugin_fixture.php"
PROJECTS = {
    "yoast": "forgeseo-seo-yoast",
    "rank_math": "forgeseo-seo-rank-math",
}
HOSTS = {
    "yoast": "yoast.seo.fixture.test",
    "rank_math": "rank-math.seo.fixture.test",
}


def _live_gate_reason() -> str | None:
    if os.environ.get("FORGE_SEO_LIVE") != "1":
        return "Explicit isolated SEO-plugin integration opt-in required"

    docker = shutil.which("docker")
    if docker is None:
        return "Docker CLI is unavailable; isolated Yoast/Rank Math live gates are unverified"

    checks = (
        ("Docker engine", [docker, "info", "--format", "{{.ServerVersion}}"]),
        ("Docker Compose", [docker, "compose", "version"]),
    )
    for label, command in checks:
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return f"{label} check could not complete; isolated Yoast/Rank Math live gates are unverified"
        if result.returncode != 0:
            return f"{label} is unavailable; isolated Yoast/Rank Math live gates are unverified"
    return None


LIVE_GATE_REASON = _live_gate_reason()
LIVE_ONLY = pytest.mark.skipif(LIVE_GATE_REASON is not None, reason=LIVE_GATE_REASON or "")


def _mock_response(request: httpx.Request, status: int, value: object) -> httpx.Response:
    return httpx.Response(status, json=value, request=request)


def _mock_seo_index(*, provider: str, ambiguous: bool = False) -> dict[str, object]:
    namespaces = ["wp/v2", "forgeseo/v1"]
    if ambiguous:
        namespaces.extend(["yoast/v1", "rankmath/v1"])
    elif provider == "yoast":
        namespaces.append("yoast/v1")
    else:
        namespaces.append("rankmath/v1")

    routes: dict[str, object] = {
        "/wp/v2/posts": {"methods": ["GET", "POST"]},
        "/wp/v2/posts/(?P<id>[\\d]+)": {"methods": ["GET", "POST"]},
        "/wp/v2/pages": {"methods": ["GET", "POST"]},
        "/wp/v2/users": {"methods": ["GET"]},
        "/wp/v2/statuses": {"methods": ["GET"]},
        "/forgeseo/v1/capabilities": {"methods": ["GET"]},
        "/forgeseo/v1/posts/(?P<id>[\\d]+)/seo": {
            "methods": ["GET", "POST"],
            "endpoints": [
                {"methods": ["GET"]},
                {
                    "methods": ["POST"],
                    "args": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            ],
        },
        "/forgeseo/v1/operations/(?P<operation_key>[A-Za-z0-9._:-]+)": {
            "methods": ["GET"]
        },
    }
    return {"name": "Mock isolated WordPress", "namespaces": namespaces, "routes": routes}


def _mock_connector_capabilities(provider: str, *, supported: bool = True) -> dict[str, object]:
    return {
        "namespace": "forgeseo/v1",
        "seo_fields": ["title", "description"],
        "operation_mapping": True,
        "webhooks": True,
        "seo_provider": provider if supported else "ambiguous",
        "seo_write_supported": supported,
        "seo_write_mode": "documented_frontend_filters" if supported else "unsupported",
        "seo_restore_supported": supported,
        "direct_provider_meta_write": False,
        "provider_filters": {
            "yoast": {"title": "wpseo_title", "description": "wpseo_metadesc"},
            "rank_math": {
                "title": "rank_math/frontend/title",
                "description": "rank_math/frontend/description",
            },
        },
    }


def _mock_post(*, title: str = "Original title", description: str = "") -> dict[str, object]:
    return {
        "id": 7,
        "date": "2026-01-01T00:00:00",
        "modified": "2026-09-14T00:00:00",
        "slug": "original-title",
        "status": "publish",
        "type": "post",
        "link": "https://seo.fixture.test/original-title/",
        "title": {"raw": title, "rendered": title},
        "content": {"raw": "<p>Body</p>", "rendered": "<p>Body</p>"},
        "excerpt": {"raw": "", "rendered": ""},
        "author": 3,
        "featured_media": 0,
        "categories": [],
        "tags": [],
        "meta": {},
        "forgeseo_seo": {"title": "", "description": description},
    }


@pytest.mark.parametrize("php_file", (CONNECTOR_FILE, FIXTURE_FILE))
def test_owned_php_files_pass_php_lint_when_php_is_available(php_file: Path) -> None:
    php = shutil.which("php")
    if php is None:
        pytest.skip(f"PHP CLI unavailable; syntax gate for {php_file.name} is unverified")
    result = subprocess.run(
        [php, "-l", str(php_file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_connector_uses_documented_wordpress_provider_hooks_and_routes() -> None:
    source = CONNECTOR_FILE.read_text(encoding="utf-8")
    for fragment in (
        "add_action('rest_api_init'",
        "register_rest_route(self::NAMESPACE, '/capabilities'",
        "register_rest_route(self::NAMESPACE, '/posts/(?P<id>[\\\\d]+)/seo'",
        "add_filter('wpseo_title'",
        "add_filter('wpseo_metadesc'",
        "add_filter('rank_math/frontend/title'",
        "add_filter('rank_math/frontend/description'",
        "add_action('rest_after_insert_post'",
        "add_action('admin_init'",
        "add_action('admin_menu'",
        "X_FORGESEO_OPERATION_KEY",
        "if (!$creating) { return; }",
    ):
        assert fragment in source


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("yoast", "rank_math"))
async def test_mocked_provider_capability_and_seo_route(provider: str) -> None:
    state = {"post": _mock_post()}
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/wp-json/":
            return _mock_response(request, 200, _mock_seo_index(provider=provider))
        if request.url.path == "/wp-json/forgeseo/v1/capabilities":
            return _mock_response(request, 200, _mock_connector_capabilities(provider))
        if request.url.path == "/wp-json/wp/v2/users/me":
            return _mock_response(
                request,
                200,
                {
                    "id": 3,
                    "name": "Fixture editor",
                    "capabilities": {"edit_posts": True, "publish_posts": True},
                },
            )
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _mock_response(request, 200, state["post"])
        if request.url.path == "/wp-json/forgeseo/v1/posts/7/seo" and request.method == "POST":
            body = json.loads(request.content)
            assert set(body) <= {"title", "description"}
            state["post"]["forgeseo_seo"].update(body)
            return _mock_response(request, 200, {"updated": True})
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://seo.fixture.test",
        {"username": "fixture_owner", "application_password": "fixture-password"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["plugins"][provider] == {
            "detected": True,
            "read": True,
            "write": False,
            "writable_fields": [],
        }
        assert capabilities["seo"]["active_provider"] == provider
        assert capabilities["seo"]["write"] is True
        assert capabilities["forgeseo_plugin"]["writable_fields"] == ["description", "title"]

        before = await client.read("post:7")
        changed = await client.update(
            "post:7",
            {"seo": {"title": "Managed title", "description": "Managed description"}},
            before["source_hash"],
        )
        assert changed["metadata"]["seo"]["forgeseo"] == {
            "title": "Managed title",
            "description": "Managed description",
        }

    assert ("POST", "/wp-json/forgeseo/v1/posts/7/seo") in requests


@pytest.mark.asyncio
async def test_mocked_ambiguous_provider_disables_seo_route_writes() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/wp-json/":
            return _mock_response(request, 200, _mock_seo_index(provider="yoast", ambiguous=True))
        if request.url.path == "/wp-json/forgeseo/v1/capabilities":
            return _mock_response(request, 200, _mock_connector_capabilities("ambiguous", supported=False))
        if request.url.path == "/wp-json/wp/v2/posts/7" and request.method == "GET":
            return _mock_response(request, 200, _mock_post())
        pytest.fail(f"unexpected mocked request: {request.method} {request.url}")

    async with WordPressClient(
        "https://seo.fixture.test",
        {"username": "fixture_owner", "application_password": "fixture-password"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["seo"]["active_provider"] == "ambiguous"
        assert capabilities["seo"]["write"] is False
        before = await client.read("post:7")
        with pytest.raises(UnsupportedField):
            await client.update("post:7", {"seo": {"title": "must not apply"}}, before["source_hash"])

    assert not any(path.endswith("/seo") and method == "POST" for method, path in requests)


class FixtureTransport(httpx.AsyncBaseTransport):
    """Only the isolated SEO fixture domains can reach the loopback port."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ports = {HOSTS["yoast"]: 18092, HOSTS["rank_math"]: 18092}
        if request.url.scheme != "https" or request.url.host not in ports:
            raise ValueError("Request escaped the isolated SEO fixture allowlist")
        target = request.url.copy_with(
            scheme="http",
            host="127.0.0.1",
            port=ports[request.url.host],
        )
        headers = httpx.Headers(request.headers)
        headers["host"] = request.headers.get("host", request.url.host)
        headers["x-forwarded-proto"] = "https"
        forwarded = httpx.Request(
            request.method,
            target,
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self.inner.aclose()


class FixtureConfig(dict[str, str]):
    def __repr__(self) -> str:
        return "<isolated SEO fixture credentials redacted>"


def _compose(provider: str, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECTS[provider],
        "--file",
        str(COMPOSE_FILE),
        *arguments,
    ]


def _cleanup(provider: str) -> None:
    # The project name and compose file are unique to this test fixture. No
    # integration or legacy project is addressed by this cleanup.
    try:
        subprocess.run(
            _compose(provider, "down", "--volumes", "--remove-orphans"),
            cwd=ROOT,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _configure(provider: str) -> FixtureConfig:
    if LIVE_GATE_REASON is not None:
        pytest.skip(LIVE_GATE_REASON)
    _cleanup(provider)
    started = subprocess.run(
        _compose(provider, "up", "--detach"),
        cwd=ROOT,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if started.returncode:
        _cleanup(provider)
        pytest.fail("Isolated SEO fixture failed to start; command output withheld")

    command = _compose(
        provider,
        "exec",
        "-T",
        "--user",
        "www-data",
        "seo-wp",
        "php",
        "-d",
        "memory_limit=512M",
        "/fixture/setup.php",
        provider,
    )
    for _ in range(90):
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except ValueError:
                _cleanup(provider)
                pytest.fail("SEO fixture returned an invalid credential envelope; output withheld")
            if not isinstance(payload, dict) or payload.get("provider") != provider:
                _cleanup(provider)
                pytest.fail("SEO fixture returned an unexpected credential envelope; output withheld")
            return FixtureConfig(payload)
        time.sleep(2)
    _cleanup(provider)
    pytest.fail("Isolated SEO fixture initialization did not become ready; output withheld")


@pytest.fixture(scope="module", params=("yoast", "rank_math"))
def seo_site(request: pytest.FixtureRequest):
    if LIVE_GATE_REASON is not None:
        pytest.skip(LIVE_GATE_REASON)
    provider = str(request.param)
    site = _configure(provider)
    try:
        yield site
    finally:
        _cleanup(provider)


@LIVE_ONLY
@pytest.mark.asyncio
async def test_seo_provider_capabilities_are_explicit_and_honest(seo_site: FixtureConfig) -> None:
    provider = seo_site["provider"]
    async with WordPressClient(
        seo_site["origin"],
        seo_site,
        transport=FixtureTransport(),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["native"]["create"] is True
        assert capabilities["seo"]["write"] is True
        assert capabilities["seo"]["active_provider"] == provider
        assert capabilities["plugins"][provider]["detected"] is True
        # The vendor namespaces are read-only to this connector. Writes are
        # supported only through the narrow ForgeSEO route below.
        assert capabilities["plugins"][provider]["write"] is False
        assert capabilities["forgeseo_plugin"]["write"] is True

        async with httpx.AsyncClient(
            transport=FixtureTransport(),
            auth=(seo_site["username"], seo_site["application_password"]),
        ) as raw:
            response = await raw.get(
                f'{seo_site["origin"]}/wp-json/forgeseo/v1/capabilities'
            )
        assert response.status_code == 200
        plugin_capabilities = response.json()
        assert plugin_capabilities["seo_provider"] == provider
        assert plugin_capabilities["seo_write_supported"] is True
        assert plugin_capabilities["seo_write_mode"] == "documented_frontend_filters"
        assert plugin_capabilities["seo_restore_supported"] is True
        assert plugin_capabilities["direct_provider_meta_write"] is False
        assert plugin_capabilities["provider_filters"]["yoast"]["title"] == "wpseo_title"
        assert plugin_capabilities["provider_filters"]["rank_math"]["description"] == "rank_math/frontend/description"


@LIVE_ONLY
@pytest.mark.asyncio
async def test_seo_metadata_renders_restores_and_rejects_stale_edits(
    seo_site: FixtureConfig,
) -> None:
    provider = seo_site["provider"]
    article = {
        "title": f"SEO fixture article {uuid4().hex[:8]}",
        "body": "<h2>Repair preparation</h2><p>Keep this fixture body unchanged.</p>",
        "author_id": seo_site["author_id"],
    }
    expected_title = "A complete isolated repair preparation guide"
    expected_description = "Prepare for an isolated repair visit with practical instructions."

    async with WordPressClient(
        seo_site["origin"],
        seo_site,
        transport=FixtureTransport(),
    ) as client:
        draft = await client.create_draft(article, f"seo-fixture-{uuid4().hex}")
        published = await client.publish(str(draft["id"]))
        before = await client.read(published["resource_key"])
        assert before["title"] == article["title"]
        assert before["body"] == article["body"]

        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            original_response = await public.get(published["url"])
        assert original_response.status_code == 200
        original_html = original_response.text
        original_soup = BeautifulSoup(original_html, "html.parser")

        changed = await client.update(
            before["resource_key"],
            {"seo": {"title": expected_title, "description": expected_description}},
            before["source_hash"],
        )
        assert changed["title"] == before["title"]
        assert changed["body"] == before["body"]
        assert changed["metadata"]["seo"]["forgeseo"] == {
            "title": expected_title,
            "description": expected_description,
        }

        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            changed_response = await public.get(published["url"])
        assert changed_response.status_code == 200
        changed_soup = BeautifulSoup(changed_response.text, "html.parser")
        assert changed_soup.title is not None
        assert changed_soup.title.get_text() == expected_title
        descriptions = changed_soup.select('meta[name="description"]')
        assert len(descriptions) == 1
        assert descriptions[0].get("content") == expected_description
        assert protected_html(original_html) == protected_html(changed_response.text)
        assert changed["raw"].get("content", {}).get("raw") == before["raw"].get("content", {}).get("raw")

        external = await client.update(
            before["resource_key"],
            {"body": "<p>External editor replacement remains authoritative.</p>"},
            changed["source_hash"],
        )
        with pytest.raises(SourceConflict):
            await client.update(
                before["resource_key"],
                {"seo": {"title": "Stale SEO edit must not apply"}},
                changed["source_hash"],
            )
        observed = await client.read(before["resource_key"])
        assert observed["body"] == external["body"]
        assert observed["metadata"]["seo"]["forgeseo"]["title"] == expected_title

        restored = await client.restore(
            before["resource_key"],
            before,
            external["source_hash"],
        )
        assert restored["source_hash"] == before["source_hash"]
        assert restored["title"] == before["title"]
        assert restored["body"] == before["body"]
        assert restored["metadata"]["seo"]["forgeseo"] == {"title": "", "description": ""}

        async with httpx.AsyncClient(transport=FixtureTransport()) as public:
            restored_response = await public.get(published["url"])
        assert restored_response.status_code == 200
        restored_soup = BeautifulSoup(restored_response.text, "html.parser")
        assert restored_soup.title is not None
        assert original_soup.title is not None
        assert restored_soup.title.get_text() == original_soup.title.get_text()
        assert [tag.get("content") for tag in restored_soup.select('meta[name="description"]')] == [
            tag.get("content") for tag in original_soup.select('meta[name="description"]')
        ]
        assert protected_html(original_html) == protected_html(restored_response.text)
