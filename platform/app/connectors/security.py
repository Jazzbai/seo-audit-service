"""Reusable connector security helpers.

No environment or credential files are read here.  Callers provide the
master key and credential payload explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Mapping
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .errors import ConnectorError, SSRFError


_PRIVATE_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata",
}
_NUMERIC_HOST_RE = re.compile(r"^[0-9a-fA-FxX.]+$")


def _numeric_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Recognize ordinary and integer-form IP literals."""

    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass

    if not _NUMERIC_HOST_RE.fullmatch(host):
        return None
    try:
        if host.lower().startswith("0x"):
            value = int(host, 16)
            if value <= 0xFFFFFFFF:
                return ipaddress.ip_address(value)
        if host.isdigit():
            value = int(host, 10)
            if value <= 0xFFFFFFFF:
                return ipaddress.ip_address(value)
    except ValueError:
        return None
    return None


def _is_private_address(address: ipaddress._BaseAddress) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _check_host(host: str, *, resolve_dns: bool) -> None:
    normalized = host.rstrip(".").lower()
    if not normalized or normalized in _PRIVATE_HOSTNAMES:
        raise SSRFError("Private or local hostnames are not allowed")
    if normalized.endswith((".localhost", ".local", ".internal")):
        raise SSRFError("Private or local hostnames are not allowed")

    literal = _numeric_ip(normalized)
    if literal is not None:
        if _is_private_address(literal):
            raise SSRFError("Private or reserved IP addresses are not allowed")
        return

    if not resolve_dns:
        return

    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                normalized,
                None,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise SSRFError("The public origin could not be resolved") from exc

    if not addresses:
        raise SSRFError("The public origin could not be resolved")
    for address_text in addresses:
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:  # pragma: no cover - socket normally returns IPs
            raise SSRFError("The public origin resolved to an invalid address") from exc
        if _is_private_address(address):
            raise SSRFError("The public origin resolves to a private address")


def _normalized_url_parts(url: str) -> tuple[SplitResult, str]:
    if not isinstance(url, str) or not url.strip():
        raise SSRFError("A non-empty HTTP origin is required")
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SSRFError("The URL is malformed") from exc

    if scheme not in {"http", "https"}:
        raise SSRFError("Only http and https URLs are allowed")
    if not host or parsed.username is not None or parsed.password is not None:
        raise SSRFError("URLs may not contain credentials and must include a host")
    if parsed.query or parsed.fragment:
        raise SSRFError("An origin may not contain a query or fragment")
    _ = port  # Accessing it above validates malformed ports.
    host_ascii = host.encode("idna").decode("ascii").lower().rstrip(".")
    netloc = host_ascii
    if ":" in host_ascii and not host_ascii.startswith("["):
        netloc = f"[{host_ascii}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    normalized_path = parsed.path.rstrip("/")
    normalized = urlunsplit((scheme, netloc, normalized_path, "", ""))
    return parsed, normalized


def validate_public_url(
    url: str,
    *,
    resolve_dns: bool = False,
) -> str:
    """Validate and normalize a public HTTP(S) URL.

    DNS resolution is opt-in so test transports can use reserved test domains
    such as ``example.test`` without making a real DNS request.  Connector
    instances enable it automatically when no mock transport is supplied.
    """

    parsed, normalized = _normalized_url_parts(url)
    _check_host(parsed.hostname or "", resolve_dns=resolve_dns)
    return normalized


def validate_public_origin(url: str, *, resolve_dns: bool = False) -> str:
    """Alias used by connector constructors and future intelligence clients."""

    return validate_public_url(url, resolve_dns=resolve_dns)


def assert_public_url(url: str, *, resolve_dns: bool = False) -> str:
    """Compatibility alias for code that uses assertion-style naming."""

    return validate_public_url(url, resolve_dns=resolve_dns)


def _authority(parts: SplitResult) -> tuple[str, str, int]:
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    return parts.scheme.lower(), host, port


