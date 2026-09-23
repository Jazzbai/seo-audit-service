"""Run a bounded, local-only deployment gate for the standalone platform.

The gate validates configuration, asks Docker Compose to render the selected
profile, and optionally starts the named project.  It never performs provider
or application-connection checks.  A seven-day pilot is an operator gate and
is deliberately reported as not started by this command.

Examples::

    python -m scripts.deployment_gate --env-file .env
    python -m scripts.deployment_gate --env-file .env --start \
        --health-url https://seo.example.com/health
    python -m scripts.deployment_gate --env-file .env --start --backup \
        --restart-check --timeout 180 --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

# Keep both ``python -m scripts.deployment_gate`` and
# ``python scripts/deployment_gate.py`` usable from the repository root.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import pilot_readiness
from scripts.pilot_readiness import (
    load_env_file,
    summarize_compose,
    summarize_compose_prerequisites,
)
from scripts.preflight import validate_environment


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_NAME = "forgeseo-platform"
DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL_SECONDS = 1.0

_CORE_SERVICES = frozenset(
    {
        "api",
        "beat",
        "browser",
        "db",
        "queue",
        "scheduler-worker",
        "web",
        "worker",
    }
)
_CONFIGURATION_SERVICES = _CORE_SERVICES | {"migrate"}
_BACKUP_SERVICES = frozenset({"backup", "backup-init"})
_RESTART_SERVICES = ("worker", "scheduler-worker", "beat", "browser")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

Runner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
HealthProbe = Callable[..., dict[str, Any]]


def _check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    result.update(extra)
    return result


def _not_requested(name: str, detail: str) -> dict[str, Any]:
    return _check(name, "not_requested", detail)


def _not_run(name: str, detail: str) -> dict[str, Any]:
    return _check(name, "not_run", detail)


def _env_path(value: str | Path, *, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _configuration_check(
    *,
    env_file: str | Path | None,
    backup: bool,
    root: Path,
    environ: Mapping[str, str] | None,
    require_env_file: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Load and validate configuration without retaining values in the report."""

    if env_file is not None:
        try:
            values = load_env_file(_env_path(env_file, root=root))
        except RuntimeError as exc:
            return _check("configuration", "fail", str(exc)), {}
    elif environ is not None:
        values = dict(environ)
    else:
        values = dict(os.environ)

    errors = validate_environment(values, backup=backup)
    if require_env_file and env_file is None:
        errors = [
            "--env-file is required before starting or restarting the Compose project",
            *errors,
        ]
    if errors:
        return (
            _check(
                "configuration",
                "fail",
                "deployment preflight failed",
                errors=errors,
            ),
            values,
        )

    source = "private env file" if env_file is not None else "process environment"
    return _check("configuration", "pass", f"deployment preflight passed from {source}"), values


def _validate_project_name(project_name: str) -> dict[str, Any] | None:
    if not isinstance(project_name, str) or not _PROJECT_NAME_RE.fullmatch(project_name):
        return _check(
            "compose_configuration",
            "fail",
            "project name is invalid",
        )
    return None


def _compose_base_command(
    *,
    docker: str,
    project_name: str,
    env_file: str | Path | None,
    root: Path,
) -> list[str]:
    command = [
        docker,
        "compose",
        "--project-directory",
        str(root),
        "--project-name",
        project_name,
    ]
    if env_file is not None:
        # Keep the operator's argument intact.  The command runs with cwd=root,
        # so a relative path has the same meaning as the Compose invocation.
        command.extend(["--env-file", str(env_file)])
    return command


def _with_profile(command: list[str], *, backup: bool) -> list[str]:
    if backup:
        return [*command, "--profile", "backup"]
    return command


def _command_failure_detail(action: str, reason: str) -> str:
    if reason == "timeout":
        return f"Docker Compose {action} command timed out"
    if reason == "unavailable":
        return f"Docker Compose {action} command could not be started"
    if reason == "deadline":
        return f"timed out before Docker Compose {action} could run"
    return f"Docker Compose {action} command failed"


