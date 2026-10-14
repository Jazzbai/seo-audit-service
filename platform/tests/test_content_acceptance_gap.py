from __future__ import annotations

from app.intelligence.content import plan_topics


def test_topic_planner_does_not_emit_duplicate_intent_across_new_candidates():
    briefs = plan_topics(
        {"services": ["Window repair"], "locations": ["Austin"]},
        [],
        keywords=["window repair Austin"],
    )

    assert [brief["title"] for brief in briefs] == ["Window repair in Austin"]


def test_brand_only_fallback_is_marked_for_editorial_review():
    briefs = plan_topics({"business_name": "Auto One Stop"}, [])

    assert len(briefs) == 1
    assert briefs[0]["title"] == "About Auto One Stop"
    assert briefs[0]["automation_review_required"] is True