def validate_redirect(
    current_url: str,
    location: str,
    *,
    resolve_dns: bool = False,
) -> str:
    """Resolve a redirect and require the same safe authority.

    Same-authority redirects are useful for installations in a subdirectory;
    cross-authority redirects are rejected so a remote site cannot turn a
    connector request into an SSRF primitive.
    """

    target = urljoin(current_url, location)
    normalized = validate_public_url(target, resolve_dns=resolve_dns)
    current = urlsplit(current_url)
    destination = urlsplit(normalized)
    if _authority(current) != _authority(destination):
        raise SSRFError("Redirects may not change authority")
    if current.scheme.lower() == "https" and destination.scheme.lower() != "https":
        raise SSRFError("HTTPS requests may not be downgraded by redirect")
    return normalized


async def safe_get(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
    max_redirects: int = 3,
) -> httpx.Response:
    """Fetch a public URL for availability/HTML inspection.

    This helper intentionally supports GET only and does not retry.  A custom
    ``httpx`` transport is accepted for isolated tests; with no custom
    transport DNS answers are checked before each request.
    """

    resolve_dns = transport is None
    current = validate_public_url(url, resolve_dns=resolve_dns)
    if transport is None:
        from app.network import PublicTransport

        transport = PublicTransport(current)
    client = httpx.AsyncClient(
        transport=transport,
        headers={"Accept": "text/html,application/xhtml+json;q=0.9,*/*;q=0.1", **dict(headers or {})},
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    )
    try:
        for redirect_count in range(max_redirects + 1):
            try:
                response = await client.get(current)
            except httpx.HTTPError as exc:
                raise ConnectorError(
                    "Public URL request failed before a response was received",
                    method="GET",
                    url=f"{urlsplit(current).scheme}://{urlsplit(current).netloc}{urlsplit(current).path}",
                    transport_error=True,
                ) from exc
            if response.status_code < 300 or response.status_code >= 400:
                return response
            location = response.headers.get("location")
            if not location:
                raise ConnectorError(
                    "Public URL redirect did not provide a location",
                    status_code=response.status_code,
                    method="GET",
                    response_received=True,
                )
            current = validate_redirect(
                str(response.request.url),
                location,
                resolve_dns=resolve_dns,
            )
            if redirect_count == max_redirects:
                raise ConnectorError("Too many public URL redirects")
        raise ConnectorError("Public URL request could not be completed")  # pragma: no cover
    finally:
        await client.aclose()


def _fernet_key(master_key: bytes | str) -> bytes:
    if isinstance(master_key, str):
        raw = master_key.encode("utf-8")
    elif isinstance(master_key, bytes):
        raw = master_key
    else:
        raise TypeError("master_key must be bytes or str")

    if len(raw) < 32:
        raise ValueError("master_key must contain at least 32 bytes")
    candidate = raw
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception:
        decoded = b""
    if len(decoded) != 32:
        candidate = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    else:
        candidate = raw
    return candidate


def encrypt_credentials(payload: Mapping[str, object], master_key: bytes | str, /) -> str:
    """Encrypt a JSON credential mapping with the explicit positional key."""

    if not isinstance(payload, Mapping):
        raise TypeError("credential payload must be a mapping")
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError("credential payload must be JSON serializable") from exc
    return Fernet(_fernet_key(master_key)).encrypt(encoded).decode("ascii")


def decrypt_credentials(ciphertext: str | bytes, master_key: bytes | str, /) -> dict[str, object]:
    """Decrypt a credential mapping with the explicit positional key."""

    if isinstance(ciphertext, str):
        token = ciphertext.encode("ascii")
    elif isinstance(ciphertext, bytes):
        token = ciphertext
    else:
        raise TypeError("ciphertext must be str or bytes")
    try:
        decoded = Fernet(_fernet_key(master_key)).decrypt(token)
        value = json.loads(decoded.decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConnectorError("Credential ciphertext could not be decrypted") from exc
    if not isinstance(value, dict):
        raise ConnectorError("Credential ciphertext did not contain a mapping")
    return value
