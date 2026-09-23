import json
import os
import shutil
import subprocess
from datetime import timedelta
from uuid import uuid4
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select

from app.connectors.security import encrypt_credentials
from app.models import Base, Connection, Event, Heartbeat, Job, Membership, Session, Site, Team, User, utcnow
from scripts.backup import inspect_backup, restore_backup
from scripts.split_backup_loop import capture, interval_seconds
from scripts.split_restore_drill import validate_target, verify_recovery


ROOT = Path(__file__).resolve().parents[1]


def drill_environment():
    return {"DATABASE_URL": "postgresql+psycopg://restore:" + "x" * 40 + "@restore-db:5432/forgeseo_restore_drill",
            "GLOBAL_PAUSE": "true", "RESTORE_DRILL": "true", "ARTIFACT_ROOT": "/srv/restore-artifacts",
            "BACKUP_ARCHIVE": "forgeseo-20260923T120000Z-abc.forge"}


@pytest.mark.parametrize("change", [
    {"DATABASE_URL": "postgresql+psycopg://owner:secret@production/db"},
    {"DATABASE_URL": drill_environment()["DATABASE_URL"] + "?hostaddr=10.20.0.9"},
    {"DATABASE_URL": drill_environment()["DATABASE_URL"].replace("/forgeseo_restore_drill", "/production")},
    {"GLOBAL_PAUSE": "false"}, {"RESTORE_DRILL": "false"},
    {"ARTIFACT_ROOT": "/srv/artifacts"}, {"BACKUP_ARCHIVE": "../backup.forge"},
    {"BACKUP_ARCHIVE": "/srv/backups/forgeseo-test.forge"},
])
def test_restore_drill_rejects_production_and_unscoped_targets(change):
    with pytest.raises(ValueError):
        validate_target(drill_environment() | change)


def test_restore_drill_accepts_only_explicit_internal_target():
    assert validate_target(drill_environment()) == "forgeseo-20260923T120000Z-abc.forge"


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "59", "604801", "bad"])
def test_backup_interval_is_bounded(value):
    with pytest.raises(ValueError):
        interval_seconds(value)


def make_recovery(tmp_path):
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    credential_key = Fernet.generate_key().decode()
    backup_key = Fernet.generate_key().decode()
    secret = "fixture-only-password-that-must-not-be-printed"
    with database.begin() as connection:
        connection.execute(Team.__table__.insert(), {"id":"team", "name":"Recovery fixture"})
        connection.execute(User.__table__.insert(), {"id":"user", "email":"owner@example.test", "name":"Fixture owner", "password_hash":"synthetic-not-a-password-hash"})
        connection.execute(Membership.__table__.insert(), {"team_id":"team", "user_id":"user", "role":"owner"})
        connection.execute(Session.__table__.insert(), {"user_id":"user", "team_id":"team", "token_hash":"synthetic-session", "csrf_token":"synthetic-csrf", "expires_at":utcnow() + timedelta(hours=1)})
        connection.execute(Site.__table__.insert(), {"id":"site", "team_id":"team", "name":"Recovery fixture", "origin":"https://example.test", "paused":False})
        connection.execute(Event.__table__.insert(), {"id":42, "team_id":"team", "site_id":"site", "kind":"fixture", "message":"Historical recovery evidence"})
        connection.execute(Connection.__table__.insert(), {"site_id":"site", "kind":"wordpress", "encrypted_credentials":encrypt_credentials({"password":secret},credential_key)})
        connection.execute(Heartbeat.__table__.insert(), {"name":"platform_controls", "last_seen_at":utcnow(), "details":{"global_pause":False}})
        connection.execute(Job.__table__.insert(), {"site_id":"site", "kind":"publish", "status":"running", "payload":{}, "result":{}, "idempotency_key":"recovery-fixture"})
    artifacts = tmp_path / "source"
    artifacts.mkdir()
    (artifacts / "evidence.html").write_text("<h1>Evidence</h1>")
    summary = capture(database, artifacts, tmp_path / "backups", backup_key, credential_key)
    payload = (tmp_path / "backups" / summary["archive"]).read_bytes()
    assert secret.encode() not in payload
    assert secret not in json.dumps(summary)
    assert summary["off_host_copy"] == "not_configured"
    assert "sessions" not in inspect_backup(payload, backup_key, credential_key)["tables"]
    target = create_engine("sqlite://")
    Base.metadata.create_all(target)
    target_artifacts = tmp_path / "recovered"
    restore_backup(target, target_artifacts, payload, backup_key, credential_key)
    return target, target_artifacts, payload, backup_key, credential_key


