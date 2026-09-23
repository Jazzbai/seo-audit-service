import json

import httpx
import pytest

from scripts.platform_live_probe import PlatformClient, ProbeError, normalize_origin, run_probe


def _client(handler):
    origin = "https://seo.example.test"
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url=f"{origin}/api/v1")
    return http_client, PlatformClient(http_client, origin)


def _base_gets(request: httpx.Request):
    if request.method == "GET" and request.url.path == "/api/v1/sites":
        return httpx.Response(
            200,
            json={"items": [{"id": "site-1", "name": "Pilot", "origin": "https://site.example.test", "paused": True}], "count": 1},
            request=request,
        )
    if request.method == "GET" and request.url.path == "/api/v1/sites/site-1/overview":
        return httpx.Response(
            200,
            json={
                "site": {"id": "site-1", "paused": True},
                "connections": [{"kind": "wordpress", "status": "connected", "capabilities": {"authenticated": True}}],
                "coverage": {"status": "not_checked", "error_count": 0, "pending_url_count": 0},
            },
            request=request,
        )
    if request.method == "GET" and request.url.path == "/api/v1/settings":
        return httpx.Response(200, json={"global_pause": True}, request=request)
    return None


def test_origin_validation_requires_https_for_non_local_deployments():
    assert normalize_origin("http://127.0.0.1:18080/") == "http://127.0.0.1:18080"
    assert normalize_origin("https://SEO.Example.TEST/") == "https://seo.example.test"
    with pytest.raises(ProbeError, match="HTTPS"):
        normalize_origin("http://seo.example.test")
    with pytest.raises(ProbeError, match="origin"):
        normalize_origin("https://seo.example.test/app")
    with pytest.raises(ProbeError, match="credentials"):
        normalize_origin("https://owner:secret@seo.example.test")


def test_probe_is_read_only_by_default_and_reports_pause_and_connection_state():
    seen_methods = []

    def handler(request):
        seen_methods.append(request.method)
        return _base_gets(request)

    http_client, platform = _client(handler)
    try:
        report = run_probe(platform, site_id="site-1")
    finally:
        http_client.close()

    assert report["status"] == "READ_ONLY_CHECK_PASSED"
    assert report["remote_wordpress_writes"] is False
    assert report["paid_provider_requests"] is False
    assert {check["name"] for check in report["checks"]} >= {"site_access", "wordpress_connection", "pause_controls"}
    assert seen_methods == ["GET", "GET", "GET"]


def test_probe_can_queue_only_inventory_and_audit_and_keeps_job_result_bounded():
    requests = []
    job_ids = {"inventory": "job-inventory", "audit": "job-audit"}

    def handler(request):
        requests.append((request.method, request.url.path))
        base = _base_gets(request)
        if base is not None:
            return base
        if request.method == "POST" and request.url.path == "/api/v1/sites/site-1/jobs":
            body = json.loads(request.content.decode("utf-8"))
            kind = body["kind"]
            assert kind in {"inventory", "audit"}
            assert body["payload"] == {}
            assert body["idempotency_key"].startswith(f"platform-live-probe:{kind}:")
            return httpx.Response(202, json={"id": job_ids[kind], "status": "queued"}, request=request)
        if request.method == "GET" and request.url.path.endswith("/jobs/job-inventory"):
            return httpx.Response(200, json={"id": "job-inventory", "status": "complete", "result": {
                "complete": True,
                "resources": 48,
                "counts": {"wordpress": {"seen": 48}},
                "raw_url": "must not be copied into the report",
            }}, request=request)
        if request.method == "GET" and request.url.path.endswith("/jobs/job-audit"):
            return httpx.Response(200, json={"id": "job-audit", "status": "partial", "result": {
                "complete": False,
                "pending_url_count": 2,
                "pending_urls": ["https://site.example.test/private"],
            }}, request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http_client, platform = _client(handler)
    try:
        report = run_probe(platform, site_id="site-1", run_inventory=True, run_audit=True, sleep=lambda _: None)
    finally:
        http_client.close()

    assert report["status"] == "READ_ONLY_CHECK_PASSED"
    assert [job["kind"] for job in report["jobs"]] == ["inventory", "audit"]
    assert report["jobs"][0]["resources"] == 48
    assert report["jobs"][1]["pending_url_count"] == 2
    rendered = json.dumps(report)
    assert "must not be copied" not in rendered
    assert "private" not in rendered
    assert all(method in {"GET", "POST"} for method, _path in requests)
