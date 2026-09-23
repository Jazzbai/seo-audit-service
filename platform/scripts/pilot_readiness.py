"""Produce a secret-safe readiness report for an isolated ForgeSEO pilot.

The command is intentionally read-only.  By default it validates only the
server configuration and records the external gates that still need evidence.
Optional health, Compose, and backup probes can be enabled when an operator
has a running deployment.  No provider, WordPress, or publishing operation is
performed by this command.

Examples::

    python -m scripts.pilot_readiness --env-file .env
    python -m scripts.pilot_readiness --env-file .env --health-url http://127.0.0.1:18080/health --compose
    python -m scripts.pilot_readiness --env-file .env --compose --backup
    python -m scripts.pilot_readiness --env-file .env --storage-path /srv --storage-min-free-bytes 1073741824

The command returns 0 only when every requested check passes and no external
or unattended-pilot gate remains outstanding.  A non-zero result is expected
until a real deployment has completed those gates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scripts.backup_healthcheck import inspect_latest_backup
from scripts.preflight import validate_environment


ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_COMPOSE_SERVICES = {
    "api",
    "beat",
    "browser",
    "db",
    "queue",
    "scheduler-worker",
    "web",
    "worker",
}
_BACKUP_COMPOSE_SERVICE = "backup"
_MIGRATION_COMPOSE_SERVICE = "migrate"
_BACKUP_INIT_COMPOSE_SERVICE = "backup-init"
_COMPOSE_OWNER_SERVICES = frozenset({"beat", "scheduler-worker"})
_EXPECTED_COMPOSE_PROJECT = "forgeseo-platform"
_EXPECTED_COMPOSE_FILE = "compose.yaml"
DEFAULT_STORAGE_MIN_FREE_BYTES = 1024 * 1024 * 1024


def _check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    result.update(extra)
    return result


def load_env_file(path: str | Path) -> dict[str, str]:
    """Load simple KEY=VALUE entries without expanding or printing secrets."""

    env_path = Path(path)
    if env_path.is_symlink() or not env_path.is_file():
        raise RuntimeError("environment file is unavailable")
    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise RuntimeError("environment file cannot be read") from None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _merged_environment(env_file: str | Path | None) -> tuple[dict[str, str], str | None]:
    values: dict[str, str] = {}
    file_error: str | None = None
    if env_file:
        try:
            values.update(load_env_file(env_file))
        except RuntimeError as exc:
            file_error = str(exc)
    # Process variables intentionally win, matching normal Compose behavior.
    values.update(os.environ)
    return values, file_error


def _environment_check(environ: Mapping[str, str], *, backup: bool, file_error: str | None) -> dict[str, Any]:
    if file_error:
        return _check("server_configuration", "fail", file_error)
    errors = validate_environment(environ, backup=backup)
    if errors:
        return _check(
            "server_configuration",
            "fail",
            "production preflight failed",
            errors=errors,
        )
    return _check("server_configuration", "pass", "production preflight passed")


def _safe_host(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return "configured target"
    if parsed.hostname:
        host = parsed.hostname
        if port:
            host = f"{host}:{port}"
        return host
    return "configured target"


def health_origin_matches(url: str, expected_origin: str | None) -> bool:
    """Return whether a health URL belongs to the configured public origin."""

    if not str(expected_origin or "").strip():
        return True
    try:
        actual = urlsplit(url)
        expected = urlsplit(str(expected_origin).strip())
        actual_port = actual.port
        expected_port = expected.port
    except (TypeError, ValueError):
        return False
    if actual_port is None:
        actual_port = 443 if actual.scheme.casefold() == "https" else 80
    if expected_port is None:
        expected_port = 443 if expected.scheme.casefold() == "https" else 80
    return (
        actual.scheme.casefold() == expected.scheme.casefold()
        and actual.hostname is not None
        and expected.hostname is not None
        and actual.hostname.casefold().rstrip(".") == expected.hostname.casefold().rstrip(".")
        and actual_port == expected_port
    )


def probe_health(
    url: str,
    *,
    opener=urlopen,
    require_https: bool = False,
    expected_origin: str | None = None,
) -> dict[str, Any]:
    """GET the public health contract without returning response contents.

    ``require_https`` is reserved for production handoff probes.  The default
    remains transport-agnostic so an operator can use an explicitly local HTTP
    endpoint for diagnostics.
    """

    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return _check("public_health", "fail", "health URL is malformed", target=_safe_host(url))
        if require_https and parsed.scheme != "https":
            return _check(
                "public_health",
                "fail",
                "health URL must use HTTPS for production handoff",
                target=_safe_host(url),
            )
        if not health_origin_matches(url, expected_origin):
            return _check(
                "public_health",
                "fail",
                "health URL does not match the configured public origin",
                target=_safe_host(url),
            )
        try:
            _ = parsed.port
        except ValueError:
            return _check("public_health", "fail", "health URL is malformed", target=_safe_host(url))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return _check(
                "public_health",
                "fail",
                "health URL must not contain credentials or query data",
                target=_safe_host(url),
            )
        request = Request(url, headers={"Accept": "application/json"})
        with opener(request, timeout=8) as response:
            if require_https or expected_origin:
                final_url = response.geturl() if callable(getattr(response, "geturl", None)) else ""
                try:
                    final_parsed = urlsplit(final_url)
                except (TypeError, ValueError):
                    final_parsed = None
                if require_https and (
                    final_parsed is None or final_parsed.scheme.casefold() != "https"
                ):
                    return _check(
                        "public_health",
                        "fail",
                        "health endpoint did not remain on HTTPS",
                        target=_safe_host(url),
                    )
                if expected_origin and (
                    final_parsed is None
                    or not health_origin_matches(final_url, expected_origin)
                ):
                    return _check(
                        "public_health",
                        "fail",
                        "health endpoint did not remain on the configured public origin",
                        target=_safe_host(url),
                    )
            body = json.loads(response.read().decode("utf-8"))
            # A syntactically valid JSON scalar or array is still a broken
            # health contract.  Check its shape before reading the status so
            # readiness fails closed instead of crashing with AttributeError.
            healthy = (
                response.status == 200
                and isinstance(body, Mapping)
                and body.get("status") == "ok"
            )
    except HTTPError as exc:
        return _check("public_health", "fail", f"health endpoint returned HTTP {exc.code}", target=_safe_host(url))
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return _check("public_health", "fail", f"health endpoint unavailable ({type(exc).__name__})", target=_safe_host(url))
    if not healthy:
        return _check("public_health", "fail", "health endpoint did not return status ok", target=_safe_host(url))
    return _check("public_health", "pass", "public health endpoint returned status ok", target=_safe_host(url))


def _compose_rows(raw: str) -> list[dict[str, Any]]:
    """Parse Docker Compose's JSON-array or one-object-per-line output."""

    text = raw.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]
    return [decoded] if isinstance(decoded, dict) else []


