from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Article, Connection, Page, Site, Team
from test_platform import platform


def _cached_author_page(site_id: str, author_id: str = "41") -> Page:
    return Page(
        site_id=site_id,
        resource_key=f"authors:{author_id}",
        resource_type="authors",
        url=f"https://example.test/author/{author_id}",
        title="Cached author page",
        source={"author_id": author_id, "name": "Removed author"},
        signals={"author_discovery": {"items": [{"id": author_id, "name": "Removed author"}]}},
    )


def _add_wordpress_connection(factory, site_id: str, *, capabilities: dict | None = None) -> None:
    with factory() as db:
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="offline-encrypted-fixture",
            status="connected",
            capabilities=capabilities or {},
        ))
        db.commit()


def _author_client(monkeypatch, discover):
    from app import workflows

    class FakeWordPressClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def discover_authors(self):
            return await discover()

    async def client_for(db, site, kind="wordpress"):
        assert kind == "wordpress"
        return FakeWordPressClient()

    monkeypatch.setattr(workflows, "client_for", client_for)


def test_authors_get_uses_fresh_discovery_when_cached_page_has_removed_user(platform, monkeypatch):
    client, factory, site_id = platform
    _add_wordpress_connection(factory, site_id, capabilities={
        "author_discovery": {
            "items": [{"id": "41", "name": "Removed author"}],
            "complete": True,
            "blockers": [],
        },
    })
    with factory() as db:
        db.add(_cached_author_page(site_id))
        db.commit()

    async def discover():
        return {
            "items": [{"id": 1, "name": "Current editor"}],
            "complete": True,
            "authenticated_user_id": "1",
            "blockers": [],
        }

    _author_client(monkeypatch, discover)
    response = client.get(f"/api/v1/sites/{site_id}/authors")

    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{"id": "1", "name": "Current editor"}]
    assert response.json()["complete"] is True
    assert "Removed author" not in response.text
    with factory() as db:
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == "wordpress",
        ))
        saved = connection.capabilities["author_discovery"]
        assert saved["items"] == [{"id": "1", "name": "Current editor"}]
        assert saved["connection_fingerprint"]
        assert saved["connection_fingerprint"] != connection.encrypted_credentials


@pytest.mark.parametrize("scenario", ["no_connection", "failed", "incomplete"])
def test_authors_get_never_falls_back_to_cached_users(platform, monkeypatch, scenario):
    client, factory, site_id = platform
    with factory() as db:
        db.add(_cached_author_page(site_id))
        db.commit()

    if scenario != "no_connection":
        _add_wordpress_connection(factory, site_id, capabilities={
            "author_discovery": {
                "items": [{"id": "41", "name": "Removed author"}],
                "complete": True,
                "blockers": [],
            },
        })

    async def discover():
        if scenario == "failed":
            raise RuntimeError("private provider diagnostic")
        return {
            "items": [{"id": "41", "name": "Removed author"}],
            "complete": False,
            "blockers": ["author_listing_incomplete"],
        }

    _author_client(monkeypatch, discover)
    response = client.get(f"/api/v1/sites/{site_id}/authors")

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["complete"] is False
    assert "Removed author" not in response.text
    assert "private provider diagnostic" not in response.text


def test_authors_endpoint_does_not_cross_tenant_boundary(platform, monkeypatch):
    client, factory, _site_id = platform
    with factory() as db:
        foreign_team = Team(name="Other tenant")
        db.add(foreign_team)
        db.flush()
        foreign_site = Site(
            team_id=foreign_team.id,
            name="Other tenant site",
            origin="https://other.example.test",
        )
        db.add(foreign_site)
        db.flush()
        foreign_site_id = foreign_site.id
        db.add(Connection(
            site_id=foreign_site_id,
            kind="wordpress",
            encrypted_credentials="foreign-tenant-fixture",
            status="connected",
            capabilities={"author_discovery": {
                "items": [{"id": "88", "name": "Private tenant user"}],
                "complete": True,
                "blockers": [],
            }},
        ))
        db.commit()

    calls = []

    async def forbidden_discovery():
        calls.append(True)
        return {"items": [{"id": "88", "name": "Private tenant user"}], "complete": True}

    _author_client(monkeypatch, forbidden_discovery)
    response = client.get(f"/api/v1/sites/{foreign_site_id}/authors")

    assert response.status_code == 404
    assert "Private tenant user" not in response.text
    assert calls == []


def test_article_check_rejects_author_seen_only_in_cached_inventory(platform, monkeypatch):
    client, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.facts = {"authors": [{"id": "41", "name": "Removed author"}]}
        page = _cached_author_page(site_id)
        article = Article(
            site_id=site_id,
            title="A useful repair guide",
            body="<p>Useful repair guidance.</p>",
            author_id="41",
            sources=[],
        )
        db.add_all([page, article])
        db.commit()
        article_id = article.id

    from app.intelligence import content

    monkeypatch.setattr(content, "check_article", lambda *args, **kwargs: {
        "passed": True,
        "blockers": [],
        "warnings": [],
    })
    response = client.post(f"/api/v1/sites/{site_id}/articles/{article_id}/check")

    assert response.status_code == 200, response.text
    assert response.json()["passed"] is False
    assert "author_not_verified" in response.json()["blockers"]
    assert response.json()["author_discovery"]["items"] == []


def test_author_result_is_discarded_if_connection_is_revoked_during_fetch(platform, monkeypatch):
    client, factory, site_id = platform
    _add_wordpress_connection(factory, site_id)

    from app import workflows

    async def client_for(db, site, kind="wordpress"):
        assert kind == "wordpress"
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site.id,
            Connection.kind == "wordpress",
        ))

        class RevokingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def discover_authors(self):
                connection.status = "revoked"
                db.flush()
                return {
                    "items": [{"id": "1", "name": "Should be discarded"}],
                    "complete": True,
                    "authenticated_user_id": "1",
                    "blockers": [],
                }

        return RevokingClient()

    monkeypatch.setattr(workflows, "client_for", client_for)
    response = client.get(f"/api/v1/sites/{site_id}/authors")

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["complete"] is False
    assert "wordpress_connection_changed" in response.json()["blockers"]
    with factory() as db:
        connection = db.scalar(select(Connection).where(
            Connection.site_id == site_id,
            Connection.kind == "wordpress",
        ))
        assert connection.status == "revoked"
