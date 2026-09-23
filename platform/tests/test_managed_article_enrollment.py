from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Article, Base, Site, Team
from app.workflows import store_page


def test_inventory_auto_enrolls_platform_created_article_but_not_existing_content():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        team = Team(name="Managed content team")
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name="Managed content site", origin="https://managed.example.test")
        db.add(site)
        db.flush()
        managed = Article(site_id=site.id, title="Platform article", remote_id="101", managed=True)
        existing = Article(site_id=site.id, title="Existing article", remote_id="102", managed=False)
        db.add_all([managed, existing])
        db.flush()

        managed_page = store_page(db, site, {
            "resource_key": "post:101",
            "resource_type": "posts",
            "id": 101,
            "url": f"{site.origin}/platform-article",
            "title": "Platform article",
        })
        existing_page = store_page(db, site, {
            "resource_key": "post:102",
            "resource_type": "posts",
            "id": 102,
            "url": f"{site.origin}/existing-article",
            "title": "Existing article",
        })

        assert managed_page.enrolled is True
        assert managed_page.managed is True
        assert managed_page.signals["enrollment"]["mode"] == "platform_created"
        assert managed_page.signals["enrollment"]["article_id"] == managed.id
        assert existing_page.enrolled is False
        assert "enrollment" not in existing_page.signals
    engine.dispose()