def _run_command(
    command: list[str],
    *,
    action: str,
    root: Path,
    deadline: float,
    runner: Runner,
    clock: Clock,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run one command with a remaining, finite deadline and no shell."""

    remaining = deadline - clock()
    if remaining <= 0:
        return None, "deadline"
    try:
        result = runner(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=remaining,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None, "unavailable"
    if result.returncode != 0:
        return result, "returncode"
    return result, None


def _compose_rows(raw: Any) -> list[dict[str, Any]]:
    """Use the readiness parser while accepting test doubles with no stdout."""

    return pilot_readiness._compose_rows(str(raw or ""))


def _render_compose(
    *,
    base_command: list[str],
    backup: bool,
    root: Path,
    deadline: float,
    runner: Runner,
    clock: Clock,
) -> dict[str, Any]:
    command = [*_with_profile(base_command, backup=backup), "config", "--format", "json"]
    result, reason = _run_command(
        command,
        action="configuration render",
        root=root,
        deadline=deadline,
        runner=runner,
        clock=clock,
    )
    if result is None:
        return _check(
            "compose_configuration",
            "fail",
            _command_failure_detail("configuration render", reason or "unavailable"),
        )
    if reason is not None:
        return _check(
            "compose_configuration",
            "fail",
            _command_failure_detail("configuration render", reason),
        )

    try:
        model = json.loads(str(result.stdout or ""))
    except (TypeError, ValueError, UnicodeError):
        return _check(
            "compose_configuration",
            "fail",
            "Compose returned invalid rendered configuration",
        )
    services = model.get("services") if isinstance(model, Mapping) else None
    if not isinstance(services, Mapping):
        return _check(
            "compose_configuration",
            "fail",
            "Compose rendered configuration without a services map",
        )

    expected = set(_CONFIGURATION_SERVICES)
    if backup:
        expected.update(_BACKUP_SERVICES)
    present = {str(name) for name in services if isinstance(name, str)}
    missing = sorted(expected - present)
    if missing:
        return _check(
            "compose_configuration",
            "fail",
            "rendered Compose configuration is missing required services",
            missing=missing,
            service_count=len(present),
        )
    return _check(
        "compose_configuration",
        "pass",
        "Compose configuration rendered with the required services",
        service_count=len(present),
        services=sorted(present),
    )


def _status_failure(name: str, detail: str) -> dict[str, Any]:
    return _check(name, "fail", detail)


def _runtime_status(
    raw: Any,
    *,
    backup: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        rows = _compose_rows(raw)
        runtime_services = set(_CORE_SERVICES)
        if backup:
            runtime_services.add("backup")
        # ``ps --all`` also returns exited migration/initialization rows.  The
        # readiness helper's service summary is intentionally for long-running
        # services, so keep one-shot rows for the prerequisite summary only.
        runtime_rows = [row for row in rows if _row_service(row) in runtime_services]
        return (
            summarize_compose(runtime_rows, backup=backup),
            summarize_compose_prerequisites(rows, backup=backup),
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return (
            _status_failure("compose_services", "Compose returned an unreadable service status"),
            _status_failure(
                "compose_prerequisites",
                "Compose returned an unreadable prerequisite status",
            ),
        )


def _timeout_runtime_status(
    service_check: dict[str, Any],
    prerequisite_check: dict[str, Any],
    *,
    operation: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    services = dict(service_check)
    prerequisites = dict(prerequisite_check)
    if services.get("status") != "pass":
        services["status"] = "fail"
        services["detail"] = f"timed out waiting for {operation} Compose services"
        services["timed_out"] = True
    if prerequisites.get("status") != "pass":
        prerequisites["status"] = "fail"
        prerequisites["detail"] = f"timed out waiting for {operation} Compose prerequisites"
        prerequisites["timed_out"] = True
    return services, prerequisites


def _poll_runtime(
    *,
    base_command: list[str],
    backup: bool,
    root: Path,
    deadline: float,
    runner: Runner,
    clock: Clock,
    sleeper: Sleeper,
    operation: str,
    initial_services: dict[str, Any] | None = None,
    initial_prerequisites: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait for Compose services and one-shot prerequisites to settle."""

    service_check = initial_services or _not_run(
        "compose_services", "Compose service status was not collected"
    )
    prerequisite_check = initial_prerequisites or _not_run(
        "compose_prerequisites", "Compose prerequisite status was not collected"
    )
    # The deadline is authoritative; this cap also keeps injected/static clocks
    # in tests from turning a failed probe into an infinite loop.
    max_polls = max(1, int(math.ceil(max(deadline - clock(), 0) / POLL_INTERVAL_SECONDS)) + 2)
    for _ in range(max_polls):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        command = [
            *_with_profile(base_command, backup=backup),
            "ps",
            "--all",
            "--format",
            "json",
        ]
        result, reason = _run_command(
            command,
            action=f"{operation} status",
            root=root,
            deadline=deadline,
            runner=runner,
            clock=clock,
        )
        if result is None:
            service_check = _status_failure(
                "compose_services",
                _command_failure_detail(f"{operation} status", reason or "unavailable"),
            )
            prerequisite_check = _status_failure(
                "compose_prerequisites",
                _command_failure_detail(f"{operation} prerequisite status", reason or "unavailable"),
            )
        elif reason is not None:
            service_check = _status_failure(
                "compose_services",
                _command_failure_detail(f"{operation} status", reason),
            )
            prerequisite_check = _status_failure(
                "compose_prerequisites",
                _command_failure_detail(f"{operation} prerequisite status", reason),
            )
        else:
            service_check, prerequisite_check = _runtime_status(
                result.stdout,
                backup=backup,
            )
            if service_check.get("status") == "pass" and prerequisite_check.get("status") == "pass":
                return service_check, prerequisite_check

        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(POLL_INTERVAL_SECONDS, remaining))

    return _timeout_runtime_status(
        service_check,
        prerequisite_check,
        operation=operation,
    )


