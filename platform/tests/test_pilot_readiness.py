import json
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import create_engine

from app.models import Base
from scripts.backup import create_backup
from scripts import pilot_readiness
from scripts.pilot_readiness import (
    _compose_rows,
    build_report,
    load_env_file,
    probe_health,
    probe_backup,
    probe_compose,
    probe_compose_prerequisites,
    probe_compose_schedule_write_ownership,
    summarize_compose,
    summarize_compose_prerequisites,
)


def production_environment():
    return {
        "DB_PASSWORD": "db-" + "x" * 40,
        "QUEUE_PASSWORD": "queue-" + "x" * 40,
        "ENCRYPTION_KEY": "encrypt-" + "x" * 40,
        "BOOTSTRAP_TOKEN": "bootstrap-" + "x" * 40,
        "PUBLIC_URL": "https://seo.forgeseo.com",
        "APP_ADDRESS": "seo.forgeseo.com",
        "COOKIE_SECURE": "true",
    }


def compose_rows(*, backup=False):
    services = ["api", "beat", "browser", "db", "queue", "scheduler-worker", "web", "worker"]
    if backup:
        services.append("backup")
    return [
        {"Service": service, "State": "running", "Health": "healthy"}
        for service in services
    ]


def _backup_readiness_fixture(tmp_path: Path):
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "evidence.html").write_text("<h1>Evidence</h1>", encoding="utf-8")

    backup_key = Fernet.generate_key()
    credential_key = "application-encryption-key-" + "x" * 40
    payload = create_backup(database, source, backup_key, credential_key)
    local = tmp_path / "local-backups"
    local.mkdir()
    archive = local / "forgeseo-readiness.forge"
    archive.write_bytes(payload)
    mirror = tmp_path / "backup-mirror"
    mirror.mkdir()
    return local, archive, mirror, backup_key, credential_key


def ownership_rows(
    *,
    project="forgeseo-platform",
    root="D:/Adib/ForgeSEOPlatform",
    config="D:/Adib/ForgeSEOPlatform/compose.yaml",
):
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.project.working_dir": root,
        "com.docker.compose.project.config_files": config,
    }
    return [
        {"Labels": {**labels, "com.docker.compose.service": service}}
        for service in ("beat", "scheduler-worker")
    ]


def test_report_is_secret_safe_and_keeps_external_gates_visible():
    environment = production_environment()
    environment["DB_PASSWORD"] = "db-secret-that-must-not-be-printed-1234567890"

    report = build_report(environment)
    rendered = json.dumps(report, sort_keys=True)

    assert report["status"] == "NOT_READY"
    assert "external_connections" in report["outstanding"]
    assert "unattended_pilot" in report["outstanding"]
    assert environment["DB_PASSWORD"] not in rendered
    assert "--health-url" in report["checks"][1]["detail"]


def test_env_file_loader_supports_comments_export_and_quoted_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport DB_PASSWORD='safe=value'\nQUEUE_PASSWORD=queue\nIGNORED_LINE\n",
        encoding="utf-8",
    )

    assert load_env_file(env_file) == {
        "DB_PASSWORD": "safe=value",
        "QUEUE_PASSWORD": "queue",
    }


def test_health_probe_accepts_only_the_json_ok_contract():
    class Response:
        status = 200

        def read(self):
            return b'{"status":"ok","service":"forgeseo-platform"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    result = probe_health("http://127.0.0.1:18080/health", opener=opener)

    assert result["status"] == "pass"
    assert result["target"] == "127.0.0.1:18080"
    assert seen["timeout"] == 8


def test_health_probe_fails_closed_for_valid_json_with_the_wrong_shape():
    class Response:
        status = 200

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    result = probe_health(
        "http://127.0.0.1:18080/health",
        opener=lambda request, timeout: Response(),
    )

    assert result["status"] == "fail"
    assert result["detail"] == "health endpoint did not return status ok"
    assert result["target"] == "127.0.0.1:18080"


def test_health_probe_does_not_echo_malformed_or_credentialed_targets():
    malformed = probe_health("http://127.0.0.1:not-a-port/health")
    credentialed = probe_health("http://secret:password@127.0.0.1/health")

    assert malformed["status"] == "fail"
    assert malformed["target"] == "configured target"
    assert credentialed["status"] == "fail"
    assert credentialed["target"] == "127.0.0.1"
    assert "password" not in json.dumps(credentialed)


