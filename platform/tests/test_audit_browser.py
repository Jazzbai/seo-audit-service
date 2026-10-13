from __future__ import annotations

import pytest

from app.browser import _browser_request_policy, _browser_url_allowed


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://example.test/", True),
        ("https://EXAMPLE.TEST/assets/app.js?version=1", True),
        ("https://example.test:443/assets/app.js", True),
        ("http://example.test/assets/app.js", False),
        ("https://example.test:8443/assets/app.js", False),
        ("https://cdn.example.test/assets/app.js", False),
        ("https://example.test.evil.example/assets/app.js", False),
        ("https://user:password@example.test/assets/app.js", False),
        ("data:text/html,<h1>not-site</h1>", False),
        ("https://[malformed/assets/app.js", False),
    ],
)
def test_browser_requests_are_allowlisted_to_the_registered_origin(url, allowed):
    assert _browser_url_allowed(url, "https://example.test") is allowed


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("GET", "https://example.test/assets/app.js", "same_origin"),
        ("HEAD", "https://example.test/health", "same_origin"),
        ("GET", "https://cdn.example.test/app.js", "blocked_external"),
        ("POST", "https://example.test/api/update", "blocked_non_read"),
        ("DELETE", "https://cdn.example.test/resource", "blocked_non_read"),
    ],
)
def test_browser_request_policy_reports_safety_blocks_separately(method, url, expected):
    assert _browser_request_policy(method, url, "https://example.test") == expected
