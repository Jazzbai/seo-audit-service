from deploy import healthcheck


def test_process_health_rejects_a_reused_pid_for_an_unrelated_process(
    tmp_path, monkeypatch
):
    pidfile = tmp_path / "beat.pid"
    pidfile.write_text("123", encoding="ascii")
    monkeypatch.setattr(healthcheck.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(
        healthcheck,
        "_read_process_commandline",
        lambda pid: ("/usr/local/bin/python", "unrelated-service.py"),
    )

    assert not healthcheck.check_pidfile(str(pidfile))


def test_process_health_accepts_the_expected_celery_beat_command(
    tmp_path, monkeypatch
):
    pidfile = tmp_path / "beat.pid"
    pidfile.write_text("123", encoding="ascii")
    monkeypatch.setattr(healthcheck.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(
        healthcheck,
        "_read_process_commandline",
        lambda pid: (
            "/usr/local/bin/python",
            "/usr/local/bin/celery",
            "-A",
            "app.worker:celery",
            "beat",
            "--pidfile=/tmp/forgeseo-beat.pid",
        ),
    )

    assert healthcheck.check_pidfile(str(pidfile))
