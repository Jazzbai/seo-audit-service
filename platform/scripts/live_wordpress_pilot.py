"""Secret-safe operator probe for the standalone WordPress pilot.

The default probe performs authenticated, read-only REST checks only.  It
loads an origin and credentials exclusively from the explicit command-line
arguments or the environment variables named in ``build_parser``; it does not
read ForgeSEO settings, a database, a dotenv file, or stored connections.

The optional temporary-post roundtrip is deliberately guarded by two
independent command-line opt-ins.  It creates one uniquely marked post,
publishes it, restores that same post to draft, and never deletes it.  The
post remains as evidence for an operator to inspect.

Tests inject local transports or fake clients.  Nothing in this module makes a
network request at import time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from app.connectors.wordpress import WordPressClient


PROBE_NAME = "standalone_wordpress_pilot"
WORDPRESS_ORIGIN_ENV = "WORDPRESS_ORIGIN"
WORDPRESS_USERNAME_ENV = "WORDPRESS_USERNAME"
WORDPRESS_APPLICATION_PASSWORD_ENV = "WORDPRESS_APPLICATION_PASSWORD"
WORDPRESS_TOKEN_ENV = "WORDPRESS_TOKEN"

_POST_RESOURCE_KEY = re.compile(r"^post:[0-9]+$")
_NATIVE_CAPABILITY_NAMES = (
    "read",
    "create",
    "update",
    "publish",
    "pages",
    "media",
    "authors",
    "statuses",
)
_PLUGIN_NAMES = ("forgeseo", "yoast", "rank_math")


class ProbeConfigurationError(ValueError):
    """The operator did not provide a safe, complete probe configuration."""


class ProbeExecutionError(RuntimeError):
    """A read-only probe could not complete."""


def _explicit_value(
    cli_value: str | None,
    environment_name: str,
    environment: Mapping[str, str],
) -> str | None:
    """Return a value only when the caller supplied it explicitly."""

    if cli_value is not None:
        return cli_value
    return environment.get(environment_name)


def resolve_inputs(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Resolve only the explicit CLI/environment origin and credentials.

    CLI values take precedence over the matching named environment variable.
    No application settings or credential stores are consulted.
    """

    source = os.environ if environment is None else environment
    origin = _explicit_value(args.origin, WORDPRESS_ORIGIN_ENV, source)
    username = _explicit_value(args.username, WORDPRESS_USERNAME_ENV, source)
    application_password = _explicit_value(
        args.application_password,
        WORDPRESS_APPLICATION_PASSWORD_ENV,
        source,
    )
    token = _explicit_value(args.token, WORDPRESS_TOKEN_ENV, source)

    if origin is None or not origin.strip():
        raise ProbeConfigurationError(
            f"origin is required via --origin or {WORDPRESS_ORIGIN_ENV}"
        )

    if token is not None and not token.strip():
        token = None
    if username is not None and not username.strip():
        username = None
    if application_password is not None and not application_password:
        application_password = None

    if token is not None and (username is not None or application_password is not None):
        raise ProbeConfigurationError(
            "provide either --token or username/application-password credentials, not both"
        )
    if token is not None:
        return origin.strip(), {"token": token}
    if username is None or application_password is None:
        raise ProbeConfigurationError(
            "credentials are required via --username and --application-password "
            f"or --token (with the matching explicit environment variables)"
        )
    return origin.strip(), {
        "username": username,
        "application_password": application_password,
    }


def validate_write_opt_ins(
    *,
    allow_live_write: bool,
    confirm_live_write: bool,
) -> None:
    """Require both independent opt-ins before any write-capable path starts."""

    if allow_live_write == confirm_live_write:
        return
    raise ProbeConfigurationError(
        "the temporary-post roundtrip requires both --allow-live-write and "
        "--confirm-live-write; no live call was started"
    )


