"""Focused evidence for the rehearsal's automatic content-autopilot path."""

import json

from scripts.pilot_rehearsal import run_rehearsal


def test_rehearsal_covers_automatic_content_autopilot_scheduling():
    report = run_rehearsal()
    scheduling = report["checks"]["automatic_content_scheduling"]

    assert report["status"] == "REHEARSAL_PASS"
    assert report["simulation"] is True
    assert report["real_seven_day_acceptance_gate"] == "NOT_RUN"
    assert scheduling == {
        "status": "verified_in_simulation",
        "policy_limit_per_local_week": 1,
        "initial_window_parent_count": 1,
        "same_window_duplicate_parents": 0,
        "active_parent_reservation_holds": 1,
        "weekly_quota_holds": 1,
        "uncertain_outcome_holds": 1,
        "reconciled_parent_count": 1,
        "parents_queued_after_reconciliation": 1,
        "parent_count_total": 2,
        "automatic_publish_jobs": 0,
        "published_remotely": 0,
    }


def test_rehearsal_autopilot_evidence_is_scalar_and_secret_safe():
    report = run_rehearsal()
    scheduling = report["checks"]["automatic_content_scheduling"]
    rendered = json.dumps(scheduling, sort_keys=True)

    assert all(isinstance(value, (bool, int, str)) for value in scheduling.values())
    assert "fixture" not in rendered.casefold()
    assert "credential" not in rendered.casefold()
    assert "password" not in rendered.casefold()
    assert "http" not in rendered.casefold()
