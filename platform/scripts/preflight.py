"""Fail-closed deployment configuration checks.

This command validates the server environment without contacting external
services or printing secret values.  It is a preflight, not proof that the
deployment is healthy; run the container health checks and the operational
drills after it passes.

Examples:
    python -m scripts.preflight
    python -m scripts.preflight --backup
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


_REQUIRED = (
    "DB_PASSWORD",
    "QUEUE_PASSWORD",
    "ENCRYPTION_KEY",
    "BOOTSTRAP_TOKEN",
)
_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "replace-me",
    "your-",
    "example.com",
    "example.test",
)
_BACKUP_INTERVAL_DEFAULT = "86400"
_BACKUP_RETENTION_DEFAULT = "14"
_BACKUP_MAX_AGE_DEFAULT = "172800"


def _distinct_secret_errors(
    environ: Mapping[str, str],
    names: tuple[str, ...],
) -> list[str]:
    """Reject valid secret values reused across independent trust domains."""

    seen: dict[str, str] = {}
    errors: list[str] = []
    for name in names:
        value = environ.get(name, "")
        if _secret_error(name, value):
            continue
        previous = seen.get(value)
        if previous:
            errors.append(f"{name} must be different from {previous}")
        else:
            seen[value] = name
    return errors


def _secret_error(name: str, value: str) -> str | None:
    if not value.strip():
        return f"{name} is missing"
    if len(value.encode("utf-8")) < 32:
        return f"{name} must contain at least 32 bytes"
    lowered = value.casefold()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return f"{name} still contains a placeholder value"
    return None


def _public_url_error(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
    except ValueError:
        return "PUBLIC_URL is malformed"
    if parsed.scheme.casefold() != "https":
        return "PUBLIC_URL must use https in production"
    if not parsed.hostname or parsed.username or parsed.password:
        return "PUBLIC_URL must contain a host and no credentials"
    if parsed.query or parsed.fragment:
        return "PUBLIC_URL may not contain a query or fragment"
    if parsed.path not in {"", "/"}:
        return "PUBLIC_URL must be an origin without a path"
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "127.0.0.1", "::1", "example.com", "example.org", "example.net"}:
        return "PUBLIC_URL must not point to a local or placeholder host"
    if host.endswith((".localhost", ".test", ".invalid", ".example", ".example.com")):
        return "PUBLIC_URL still points to an example host"
    return None


def _normalized_hostname(value: str, *, address: bool = False) -> str | None:
    """Return a normalized host from a public URL or bare proxy address."""

    raw = value.strip()
    try:
        parsed = urlsplit(f"//{raw}" if address else raw)
        _ = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (address and parsed.path)
    ):
        return None
    return parsed.hostname.casefold().rstrip(".")


def _backup_key_error(value: str) -> str | None:
    error = _secret_error("BACKUP_KEY", value)
    if error:
        return error
    try:
        Fernet(value.encode("ascii"))
    except (TypeError, UnicodeError, ValueError):
        return "BACKUP_KEY must be a valid Fernet key"
    return None


def _backup_timing_errors(environ: Mapping[str, str]) -> list[str]:
    """Reject backup timing that cannot satisfy the archive health check."""

    errors: list[str] = []
    retention = environ.get("BACKUP_RETENTION_DAYS", _BACKUP_RETENTION_DEFAULT)
    if (
        not isinstance(retention, str)
        or not retention.isascii()
        or not retention.isdigit()
        or not any(character != "0" for character in retention)
    ):
        errors.append("BACKUP_RETENTION_DAYS must be a positive whole number")

    values: dict[str, float] = {}
    for name, default in (
        ("BACKUP_INTERVAL_SECONDS", _BACKUP_INTERVAL_DEFAULT),
        ("BACKUP_MAX_AGE_SECONDS", _BACKUP_MAX_AGE_DEFAULT),
    ):
        raw = environ.get(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a positive number")
            continue
        if not math.isfinite(value) or value <= 0:
            errors.append(f"{name} must be a positive number")
            continue
        values[name] = value

    interval = values.get("BACKUP_INTERVAL_SECONDS")
    max_age = values.get("BACKUP_MAX_AGE_SECONDS")
    if interval is not None and max_age is not None and max_age < interval * 2:
        errors.append(
            "BACKUP_MAX_AGE_SECONDS must be at least twice BACKUP_INTERVAL_SECONDS"
        )
    return errors


def _backup_mirror_errors(environ: Mapping[str, str]) -> list[str]:
    """Require the container and host sides of the backup mirror together."""

    directory_configured = bool(str(environ.get("BACKUP_MIRROR_DIRECTORY", "")).strip())
    host_path_configured = bool(str(environ.get("BACKUP_MIRROR_HOST_PATH", "")).strip())
    if directory_configured != host_path_configured:
        return [
            "BACKUP_MIRROR_DIRECTORY and BACKUP_MIRROR_HOST_PATH must be set together"
        ]
    return []


def validate_environment(
    environ: Mapping[str, str],
    *,
    backup: bool = False,
) -> list[str]:
    """Return human-readable configuration errors without exposing values."""

    errors: list[str] = []
    for name in _REQUIRED:
        error = _secret_error(name, environ.get(name, ""))
        if error:
            errors.append(error)

    secret_names = _REQUIRED

    public_url = environ.get("PUBLIC_URL", "")
    if not public_url:
        errors.append("PUBLIC_URL is missing")
    elif error := _public_url_error(public_url):
        errors.append(error)

    if environ.get("COOKIE_SECURE", "").casefold() != "true":
        errors.append("COOKIE_SECURE must be true in production")

    app_address = environ.get("APP_ADDRESS", "").strip()
    app_address_host = _normalized_hostname(app_address, address=True) if app_address else None
    if not app_address:
        errors.append("APP_ADDRESS is missing")
    elif app_address.casefold() in {"localhost", "localhost:80", ":80", "http://:80"}:
        errors.append("APP_ADDRESS must be the deployed public hostname")
    elif any(marker in app_address.casefold() for marker in ("example", ".test", ".invalid", ".localhost")):
        errors.append("APP_ADDRESS still points to an example host")
    elif app_address_host is None:
        errors.append("APP_ADDRESS must be the deployed public hostname")
    elif public_url and _public_url_error(public_url) is None:
        public_url_host = _normalized_hostname(public_url)
        if public_url_host is None or app_address_host != public_url_host:
            errors.append("APP_ADDRESS must match the PUBLIC_URL hostname")

    if backup:
        error = _backup_key_error(environ.get("BACKUP_KEY", ""))
        if error:
            errors.append(error)
        errors.extend(_backup_timing_errors(environ))
        errors.extend(_backup_mirror_errors(environ))
        secret_names += ("BACKUP_KEY",)

    errors.extend(_distinct_secret_errors(environ, secret_names))

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup",
        action="store_true",
        help="also require the encrypted-backup profile's BACKUP_KEY",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable status without secret values",
    )
    args = parser.parse_args(argv)
    errors = validate_environment(os.environ, backup=args.backup)
    result = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif errors:
        print("ForgeSEO deployment preflight failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("ForgeSEO deployment preflight passed; run Compose health checks next.")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