def _row_service(row: Mapping[str, Any]) -> str:
    value = row.get("Service") or row.get("service") or row.get("Name") or row.get("name")
    return value.strip() if isinstance(value, str) else ""


def _row_is_running_and_healthy(row: Mapping[str, Any]) -> bool:
    state = str(row.get("State") or row.get("state") or "").strip().casefold()
    status = str(row.get("Status") or row.get("status") or "").strip().casefold()
    health = str(row.get("Health") or row.get("health") or "").strip().casefold()
    if not state:
        if "up" in status or "running" in status:
            state = "running"
        elif "exited" in status or "dead" in status or "restarting" in status:
            state = "stopped"
    if state != "running":
        return False
    if health and health != "healthy":
        return False
    combined = f"{state} {status} {health}"
    return not any(marker in combined for marker in ("unhealthy", "restarting", "dead"))


def _summarize_restart(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    matches: dict[str, list[Mapping[str, Any]]] = {name: [] for name in _RESTART_SERVICES}
    for row in rows:
        service = _row_service(row)
        if service in matches:
            matches[service].append(row)

    recovered: list[str] = []
    not_recovered: list[str] = []
    for service in _RESTART_SERVICES:
        service_rows = matches[service]
        if len(service_rows) == 1 and _row_is_running_and_healthy(service_rows[0]):
            recovered.append(service)
        else:
            not_recovered.append(service)
    if not_recovered:
        return _check(
            "restart_recovery",
            "fail",
            "known long-running worker classes did not all recover",
            recovered=recovered,
            not_recovered=not_recovered,
        )
    return _check(
        "restart_recovery",
        "pass",
        "known long-running worker classes recovered",
        recovered=recovered,
    )


def _poll_restart(
    *,
    base_command: list[str],
    backup: bool,
    root: Path,
    deadline: float,
    runner: Runner,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, Any]:
    last = _check(
        "restart_recovery",
        "fail",
        "restart recovery status was not collected",
        not_recovered=list(_RESTART_SERVICES),
    )
    max_polls = max(1, int(math.ceil(max(deadline - clock(), 0) / POLL_INTERVAL_SECONDS)) + 2)
    for _ in range(max_polls):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        command = [
            *_with_profile(base_command, backup=backup),
            "ps",
            "--all",
            "--format",
            "json",
        ]
        result, reason = _run_command(
            command,
            action="restart recovery status",
            root=root,
            deadline=deadline,
            runner=runner,
            clock=clock,
        )
        if result is None:
            last = _check(
                "restart_recovery",
                "fail",
                _command_failure_detail("restart recovery status", reason or "unavailable"),
                not_recovered=list(_RESTART_SERVICES),
            )
        elif reason is not None:
            last = _check(
                "restart_recovery",
                "fail",
                _command_failure_detail("restart recovery status", reason),
                not_recovered=list(_RESTART_SERVICES),
            )
        else:
            try:
                last = _summarize_restart(_compose_rows(result.stdout))
            except (TypeError, ValueError, KeyError, AttributeError):
                last = _check(
                    "restart_recovery",
                    "fail",
                    "Compose returned an unreadable restart status",
                    not_recovered=list(_RESTART_SERVICES),
                )
            if last.get("status") == "pass":
                return last

        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(POLL_INTERVAL_SECONDS, remaining))

    result = dict(last)
    result["status"] = "fail"
    result["detail"] = "timed out waiting for restarted worker classes to recover"
    result["timed_out"] = True
    result.setdefault("not_recovered", list(_RESTART_SERVICES))
    return result


