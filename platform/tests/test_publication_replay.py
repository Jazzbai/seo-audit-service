"""A browser replay must observe the scheduler's job, never replace its authority."""
import pytest
from sqlalchemy import select

from app.models import Article, Job, Site, Team
from app.operations import enqueue
from test_platform import platform


@pytest.mark.parametrize('status', ['queued', 'running', 'complete', 'failed', 'needs_reconciliation'])
def test_browser_reuses_scheduled_publication_without_redispatch(platform, monkeypatch, status):
    client, factory, site_id = platform
    dispatches = []
    monkeypatch.setattr('app.worker.execute_job.apply_async', lambda *a, **kw: dispatches.append(kw))
    with factory() as db:
        article = Article(site_id=site_id, title='Isolated replay test')
        db.add(article)
        db.flush()
        payload = {
            'article_id': article.id,
            'authorization': {'type': 'policy', 'action': 'publish', 'policy_version': 3},
            'policy_version': 3,
        }
        job = Job(site_id=site_id, kind='publish', status=status, payload=payload,
                  idempotency_key=f'{site_id}:publish:{article.id}')
        db.add(job)
        db.commit()
        job_id, article_id = job.id, article.id
    response = client.post(f'/api/v1/sites/{site_id}/articles/{article_id}/publish')
    assert response.status_code == 202, response.text
    assert response.json()['id'] == job_id
    assert response.json()['status'] == status
    assert dispatches == []
    with factory() as db:
        assert len(db.scalars(select(Job)).all()) == 1
        assert db.get(Job, job_id).payload == payload


@pytest.mark.parametrize('corruption', ['other_article', 'other_kind', 'extra_write', 'bad_authority', 'other_site'])
def test_publish_replay_does_not_accept_conflicting_work(platform, corruption):
    client, factory, site_id = platform
    with factory() as db:
        article = Article(site_id=site_id, title='Isolated collision test')
        db.add(article)
        db.flush()
        payload = {'article_id': article.id,
                   'authorization': {'type': 'policy', 'action': 'publish', 'policy_version': 3},
                   'policy_version': 3}
        job_site, kind = site_id, 'publish'
        if corruption == 'other_article':
            payload['article_id'] = 'different-article'
        elif corruption == 'other_kind':
            kind = 'rollback'
        elif corruption == 'extra_write':
            payload['force'] = True
        elif corruption == 'bad_authority':
            payload['authorization']['policy_version'] = 4
        elif corruption == 'other_site':
            team = Team(name='Another tenant')
            db.add(team)
            db.flush()
            other = Site(team_id=team.id, name='Other', origin='https://other.example.test')
            db.add(other)
            db.flush()
            job_site = other.id
        db.add(Job(site_id=job_site, kind=kind, payload=payload,
                   idempotency_key=f'{site_id}:publish:{article.id}'))
        db.commit()
        article_id = article.id
    response = client.post(f'/api/v1/sites/{site_id}/articles/{article_id}/publish')
    assert response.status_code == 409


def test_generic_idempotency_still_rejects_different_payload(platform):
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        enqueue(db, site, 'publish', {'article_id': 'first'}, 'arbitrary-key')
        with pytest.raises(ValueError, match='different work'):
            enqueue(db, site, 'publish', {'article_id': 'second'}, 'arbitrary-key')


def test_rolled_back_article_cannot_report_an_old_job_as_new_publication(platform):
    client, factory, site_id = platform
    with factory() as db:
        article = Article(site_id=site_id, title='Rolled-back fixture', status='rolled_back')
        db.add(article)
        db.flush()
        db.add(Job(site_id=site_id, kind='publish', status='complete', payload={'article_id': article.id},
                   idempotency_key=f'{site_id}:publish:{article.id}'))
        db.commit()
        article_id = article.id
    response = client.post(f'/api/v1/sites/{site_id}/articles/{article_id}/publish')
    assert response.status_code == 409
    assert 'rolled back' in response.json()['detail']
