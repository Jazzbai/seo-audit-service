from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, worker
from app.config import settings
from app.models import Article, Base, Event, Job, Publication, Site, Team
from app.policies import create_policy


@pytest.fixture
def scheduler_platform(monkeypatch):
    engine = create_engine(
        'sqlite://',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    current = [datetime(2026, 9, 15, 16, 0)]
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'now', lambda: current[0])
    monkeypatch.setattr('app.operations.now', lambda: current[0])
    monkeypatch.setattr(worker.execute_job, 'apply_async', lambda *args, **kwargs: None)
    yield factory, current
    engine.dispose()


def _site(factory, *, posts_per_week=2, publish_days=None):
    with factory() as db:
        team = Team(name='Scheduler quota team')
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name='Scheduler quota site',
            origin='https://quota.example.test',
            timezone='America/Chicago',
            paused=False,
        )
        db.add(site)
        db.flush()
        policy = create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['publish'],
            'posts_per_week': posts_per_week,
            'publish_days': [1, 4] if publish_days is None else publish_days,
        })
        db.commit()
        return site.id, policy.version


def _article(db, site_id, title, *, status, when, scheduled_at=None, brief=None):
    article = Article(
        site_id=site_id,
        title=title,
        slug=title.lower().replace(' ', '-'),
        status=status,
        brief=brief or {},
        scheduled_at=scheduled_at,
        created_at=when,
        updated_at=when,
    )
    db.add(article)
    db.flush()
    return article


def _published(db, site_id, policy_version, title, when):
    article = _article(db, site_id, title, status='published', when=when)
    db.add(Publication(
        site_id=site_id,
        article_id=article.id,
        operation_key=f'publish:{site_id}:existing:{article.id}',
        status='published',
        policy_version=policy_version,
        created_at=when,
        updated_at=when,
        result={'status': 'published'},
    ))
    return article


def _scheduled(db, site_id, title, when):
    return _article(
        db,
        site_id,
        title,
        status='scheduled',
        when=when,
        scheduled_at=when - timedelta(minutes=1),
    )


def _publish_jobs(db, site_id):
    return db.scalars(select(Job).where(
        Job.site_id == site_id,
        Job.kind == 'publish',
    ).order_by(Job.created_at)).all()


def test_scheduler_enforces_two_posts_per_local_week(scheduler_platform):
    factory, current = scheduler_platform
    site_id, policy_version = _site(factory)
    when = current[0]
    with factory() as db:
        _published(db, site_id, policy_version, 'Already published', when - timedelta(days=1))
        first = _scheduled(db, site_id, 'First pending', when - timedelta(minutes=2))
        second = _scheduled(db, site_id, 'Second pending', when - timedelta(minutes=1))
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = _publish_jobs(db, site_id)
        assert len(jobs) == 1
        assert jobs[0].payload == {
            'article_id': first.id,
            'authorization': {
                'type': 'policy',
                'action': 'publish',
                'policy_version': policy_version,
            },
            'policy_version': policy_version,
        }
        assert db.get(Article, second.id).status == 'scheduled'
        deferred = db.scalars(select(Event).where(
            Event.site_id == site_id,
            Event.kind == 'publication_deferred_quota',
        )).all()
        assert len(deferred) == 1
        assert deferred[0].data == {
            'article_id': second.id,
            'reason': 'posts_per_week',
            'used': 2,
            'limit': 2,
            'week_key': '2026-09-14',
        }


def test_scheduler_rolls_quota_over_to_new_local_calendar_week(scheduler_platform):
    factory, current = scheduler_platform
    current[0] = datetime(2026, 9, 21, 16, 0)
    site_id, policy_version = _site(factory, publish_days=[0])
    old_week = datetime(2026, 9, 14, 16, 0)
    with factory() as db:
        _published(db, site_id, policy_version, 'Last week article', old_week)
        first = _scheduled(db, site_id, 'This week first', current[0])
        second = _scheduled(db, site_id, 'This week second', current[0])
        db.commit()

    scheduler.schedule()

    with factory() as db:
        jobs = _publish_jobs(db, site_id)
        assert {job.payload['article_id'] for job in jobs} == {first.id, second.id}
        assert not db.scalars(select(Event).where(
            Event.site_id == site_id,
            Event.kind == 'publication_deferred_quota',
        )).all()


def test_explicitly_scheduled_article_stays_visible_when_week_is_full(scheduler_platform):
    factory, current = scheduler_platform
    site_id, policy_version = _site(factory)
    when = current[0]
    with factory() as db:
        _published(db, site_id, policy_version, 'Published one', when - timedelta(hours=2))
        _published(db, site_id, policy_version, 'Published two', when - timedelta(hours=1))
        article = _scheduled(db, site_id, 'Explicitly scheduled article', when)
        original_schedule = article.scheduled_at
        db.commit()

    scheduler.schedule()

    with factory() as db:
        visible = db.get(Article, article.id)
        assert visible.status == 'scheduled'
        assert visible.scheduled_at == original_schedule
        assert _publish_jobs(db, site_id) == []
        assert db.scalar(select(Event).where(
            Event.site_id == site_id,
            Event.kind == 'publication_deferred_quota',
        )) is not None


def test_scheduler_ticks_are_idempotent_for_a_scheduled_article(scheduler_platform):
    factory, current = scheduler_platform
    site_id, _ = _site(factory)
    with factory() as db:
        article = _scheduled(db, site_id, 'Only once article', current[0])
        db.commit()

    scheduler.schedule()
    scheduler.schedule()

    with factory() as db:
        jobs = _publish_jobs(db, site_id)
        assert len(jobs) == 1
        assert jobs[0].idempotency_key.endswith(f'publish:{article.id}')