def _backup_check(
    *,
    requested: bool,
    start: bool,
    compose_configuration: Mapping[str, Any],
    compose_services: Mapping[str, Any],
    compose_prerequisites: Mapping[str, Any],
) -> dict[str, Any]:
    if not requested:
        return _not_requested("backup", "pass --backup to render and verify the backup profile")
    if compose_configuration.get("status") != "pass":
        return _not_run("backup", "backup profile was not verified because Compose rendering failed")
    if not start:
        return _check(
            "backup",
            "not_verified",
            "backup profile rendered but was not started; pass --start to verify its service",
        )
    if (
        compose_services.get("status") == "pass"
        and compose_prerequisites.get("status") == "pass"
    ):
        return _check(
            "backup",
            "pass",
            "backup profile service and initialization prerequisite are healthy; archive restore remains a separate drill",
        )
    return _check(
        "backup",
        "fail",
        "backup profile did not become ready with the standalone project",
    )


def _health_check(
    url: str,
    *,
    deadline: float,
    clock: Clock,
    health_probe: HealthProbe,
    expected_origin: str | None = None,
) -> dict[str, Any]:
    remaining = deadline - clock()
    if remaining <= 0:
        return _check("public_health", "fail", "timed out before the public health probe could run")
    try:
        scheme = urlsplit(url).scheme.casefold()
    except ValueError:
        scheme = ""
    if scheme != "https":
        return _check(
            "public_health",
            "fail",
            "deployment health URL must use HTTPS for production handoff",
        )
    if not pilot_readiness.health_origin_matches(url, expected_origin):
        return _check(
            "public_health",
            "fail",
            "health URL does not match the configured public origin",
        )
    try:
        try:
            result = health_probe(url, timeout=remaining)
        except TypeError as first_error:
            # Keep simple one-argument probe doubles usable while the built-in
            # probe still receives the bounded timeout.
            try:
                result = health_probe(url)
            except TypeError:
                raise first_error
    except TimeoutError:
        return _check("public_health", "fail", "public health probe timed out")
    except (OSError, ValueError, TypeError):
        return _check("public_health", "fail", "public health probe could not be completed")
    if not isinstance(result, Mapping):
        return _check("public_health", "fail", "public health probe returned an unreadable result")
    normalized = dict(result)
    normalized.setdefault("name", "public_health")
    normalized.setdefault("detail", "public health probe completed")
    return normalized


