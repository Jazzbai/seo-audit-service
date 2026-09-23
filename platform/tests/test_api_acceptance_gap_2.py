"""Focused regression coverage for deterministic API collection pagination."""

from datetime import datetime

from sqlalchemy import delete, select

from app import api
from app.models import Event, Membership, Page, Session as AuthSession, Site
from app.operations import event
from test_platform import platform


def test_site_collection_pagination_breaks_timestamp_ties_deterministically(platform):
    client, factory, site_id = platform
    created_at = datetime(2026, 9, 18, 12, 0, 0)
    rows = [
        Page(
            id=page_id,
            site_id=site_id,
            resource_key=f"posts:{page_id}",
            url=f"https://example.test/{page_id}",
            title=page_id,
            created_at=created_at,
        )
        for page_id in ("page-a", "page-b", "page-c")
    ]
    with factory() as db:
        db.add_all(rows)
        db.commit()

    first = client.get(
        f"/api/v1/sites/{site_id}/pages",
        params={"limit": 2, "offset": 0},
    )
    second = client.get(
        f"/api/v1/sites/{site_id}/pages",
        params={"limit": 2, "offset": 2},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 3
    first_ids = [item["id"] for item in first.json()["items"]]
    second_ids = [item["id"] for item in second.json()["items"]]
    assert first_ids == ["page-c", "page-b"]
    assert second_ids == ["page-a"]
    assert set(first_ids).isdisjoint(second_ids)
    assert first_ids + second_ids == ["page-c", "page-b", "page-a"]


def test_open_event_stream_rechecks_team_access_before_each_poll(platform, monkeypatch):
    client, factory, site_id = platform
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        site = db.get(Site, site_id)
        before = db.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0
        event(db, site, "site_updated", "Visible before revocation", {})
        db.commit()

    revoked = False

    async def revoke_before_next_poll(_seconds):
        nonlocal revoked
        if revoked:
            return
        with factory() as db:
            session = db.scalar(select(AuthSession).order_by(AuthSession.created_at.desc()))
            assert session is not None
            db.execute(delete(Membership).where(
                Membership.user_id == session.user_id,
                Membership.team_id == session.team_id,
            ))
            site = db.get(Site, site_id)
            event(db, site, "site_updated", "Private after revocation", {})
            db.commit()
        revoked = True

    monkeypatch.setattr(api.asyncio, "sleep", revoke_before_next_poll)
    response = client.get(
        f"/api/v1/sites/{site_id}/events",
        headers={"Last-Event-ID": str(before)},
    )

    assert response.status_code == 200, response.text
    assert "Visible before revocation" in response.text
    assert "Private after revocation" not in response.text
