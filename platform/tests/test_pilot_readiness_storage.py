import json
from collections import namedtuple
from pathlib import Path

from scripts.pilot_readiness import build_report, probe_storage


def test_storage_check_is_opt_in_and_does_not_change_default_report():
    report = build_report({})

    assert "storage_capacity" not in {check["name"] for check in report["checks"]}


def test_storage_check_passes_at_configured_free_byte_threshold(monkeypatch, tmp_path: Path):
    usage = namedtuple("usage", "total used free")
    calls = []

    def disk_usage(path):
        calls.append(path)
        return usage(1000, 400, 600)

    monkeypatch.setattr("scripts.pilot_readiness.shutil.disk_usage", disk_usage)

    result = probe_storage(tmp_path, minimum_free_bytes=600)

    assert result == {
        "name": "storage_capacity",
        "status": "pass",
        "detail": "storage path meets the minimum free-space threshold",
        "free_bytes": 600,
        "minimum_free_bytes": 600,
    }
    assert calls == [tmp_path]


def test_storage_check_fails_below_threshold_without_echoing_path(monkeypatch):
    usage = namedtuple("usage", "total used free")
    secret_path = "D:/private/secret-storage"

    monkeypatch.setattr(
        "scripts.pilot_readiness.shutil.disk_usage",
        lambda path: usage(1000, 900, 100),
    )

    result = probe_storage(secret_path, minimum_free_bytes=101)

    assert result["status"] == "fail"
    assert result["free_bytes"] == 100
    assert result["minimum_free_bytes"] == 101
    assert secret_path not in json.dumps(result)


def test_storage_check_reports_unreadable_capacity_without_exposing_error(monkeypatch):
    secret_path = "D:/private/secret-storage"

    def disk_usage(path):
        raise OSError("permission denied for secret-storage")

    monkeypatch.setattr("scripts.pilot_readiness.shutil.disk_usage", disk_usage)

    result = probe_storage(secret_path, minimum_free_bytes=1)

    assert result == {
        "name": "storage_capacity",
        "status": "fail",
        "detail": "storage capacity could not be read",
    }
    assert secret_path not in json.dumps(result)


def test_storage_threshold_can_come_from_environment(monkeypatch, tmp_path: Path):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "scripts.pilot_readiness.shutil.disk_usage",
        lambda path: usage(1000, 400, 600),
    )

    report = build_report(
        {"STORAGE_MIN_FREE_BYTES": "601"},
        storage_path=tmp_path,
    )

    check = next(check for check in report["checks"] if check["name"] == "storage_capacity")
    assert check["status"] == "fail"
    assert check["minimum_free_bytes"] == 601