def probe_health(
    url: str,
    *,
    timeout: float = 8.0,
    opener: Callable[..., Any] | None = None,
    expected_origin: str | None = None,
) -> dict[str, Any]:
    """Use the existing secret-safe health contract with a caller's timeout."""

    if opener is None:
        def bounded_opener(request: Any, _ignored_timeout: float = 8.0) -> Any:
            return urlopen(request, timeout=timeout)

        opener = bounded_opener
    return pilot_readiness.probe_health(
        url,
        opener=opener,
        require_https=True,
        expected_origin=expected_origin,
    )


def _finalize_report(
    *,
    project_name: str,
    start: bool,
    restart_check: bool,
    backup: bool,
    configuration: dict[str, Any],
    compose_configuration: dict[str, Any],
    compose_services: dict[str, Any],
    compose_prerequisites: dict[str, Any],
    public_health: dict[str, Any],
    backup_check: dict[str, Any],
    restart_recovery: dict[str, Any],
) -> dict[str, Any]:
    pilot = _check(
        "seven_day_pilot",
        "not_started",
        "the seven-day observation is not started; this gate never certifies it",
    )
    checks = [
        configuration,
        compose_configuration,
        compose_services,
        compose_prerequisites,
        public_health,
        backup_check,
        restart_recovery,
        pilot,
    ]
    required = [configuration, compose_configuration]
    if start:
        required.extend([compose_services, compose_prerequisites])
    if backup and start:
        required.append(backup_check)
    if public_health.get("status") not in {"not_requested", "not_run"}:
        required.append(public_health)
    if restart_check:
        required.append(restart_recovery)
    ok = all(check.get("status") == "pass" for check in required)
    failures = [check["name"] for check in checks if check.get("status") == "fail"]
    outstanding = [
        check["name"]
        for check in checks
        if check.get("status") not in {"pass", "not_requested", "not_run"}
    ]
    return {
        "status": "PASS" if ok else "FAIL",
        "ok": ok,
        "project_name": project_name,
        "start_requested": start,
        "restart_check_requested": restart_check,
        "backup_requested": backup,
        "configuration": configuration,
        "compose_configuration": compose_configuration,
        "compose_services": compose_services,
        "compose_prerequisites": compose_prerequisites,
        "compose": {
            "configuration": compose_configuration,
            "services": compose_services,
            "prerequisites": compose_prerequisites,
        },
        "public_health": public_health,
        "backup": backup_check,
        "restart_recovery": restart_recovery,
        "seven_day_pilot": pilot,
        "checks": checks,
        "failures": failures,
        "outstanding": outstanding,
    }


