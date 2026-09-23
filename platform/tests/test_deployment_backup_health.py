import os

import pytest

from scripts import backup_healthcheck


def test_backup_healthcheck_rejects_a_future_dated_latest_archive(tmp_path, monkeypatch):
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    archive = backup_directory / "forgeseo-future.forge"
    archive.write_bytes(b"encrypted archive")
    os.utime(archive, (2000.0, 2000.0))
    monkeypatch.setattr(backup_healthcheck, "inspect_backup", lambda *args: {})

    with pytest.raises(RuntimeError, match="timestamp is in the future"):
        backup_healthcheck.inspect_latest_backup(
            backup_directory,
            "backup-key",
            "credential-key",
            max_age_seconds=10,
            now=1000.0,
        )


def test_backup_healthcheck_rejects_nonfinite_freshness_inputs(tmp_path, monkeypatch):
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    archive = backup_directory / "forgeseo-invalid-clock.forge"
    archive.write_bytes(b"encrypted archive")
    os.utime(archive, (995.0, 995.0))
    monkeypatch.setattr(backup_healthcheck, "inspect_backup", lambda *args: {})

    with pytest.raises(RuntimeError, match="freshness limit must be positive"):
        backup_healthcheck.inspect_latest_backup(
            backup_directory,
            "backup-key",
            "credential-key",
            max_age_seconds=float("nan"),
            now=1000.0,
        )

    with pytest.raises(RuntimeError, match="archive metadata is invalid"):
        backup_healthcheck.inspect_latest_backup(
            backup_directory,
            "backup-key",
            "credential-key",
            max_age_seconds=10.0,
            now=float("nan"),
        )