def test_https_health_probe_rejects_a_downgraded_redirect():
    class Response:
        status = 200

        def read(self):
            return b'{"status":"ok"}'

        def geturl(self):
            return "http://127.0.0.1:18080/health"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    result = probe_health(
        "https://seo.forgeseo.com/health",
        opener=lambda request, timeout: Response(),
        require_https=True,
    )

    assert result["status"] == "fail"
    assert result["detail"] == "health endpoint did not remain on HTTPS"


def test_https_health_probe_rejects_a_cross_origin_redirect():
    class Response:
        status = 200

        def read(self):
            return b'{"status":"ok"}'

        def geturl(self):
            return "https://other.forgeseo.com/health"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    result = probe_health(
        "https://seo.forgeseo.com/health",
        opener=lambda request, timeout: Response(),
        require_https=True,
        expected_origin="https://seo.forgeseo.com",
    )

    assert result["status"] == "fail"
    assert result["detail"] == (
        "health endpoint did not remain on the configured public origin"
    )


def test_health_probe_rejects_a_different_public_origin_before_request():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise AssertionError("an unrelated health origin must not be contacted")

    result = probe_health(
        "https://other.forgeseo.com/health",
        opener=opener,
        require_https=True,
        expected_origin="https://seo.forgeseo.com",
    )

    assert result["status"] == "fail"
    assert result["detail"] == "health URL does not match the configured public origin"
    assert calls == []


def test_compose_summary_requires_all_core_services_and_hides_raw_output():
    raw = json.dumps(compose_rows())

    assert len(_compose_rows(raw)) == 8
    result = summarize_compose(_compose_rows(raw))
    assert result["status"] == "pass"
    assert result["service_count"] == 8
    assert "password" not in json.dumps(result).casefold()

    missing = summarize_compose(_compose_rows(raw)[:-1])
    assert missing["status"] == "fail"
    assert "worker" in missing["missing"]


def test_compose_summary_fails_closed_for_non_running_required_services():
    for state in ("created", "exited", "paused"):
        rows = compose_rows()
        rows[0]["State"] = state
        rows[0]["Health"] = ""

        result = summarize_compose(rows)

        assert result["status"] == "fail"
        assert result["detail"] == "required Compose services are not running"
        assert result["not_running"] == ["api"]


def test_compose_summary_fails_closed_while_a_healthcheck_is_starting():
    rows = compose_rows()
    rows[0]["Health"] = "starting"

    result = summarize_compose(rows)

    assert result["status"] == "fail"
    assert result["unhealthy"] == ["api"]


def test_compose_summary_requires_backup_only_when_requested():
    core_rows = _compose_rows(json.dumps(compose_rows()))

    ordinary = summarize_compose(core_rows)
    backup_missing = summarize_compose(core_rows, backup=True)
    backup_present = summarize_compose(_compose_rows(json.dumps(compose_rows(backup=True))), backup=True)

    assert ordinary["status"] == "pass"
    assert backup_missing["status"] == "fail"
    assert backup_missing["missing"] == ["backup"]
    assert backup_present["status"] == "pass"
    assert backup_present["service_count"] == 9


def test_compose_probe_only_reads_status(monkeypatch, tmp_path: Path):
    calls = []

    def which(name):
        assert name == "docker"
        return "docker"

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(compose_rows()), "secret stderr")

    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", which)
    result = probe_compose(root=tmp_path, runner=runner)

    assert result["status"] == "pass"
    assert calls[0][0][-3:] == ["ps", "--format", "json"]
    assert calls[0][1]["cwd"] == tmp_path


def test_compose_probe_requires_backup_service_when_requested(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(compose_rows(backup=True)), "secret stderr")

    result = probe_compose(root=tmp_path, backup=True, runner=runner)

    assert result["status"] == "pass"
    assert "secret" not in json.dumps(result).casefold()
    assert calls[0][0][-5:] == ["--profile", "backup", "ps", "--format", "json"]


def prerequisite_rows(*, backup=False, state="exited", exit_code=0):
    rows = [
        {"Service": "migrate", "State": state, "ExitCode": exit_code},
    ]
    if backup:
        rows.append({"Service": "backup-init", "State": state, "ExitCode": exit_code})
    return rows


def test_compose_prerequisites_pass_when_required_one_shots_exited_zero():
    result = summarize_compose_prerequisites(prerequisite_rows(backup=True), backup=True)

    assert result["status"] == "pass"
    assert result["completed"] == ["migrate", "backup-init"]


