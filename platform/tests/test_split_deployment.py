import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from scripts.split_preflight import CA_PATH, validate_split_environment


ROOT = Path(__file__).resolve().parents[1]


def environment():
    return {
        "DATABASE_URL": URL.create(
            "postgresql+psycopg", username="forgeseo_app", password="db-" + "x" * 40,
            host="database.internal", port=5432, database="forgeseo_platform",
            query={"hostaddr": "10.20.0.9", "sslmode": "verify-full", "sslrootcert": CA_PATH},
        ).render_as_string(hide_password=False),
        "QUEUE_PASSWORD": "queue-" + "y" * 40,
        "ENCRYPTION_KEY": "encryption-" + "z" * 40,
        "BOOTSTRAP_TOKEN": "bootstrap-" + "b" * 40,
        "PUBLIC_URL": "https://seo.forgeseo.com",
        "FORGE_HOSTNAME": "seo.forgeseo.com",
        "APP_ADDRESS": "seo.forgeseo.com",
        "COOKIE_SECURE": "true",
        "GLOBAL_PAUSE": "true",
        "API_BIND_IP": "10.20.0.10",
        "TRUSTED_PROXY_IP": "10.20.0.13",
        "API_PORT": "18001",
        "FRONTEND_PORT": "18080",
    }


def test_split_preflight_accepts_verified_tls_configuration():
    assert validate_split_environment(environment()) == []


@pytest.mark.parametrize("change", [
    {"GLOBAL_PAUSE": "false"},
    {"COOKIE_SECURE": "false"},
    {"API_BIND_IP": "0.0.0.0"},
    {"API_BIND_IP": "8.8.8.8"},
    {"TRUSTED_PROXY_IP": "*"},
    {"TRUSTED_PROXY_IP": "127.0.0.1"},
    {"QUEUE_PASSWORD": "unsafe@password/" + "x" * 40},
    {"APP_ADDRESS": "wrong.domain.io"},
])
def test_split_preflight_rejects_unsafe_staging_settings(change):
    values = environment() | change
    errors = validate_split_environment(values)
    assert errors
    assert values["DATABASE_URL"] not in " ".join(errors)


@pytest.mark.parametrize("suffix", [
    "sslmode=require", "sslmode=disable", "sslmode=verify-ca",
    "sslmode=verify-full",  # Missing trusted certificate path.
    f"sslmode=verify-full&sslrootcert={CA_PATH}&sslmode=disable",
    f"sslmode=verify-full&sslrootcert={CA_PATH}&host=unexpected.internal",
    f"sslmode=verify-full&sslrootcert={CA_PATH}&user=postgres",
])
def test_database_parameters_cannot_weaken_tls_or_override_identity(suffix):
    values = environment()
    values["DATABASE_URL"] = values["DATABASE_URL"].split("?")[0] + "?" + suffix
    assert validate_split_environment(values)


@pytest.mark.parametrize("url", ["", "not-a-url-secret", "postgresql+psycopg://u:secret@host:bad/db", "sqlite:///local.db"])
def test_database_parser_errors_never_print_the_url(url):
    errors = validate_split_environment(environment() | {"DATABASE_URL": url})
    assert errors
    assert "secret" not in " ".join(errors)


def test_database_password_is_checked_instead_of_unrelated_db_password():
    values = environment()
    values["DATABASE_URL"] = values["DATABASE_URL"].replace("db-" + "x" * 40, values["QUEUE_PASSWORD"])
    values["DB_PASSWORD"] = "unrelated-" + "a" * 40
    errors = validate_split_environment(values)
    assert any("different from DATABASE_URL password" in error for error in errors)


