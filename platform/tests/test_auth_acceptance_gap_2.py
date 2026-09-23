from __future__ import annotations

from fastapi import Response

from app import auth
from app.config import settings


def _set_cookie_headers(response: Response) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]


def test_secure_auth_cookies_are_host_only(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://seo.example.test")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)

    response = Response()
    auth._set_auth_cookies(response, "session-fixture", "csrf-fixture")

    cookies = _set_cookie_headers(response)
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("__Host-forge_session="))
    csrf_cookie = next(cookie for cookie in cookies if cookie.startswith("__Host-forge_csrf="))

    for cookie in (session_cookie, csrf_cookie):
        assert "Secure" in cookie
        assert "Path=/" in cookie
        assert "SameSite=lax" in cookie
        assert "Domain=" not in cookie
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie


def test_secure_auth_cookie_deletion_preserves_host_only_prefix_requirements(monkeypatch):
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)

    response = Response()
    auth._delete_auth_cookies(response)

    cookies = _set_cookie_headers(response)
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("__Host-forge_session="))
    csrf_cookie = next(cookie for cookie in cookies if cookie.startswith("__Host-forge_csrf="))

    for cookie in (session_cookie, csrf_cookie):
        assert "Secure" in cookie
        assert "Path=/" in cookie
        assert "SameSite=lax" in cookie
        assert "Max-Age=0" in cookie
        assert "Domain=" not in cookie
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