def _redact_text(value: str, secrets: Sequence[str]) -> str:
    """Remove explicitly supplied credential values from an error string."""

    redacted = value
    for secret in sorted(
        {candidate for candidate in secrets if candidate},
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _safe_error(error: BaseException, secrets: Sequence[str] = ()) -> dict[str, Any]:
    """Return an error shape that cannot echo supplied credentials."""

    message = _redact_text(str(error), secrets)
    result: dict[str, Any] = {
        "type": type(error).__name__,
        "message": message or type(error).__name__,
    }
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        result["status_code"] = status_code
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        result["code"] = _redact_text(code, secrets)
    return result


def _redact_value(value: Any, secrets: Sequence[str]) -> Any:
    """Recursively redact supplied credentials from a report value."""

    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            _redact_text(str(key), secrets): _redact_value(child, secrets)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(child, secrets) for child in value]
    return value


def _safe_capabilities(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Keep capability evidence while excluding raw remote documents."""

    native_value = capabilities.get("native")
    native = {
        name: bool(native_value.get(name) is True)
        for name in _NATIVE_CAPABILITY_NAMES
    } if isinstance(native_value, Mapping) else {
        name: False for name in _NATIVE_CAPABILITY_NAMES
    }

    editorial_value = capabilities.get("editorial")
    editorial: dict[str, Any] = {
        "read": False,
        "write": False,
        "writable_fields": [],
        "conditionally_writable_fields": [],
    }
    if isinstance(editorial_value, Mapping):
        editorial["read"] = editorial_value.get("read") is True
        editorial["write"] = editorial_value.get("write") is True
        for field_name in ("writable_fields", "conditionally_writable_fields"):
            values = editorial_value.get(field_name)
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                editorial[field_name] = sorted(
                    str(value) for value in values if isinstance(value, str)
                )

    resource_types: list[dict[str, Any]] = []
    raw_resource_types = capabilities.get("resource_types")
    if isinstance(raw_resource_types, Sequence) and not isinstance(
        raw_resource_types, (str, bytes)
    ):
        for raw in raw_resource_types:
            if not isinstance(raw, Mapping):
                continue
            collection = raw.get("collection")
            item = raw.get("item")
            editorial_write = raw.get("editorial_write")
            resource_types.append(
                {
                    "key": str(raw.get("key") or "unknown"),
                    "rest_base": str(raw.get("rest_base") or "unknown"),
                    "viewable": raw.get("viewable") is True,
                    "inventoryable": raw.get("inventoryable") is True,
                    "collection_read": bool(
                        isinstance(collection, Mapping)
                        and collection.get("read") is True
                    ),
                    "item_read": bool(
                        isinstance(item, Mapping) and item.get("read") is True
                    ),
                    "editorial_write": {
                        "supported": bool(
                            isinstance(editorial_write, Mapping)
                            and editorial_write.get("supported") is True
                        ),
                        "automatic": bool(
                            isinstance(editorial_write, Mapping)
                            and editorial_write.get("automatic") is True
                        ),
                    },
                }
            )

    plugins: dict[str, dict[str, Any]] = {}
    raw_plugins = capabilities.get("plugins")
    for name in _PLUGIN_NAMES:
        raw_plugin = raw_plugins.get(name) if isinstance(raw_plugins, Mapping) else None
        plugin: dict[str, Any] = {
            "detected": bool(
                isinstance(raw_plugin, Mapping) and raw_plugin.get("detected") is True
            ),
            "read": bool(
                isinstance(raw_plugin, Mapping) and raw_plugin.get("read") is True
            ),
            "write": bool(
                isinstance(raw_plugin, Mapping) and raw_plugin.get("write") is True
            ),
        }
        if isinstance(raw_plugin, Mapping):
            for field_name in ("operation_mapping", "webhooks"):
                if field_name in raw_plugin:
                    plugin[field_name] = raw_plugin.get(field_name) is True
            fields = raw_plugin.get("writable_fields")
            if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)):
                plugin["writable_fields"] = sorted(
                    str(field) for field in fields if isinstance(field, str)
                )
        plugins[name] = plugin

    authenticated_user: dict[str, str] | None = None
    raw_user = capabilities.get("authenticated_author")
    if isinstance(raw_user, Mapping):
        user_id = raw_user.get("id")
        user_name = raw_user.get("name")
        if user_id is not None or user_name is not None:
            authenticated_user = {
                "id": str(user_id) if user_id is not None else "",
                "name": str(user_name) if user_name is not None else "",
            }

    result: dict[str, Any] = {
        "authenticated": capabilities.get("authenticated") is True,
        "rest_api": capabilities.get("rest_api") is True,
        "wp_v2": capabilities.get("wp_v2") is True,
        "native": native,
        "editorial": editorial,
        "resource_types": resource_types,
        "plugins": plugins,
        "seo": {
            "provider": (
                str(capabilities.get("seo", {}).get("provider"))
                if isinstance(capabilities.get("seo"), Mapping)
                and capabilities.get("seo", {}).get("provider") is not None
                else None
            ),
            "read": bool(
                isinstance(capabilities.get("seo"), Mapping)
                and capabilities.get("seo", {}).get("read") is True
            ),
            "write": bool(
                isinstance(capabilities.get("seo"), Mapping)
                and capabilities.get("seo", {}).get("write") is True
            ),
        },
        "permissions_verified": bool(capabilities.get("authenticated") is True),
    }
    if authenticated_user is not None:
        result["authenticated_user"] = authenticated_user
    return result


def _inventory_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count inventory without returning titles, bodies, URLs, or metadata."""

    by_resource_type: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    for record in records:
        resource_type = record.get("resource_type")
        status = record.get("status")
        by_resource_type[str(resource_type or "unknown")] += 1
        by_status[str(status or "unknown")] += 1
    return {
        "total": len(records),
        "by_resource_type": dict(sorted(by_resource_type.items())),
        "by_status": dict(sorted(by_status.items())),
    }


def _new_write_evidence() -> dict[str, Any]:
    return {
        "status": "not_requested",
        "attempted": False,
        "mutations": [],
        "target_scope": "none",
        "deletion_attempted": False,
        "deletion_performed": False,
        "evidence_preserved": True,
    }


def _valid_post_resource_key(value: Any) -> bool:
    return isinstance(value, str) and _POST_RESOURCE_KEY.fullmatch(value) is not None


async def _restore_created_post_if_safe(
    client: Any,
    *,
    created: Mapping[str, Any],
    resource_key: str,
    evidence: dict[str, Any],
    secrets: Sequence[str],
    restore_already_attempted: bool,
) -> bool:
    """Leave a created post in draft after an uncertain write, fail closed."""

    if restore_already_attempted:
        return False
    try:
        current = await client.read(resource_key)
        evidence["steps"].append(
            {"operation": "read_created_post_after_error", "result": "succeeded"}
        )
        current_status = str(current.get("status") or "unknown")
        evidence["final_status_observed"] = current_status
        if current_status == "draft":
            evidence["left_in_draft"] = True
            return True
        if not hasattr(client, "matches_snapshot") or not client.matches_snapshot(
            current, created, ignore_status=True
        ):
            evidence["restoration"] = "refused_source_changed"
            evidence["status"] = "needs_reconciliation"
            return False
        await client.restore(
            resource_key,
            dict(created),
            expected_hash=current.get("source_hash"),
            operation_key=evidence["operation_keys"]["restore"],
        )
        evidence["steps"].append(
            {"operation": "restore_to_draft", "result": "succeeded"}
        )
        evidence["restored_to_draft"] = True
        evidence["final_status_observed"] = "draft"
        return True
    except Exception as error:  # pragma: no cover - exercised by live failures
        evidence["restoration_error"] = _safe_error(error, secrets)
        evidence["status"] = "needs_reconciliation"
        return False


async def _run_temporary_post_roundtrip(
    client: Any,
    *,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    """Create, publish, and restore one uniquely marked post, never delete."""

    probe_id = uuid4().hex
    marker = f"FORGESEO_LIVE_PROBE::{probe_id}"
    operation_prefix = f"forge-seo-live-probe:{probe_id}"
    evidence: dict[str, Any] = {
        "status": "started",
        "attempted": False,
        "mutations": [],
        "target_scope": "one newly created post only",
        "marker": marker,
        "created_resource_key": None,
        "created_status": None,
        "published_status": None,
        "final_status_observed": None,
        "restored_to_draft": False,
        "left_in_draft": False,
        "deletion_attempted": False,
        "deletion_performed": False,
        "evidence_preserved": True,
        "steps": [],
        "operation_keys": {
            "create": f"{operation_prefix}:create",
            "publish": f"{operation_prefix}:publish",
            "restore": f"{operation_prefix}:restore",
        },
    }
    created: Mapping[str, Any] | None = None
    resource_key: str | None = None
    restore_attempted = False

    article = {
        "title": f"ForgeSEO live probe [{marker}]",
        "body": (
            f"<p>This temporary ForgeSEO operator probe is uniquely marked "
            f"{marker}. It is retained as draft evidence after the roundtrip.</p>"
        ),
    }

    try:
        evidence["attempted"] = True
        created = await client.create_draft(
            article,
            operation_key=evidence["operation_keys"]["create"],
        )
        evidence["mutations"].append("create_draft")
        evidence["steps"].append(
            {"operation": "create_draft", "result": "succeeded"}
        )
        resource_key = created.get("resource_key") if isinstance(created, Mapping) else None
        if not _valid_post_resource_key(resource_key):
            raise ProbeExecutionError(
                "temporary post creation did not return a safe post resource key"
            )
        evidence["created_resource_key"] = resource_key
        evidence["created_status"] = str(created.get("status") or "unknown")
        if evidence["created_status"] != "draft":
            raise ProbeExecutionError(
                "temporary post creation did not return a draft; publication was refused"
            )
        if marker not in str(created.get("title") or ""):
            raise ProbeExecutionError(
                "temporary post response did not retain its unique marker"
            )

        published = await client.publish(
            resource_key,
            expected_hash=created.get("source_hash"),
            operation_key=evidence["operation_keys"]["publish"],
        )
        evidence["mutations"].append("publish")
        evidence["steps"].append(
            {"operation": "publish", "result": "succeeded"}
        )
        evidence["published_status"] = str(published.get("status") or "unknown")

        restore_attempted = True
        restored = await client.restore(
            resource_key,
            dict(created),
            expected_hash=published.get("source_hash"),
            operation_key=evidence["operation_keys"]["restore"],
        )
        evidence["mutations"].append("restore_to_draft")
        evidence["steps"].append(
            {"operation": "restore_to_draft", "result": "succeeded"}
        )
        evidence["final_status_observed"] = str(
            restored.get("status") or "unknown"
        )
        evidence["restored_to_draft"] = evidence["final_status_observed"] == "draft"
        evidence["status"] = (
            "passed" if evidence["restored_to_draft"] else "needs_reconciliation"
        )
        if not evidence["restored_to_draft"]:
            evidence["restoration"] = "restore_response_was_not_draft"
        return evidence
    except Exception as error:
        evidence["error"] = _safe_error(error, secrets)
        if created is not None and resource_key is not None:
            await _restore_created_post_if_safe(
                client,
                created=created,
                resource_key=resource_key,
                evidence=evidence,
                secrets=secrets,
                restore_already_attempted=restore_attempted,
            )
        if evidence["status"] == "started":
            evidence["status"] = "failed"
        return evidence
    finally:
        # Operation keys are useful local evidence but must never be mistaken
        # for credentials.  Keep them in the report only for write auditability.
        evidence.pop("operation_keys", None)


def _base_report(*, mode: str, origin: str | None) -> dict[str, Any]:
    return {
        "probe": PROBE_NAME,
        "result": "FAIL",
        "mode": mode,
        "origin": origin,
        "read_only_checks": {
            "status": "not_started",
            "authenticated_capability": None,
            "inventory_counts": None,
        },
        "live_write_evidence": _new_write_evidence(),
    }


async def run_probe(
    origin: str,
    credentials: Mapping[str, object],
    *,
    allow_live_write: bool = False,
    confirm_live_write: bool = False,
    transport: Any = None,
    client_factory: Callable[..., Any] = WordPressClient,
) -> dict[str, Any]:
    """Run the probe and return only secret-safe evidence.

    ``transport`` and ``client_factory`` are dependency-injection points for
    offline tests.  The real CLI leaves both unset, so the connector performs
    its normal public-origin and same-authority checks.
    """

    validate_write_opt_ins(
        allow_live_write=allow_live_write,
        confirm_live_write=confirm_live_write,
    )
    mode = "temporary_post_roundtrip" if allow_live_write else "read_only"
    constructor_kwargs = {"transport": transport} if transport is not None else {}
    client = client_factory(origin, dict(credentials), **constructor_kwargs)
    secrets = [str(value) for value in credentials.values() if value is not None]

    async with client:
        report = _base_report(mode=mode, origin=str(client.origin))
        try:
            capabilities = await client.validate_connection()
            inventory = await client.inventory()
        except Exception as error:
            report["read_only_checks"]["status"] = "failed"
            report["error"] = _safe_error(error, secrets)
            raise ProbeExecutionError(
                json.dumps(report, sort_keys=True, ensure_ascii=False)
            ) from error

        report["read_only_checks"] = {
            "status": "passed",
            "authenticated_capability": _safe_capabilities(capabilities),
            "inventory_counts": _inventory_counts(inventory),
        }

        if allow_live_write:
            report["live_write_evidence"] = await _run_temporary_post_roundtrip(
                client,
                secrets=secrets,
            )
            report["result"] = (
                "PASS"
                if report["live_write_evidence"]["status"] == "passed"
                else "FAIL"
            )
        else:
            report["result"] = "PASS"
        return _redact_value(report, secrets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a secret-safe standalone WordPress pilot probe. "
            "Default mode performs authenticated GET-only checks."
        )
    )
    parser.add_argument(
        "--origin",
        help=f"WordPress origin; otherwise use {WORDPRESS_ORIGIN_ENV}",
    )
    parser.add_argument(
        "--username",
        help=f"WordPress username; otherwise use {WORDPRESS_USERNAME_ENV}",
    )
    parser.add_argument(
        "--application-password",
        help=(
            "WordPress application password; otherwise use "
            f"{WORDPRESS_APPLICATION_PASSWORD_ENV}"
        ),
    )
    parser.add_argument(
        "--token",
        help=f"WordPress bearer token; otherwise use {WORDPRESS_TOKEN_ENV}",
    )
    parser.add_argument(
        "--allow-live-write",
        "--enable-temporary-post",
        "--write-probe",
        dest="allow_live_write",
        action="store_true",
        help="first opt-in: allow the temporary-post write probe",
    )
    parser.add_argument(
        "--confirm-live-write",
        "--confirm-temporary-post",
        "--confirm-write-probe",
        dest="confirm_live_write",
        action="store_true",
        help="second opt-in: confirm creation, publication, and draft restoration",
    )
    return parser


def _failure_report(
    error: BaseException,
    *,
    allow_live_write: bool = False,
    confirm_live_write: bool = False,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    mode = (
        "temporary_post_roundtrip"
        if allow_live_write and confirm_live_write
        else "read_only"
    )
    report = _base_report(mode=mode, origin=None)
    report["error"] = _safe_error(error, secrets)
    if allow_live_write != confirm_live_write:
        report["live_write_evidence"] = {
            **_new_write_evidence(),
            "status": "refused",
            "refusal": (
                "both independent write opt-ins are required; no live mutation "
                "was attempted"
            ),
        }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    secrets: list[str] = []
    try:
        validate_write_opt_ins(
            allow_live_write=args.allow_live_write,
            confirm_live_write=args.confirm_live_write,
        )
        origin, credentials = resolve_inputs(args)
        secrets = [str(value) for value in credentials.values()]
        report = asyncio.run(
            run_probe(
                origin,
                credentials,
                allow_live_write=args.allow_live_write,
                confirm_live_write=args.confirm_live_write,
            )
        )
    except ProbeExecutionError as error:
        try:
            report = json.loads(str(error))
        except (TypeError, ValueError):  # pragma: no cover - defensive fallback
            report = _failure_report(error, secrets=secrets)
    except Exception as error:
        report = _failure_report(
            error,
            allow_live_write=args.allow_live_write,
            confirm_live_write=args.confirm_live_write,
            secrets=secrets,
        )

    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
