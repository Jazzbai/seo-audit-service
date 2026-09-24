import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select

from app import scheduler, workflows
from app.config import settings
from app.models import Article, Job, Site
from app.policies import create_policy, current_policy, evaluate_policy
from test_platform import platform
from test_publication_protection import seed
from test_scheduler_content_autopilot import autopilot_scheduler


@pytest.mark.parametrize('scope', [[], ['another-article']])
def test_unselected_article_never_opens_wordpress(platform, monkeypatch, scope):
    _, factory, site_id = platform
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    article_id = seed(factory, site_id, [])
    async def forbidden(*args): pytest.fail('An unselected article must not open WordPress')
    monkeypatch.setattr(workflows, 'client_for', forbidden)
    with factory() as db:
        site = db.get(Site, site_id)
        create_policy(db, site, None, {**current_policy(db, site_id).settings, 'publication_article_ids':scope})
        db.commit()
        with pytest.raises(ValueError, match='article_not_in_publication_scope'):
            asyncio.run(workflows.publish(db, site, Job(payload={'article_id':article_id})))


def test_selected_article_retains_other_safeguards(platform):
    _, factory, site_id = platform
    article_id = seed(factory, site_id, ['/repair-guide*'])
    with factory() as db:
        site = db.get(Site, site_id)
        policy = create_policy(db, site, None, {**current_policy(db, site_id).settings,
                                               'publication_article_ids':[article_id]})
        target = workflows.publication_target(site, db.get(Article, article_id))
        blockers = evaluate_policy(site, policy, 'publish', target, global_pause=True)
        assert 'article_not_in_publication_scope' not in blockers
        assert 'protected_path' in blockers and 'global_pause' in blockers
        assert 'article_not_in_publication_scope' in evaluate_policy(site,policy,'publish',{'url':target['url'],'enrolled':True})


def test_scope_must_belong_to_site_and_round_trips(platform):
    client, _, site_id = platform
    article = client.post(f'/api/v1/sites/{site_id}/articles',json={'title':'A complete selected article'}).json()
    policy = client.get(f'/api/v1/sites/{site_id}/policy').json()['settings']
    route = f'/api/v1/sites/{site_id}/policy'
    assert client.put(route,json={'settings':{**policy,'publication_article_ids':['foreign']}}).status_code == 422
    response = client.put(route,json={'settings':{**policy,'publication_article_ids':[article['id']]}})
    assert response.status_code == 200
    assert response.json()['settings']['publication_article_ids'] == [article['id']]
    assert response.json()['settings']['enabled'] is False


@pytest.mark.parametrize('scope', ['article-id', [False], [4], [''], ['has space'], ['a'*129]])
def test_malformed_scope_fails_closed(platform, scope):
    client, _, site_id = platform
    result = client.put(f'/api/v1/sites/{site_id}/policy',json={'settings':{'publication_article_ids':scope}})
    assert result.status_code == 422


def test_scheduler_queues_only_selected_article_and_no_autopilot(autopilot_scheduler):
    _, factory, site_id, clock, _ = autopilot_scheduler
    clock[0] = datetime(2026, 9, 15, 14, 0)
    with factory() as db:
        site = db.get(Site, site_id)
        articles = [Article(site_id=site_id,title=f'Article {i}',status='scheduled',scheduled_at=clock[0]) for i in range(2)]
        db.add_all(articles)
        db.flush()
        selected, other = [a.id for a in articles]
        create_policy(db,site,None,{**current_policy(db,site_id).settings,'publication_article_ids':[selected]})
        db.commit()
    scheduler.schedule()
    with factory() as db:
        jobs = db.scalars(select(Job).where(Job.site_id==site_id,Job.kind.in_(['publish','content_autopilot']))).all()
        assert len(jobs) == 1 and jobs[0].kind == 'publish'
        assert jobs[0].payload['article_id'] == selected
        assert db.get(Article,other).status == 'scheduled'
        assert 'restricted_publication_scope' in workflows._content_autopilot_preflight(db,db.get(Site,site_id))['blockers']
