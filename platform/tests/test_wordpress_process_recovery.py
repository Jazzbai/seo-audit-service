"""Real process death after real WP draft creation; SQLite is test-only here."""
from datetime import timedelta
import json
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import scheduler, worker
from app.config import settings
from app.models import Article, Base, Job, Publication, Site, Team
from app.operations import enqueue, enqueue_article_publish, now
from app.policies import create_policy
from test_wordpress_live import ROOT, FixtureTransport, integration_stack, native_site, pytestmark


@pytest.mark.asyncio
async def test_killed_worker_recovers_same_remote_post_in_a_fresh_process(native_site, tmp_path, monkeypatch):
    database = tmp_path / 'recovery-fixture.db'
    engine = create_engine('sqlite:///' + database.as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(worker.execute_job, 'apply_async', lambda *a, **kw: None)
    with factory() as db:
        team = Team(name='Isolated crash recovery')
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name='Isolated WordPress', origin=native_site['origin'], paused=False,
                    facts={'business_name': 'Independent Workshop', 'services': ['Repairs'],
                           'confirmed_sources': [{'url': native_site['origin'] + '/about', 'title': 'Fixture facts'}]})
        db.add(site)
        db.flush()
        create_policy(db, site, None, {'enabled': True, 'allowed_actions': ['publish'], 'author_id': native_site['author_id']})
        article = Article(site_id=site.id, title='Worker restart rehearsal ' + uuid4().hex[:8],
                          slug='recovery-' + uuid4().hex, body='<p>Independent Workshop provides Repairs.</p>',
                          author_id=native_site['author_id'], sources=site.facts['confirmed_sources'],
                          brief={'generation': {'kind': 'fixture_authored'}})
        db.add(article)
        db.flush()
        job = enqueue_article_publish(db, site, article.id)
        article_id, site_id, job_id, slug = article.id, site.id, job.id, article.slug

    def run(job_id, crash=False):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/wordpress_recovery_worker.py')],
                                input=json.dumps({'fixture': native_site, 'database': str(database),
                                                  'job_id': job_id, 'crash_after_create': crash}),
                                capture_output=True, text=True, cwd=ROOT, timeout=60)
        assert result.returncode == (73 if crash else 0), 'Isolated worker process failed; output withheld'
        return None if crash else json.loads(result.stdout)

    async def remote_posts():
        async with httpx.AsyncClient(transport=FixtureTransport(), auth=(native_site['username'], native_site['application_password'])) as remote:
            response = await remote.get(native_site['origin'] + '/wp-json/wp/v2/posts',
                                        params={'slug': slug, 'context': 'edit', 'status': 'any'})
            assert response.status_code == 200
            return response.json()

    run(job_id, crash=True)
    posts = await remote_posts()
    assert len(posts) == 1 and posts[0]['status'] == 'draft'
    remote_id = str(posts[0]['id'])
    with factory() as db:
        interrupted = db.get(Job, job_id)
        pub = db.scalar(select(Publication).where(Publication.article_id == article_id))
        assert interrupted.status == 'running'
        assert pub.remote_id is None and pub.snapshot['create_started']
        publication_id = pub.id
        # Advance only this test's durable lease; do not wait sixteen minutes.
        interrupted.lease_until = now() - timedelta(seconds=1)
        db.commit()
    scheduler.schedule()
    with factory() as db:
        assert db.get(Job, job_id).status == 'needs_reconciliation'
        assert db.get(Publication, publication_id).status == 'ambiguous'
        reconciliation = enqueue(db, db.get(Site, site_id), 'reconcile_publication',
                                 {'publication_id': publication_id}, 'recovery-read-only')
        reconciliation_id = reconciliation.id
    reconciled = run(reconciliation_id)
    assert reconciled['status'] == 'draft_reconciled', reconciled
    assert len(await remote_posts()) == 1
    with factory() as db:
        pub = db.get(Publication, publication_id)
        assert pub.remote_id == remote_id and pub.status == 'preparing'
        resumed = enqueue_article_publish(db, db.get(Site, site_id), article_id)
        assert resumed.id != job_id
        assert db.get(Job, job_id).status == 'needs_reconciliation'
        assert enqueue_article_publish(db, db.get(Site, site_id), article_id).id == resumed.id
        resumed_id = resumed.id
    assert run(resumed_id)['status'] == 'published'
    posts = await remote_posts()
    assert len(posts) == 1 and str(posts[0]['id']) == remote_id and posts[0]['status'] == 'publish'
    with factory() as db:
        rollback = enqueue(db, db.get(Site, site_id), 'rollback', {'article_id': article_id}, 'recovery-rollback')
        rollback_id = rollback.id
    assert run(rollback_id)['status'] == 'rolled_back'
    posts = await remote_posts()
    assert len(posts) == 1 and posts[0]['status'] == 'draft'
    with factory() as db:
        assert db.get(Publication, publication_id).status == 'rolled_back'
        assert db.get(Article, article_id).status == 'rolled_back'
    engine.dispose()
