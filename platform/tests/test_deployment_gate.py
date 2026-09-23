import base64
import json
import subprocess
from pathlib import Path

import pytest

from scripts import deployment_gate


CORE_SERVICES = {
    "api",
    "beat",
    "browser",
    "db",
    "migrate",
    "queue",
    "scheduler-worker",
    "web",
    "worker",
}
RESTART_SERVICES = ["worker", "scheduler-worker", "beat", "browser"]


def _environment(*, backup=False):
    values = {
        "DB_PASSWORD": "db-" + "d" * 40,
        "QUEUE_PASSWORD": "queue-" + "q" * 40,
        "ENCRYPTION_KEY": "encryption-" + "e" * 40,
        "BOOTSTRAP_TOKEN": "bootstrap-" + "b" * 40,
        "PUBLIC_URL": "https://seo.forgeseo.com",
        "APP_ADDRESS": "seo.forgeseo.com",
        "COOKIE_SECURE": "true",
    }
    if backup:
        values["BACKUP_KEY"] = base64.urlsafe_b64encode(b"b" * 32).decode("ascii")
    return values


def _env_file(tmp_path: Path, *, backup=False) -> Path:
    path = tmp_path / "private.env"
    path.write_text(
        "\n".join(f"{name}={value}" for name, value in _environment(backup=backup).items())
        + "\n",
        encoding="utf-8",
    )
    return path


def _compose_config(*, backup=False):
    services = {name: {} for name in CORE_SERVICES}
    if backup:
        services.update({"backup": {}, "backup-init": {}})
    return json.dumps({"name": "ignored-in-report", "services": services})


def _runtime_rows(*, backup=False, workers_running=True):
    rows = []
    for service in sorted(CORE_SERVICES - {"migrate"}):
        running = workers_running or service not in set(RESTART_SERVICES)
        rows.append(
            {
                "Service": service,
                "State": "running" if running else "exited",
                "Health": "healthy" if running else "unhealthy",
            }
        )
    rows.append({"Service": "migrate", "State": "exited", "ExitCode": 0})
    if backup:
        rows.append({"Service": "backup", "State": "running", "Health": "healthy"})
        rows.append({"Service": "backup-init", "State": "exited", "ExitCode": 0})
    return json.dumps(rows)


