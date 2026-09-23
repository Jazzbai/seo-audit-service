"""Focused checks for the local seven-day rehearsal fixture."""

import json

from scripts.pilot_rehearsal import run_rehearsal


def test_rehearsal_is_explicitly_simulated_and_secret_safe():
    report = run_rehearsal()
    rendered = json.dumps(report, sort_keys=True)

    assert report["status"] == "REHEARSAL_PASS"
    assert report["simulation"] is True
    assert report["real_seven_day_acceptance_gate"] == "NOT_RUN"
    assert report["network_access"] == "disabled_for_fixture"
    assert "fixture-only" not in rendered
    assert "encrypted_credentials" not in rendered
    assert "password" not in rendered.casefold()


def test_rehearsal_verifies_cadence_quota_recovery_and_guards():
    report = run_rehearsal()
    checks = report["checks"]

    cadence = checks["daily_weekly_cadence"]
    assert cadence["status"] == "verified_in_simulation"
    assert cadence["scheduled_slots"] == {
        "availability": 8,
        "poll_changes": 8,
        "inventory": 8,
        "audit": 2,
        "plan": 2,
        "refresh": 2,
    }
    assert all(
        cadence["scheduled_slots"][kind] <= limit
        for kind, limit in cadence["limits"].items()
    )

    quota = checks["publication_quota"]
    assert quota["status"] == "verified_in_simulation"
    assert quota["automatic_publish_jobs"] == 2
    assert quota["automatic_publish_jobs"] <= quota["policy_limit_per_local_week"]
    assert quota["deferred_quota_events"] >= 1
    assert quota["published_remotely"] == 0

    recovery = checks["worker_interruption_recovery"]
    assert recovery == {
        "status": "verified_in_simulation",
        "interruption_events": 1,
        "recovered_job_status": "complete",
        "remote_outcome": "not_contacted",
    }

    guards = checks["pause_and_protected_write_guards"]
    assert guards["status"] == "verified_in_simulation"
    assert guards["site_paused_after_rehearsal"] is True
    assert guards["pause_held_publish_jobs"] == 1
    assert guards["protected_path_blocked"] is True
    assert guards["paused_site_blocked"] is True


def test_command_returns_json_evidence_without_real_acceptance_claim(capsys):
    from scripts.pilot_rehearsal import main

    assert main(["--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)

    assert report["status"] == "REHEARSAL_PASS"
    assert report["real_seven_day_acceptance_gate"] == "NOT_RUN"
    assert "production acceptance result" in " ".join(report["limitations"])
