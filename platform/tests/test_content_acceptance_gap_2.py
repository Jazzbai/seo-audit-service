from __future__ import annotations

from app.intelligence.content import plan_topics


def test_topic_planner_keeps_non_article_inventory_out_of_refresh_work():
    briefs = plan_topics(
        {},
        [
            {
                "title": "Repair product",
                "url": "https://northwind.example/product/repair",
                "resource_type": "products",
                "enrolled": True,
            },
            {
                "title": "Editorial author",
                "url": "https://northwind.example/author/editorial",
                "resource_type": "authors",
                "enrolled": True,
            },
            {
                "title": "Existing guide",
                "url": "https://northwind.example/guides/existing",
                "resource_type": "posts",
                "enrolled": True,
            },
        ],
    )

    assert [(brief["title"], brief["purpose"]) for brief in briefs] == [
        ("Existing guide", "refresh_existing"),
    ]