def test_preflight_is_fail_closed_and_never_invokes_compose(tmp_path):
    env_file = tmp_path / "invalid.env"
    env_file.write_text("DB_PASSWORD=too-short\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        raise AssertionError("Compose must not run after a failed preflight")

    report = deployment_gate.build_report(
        env_file=env_file,
        start=True,
        runner=runner,
    )

    assert report["status"] == "FAIL"
    assert report["configuration"]["status"] == "fail"
    assert report["compose_configuration"]["status"] == "not_run"
    assert calls == []


def test_compose_render_failure_does_not_start_project(tmp_path):
    env_file = _env_file(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "", "secret=must-not-appear")

    report = deployment_gate.build_report(
        env_file=env_file,
        project_name="isolated-gate",
        start=True,
        runner=runner,
    )

    assert report["compose_configuration"]["status"] == "fail"
    assert report["compose_services"]["status"] == "not_run"
    assert not any("up" in command for command, _ in calls)


def test_json_report_is_secret_safe(tmp_path, monkeypatch, capsys):
    env_file = _env_file(tmp_path)

    def runner(command, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(
            command,
            0,
            _compose_config(),
            "Compose expanded DB_PASSWORD=db-secret-that-must-not-appear",
        )

    monkeypatch.setattr(deployment_gate.shutil, "which", lambda name: "docker")
    monkeypatch.setattr(deployment_gate.subprocess, "run", runner)

    assert deployment_gate.main(["--env-file", str(env_file), "--json"]) == 0
    rendered = capsys.readouterr().out
    for name in ("DB_PASSWORD", "QUEUE_PASSWORD", "ENCRYPTION_KEY", "BOOTSTRAP_TOKEN"):
        value = _environment()[name]
        assert value not in rendered
    assert "db-secret-that-must-not-appear" not in rendered
    report = json.loads(rendered)
    assert report["configuration"]["status"] == "pass"
    assert report["seven_day_pilot"]["status"] == "not_started"


def test_deployment_health_probe_fails_closed_for_valid_json_with_the_wrong_shape():
    class Response:
        status = 200

        def read(self):
            return b"null"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return "https://127.0.0.1:18443/health"

    result = deployment_gate.probe_health(
        "https://127.0.0.1:18443/health",
        opener=lambda request, timeout: Response(),
    )

    assert result["status"] == "fail"
    assert result["detail"] == "health endpoint did not return status ok"


def test_deployment_gate_rejects_http_health_url_before_probe(tmp_path):
    env_file = _env_file(tmp_path)
    health_calls = []

    def runner(command, **kwargs):
        assert "config" in command
        return subprocess.CompletedProcess(command, 0, _compose_config(), "")

    def health_probe(url, **kwargs):
        health_calls.append((url, kwargs))
        return {"status": "pass", "detail": "test double must not be called"}

    report = deployment_gate.build_report(
        env_file=env_file,
        health_url="http://127.0.0.1:18080/health",
        runner=runner,
        health_probe=health_probe,
    )

    assert report["status"] == "FAIL"
    assert report["public_health"]["status"] == "fail"
    assert report["public_health"]["detail"] == (
        "deployment health URL must use HTTPS for production handoff"
    )
    assert health_calls == []


def test_deployment_gate_rejects_a_different_health_origin_before_probe(tmp_path):
    env_file = _env_file(tmp_path)
    health_calls = []

    def runner(command, **kwargs):
        assert "config" in command
        return subprocess.CompletedProcess(command, 0, _compose_config(), "")

    def health_probe(url, **kwargs):
        health_calls.append((url, kwargs))
        return {"status": "pass", "detail": "test double must not be called"}

    report = deployment_gate.build_report(
        env_file=env_file,
        health_url="https://other.forgeseo.com/health",
        runner=runner,
        health_probe=health_probe,
    )

    assert report["status"] == "FAIL"
    assert report["public_health"]["status"] == "fail"
    assert report["public_health"]["detail"] == (
        "health URL does not match the configured public origin"
    )
    assert health_calls == []


def test_start_and_restart_sequence_is_bounded_and_uses_exact_scope(tmp_path, monkeypatch):
    env_file = _env_file(tmp_path, backup=True)
    calls = []
    ps_outputs = iter(
        [
            _runtime_rows(backup=True),
            _runtime_rows(backup=True),
        ]
    )

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["shell"] is False
        if "config" in command:
            return subprocess.CompletedProcess(command, 0, _compose_config(backup=True), "")
        if "build" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "up" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "restart" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "ps" in command:
            return subprocess.CompletedProcess(command, 0, next(ps_outputs), "")
        raise AssertionError(command)

    health_calls = []

    def health_probe(url, *, timeout):
        health_calls.append((url, timeout))
        return {
            "name": "public_health",
            "status": "pass",
            "detail": "public health endpoint returned status ok",
            "target": "seo.forgeseo.com",
        }

    monkeypatch.setattr(deployment_gate.shutil, "which", lambda name: "docker")
    report = deployment_gate.build_report(
        project_name="isolated-gate",
        env_file=env_file,
        backup=True,
        start=True,
        restart_check=True,
        health_url="https://seo.forgeseo.com/health",
        timeout=30,
        runner=runner,
        health_probe=health_probe,
    )

    assert report["status"] == "PASS"
    assert report["backup"]["status"] == "pass"
    assert report["restart_recovery"]["status"] == "pass"
    assert report["seven_day_pilot"]["status"] == "not_started"
    assert health_calls and health_calls[0][0].endswith("/health")

    commands = [command for command, _ in calls]
    assert [part for part in commands[0] if part in {"--project-name", "--env-file"}] == [
        "--project-name",
        "--env-file",
    ]
    for command in commands:
        project_index = command.index("--project-name")
        env_index = command.index("--env-file")
        assert command[project_index + 1] == "isolated-gate"
        assert command[env_index + 1] == str(env_file)
    assert commands[1][-1] == "build"
    assert commands[2][-3:] == ["up", "--detach", "--no-build"]
    assert not any("--build" in command for command in commands)
    assert all(0 < kwargs["timeout"] <= 30 for _, kwargs in calls)
    restart = next(command for command in commands if "restart" in command)
    assert restart[-4:] == RESTART_SERVICES
    assert not any(
        forbidden in command for command in commands for forbidden in ("down", "rm", "prune")
    )


@pytest.mark.parametrize("failure", ["build_exit", "build_timeout", "build_deadline", "up_exit"])
def test_failed_build_or_start_does_not_probe_or_restart_old_services(tmp_path, failure):
    calls = []
    now = [0.0]

    def runner(command, **kwargs):
        calls.append(command)
        if "config" in command:
            return subprocess.CompletedProcess(command, 0, _compose_config(), "")
        if "build" in command:
            if failure == "build_timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="private build log")
            if failure == "build_deadline":
                now[0] = 31.0
            return subprocess.CompletedProcess(command, 1 if failure == "build_exit" else 0,
                                               "private build log", "private failure log")
        if "up" in command and failure == "up_exit":
            return subprocess.CompletedProcess(command, 1, "private startup log", "")
        pytest.fail(f"Unexpected Docker action after {failure}: {command}")

    def no_health(*args, **kwargs):
        pytest.fail("A failed startup must not certify an older deployment's endpoint")

    report = deployment_gate.build_report(
        project_name="isolated-gate", env_file=_env_file(tmp_path), start=True,
        restart_check=True, health_url="https://seo.forgeseo.com/health", timeout=30,
        runner=runner, health_probe=no_health, clock=lambda: now[0],
    )
    assert report["status"] == "FAIL"
    assert report["compose_services"]["status"] == "fail"
    assert report["compose_prerequisites"]["status"] == "not_run"
    assert report["restart_recovery"]["status"] == "not_run"
    assert report["public_health"]["status"] == "not_run"
    assert not any("restart" in command or "ps" in command for command in calls)
    assert sum("up" in command for command in calls) == (1 if failure == "up_exit" else 0)
    assert all(value not in json.dumps(report) for value in (
        "private build log", "private failure log", "private startup log",
    ))


def test_render_only_never_builds_or_starts_a_project(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert "config" in command
        return subprocess.CompletedProcess(command, 0, _compose_config(), "")

    report = deployment_gate.build_report(env_file=_env_file(tmp_path), runner=runner)
    assert report["status"] == "PASS"
    assert report["compose_services"]["status"] == "not_requested"
    assert len(calls) == 1


def test_restart_failure_times_out_without_restarting_other_services(tmp_path, monkeypatch):
    env_file = _env_file(tmp_path)
    calls = []
    now = [0.0]

    def clock():
        return now[0]

    def sleeper(seconds):
        now[0] += seconds

    def runner(command, **kwargs):
        calls.append(command)
        if "config" in command:
            return subprocess.CompletedProcess(command, 0, _compose_config(), "")
        if "restart" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "ps" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                _runtime_rows(workers_running=False),
                "",
            )
        raise AssertionError(command)

    monkeypatch.setattr(deployment_gate.shutil, "which", lambda name: "docker")
    report = deployment_gate.build_report(
        project_name="isolated-gate",
        env_file=env_file,
        restart_check=True,
        timeout=0.2,
        runner=runner,
        clock=clock,
        sleeper=sleeper,
    )

    assert report["status"] == "FAIL"
    assert report["restart_recovery"]["status"] == "fail"
    assert report["restart_recovery"]["timed_out"] is True
    assert report["restart_recovery"]["not_recovered"] == RESTART_SERVICES
    restart = next(command for command in calls if "restart" in command)
    assert restart[-4:] == RESTART_SERVICES
    assert not any(service in restart for service in ("api", "db", "queue", "web"))