def build_report(
    *,
    project_name: str = DEFAULT_PROJECT_NAME,
    env_file: str | Path | None = None,
    backup: bool = False,
    start: bool = False,
    restart_check: bool = False,
    health_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    health_probe: HealthProbe | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    """Build a local deployment-gate report.

    ``runner``, ``health_probe``, ``clock``, and ``sleeper`` are injectable so
    tests can exercise sequencing and timeouts without requiring Docker or a
    network endpoint.
    """

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        timeout_value = 0.0
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        return _finalize_report(
            project_name=project_name,
            start=start,
            restart_check=restart_check,
            backup=backup,
            configuration=_check("configuration", "fail", "timeout must be a positive finite number"),
            compose_configuration=_not_run(
                "compose_configuration", "Compose was not invoked after invalid timeout"
            ),
            compose_services=_not_run("compose_services", "Compose was not started"),
            compose_prerequisites=_not_run(
                "compose_prerequisites", "Compose was not started"
            ),
            public_health=_not_run("public_health", "health probe was not run"),
            backup_check=_not_run("backup", "backup was not run"),
            restart_recovery=_not_run("restart_recovery", "restart check was not run"),
        )

    runner = runner or subprocess.run
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    health_probe = health_probe or probe_health
    start_time = clock()
    deadline = start_time + timeout_value

    configuration, configuration_values = _configuration_check(
        env_file=env_file,
        backup=backup,
        root=root,
        environ=environ,
        require_env_file=start or restart_check,
    )
    compose_services: dict[str, Any] = _not_requested(
        "compose_services", "pass --start to start and verify the standalone Compose services"
    )
    compose_prerequisites: dict[str, Any] = _not_requested(
        "compose_prerequisites",
        "pass --start to verify migration and backup initialization prerequisites",
    )
    public_health: dict[str, Any] = (
        _not_requested("public_health", "pass --health-url to check the public health contract")
        if not health_url
        else _not_run("public_health", "health probe was blocked by an earlier gate")
    )
    backup_check: dict[str, Any] = _not_requested(
        "backup", "pass --backup to render and verify the backup profile"
    )
    restart_recovery: dict[str, Any] = _not_requested(
        "restart_recovery", "pass --restart-check to verify worker restart recovery"
    )

    project_error = _validate_project_name(project_name)
    if configuration.get("status") != "pass":
        compose_configuration = _not_run(
            "compose_configuration",
            "Compose was not invoked because deployment preflight failed",
        )
        compose_services = _not_run(
            "compose_services", "Compose was not started because deployment preflight failed"
        )
        compose_prerequisites = _not_run(
            "compose_prerequisites",
            "Compose prerequisites were not checked because deployment preflight failed",
        )
        if health_url:
            public_health = _not_run(
                "public_health", "public health was not contacted after deployment preflight failed"
            )
        if backup:
            backup_check = _not_run("backup", "backup was not checked after deployment preflight failed")
        if restart_check:
            restart_recovery = _not_run(
                "restart_recovery", "restart check was blocked by deployment preflight failure"
            )
        return _finalize_report(
            project_name=project_name,
            start=start,
            restart_check=restart_check,
            backup=backup,
            configuration=configuration,
            compose_configuration=compose_configuration,
            compose_services=compose_services,
            compose_prerequisites=compose_prerequisites,
            public_health=public_health,
            backup_check=backup_check,
            restart_recovery=restart_recovery,
        )

    if project_error is not None:
        compose_configuration = project_error
        if health_url:
            public_health = _not_run(
                "public_health", "public health was not contacted after invalid project name"
            )
        if backup:
            backup_check = _not_run("backup", "backup was not checked after invalid project name")
        if restart_check:
            restart_recovery = _not_run(
                "restart_recovery", "restart check was blocked by invalid project name"
            )
        return _finalize_report(
            project_name=project_name,
            start=start,
            restart_check=restart_check,
            backup=backup,
            configuration=configuration,
            compose_configuration=compose_configuration,
            compose_services=compose_services,
            compose_prerequisites=compose_prerequisites,
            public_health=public_health,
            backup_check=backup_check,
            restart_recovery=restart_recovery,
        )

    docker = shutil.which("docker") or "docker"
    base_command = _compose_base_command(
        docker=docker,
        project_name=project_name,
        env_file=env_file,
        root=root,
    )
    compose_configuration = _render_compose(
        base_command=base_command,
        backup=backup,
        root=root,
        deadline=deadline,
        runner=runner,
        clock=clock,
    )

    if compose_configuration.get("status") == "pass":
        if start:
            # Resolve startup images only after the build has completed. The
            # local release exposed stale image selection with combined up/build.
            for action, arguments in (("build", ["build"]),
                                      ("start", ["up", "--detach", "--no-build"])):
                result, reason = _run_command(
                    [*_with_profile(base_command, backup=backup), *arguments],
                    action=action,
                    root=root,
                    deadline=deadline,
                    runner=runner,
                    clock=clock,
                )
                if result is None or reason is not None:
                    detail = _command_failure_detail(action, reason or "returncode")
                    compose_services = _status_failure("compose_services", detail)
                    compose_prerequisites = _not_run(
                        "compose_prerequisites", f"Compose prerequisites were not checked after {action} failure"
                    )
                    break
            else:
                compose_services, compose_prerequisites = _poll_runtime(
                    base_command=base_command,
                    backup=backup,
                    root=root,
                    deadline=deadline,
                    runner=runner,
                    clock=clock,
                    sleeper=sleeper,
                    operation="startup",
                )

        if health_url and (
            not start or compose_services.get("status") == "pass"
        ):
            public_health = _health_check(
                health_url,
                deadline=deadline,
                clock=clock,
                health_probe=health_probe,
                expected_origin=configuration_values.get("PUBLIC_URL"),
            )
        elif health_url and start:
            public_health = _not_run(
                "public_health", "public health was not checked because Compose services were not ready"
            )

        if restart_check and (not start or compose_services.get("status") == "pass"):
            restart_command = [
                *_with_profile(base_command, backup=backup),
                "restart",
                *_RESTART_SERVICES,
            ]
            result, reason = _run_command(
                restart_command,
                action="restart",
                root=root,
                deadline=deadline,
                runner=runner,
                clock=clock,
            )
            if result is None or reason is not None:
                restart_recovery = _check(
                    "restart_recovery",
                    "fail",
                    _command_failure_detail("restart", reason or "returncode"),
                    not_recovered=list(_RESTART_SERVICES),
                )
            else:
                restart_recovery = _poll_restart(
                    base_command=base_command,
                    backup=backup,
                    root=root,
                    deadline=deadline,
                    runner=runner,
                    clock=clock,
                    sleeper=sleeper,
                )
        elif restart_check:
            restart_recovery = _not_run(
                "restart_recovery", "restart check was blocked by deployment startup failure"
            )
    else:
        compose_services = _not_run(
            "compose_services", "Compose was not started because rendering failed"
        )
        compose_prerequisites = _not_run(
            "compose_prerequisites", "Compose prerequisites were not checked because rendering failed"
        )
        if health_url:
            public_health = _not_run(
                "public_health", "public health was not contacted because rendering failed"
            )
        if backup:
            backup_check = _not_run("backup", "backup profile was not checked because rendering failed")
        if restart_check:
            restart_recovery = _not_run(
                "restart_recovery", "restart check was blocked by Compose rendering failure"
            )

    backup_check = _backup_check(
        requested=backup,
        start=start,
        compose_configuration=compose_configuration,
        compose_services=compose_services,
        compose_prerequisites=compose_prerequisites,
    )
    return _finalize_report(
        project_name=project_name,
        start=start,
        restart_check=restart_check,
        backup=backup,
        configuration=configuration,
        compose_configuration=compose_configuration,
        compose_services=compose_services,
        compose_prerequisites=compose_prerequisites,
        public_health=public_health,
        backup_check=backup_check,
        restart_recovery=restart_recovery,
    )


def run_gate(**kwargs: Any) -> dict[str, Any]:
    """Compatibility-friendly alias for callers that prefer an imperative name."""

    return build_report(**kwargs)


def _print_report(report: Mapping[str, Any]) -> None:
    print(f"ForgeSEO deployment gate: {report['status']}")
    for check in report["checks"]:
        print(f"- {check['name']}: {str(check['status']).upper()} - {check['detail']}")
        for key in ("errors", "missing", "not_running", "unhealthy", "failed", "not_recovered"):
            values = check.get(key)
            if values:
                print(f"  {key}: {', '.join(str(value) for value in values)}")
    print("The seven-day pilot remains NOT_STARTED; this command does not certify it.")


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-name",
        default=DEFAULT_PROJECT_NAME,
        help=f"isolated Compose project name (default: {DEFAULT_PROJECT_NAME})",
    )
    parser.add_argument(
        "--env-file",
        help="private environment file to validate and pass exactly to Compose",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="validate and include the encrypted-backup profile",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="start/rebuild the selected Compose project; omitted means read-only render",
    )
    parser.add_argument(
        "--restart-check",
        action="store_true",
        help="restart and verify only worker, scheduler-worker, beat, and browser",
    )
    parser.add_argument(
        "--health-url",
        help="optional HTTPS public health endpoint to probe (required for a handoff check)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=DEFAULT_TIMEOUT,
        help=f"total bounded operation timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a secret-safe machine-readable report",
    )
    args = parser.parse_args(argv)
    report = build_report(
        project_name=args.project_name,
        env_file=args.env_file,
        backup=args.backup,
        start=args.start,
        restart_check=args.restart_check,
        health_url=args.health_url,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_report(report)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