def test_compose_prerequisites_fail_when_required_one_shot_is_missing():
    result = summarize_compose_prerequisites(prerequisite_rows(), backup=True)

    assert result["status"] == "fail"
    assert result["missing"] == ["backup-init"]


def test_compose_prerequisites_fail_when_migration_is_missing():
    result = summarize_compose_prerequisites([], backup=False)

    assert result["status"] == "fail"
    assert result["missing"] == ["migrate"]


def test_compose_prerequisites_fail_when_required_one_shot_is_running():
    result = summarize_compose_prerequisites(
        prerequisite_rows(backup=True, state="running", exit_code=0),
        backup=True,
    )

    assert result["status"] == "fail"
    assert result["running"] == ["migrate", "backup-init"]


def test_compose_prerequisites_fail_when_required_one_shot_exited_nonzero():
    result = summarize_compose_prerequisites(
        prerequisite_rows(backup=True, exit_code=1),
        backup=True,
    )

    assert result["status"] == "fail"
    assert result["failed"] == ["migrate", "backup-init"]


def test_compose_prerequisite_probe_reads_all_without_starting_services(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(prerequisite_rows(backup=True)),
            "secret stderr",
        )

    result = probe_compose_prerequisites(root=tmp_path, backup=True, runner=runner)

    assert result["status"] == "pass"
    assert calls[0][0][-4:] == ["ps", "--all", "--format", "json"]
    assert "up" not in calls[0][0]
    assert "start" not in calls[0][0]
    assert "secret" not in json.dumps(result).casefold()


def test_compose_probe_classifies_engine_failures_without_echoing_diagnostics(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "500 Internal Server Error: daemon password=must-not-appear",
        )

    result = probe_compose(root=tmp_path, runner=runner)

    assert result == {
        "name": "compose_services",
        "status": "fail",
        "detail": "Docker engine did not accept the Compose status request",
    }


def test_compose_probe_reports_a_bounded_timeout(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout=20)

    result = probe_compose(root=tmp_path, runner=runner)

    assert result["status"] == "fail"
    assert result["detail"] == "Docker Compose status command timed out"


def test_compose_ownership_requires_one_matching_scheduler_pair(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")
    rows = ownership_rows(root=str(tmp_path), config=str(tmp_path / "compose.yaml"))
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(row) for row in rows),
            "",
        )

    result = probe_compose_schedule_write_ownership(root=tmp_path, runner=runner)

    assert result["status"] == "pass"
    assert result["owner_counts"] == {"beat": 1, "scheduler-worker": 1}
    assert calls[0][0][-2:] == ["--format", "{{json .}}"]
    assert calls[0][1]["cwd"] == tmp_path


def test_compose_ownership_parses_docker_label_string(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")
    mapping_rows = ownership_rows(root=str(tmp_path), config=str(tmp_path / "compose.yaml"))
    rows = [
        {
            "Labels": ",".join(f"{key}={value}" for key, value in row["Labels"].items())
        }
        for row in mapping_rows
    ]

    result = probe_compose_schedule_write_ownership(
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(row) for row in rows),
            "",
        ),
    )

    assert result["status"] == "pass"


def test_compose_ownership_is_not_verified_when_identity_is_incomplete(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")
    rows = ownership_rows(root=str(tmp_path), config=str(tmp_path / "compose.yaml"))
    del rows[0]["Labels"]["com.docker.compose.project.config_files"]

    result = probe_compose_schedule_write_ownership(
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps(rows),
            "secret stderr",
        ),
    )

    assert result["status"] == "not_verified"
    assert "incomplete" in result["detail"]
    assert "secret" not in json.dumps(result).casefold()


def test_compose_ownership_fails_for_duplicate_running_owners(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")
    rows = ownership_rows(root=str(tmp_path), config=str(tmp_path / "compose.yaml"))
    rows.extend(ownership_rows(root=str(tmp_path), config=str(tmp_path / "compose.yaml")))

    result = probe_compose_schedule_write_ownership(
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(row) for row in rows),
            "",
        ),
    )

    assert result["status"] == "fail"
    assert result["owner_counts"] == {"beat": 2, "scheduler-worker": 2}