def render_compose(filename, *, monitoring=False, settings=None, project_directory=None):
    # Never read the user's .env or contact any daemon/host with real credentials.
    values = os.environ.copy()
    # CI and host shell state must not accidentally supply configuration under test.
    for name in (*environment(), "COMPOSE_PROFILES", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
        values.pop(name, None)
    values.update(environment() if settings is None else settings)
    command = ["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT / filename)]
    if project_directory is not None:
        command += ["--project-directory", str(project_directory)]
    if monitoring:
        command += ["--profile", "monitoring"]
    result = subprocess.run(command + ["config", "--format", "json"],
                            cwd=ROOT, env=values, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "Split Compose rendering failed (output withheld to protect environment values)"
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_backend_is_external_db_private_port_and_paused_without_default_schedules():
    model = render_compose("compose.backend.yaml")
    services = model["services"]
    assert set(services) == {"queue", "migrate", "api", "worker", "browser"}
    assert "database" not in model["volumes"]
    assert services["api"]["ports"][0]["host_ip"] == "10.20.0.10"
    assert services["api"]["ports"][0]["published"] == "18001"
    assert services["api"]["ports"][0]["target"] == 8000
    assert "--forwarded-allow-ips=10.20.0.13" in services["api"]["command"]
    assert "split_preflight && alembic upgrade head" in services["migrate"]["command"][-1]
    for name, service in services.items():
        if name != "api":
            assert not service.get("ports")
        if name == "queue":
            assert service["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
            continue
        assert service["environment"]["GLOBAL_PAUSE"] == "true"
        assert service["environment"]["COOKIE_SECURE"] == "true"
        assert service["environment"]["DATABASE_URL"] == environment()["DATABASE_URL"]
        assert Path(service["build"]["context"]).resolve() == ROOT
        certificate = next(volume for volume in service["volumes"] if volume["target"] == CA_PATH)
        assert certificate["read_only"] is True
        assert certificate["source"].replace("\\", "/").endswith("/etc/forgeseo/db-ca.crt")
        assert certificate.get("bind", {}).get("create_host_path", False) is False
        if name != "migrate":
            assert service["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
            assert "healthcheck" in service


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_schedulers_require_explicit_monitoring_profile():
    services = render_compose("compose.backend.yaml", monitoring=True)["services"]
    for name in ("beat", "scheduler-worker"):
        assert services[name]["profiles"] == ["monitoring"]
        assert services[name]["environment"]["GLOBAL_PAUSE"] == "true"
        assert not services[name].get("ports")
        assert "healthcheck" in services[name]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_frontend_has_no_secrets_and_does_not_claim_existing_caddy_ports():
    services = render_compose("compose.frontend.yaml")["services"]
    assert set(services) == {"web"}
    web = services["web"]
    assert not web.get("environment")
    assert not web.get("volumes")
    assert web["read_only"] is True
    assert web["cap_drop"] == ["ALL"]
    assert web["cap_add"] == ["NET_BIND_SERVICE"]
    assert web["ports"] == [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 8080, "published": "18080", "protocol": "tcp"}]
    assert web["build"]["args"]["CADDYFILE"] == "deploy/split/Caddyfile.web"
    assert Path(web["build"]["context"]).resolve() == ROOT


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
@pytest.mark.parametrize("filename", ["compose.backend.yaml", "compose.frontend.yaml"])
def test_coolify_application_root_keeps_all_build_contexts_inside_repo(filename):
    services = render_compose(filename, project_directory=ROOT)["services"]
    for service in services.values():
        if "build" in service:
            assert Path(service["build"]["context"]).resolve() == ROOT
            assert not service["build"].get("args", {}).keys() & {
                "DATABASE_URL", "QUEUE_PASSWORD", "ENCRYPTION_KEY", "BOOTSTRAP_TOKEN"
            }


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_secretless_build_render_still_refuses_runtime_startup():
    services = render_compose("compose.backend.yaml", settings={}, project_directory=ROOT)["services"]
    startup = services["migrate"]["environment"]
    for name in ("DATABASE_URL", "QUEUE_PASSWORD", "ENCRYPTION_KEY", "BOOTSTRAP_TOKEN"):
        assert startup[name] == ""
    errors = validate_split_environment(startup)
    assert errors
    assert "DATABASE_URL is missing or malformed" in errors
    assert "ENCRYPTION_KEY is missing" in errors
    assert services["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["queue"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "split_preflight && alembic upgrade head" in services["migrate"]["command"][-1]


def test_default_web_image_keeps_original_caddyfile_and_split_edge_is_additive():
    dockerfile = (ROOT / "Dockerfile.web").read_text()
    assert "ARG CADDYFILE=deploy/Caddyfile" in dockerfile
    assert "COPY ${CADDYFILE} /etc/caddy/Caddyfile" in dockerfile
    edge = (ROOT / "deploy/split/Caddyfile.edge.example").read_text()
    assert "seo.example.com {" in edge
    assert "reverse_proxy 10.20.0.10:18001" in edge
    assert "reverse_proxy 127.0.0.1:18080" in edge
    assert "flush_interval -1" in edge
    assert "handle_path" not in edge  # API prefix must survive proxying.


@pytest.mark.skipif(not os.environ.get("FORGE_SPLIT_TEST_IMAGE"), reason="Set FORGE_SPLIT_TEST_IMAGE to an existing local web image")
def test_static_frontend_runs_read_only_without_network_or_published_ports():
    """Opt-in real runtime check; only its own disposable container is removed."""
    result = subprocess.run([
        "docker", "run", "--detach", "--rm", "--pull=never", "--network", "none",
        "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
        "--cap-add", "NET_BIND_SERVICE", "--security-opt", "no-new-privileges",
        "--label", "com.forgeseo.purpose=split-config-test",
        "--mount", f"type=bind,source={ROOT / 'deploy/split/Caddyfile.web'},target=/etc/caddy/Caddyfile,readonly",
        os.environ["FORGE_SPLIT_TEST_IMAGE"],
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    container = result.stdout.strip()
    assert len(container) == 64 and all(c in "0123456789abcdef" for c in container)
    try:
        deadline = time.monotonic() + 10
        while True:
            health = subprocess.run(["docker", "exec", container, "wget", "-qO-", "http://127.0.0.1:8080/healthz"],
                                    capture_output=True, text=True, timeout=5)
            if health.returncode == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        assert health.returncode == 0 and health.stdout == "ok", health.stderr
        for path in ("/", "/settings/connections"):
            page = subprocess.run(["docker", "exec", container, "wget", "-qO-", f"http://127.0.0.1:8080{path}"],
                                  capture_output=True, text=True, timeout=5)
            assert page.returncode == 0 and "<!doctype html>" in page.stdout.lower()
        for path in ("/api", "/api/v1/sites", "/health"):
            response = subprocess.run(["docker", "exec", container, "wget", "-S", "-O", "/dev/null", f"http://127.0.0.1:8080{path}"],
                                      capture_output=True, text=True, timeout=5)
            assert response.returncode != 0 and "503" in response.stderr
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", container],
                       capture_output=True, text=True, timeout=15, check=True)


@pytest.mark.skipif(not os.environ.get("FORGE_SPLIT_PACKAGED_IMAGE"), reason="Set FORGE_SPLIT_PACKAGED_IMAGE to a newly built split frontend image")
def test_new_web_image_packages_the_split_caddyfile_without_runtime_mounts():
    result = subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--entrypoint", "cat", os.environ["FORGE_SPLIT_PACKAGED_IMAGE"], "/etc/caddy/Caddyfile",
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (ROOT / "deploy/split/Caddyfile.web").read_text()