def summarize_compose(rows: list[Mapping[str, Any]], *, backup: bool = False) -> dict[str, Any]:
    """Summarize service health without exposing Compose output or env values."""

    names: set[str] = set()
    not_running: list[str] = []
    unhealthy: list[str] = []
    for row in rows:
        service = row.get("Service") or row.get("service") or row.get("Name") or row.get("name")
        if not isinstance(service, str) or not service:
            continue
        names.add(service)
        state = str(row.get("State") or row.get("state") or "").strip().casefold()
        if state != "running":
            not_running.append(service)
        state = " ".join(
            str(row.get(key, "")) for key in ("State", "state", "Health", "health", "Status", "status")
        ).casefold()
        health = str(row.get("Health") or row.get("health") or "").strip().casefold()
        if health and health != "healthy":
            unhealthy.append(service)
        if (
            ("unhealthy" in state or "restarting" in state or "dead" in state)
            and service not in unhealthy
        ):
            unhealthy.append(service)
    expected_services = set(_EXPECTED_COMPOSE_SERVICES)
    if backup:
        expected_services.add(_BACKUP_COMPOSE_SERVICE)
    missing = sorted(expected_services - names)
    if not rows:
        return _check("compose_services", "fail", "Compose returned no service records", service_count=0)
    if missing:
        return _check(
            "compose_services",
            "fail",
            "required Compose services are missing",
            service_count=len(names),
            missing=missing,
        )
    if not_running:
        return _check(
            "compose_services",
            "fail",
            "required Compose services are not running",
            service_count=len(names),
            not_running=sorted(set(not_running)),
        )
    if unhealthy:
        return _check(
            "compose_services",
            "fail",
            "Compose reported unhealthy services",
            service_count=len(names),
            unhealthy=sorted(set(unhealthy)),
        )
    return _check("compose_services", "pass", "required Compose services are present and not unhealthy", service_count=len(names))