def test_compose_ownership_rejects_a_mixed_owner_identity(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")
    rows = ownership_rows(root=str(tmp_path), config=str(tmp_path / "compose.yaml"))
    rows[1]["Labels"]["com.docker.compose.project.working_dir"] = str(tmp_path / "other")

    result = probe_compose_schedule_write_ownership(
        root=tmp_path,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            "\n".join(json.dumps(row) for row in rows),
            "",
        ),
    )

    assert result["status"] == "not_verified"
    assert result["detail"] == (
        "running scheduler/write containers use an unexpected Compose working directory"
    )


def test_compose_ownership_returns_not_verified_for_command_failure_or_timeout(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("scripts.pilot_readiness.shutil.which", lambda name: "docker")

    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "daemon secret=must-not-appear")

    failed_result = probe_compose_schedule_write_ownership(root=tmp_path, runner=failed)
    assert failed_result["status"] == "not_verified"
    assert "secret" not in json.dumps(failed_result).casefold()

    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout=20)

    timeout_result = probe_compose_schedule_write_ownership(root=tmp_path, runner=timed_out)
    assert timeout_result["status"] == "not_verified"
    assert "timed out" in timeout_result["detail"]


def test_report_surfaces_compose_ownership_gate(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "scripts.pilot_readiness.probe_compose",
        lambda **kwargs: {"name": "compose_services", "status": "pass", "detail": "ok"},
    )
    monkeypatch.setattr(
        "scripts.pilot_readiness.probe_compose_schedule_write_ownership",
        lambda **kwargs: {
            "name": "compose_schedule_write_ownership",
            "status": "not_verified",
            "detail": "identity unavailable",
        },
    )
    monkeypatch.setattr(
        "scripts.pilot_readiness.probe_compose_prerequisites",
        lambda **kwargs: {
            "name": "compose_prerequisites",
            "status": "pass",
            "detail": "ok",
        },
    )

    report = build_report(production_environment(), compose=True, root=tmp_path)

    ownership = next(
        check for check in report["checks"] if check["name"] == "compose_schedule_write_ownership"
    )
    assert ownership["status"] == "not_verified"
    assert "compose_schedule_write_ownership" in report["outstanding"]


def test_backup_check_is_optional_and_missing_archive_is_explicit(tmp_path: Path):
    report = build_report(production_environment(), backup=True, backup_directory=tmp_path)

    backup_check = next(check for check in report["checks"] if check["name"] == "encrypted_backup")
    assert backup_check["status"] == "fail"
    assert "archive" not in json.dumps(backup_check).casefold()


def test_backup_readiness_verifies_a_configured_exact_mirror(tmp_path: Path, monkeypatch):
    local, archive, mirror, backup_key, credential_key = _backup_readiness_fixture(tmp_path)
    mirrored_archive = mirror / archive.name
    mirrored_archive.write_bytes(archive.read_bytes())
    environment = production_environment()
    environment.update(
        {
            "BACKUP_KEY": backup_key.decode("ascii"),
            "ENCRYPTION_KEY": credential_key,
            "BACKUP_MIRROR_DIRECTORY": str(mirror),
        }
    )
    calls = {}
    real_inspector = pilot_readiness.inspect_latest_backup

    def inspect(*args, **kwargs):
        calls["mirror_directory"] = kwargs.get("mirror_directory")
        return real_inspector(*args, **kwargs)

    monkeypatch.setattr(pilot_readiness, "inspect_latest_backup", inspect)

    result = probe_backup(environment, directory=local)

    assert result["status"] == "pass"
    assert "decryptable" in result["detail"]
    assert "mirror copy is verified" in result["detail"]
    assert calls["mirror_directory"] == str(mirror)
    assert str(mirror) not in json.dumps(result)


def test_backup_readiness_fails_secret_safely_when_configured_mirror_is_missing(tmp_path: Path):
    local, archive, mirror, backup_key, credential_key = _backup_readiness_fixture(tmp_path)
    environment = production_environment()
    environment.update(
        {
            "BACKUP_KEY": backup_key.decode("ascii"),
            "ENCRYPTION_KEY": credential_key,
            "BACKUP_MIRROR_DIRECTORY": str(mirror),
        }
    )

    result = probe_backup(environment, directory=local)

    assert result["status"] == "fail"
    assert result["detail"] == "backup inspection failed (RuntimeError)"
    assert str(mirror) not in json.dumps(result)


def test_backup_key_shape_is_not_exposed_in_report():
    environment = production_environment()
    environment["BACKUP_KEY"] = Fernet.generate_key().decode("ascii")
    report = build_report(environment, backup=True)

    assert environment["BACKUP_KEY"] not in json.dumps(report)
