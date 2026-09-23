from __future__ import annotations

from app.intelligence.content import plan_topics


def test_topic_planner_does_not_emit_duplicate_intent_across_new_candidates():
    briefs = plan_topics(
        {"services": ["Window repair"], "locations": ["Austin"]},
        [],
        keywords=["window repair Austin"],
    )

    assert [brief["title"] for brief in briefs] == ["Window repair in Austin"]