def probe_compose(
    *,
    env_file: str | Path | None = None,
    root: Path = ROOT,
    backup: bool = False,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Read service state using ``docker compose ps``; never start or stop it."""

    docker = shutil.which("docker")
    if not docker:
        return _check("compose_services", "not_verified", "Docker CLI is not installed")
    command = [docker, "compose", "--project-directory", str(root)]
    if env_file:
        command.extend(["--env-file", str(Path(env_file).resolve())])
    if backup:
        # Profile services are otherwise omitted from Compose's status model.
        command.extend(["--profile", "backup"])
    command.extend(["ps", "--format", "json"])
    try:
        result = runner(command, cwd=root, capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return _check("compose_services", "fail", "Docker Compose status command timed out")
    except OSError:
        return _check("compose_services", "fail", "Docker CLI could not be started")
    except subprocess.SubprocessError:
        return _check("compose_services", "fail", "Docker Compose status could not be read")
    if result.returncode != 0:
        # Keep daemon diagnostics useful without echoing stderr, which may
        # contain paths, project names, or environment-derived values.
        diagnostic = str(result.stderr or "").casefold()
        if any(marker in diagnostic for marker in ("500", "502", "daemon", "engine")):
            return _check(
                "compose_services",
                "fail",
                "Docker engine did not accept the Compose status request",
            )
        return _check("compose_services", "fail", "Docker Compose status command failed")
    return summarize_compose(_compose_rows(result.stdout), backup=backup)


def _compose_service(row: Mapping[str, Any]) -> str:
    service = row.get("Service") or row.get("service") or row.get("Name") or row.get("name")
    return service.strip() if isinstance(service, str) else ""


def _compose_exit_code(row: Mapping[str, Any], status: str) -> int | None:
    """Read a one-shot exit code from Compose JSON without trusting raw output."""

    raw_code: Any = None
    for key in ("ExitCode", "exit_code", "Exitcode", "exitCode"):
        if key in row and row[key] is not None:
            raw_code = row[key]
            break
    if raw_code is not None and not isinstance(raw_code, bool):
        if isinstance(raw_code, int):
            return raw_code
        if isinstance(raw_code, str) and re.fullmatch(r"[+-]?\d+", raw_code.strip()):
            return int(raw_code.strip())

    match = re.search(r"\bexited\s*\(\s*([+-]?\d+)\s*\)", status, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _compose_prerequisite_state(row: Mapping[str, Any]) -> tuple[str, int | None]:
    """Normalize Compose state while requiring explicit completion evidence."""

    raw_state = row.get("State") or row.get("state")
    raw_status = row.get("Status") or row.get("status")
    state = str(raw_state or "").strip().casefold()
    status = str(raw_status or "").strip().casefold()
    if not state:
        if re.search(r"\bup\b", status):
            state = "running"
        elif re.search(r"\bexited\b", status):
            state = "exited"
        else:
            state = "unknown"
    return state, _compose_exit_code(row, status)


def summarize_compose_prerequisites(
    rows: list[Mapping[str, Any]],
    *,
    backup: bool = False,
) -> dict[str, Any]:
    """Require successful completion of Compose's one-shot prerequisite services."""

    required = [_MIGRATION_COMPOSE_SERVICE]
    if backup:
        required.append(_BACKUP_INIT_COMPOSE_SERVICE)

    matches: dict[str, list[Mapping[str, Any]]] = {service: [] for service in required}
    for row in rows:
        service = _compose_service(row)
        if service in matches:
            matches[service].append(row)

    missing = [service for service in required if not matches[service]]
    if missing:
        return _check(
            "compose_prerequisites",
            "fail",
            "required one-shot Compose prerequisites are missing",
            missing=missing,
        )

    running: list[str] = []
    failed: list[str] = []
    for service in required:
        service_rows = matches[service]
        if len(service_rows) != 1:
            failed.append(service)
            continue
        state, exit_code = _compose_prerequisite_state(service_rows[0])
        if state == "running":
            running.append(service)
        elif state != "exited" or exit_code != 0:
            failed.append(service)

    if running:
        return _check(
            "compose_prerequisites",
            "fail",
            "one-shot Compose prerequisites are still running",
            running=running,
        )
    if failed:
        return _check(
            "compose_prerequisites",
            "fail",
            "one-shot Compose prerequisites did not complete successfully",
            failed=failed,
        )
    return _check(
        "compose_prerequisites",
        "pass",
        "required one-shot Compose prerequisites completed successfully",
        completed=required,
    )


def probe_compose_prerequisites(
    *,
    env_file: str | Path | None = None,
    root: Path = ROOT,
    backup: bool = False,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Read completed one-shot service state; never start or stop Compose."""

    docker = shutil.which("docker")
    if not docker:
        return _check("compose_prerequisites", "not_verified", "Docker CLI is not installed")
    command = [docker, "compose", "--project-directory", str(root)]
    if env_file:
        command.extend(["--env-file", str(Path(env_file).resolve())])
    if backup:
        # Profile services are otherwise omitted from Compose's status model.
        command.extend(["--profile", "backup"])
    command.extend(["ps", "--all", "--format", "json"])
    try:
        result = runner(command, cwd=root, capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return _check("compose_prerequisites", "fail", "Docker Compose prerequisite status command timed out")
    except OSError:
        return _check("compose_prerequisites", "fail", "Docker CLI could not be started")
    except subprocess.SubprocessError:
        return _check("compose_prerequisites", "fail", "Docker Compose prerequisite status could not be read")
    if result.returncode != 0:
        diagnostic = str(result.stderr or "").casefold()
        if any(marker in diagnostic for marker in ("500", "502", "daemon", "engine")):
            return _check(
                "compose_prerequisites",
                "fail",
                "Docker engine did not accept the Compose prerequisite status request",
            )
        return _check("compose_prerequisites", "fail", "Docker Compose prerequisite status command failed")
    return summarize_compose_prerequisites(_compose_rows(str(result.stdout or "")), backup=backup)


def _docker_labels(row: Mapping[str, Any]) -> dict[str, str]:
    """Return Docker labels without retaining or printing unrelated fields.

    ``docker ps --format json`` currently renders ``Labels`` as a comma
    separated string, while test doubles and some Docker clients expose a
    mapping.  Only the label values needed for the ownership check are kept.
    """

    raw = row.get("Labels") or row.get("labels")
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    if not isinstance(raw, str):
        return {}
    labels: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator and key:
            labels[key.strip()] = value.strip()
    return labels


def _normalized_location(value: str) -> str:
    """Normalize a Compose host path without resolving or exposing it."""

    return value.strip().strip('"').replace("\\", "/").rstrip("/").casefold()


def _compose_config_paths(value: str) -> set[str]:
    return {
        _normalized_location(item)
        for item in value.split(",")
        if item.strip()
    }


def _ownership_not_verified(detail: str, **extra: Any) -> dict[str, Any]:
    return _check("compose_schedule_write_ownership", "not_verified", detail, **extra)


def probe_compose_schedule_write_ownership(
    *,
    root: Path = ROOT,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Check the Compose-visible singleton scheduler/write owner.

    This is deliberately narrower than proof that no external systemd job or
    second host exists.  Docker can only prove what its running container
    labels expose, so missing or ambiguous identity evidence is
    ``not_verified`` rather than a pass.  The command is read-only and does
    not include container environments or logs in the report.
    """

    docker = shutil.which("docker")
    if not docker:
        return _ownership_not_verified("Docker CLI is not installed")
    command = [docker, "ps", "--format", "{{json .}}"]
    try:
        result = runner(command, cwd=root, capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return _ownership_not_verified("Docker ownership status command timed out")
    except (OSError, subprocess.SubprocessError):
        return _ownership_not_verified("Docker ownership status could not be read")
    if result.returncode != 0:
        return _ownership_not_verified("Docker ownership status command failed")

    rows = _compose_rows(str(result.stdout or ""))
    owner_rows: list[dict[str, str]] = []
    for row in rows:
        labels = _docker_labels(row)
        service = labels.get("com.docker.compose.service", "").strip()
        if service in _COMPOSE_OWNER_SERVICES:
            owner_rows.append(labels)

    if not owner_rows:
        return _ownership_not_verified(
            "Docker did not expose running Compose beat and scheduler-worker containers"
        )

    required_labels = (
        "com.docker.compose.project",
        "com.docker.compose.project.working_dir",
        "com.docker.compose.project.config_files",
        "com.docker.compose.service",
    )
    if any(not all(labels.get(name, "").strip() for name in required_labels) for labels in owner_rows):
        return _ownership_not_verified(
            "Compose scheduler/write identity labels are incomplete",
            owner_count=len(owner_rows),
        )

    counts: dict[str, int] = {}
    projects: set[str] = set()
    for labels in owner_rows:
        service = labels["com.docker.compose.service"]
        counts[service] = counts.get(service, 0) + 1
        projects.add(labels["com.docker.compose.project"])

    if len(projects) != 1 or any(counts.get(service) != 1 for service in _COMPOSE_OWNER_SERVICES):
        return _check(
            "compose_schedule_write_ownership",
            "fail",
            "multiple Compose schedule/write owners are running",
            owner_counts=dict(sorted(counts.items())),
        )

    try:
        expected_root = _normalized_location(str(root.resolve()))
        expected_compose = _normalized_location(
            str((root / _EXPECTED_COMPOSE_FILE).resolve())
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _ownership_not_verified("Compose deployment identity could not be resolved")

    for labels in owner_rows:
        if labels["com.docker.compose.project"] != _EXPECTED_COMPOSE_PROJECT:
            return _ownership_not_verified(
                "running scheduler/write containers use an unexpected Compose project identity"
            )
        if _normalized_location(labels["com.docker.compose.project.working_dir"]) != expected_root:
            return _ownership_not_verified(
                "running scheduler/write containers use an unexpected Compose working directory"
            )
        config_paths = _compose_config_paths(labels["com.docker.compose.project.config_files"])
        if config_paths != {expected_compose}:
            return _ownership_not_verified(
                "running scheduler/write containers use an unverified Compose configuration"
            )

    return _check(
        "compose_schedule_write_ownership",
        "pass",
        "one Compose-visible scheduler/write owner matches the expected project identity",
        owner_counts=dict(sorted(counts.items())),
    )


def probe_backup(environ: Mapping[str, str], *, directory: str | Path | None = None) -> dict[str, Any]:
    """Inspect the newest encrypted archive without restoring or modifying it."""

    backup_directory = directory or environ.get("BACKUP_DIRECTORY", "/srv/backups")
    mirror_directory = environ.get("BACKUP_MIRROR_DIRECTORY")
    try:
        summary = inspect_latest_backup(
            backup_directory,
            environ.get("BACKUP_KEY", ""),
            environ.get("ENCRYPTION_KEY", ""),
            environ.get("BACKUP_MAX_AGE_SECONDS", 172800),
            mirror_directory=mirror_directory,
        )
    except Exception as exc:  # archive/key errors intentionally stay secret-safe
        return _check("encrypted_backup", "fail", f"backup inspection failed ({type(exc).__name__})")
    mirror_configured = bool(str(mirror_directory or "").strip())
    return _check(
        "encrypted_backup",
        "pass",
        (
            "newest encrypted archive is fresh, decryptable, and exact configured mirror copy is verified"
            if mirror_configured
            else "newest encrypted archive is fresh and integrity-checked"
        ),
        age_seconds=summary.get("age_seconds"),
    )


def probe_storage(
    path: str | Path,
    *,
    minimum_free_bytes: int | str,
) -> dict[str, Any]:
    """Check local free space without creating files or exposing the path."""

    try:
        minimum = int(minimum_free_bytes)
    except (TypeError, ValueError, OverflowError):
        return _check("storage_capacity", "fail", "minimum free-byte threshold is invalid")
    if minimum < 0:
        return _check("storage_capacity", "fail", "minimum free-byte threshold must be non-negative")

    try:
        free_bytes = shutil.disk_usage(path).free
    except (OSError, TypeError, ValueError):
        return _check("storage_capacity", "fail", "storage capacity could not be read")

    details = {
        "free_bytes": free_bytes,
        "minimum_free_bytes": minimum,
    }
    if free_bytes < minimum:
        return _check(
            "storage_capacity",
            "fail",
            "storage path is below the minimum free-space threshold",
            **details,
        )
    return _check(
        "storage_capacity",
        "pass",
        "storage path meets the minimum free-space threshold",
        **details,
    )


def build_report(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
    health_url: str | None = None,
    compose: bool = False,
    backup: bool = False,
    backup_directory: str | Path | None = None,
    storage_path: str | Path | None = None,
    storage_min_free_bytes: int | str | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Build a report from requested local probes and explicit external gates."""

    if environ is None:
        environ, file_error = _merged_environment(env_file)
    else:
        environ = dict(environ)
        file_error = None
    checks = [_environment_check(environ, backup=backup, file_error=file_error)]
    if health_url:
        checks.append(probe_health(health_url, expected_origin=environ.get("PUBLIC_URL")))
    else:
        checks.append(_check("public_health", "not_verified", "pass --health-url to check the running deployment"))
    if compose:
        checks.append(probe_compose(env_file=env_file, root=root, backup=backup))
        checks.append(probe_compose_prerequisites(env_file=env_file, root=root, backup=backup))
        checks.append(probe_compose_schedule_write_ownership(root=root))
    else:
        checks.append(_check("compose_services", "not_verified", "pass --compose to inspect the running Compose project"))
        checks.append(
            _check(
                "compose_prerequisites",
                "not_verified",
                "pass --compose to verify completed migration and backup initialization prerequisites",
            )
        )
        checks.append(
            _check(
                "compose_schedule_write_ownership",
                "not_verified",
                "pass --compose to verify the Compose-visible scheduler/write owner",
            )
        )
    if backup:
        checks.append(probe_backup(environ, directory=backup_directory))
    else:
        checks.append(_check("encrypted_backup", "not_verified", "pass --backup to inspect encrypted backup freshness and integrity"))
    if storage_path is not None:
        minimum_free_bytes = storage_min_free_bytes
        if minimum_free_bytes is None:
            minimum_free_bytes = environ.get("STORAGE_MIN_FREE_BYTES", DEFAULT_STORAGE_MIN_FREE_BYTES)
        checks.append(
            probe_storage(
                storage_path,
                minimum_free_bytes=minimum_free_bytes,
            )
        )
    checks.extend(
        [
            _check(
                "external_connections",
                "not_verified",
                "WordPress writes, Google OAuth, Search Console/GA4, paid research, and AI sampling need real connection checks",
            ),
            _check(
                "unattended_pilot",
                "not_started",
                "the seven-day observation has not started; keep global and site pauses enabled",
            ),
        ]
    )
    outstanding = [check["name"] for check in checks if check["status"] != "pass"]
    failures = [check["name"] for check in checks if check["status"] == "fail"]
    return {
        "status": "READY" if not outstanding else "NOT_READY",
        "checks": checks,
        "outstanding": outstanding,
        "failures": failures,
        "next_actions": [
            "Deploy the standalone platform on an isolated always-on server with persistent storage.",
            "Complete real UI connection checks and an off-site backup/restore drill while paused.",
            "Start the seven-day unattended pilot only after one deployment owns schedules and writes.",
        ],
    }


def _print_report(report: Mapping[str, Any]) -> None:
    print(f"ForgeSEO pilot readiness: {report['status']}")
    for check in report["checks"]:
        print(f"- {check['name']}: {str(check['status']).upper()} - {check['detail']}")
        for key in ("errors", "missing", "not_running", "unhealthy"):
            values = check.get(key)
            if values:
                print(f"  {key}: {', '.join(str(value) for value in values)}")
    print("Next actions:")
    for action in report["next_actions"]:
        print(f"- {action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="optional private env file used for configuration checks")
    parser.add_argument("--health-url", help="optional running deployment health URL")
    parser.add_argument("--compose", action="store_true", help="read Docker Compose service state without changing it")
    parser.add_argument("--backup", action="store_true", help="inspect the newest encrypted backup archive")
    parser.add_argument("--backup-directory", help="override BACKUP_DIRECTORY for --backup")
    parser.add_argument("--storage-path", help="optionally check free space on a local storage path")
    parser.add_argument(
        "--storage-min-free-bytes",
        type=int,
        help=(
            "minimum free bytes required by --storage-path "
            f"(default: {DEFAULT_STORAGE_MIN_FREE_BYTES}; can also use STORAGE_MIN_FREE_BYTES)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output without secret values")
    args = parser.parse_args(argv)
    report = build_report(
        env_file=args.env_file,
        health_url=args.health_url,
        compose=args.compose,
        backup=args.backup,
        backup_directory=args.backup_directory,
        storage_path=args.storage_path,
        storage_min_free_bytes=args.storage_min_free_bytes,
    )
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_report(report)
    return 0 if report["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