def test_recovery_verifies_rows_artifacts_credentials_and_paused_state(tmp_path):
    arguments = make_recovery(tmp_path)
    report = verify_recovery(*arguments)
    assert report["credentials_decrypted"] == 1
    assert report["artifacts_verified"] == 1
    assert report["automation"] == "paused"
    assert report["browser_sessions"] == "invalidated"
    assert report["external_requests"] == 0
    with arguments[0].connect() as connection:
        assert connection.scalar(select(Job.status)) == "needs_reconciliation"
        assert connection.scalar(select(Site.paused)) is True
        assert connection.scalar(select(Session.id)) is None
        assert connection.scalar(select(Membership.role)) == "owner"
        assert connection.scalar(select(Event.id)) == 42


@pytest.mark.parametrize("tamper", ["row", "artifact", "pause", "credential"])
def test_recovery_rejects_inexact_data_or_unsafe_state(tmp_path, tamper):
    arguments = make_recovery(tmp_path)
    database, artifact_root = arguments[:2]
    if tamper == "artifact":
        (artifact_root / "evidence.html").write_text("changed")
    else:
        with database.begin() as connection:
            if tamper == "row":
                connection.execute(Team.__table__.update().values(name="changed"))
            elif tamper == "pause":
                connection.execute(Site.__table__.update().values(paused=False))
            else:
                connection.execute(Connection.__table__.update().values(encrypted_credentials="changed"))
    with pytest.raises(RuntimeError):
        verify_recovery(*arguments)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_recovery_compose_is_isolated_from_production(tmp_path):
    values = {"RESTORE_DB_PASSWORD":"x"*40, "RESTORE_IMAGE":"forgeseo-recovery-fixture:local",
              "RESTORE_ENCRYPTION_KEY":"y"*40, "RESTORE_BACKUP_KEY":Fernet.generate_key().decode(),
              "RESTORE_ARCHIVE":"forgeseo-fixture.forge", "FORGE_BACKUP_VOLUME":"fixture_encrypted_backups"}
    # Explicit values override shell state; no user .env file is read.
    result = subprocess.run(["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT/"compose.restore-drill.yaml"),
                             "--project-name", "forgeseo-drill-test", "config", "--format", "json"],
                            env=os.environ|values, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, "Recovery Compose did not render"
    model = json.loads(result.stdout)
    assert set(model["services"]) == {"restore-db", "restore-init", "restore-check"}
    assert model["networks"]["isolated"]["internal"] is True
    for service in model["services"].values():
        assert not service.get("ports")
    checker = model["services"]["restore-check"]
    assert checker["environment"]["GLOBAL_PAUSE"] == "true"
    assert "restore-db:5432/forgeseo_restore_drill" in checker["environment"]["DATABASE_URL"]
    assert checker["environment"]["ARTIFACT_ROOT"] == "/srv/restore-artifacts"
    archive = next(v for v in checker["volumes"] if v["target"] == "/srv/backups")
    assert archive["read_only"] is True
    assert model["volumes"]["source_backups"]["external"] is True
    assert not model["volumes"]["restore_database"].get("external")
    assert not model["volumes"]["restore_artifacts"].get("external")


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_split_backup_profile_has_readonly_sources_and_no_worker_or_queue_keys():
    result = subprocess.run(["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT/"compose.backend.yaml"),
                             "--profile", "backup", "config", "--format", "json"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, "Backup Compose did not render"
    service = json.loads(result.stdout)["services"]["backup"]
    assert service["profiles"] == ["backup"]
    assert not service.get("ports")
    assert not {"QUEUE_PASSWORD", "BROKER_URL", "BOOTSTRAP_TOKEN"} & service["environment"].keys()
    assert service["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    for target in ("/srv/artifacts", "/run/forgeseo/db-ca.crt"):
        assert next(v for v in service["volumes"] if v["target"] == target)["read_only"] is True


@pytest.mark.skipif(not os.environ.get("FORGE_SPLIT_RECOVERY_IMAGE"), reason="Set an existing local backend image for the isolated Docker recovery drill")
def test_real_isolated_postgres_recovery_drill(tmp_path):
    arguments = make_recovery(tmp_path)
    _, _, _, backup_key, credential_key = arguments
    archive = next((tmp_path / "backups").glob("forgeseo-*.forge"))
    image = os.environ["FORGE_SPLIT_RECOVERY_IMAGE"]
    project = "forgeseo-recovery-check-" + uuid4().hex[:12]
    source_volume = project + "_encrypted_source"
    values = os.environ | {"RESTORE_DB_PASSWORD": uuid4().hex + uuid4().hex, "RESTORE_IMAGE": image,
                           "RESTORE_ENCRYPTION_KEY": credential_key, "RESTORE_BACKUP_KEY": backup_key,
                           "RESTORE_ARCHIVE": archive.name, "FORGE_BACKUP_VOLUME": source_volume}
    command = ["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT / "compose.restore-drill.yaml"), "--project-name", project]
    created = False
    try:
        subprocess.run(["docker", "volume", "create", "--label", "com.forgeseo.purpose=isolated-recovery-test", source_volume],
                       capture_output=True, text=True, timeout=20, check=True)
        created = True
        copied = subprocess.run(["docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--user", "0:0",
                                 "--mount", f"type=volume,source={source_volume},target=/srv/backups",
                                 "--mount", f"type=bind,source={archive.parent},target=/fixture,readonly",
                                 "--entrypoint", "sh", image, "-ec",
                                 f"cp /fixture/{archive.name} /srv/backups/{archive.name} && chown 10001:10001 /srv/backups/{archive.name} && chmod 600 /srv/backups/{archive.name}"],
                                capture_output=True, text=True, timeout=30)
        assert copied.returncode == 0, "Could not provision the isolated encrypted fixture"
        started = subprocess.run(command + ["up", "--detach", "restore-check"], env=values, capture_output=True, text=True, timeout=150)
        assert started.returncode == 0, "Isolated recovery stack failed to start"
        container = subprocess.run(command + ["ps", "--all", "--quiet", "restore-check"], env=values,
                                   capture_output=True, text=True, timeout=15).stdout.strip()
        assert len(container) == 64 and all(c in "0123456789abcdef" for c in container)
        finished = subprocess.run(["docker", "wait", container], capture_output=True, text=True, timeout=120)
        logs = subprocess.run(["docker", "logs", "--tail", "5", container], capture_output=True, text=True, timeout=15)
        assert finished.returncode == 0 and finished.stdout.strip() == "0", logs.stdout
        report = json.loads(logs.stdout.strip().splitlines()[-1])
        assert report["credentials_decrypted"] == 1 and report["artifacts_verified"] == 1
        assert report["automation"] == "paused" and report["browser_sessions"] == "invalidated"
        network = subprocess.run(["docker", "network", "inspect", project + "_isolated"], capture_output=True, text=True, timeout=15)
        assert json.loads(network.stdout)[0]["Internal"] is True
        # A retry must preserve the already-restored database instead of dropping it.
        retry = subprocess.run(command + ["run", "--rm", "--no-deps", "restore-check"], env=values,
                               capture_output=True, text=True, timeout=30)
        assert retry.returncode != 0
        assert "Restore drill failed (ValueError)" in retry.stdout
    finally:
        # Both names were created above with this test's unique prefix. No live
        # resources or other Compose projects are included in cleanup.
        subprocess.run(command + ["down", "--volumes"], env=values, capture_output=True, text=True, timeout=45, check=True)
        if created:
            subprocess.run(["docker", "volume", "rm", source_volume], capture_output=True, text=True, timeout=20, check=True)
