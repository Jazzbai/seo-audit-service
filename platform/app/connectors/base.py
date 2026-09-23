"""Async HTTP plumbing shared by connector implementations."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.network import PublicTransport

from .common import copy_json
from .errors import ConnectorError, IncompleteInventory, SSRFError
from .security import validate_public_origin, validate_redirect


class AsyncConnector:
    """A no-retry, same-authority async HTTP client base class."""

    def __init__(
        self,
        origin: str,
        credentials: Mapping[str, object],
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        auth_kind: str = "wordpress",
    ) -> None:
        if not isinstance(credentials, Mapping):
            raise TypeError("credentials must be a mapping")
        self.credentials = dict(credentials)
        transport_was_supplied = transport is not None
        self._resolve_dns = False if transport_was_supplied else True
        self.origin = validate_public_origin(origin, resolve_dns=self._resolve_dns)
        self._transport = transport if transport is not None else PublicTransport(self.origin)
        # PublicTransport has already pinned and checked the DNS answer.  Mock
        # transports intentionally skip DNS so example.test remains usable.
        self._resolve_dns = False
        self._auth_kind = auth_kind
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    async def __aenter__(self):
        await self._client_or_create()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None and not self._closed:
            await self._client.aclose()
            self._closed = True

    def _auth(self) -> tuple[tuple[str, str] | None, dict[str, str]]:
        """Build auth without exposing credentials in logs or exceptions."""

        credentials = self.credentials
        headers: dict[str, str] = {}
        token = credentials.get("token") or credentials.get("access_token")
        username = credentials.get("username") or credentials.get("user")

        if token is not None:
            headers["Authorization"] = f"Bearer {str(token)}"
            return None, headers

        if self._auth_kind == "woocommerce":
            key = credentials.get("consumer_key")
            secret = credentials.get("consumer_secret")
            if key is not None and secret is not None:
                return (str(key), str(secret)), headers

        password = credentials.get("application_password")
        if password is None:
            password = credentials.get("password")
        if username is not None and password is not None:
            return (str(username), str(password)), headers
        return None, headers

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None or self._closed:
            auth, auth_headers = self._auth()
            self._client = httpx.AsyncClient(
                transport=self._transport,
                auth=auth,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ForgeSEOConnector/1.0",
                    **auth_headers,
                },
                follow_redirects=False,
                timeout=httpx.Timeout(20.0, connect=10.0),
                trust_env=False,
            )
            self._closed = False
        return self._client

    def _safe_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = f"{self.origin.rstrip('/')}/{path_or_url.lstrip('/')}"
        parsed = urlsplit(url)
        trailing_slash = parsed.path.endswith("/")
        normalized = validate_public_origin(url, resolve_dns=self._resolve_dns)
        if trailing_slash and not urlsplit(normalized).path.endswith("/"):
            normalized = f"{normalized}/"
        return normalized

    @staticmethod
    def _error_url(url: str) -> str:
        # Query strings can contain user-provided operation keys.  They are not
        # credentials, but omitting them makes exceptions safer for logs.
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    async def _request_response(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, object] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        authenticate: bool = True,
    ) -> httpx.Response:
        client = await self._client_or_create()
        request_method = method.upper()
        url = self._safe_url(path_or_url)
        request_params = dict(params or {})
        extra_headers = dict(headers or {})

        for redirect_count in range(4):
            try:
                request = client.build_request(
                    request_method,
                    url,
                    params=request_params,
                    json=json,
                    headers=extra_headers,
                )
                if not authenticate:
                    request.headers.pop('authorization',None)
                response = await client.send(request,auth=client.auth if authenticate else None)
            except httpx.HTTPError as exc:
                raise ConnectorError(
                    "Connector request failed before a response was received",
                    method=request_method,
                    url=self._error_url(url),
                    transport_error=True,
                ) from exc

            if response.status_code < 300 or response.status_code >= 400:
                return response

            location = response.headers.get("location")
            if not location:
                raise ConnectorError(
                    "Remote redirect did not provide a location",
                    status_code=response.status_code,
                    method=request_method,
                    url=self._error_url(url),
                    response_received=True,
                )
            try:
                destination = validate_redirect(
                    str(response.request.url),
                    location,
                    resolve_dns=self._resolve_dns,
                )
            except SSRFError:
                raise
            if request_method not in {"GET", "HEAD", "OPTIONS"}:
                raise ConnectorError(
                    "Redirects are not followed for mutating requests",
                    status_code=response.status_code,
                    method=request_method,
                    url=self._error_url(url),
                    response_received=True,
                )
            url = destination
            request_params = {}
            # The redirected URL already contains the Location query string.
            if redirect_count == 3:
                raise ConnectorError(
                    "Too many redirects from remote site",
                    method=request_method,
                    url=self._error_url(url),
                    response_received=True,
                )

        raise ConnectorError("Connector request could not be completed")  # pragma: no cover

    async def _fetch_wp_collection(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch a WordPress/WooCommerce collection without silent truncation.

        Headerless endpoints use the explicit page size as the end condition.
        A full batch at the limit cannot prove completion. Reported totals,
        when present, must remain consistent throughout the read.
        """
        query = dict(params or {})
        query.setdefault("per_page", 100)
        per_page = query["per_page"]
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        if type(per_page) is not int or not 1 <= per_page <= 100:
            raise ValueError("per_page must be an integer between 1 and 100")

        totals: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            query["page"] = page
            payload, response = await self._json_request("GET", path, params=query)
            if not isinstance(payload, list) or len(payload) > per_page:
                raise IncompleteInventory("records")
            for name in ("x-wp-totalpages", "x-wp-total"):
                value = response.headers.get(name)
                if value is None:
                    continue
                if re.fullmatch(r"[0-9]{1,12}", value.strip()) is None:
                    raise IncompleteInventory("pagination")
                total = int(value)
                if name in totals and totals[name] != total:
                    raise IncompleteInventory("pagination")
                totals[name] = total
            total_pages = totals.get("x-wp-totalpages")
            total_items = totals.get("x-wp-total")
            if total_pages is not None and total_pages > max_pages:
                raise IncompleteInventory("limit")
            for item in payload:
                if not isinstance(item, Mapping) or item.get("id") is None:
                    raise IncompleteInventory("records")
                identity = str(item["id"])
                if identity in seen:
                    raise IncompleteInventory("records")
                seen.add(identity)
                items.append(copy_json(item))

            if total_items is not None and len(items) > total_items:
                raise IncompleteInventory("pagination")
            if total_pages is not None:
                if total_pages == 0 and (page != 1 or items):
                    raise IncompleteInventory("pagination")
                complete = page >= total_pages
                if not payload and total_pages > 0:
                    raise IncompleteInventory("pagination")
            elif total_items is not None:
                complete = len(items) == total_items
                if not payload and not complete:
                    raise IncompleteInventory("pagination")
            else:
                complete = len(payload) < per_page
            if complete:
                if total_items is not None and len(items) != total_items:
                    raise IncompleteInventory("pagination")
                return items
        raise IncompleteInventory("limit")

    async def _json_request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, object] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        accepted_statuses: tuple[int, ...] | None = None,
        authenticate: bool = True,
    ) -> tuple[Any, httpx.Response]:
        response = await self._request_response(
            method,
            path_or_url,
            params=params,
            json=json,
            headers=headers,
            authenticate=authenticate,
        )
        accepted = accepted_statuses or tuple(range(200, 300))
        if response.status_code not in accepted:
            code: str | None = None
            try:
                body = response.json()
                if isinstance(body, Mapping) and isinstance(body.get("code"), str):
                    code = body["code"]
            except (ValueError, TypeError):
                pass
            if response.status_code in {401, 403}:
                message = "Remote site rejected connector authentication or permission"
            elif response.status_code == 404:
                message = "Remote connector resource was not found"
            else:
                message = "Remote connector request was rejected"
            raise ConnectorError(
                message,
                status_code=response.status_code,
                method=method.upper(),
                url=self._error_url(str(response.request.url)),
                code=code,
                response_received=True,
            )
        if response.status_code == 204 or not response.content:
            return None, response
        try:
            return response.json(), response
        except (ValueError, TypeError) as exc:
            raise ConnectorError(
                "Remote connector response was not valid JSON",
                status_code=response.status_code,
                method=method.upper(),
                url=self._error_url(str(response.request.url)),
                response_received=True,
            ) from exc
