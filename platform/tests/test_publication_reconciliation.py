"""Read-only recovery coverage for uncertain publication outcomes."""

import asyncio
import json

import pytest
from sqlalchemy import select

from app import worker, workflows
from app.connectors.errors import AmbiguousOutcome, ConnectorError
from app.config import settings
from app.models import Article, Connection, Job, Publication, Site
from app.policies import create_policy
from app.operations import now
from test_platform import platform


def _seed_ambiguous(factory, site_id, *, remote_id=None):
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = True
        site.facts = {
            "business_name": "Independent test",
            "services": ["Repairs"],
            "authors": [{"id": "author-1", "name": "Fixture writer"}],
        }
        policy = create_policy(db, site, None, {
            "enabled": True,
            "allowed_actions": ["publish"],
            "author_id": "author-1",
            "posts_per_week": 1,
        })
        article = Article(
            site_id=site_id,
            title="Prepare for a repair visit",
            slug="prepare-for-a-repair-visit",
            body="<p>Bring your repair questions.</p>",
            author_id="author-1",
            status="failed",
            checks={"passed": True, "blockers": [], "warnings": []},
            managed=True,
            updated_at=now(),
        )
        db.add(article)
        db.flush()
        publication = Publication(
            site_id=site_id,
            article_id=article.id,
            operation_key=f"publish:{site_id}:{article.id}",
            status="ambiguous",
            policy_version=policy.version,
            remote_id=remote_id,
            snapshot={
                "article": {
                    "title": article.title,
                    "body": article.body,
                    "slug": article.slug,
                    "author_id": article.author_id,
                },
                "create_started": now().isoformat(),
            },
            result={"status": "ambiguous", "error_type": "AmbiguousOutcome"},
            updated_at=now(),
        )
        db.add(publication)
        db.commit()
        return article.id, publication.id


class ReadOnlyWordPress:
    def __init__(self, record=None, *, draft=None, error=None):
        self.record = record
        self.draft = draft
        self.error = error
        self.read_calls = 0
        self.reconcile_calls = 0
        self.write_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def read(self, _resource_key):
        self.read_calls += 1
        if self.error:
            raise self.error
        return dict(self.record)

    async def reconcile_draft(self, _article, _operation_key):
        self.reconcile_calls += 1
        if self.error:
            raise self.error
        return dict(self.draft)

    @staticmethod
    def matches_snapshot(current, snapshot, *, ignore_status=False):
        fields = ("title", "body", "slug", "author_id")
        return all(current.get(field) == snapshot.get(field) for field in fields)

    async def create_draft(self, *_args, **_kwargs):
        self.write_calls += 1
        raise AssertionError("publication reconciliation must never create a draft")

    async def publish(self, *_args, **_kwargs):
        self.write_calls += 1
        raise AssertionError("publication reconciliation must never publish")


def _remote_record(*, status="draft", remote_id="7"):
    return {
        "id": remote_id,
        "resource_key": f"posts:{remote_id}",
        "resource_type": "posts",
        "url": "https://example.test/prepare-for-a-repair-visit",
        "title": "Prepare for a repair visit",
        "body": "<p>Bring your repair questions.</p>",
        "slug": "prepare-for-a-repair-visit",
        "author_id": "author-1",
        "status": status,
        "source_hash": f"source-{status}",
    }


def _install_client(monkeypatch, remote):
    async def client_for(_db, _site, kind="wordpress"):
        assert kind == "wordpress"
        return remote

    monkeypatch.setattr(workflows, "client_for", client_for)

    async def public_fetch(url):
        assert url == remote.record["url"]
        return {
            "status_code": 200,
            "url": url,
            "html": "<html><body><h1>Prepare for a repair visit</h1>"
            "<p>Bring your repair questions.</p></body></html>",
        }

    monkeypatch.setattr(workflows, "fetch", public_fetch)


def test_ambiguous_draft_reconciliation_is_read_only_and_resumable(platform, monkeypatch):
    client, factory, site_id = platform
    article_id, publication_id = _seed_ambiguous(factory, site_id)
    remote = ReadOnlyWordPress(draft=_remote_record(status="draft"))
    _install_client(monkeypatch, remote)

    queued = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    result = worker.run_job(job_id)

    assert result["status"] == "draft_reconciled"
    assert result["next_action"] == "resume_publication"
    assert remote.reconcile_calls == 1
    assert remote.read_calls == 0
    assert remote.write_calls == 0
    with factory() as db:
        article = db.get(Article, article_id)
        publication = db.get(Publication, publication_id)
        assert article.status == "checked"
        assert article.remote_id == "7"
        assert publication.status == "preparing"
        assert publication.operation_key == f"publish:{site_id}:{article_id}"
        assert publication.result["status"] == "draft_reconciled"


@pytest.mark.parametrize('mismatch', [False, True])
def test_missing_draft_snapshot_matches_native_wordpress_metadata(mismatch):
    article = Article(title='Fixture guide', body='<p>Facts.</p>', slug='fixture-guide', author_id='3')
    current = {'title': article.title, 'body': article.body,
               'metadata': {'slug': article.slug, 'author_id': 4 if mismatch else 3}}
    assert workflows._publication_remote_matches(None, current, article, None) is (not mismatch)


def test_full_snapshot_mismatch_cannot_fall_back_to_only_title_and_body():
    article = Article(title='Fixture guide', body='<p>Facts.</p>', slug='fixture-guide', author_id='3')
    current = {'title': article.title, 'body': article.body, 'slug': article.slug, 'author_id': '3'}

    class Changed:
        def matches_snapshot(self, *args, **kwargs): return False

    assert not workflows._publication_remote_matches(Changed(), current, article, {'raw': {'excerpt': 'Earlier excerpt'}})


def test_ui_publish_after_reconciliation_queues_one_linked_attempt_and_honors_pause(platform, monkeypatch):
    client, factory, site_id = platform
    article_id, publication_id = _seed_ambiguous(factory, site_id)
    remote = ReadOnlyWordPress(draft=_remote_record(status='draft'))
    _install_client(monkeypatch, remote)
    with factory() as db:
        original = Job(site_id=site_id, kind='publish', status='needs_reconciliation',
                       payload={'article_id': article_id}, idempotency_key=f'{site_id}:publish:{article_id}')
        db.add(original)
        db.commit()
        original_id = original.id
    path = f'/api/v1/sites/{site_id}'
    reconciliation = client.post(f'{path}/publications/{publication_id}/reconcile')
    assert worker.run_job(reconciliation.json()['id'])['status'] == 'draft_reconciled'
    resumed = client.post(f'{path}/articles/{article_id}/publish')
    assert resumed.status_code == 202, resumed.text
    resumed_id = resumed.json()['id']
    assert resumed_id != original_id
    assert resumed.json()['payload']['resumes_job_id'] == original_id
    assert client.post(f'{path}/articles/{article_id}/publish').json()['id'] == resumed_id
    assert worker.run_job(resumed_id) == {'status': 'held', 'reason': 'site_paused'}
    assert remote.write_calls == 0
    with factory() as db:
        assert db.get(Job, original_id).status == 'needs_reconciliation'
        assert len(db.scalars(select(Job).where(Job.kind == 'publish')).all()) == 2


def test_held_reconciliation_can_be_checked_again_without_duplicate_active_reads(platform, monkeypatch):
    client, factory, site_id = platform
    _, publication_id = _seed_ambiguous(factory, site_id)
    remote = ReadOnlyWordPress(error=ConnectorError('offline', transport_error=True))
    _install_client(monkeypatch, remote)
    path = f'/api/v1/sites/{site_id}/publications/{publication_id}/reconcile'
    first = client.post(path).json()['id']
    assert client.post(path).json()['id'] == first
    assert worker.run_job(first)['status'] == 'held'
    second = client.post(path).json()['id']
    assert second != first
    assert client.post(path).json()['id'] == second


def test_published_reconciliation_verifies_public_state_and_closes_parent(platform, monkeypatch):
    client, factory, site_id = platform
    article_id, publication_id = _seed_ambiguous(factory, site_id, remote_id="7")
    remote = ReadOnlyWordPress(record=_remote_record(status="publish"))
    _install_client(monkeypatch, remote)

    queued = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    result = worker.run_job(queued.json()["id"])

    assert result["status"] == "published"
    assert result["complete"] is True
    assert remote.read_calls == 1
    assert remote.write_calls == 0
    with factory() as db:
        article = db.get(Article, article_id)
        publication = db.get(Publication, publication_id)
        assert article.status == "published"
        assert publication.status == "published"
        assert publication.result["reconciled"] is True


def test_reconciliation_holds_unavailable_or_mismatched_remote_state(platform, monkeypatch):
    client, factory, site_id = platform
    _article_id, publication_id = _seed_ambiguous(factory, site_id, remote_id="7")
    remote = ReadOnlyWordPress(error=ConnectorError("provider secret must not escape", transport_error=True))
    _install_client(monkeypatch, remote)

    queued = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    result = worker.run_job(queued.json()["id"])

    assert result["status"] == "held"
    assert result["reason"] == "reconciliation_unavailable"
    assert "provider secret" not in json.dumps(result)
    with factory() as db:
        publication = db.get(Publication, publication_id)
        assert publication.status == "ambiguous"
        assert publication.result["reason"] == "reconciliation_unavailable"


def test_reconciliation_holds_a_remote_snapshot_mismatch(platform, monkeypatch):
    client, factory, site_id = platform
    _article_id, publication_id = _seed_ambiguous(factory, site_id, remote_id="7")
    changed = _remote_record(status="draft")
    changed["title"] = "Someone else's article"
    remote = ReadOnlyWordPress(record=changed)
    _install_client(monkeypatch, remote)

    queued = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    result = worker.run_job(queued.json()["id"])

    assert result["status"] == "held"
    assert result["reason"] == "snapshot_mismatch"
    assert remote.read_calls == 1
    assert remote.write_calls == 0


def test_reconciliation_holds_when_local_article_changed_after_publication(platform, monkeypatch):
    client, factory, site_id = platform
    article_id, publication_id = _seed_ambiguous(factory, site_id, remote_id="7")
    with factory() as db:
        db.get(Article, article_id).body = "<p>A later local edit.</p>"
        db.commit()

    remote = ReadOnlyWordPress(record=_remote_record(status="draft"))
    _install_client(monkeypatch, remote)

    queued = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    result = worker.run_job(queued.json()["id"])

    assert result["status"] == "held"
    assert result["reason"] == "article_changed_since_publication"
    assert remote.read_calls == 0
    assert remote.reconcile_calls == 0
    assert remote.write_calls == 0


def test_reconciliation_request_is_site_scoped_and_idempotent(platform, monkeypatch):
    client, factory, site_id = platform
    _article_id, publication_id = _seed_ambiguous(factory, site_id, remote_id="7")
    remote = ReadOnlyWordPress(record=_remote_record(status="draft"))
    _install_client(monkeypatch, remote)

    other = client.post(
        "/api/v1/sites",
        json={"name": "Other", "origin": "https://other.example.test", "facts": {}},
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    assert client.post(f"/api/v1/sites/{other_id}/publications/{publication_id}/reconcile").status_code == 404

    first = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    second = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    worker.run_job(first.json()["id"])
    assert worker.run_job(first.json()["id"]) == {"ignored": True}
    assert remote.read_calls == 1


def test_reconciliation_rejects_resolved_publication(platform):
    client, factory, site_id = platform
    _article_id, publication_id = _seed_ambiguous(factory, site_id)
    with factory() as db:
        db.get(Publication, publication_id).status = "published"
        db.commit()

    response = client.post(f"/api/v1/sites/{site_id}/publications/{publication_id}/reconcile")
    assert response.status_code == 409


def test_ambiguous_publish_does_not_rollback_a_possible_remote_success(platform, monkeypatch):
    """An unknown publish outcome must remain reconciliable, never compensated blindly."""

    _, factory, site_id = platform
    remote = {
        "id": "19",
        "resource_key": "posts:19",
        "resource_type": "posts",
        "url": "https://example.test/prepare-for-a-repair-visit",
        "title": "Prepare for a repair visit",
        "body": "<p>Bring your repair questions.</p>",
        "slug": "prepare-for-a-repair-visit",
        "author_id": "author-1",
        "status": "draft",
        "source_hash": "draft-source",
    }
    calls = {"restore": 0}

    class PublishTimeoutWordPress:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create_draft(self, _article, _operation_key):
            return dict(remote)

        async def read(self, resource_key):
            assert resource_key == remote["resource_key"]
            return dict(remote)

        @staticmethod
        def matches_snapshot(current, snapshot, *, ignore_status=False):
            fields = ("resource_key", "title", "body", "slug", "author_id")
            return all(current.get(field) == snapshot.get(field) for field in fields)

        async def publish(self, _remote_id, expected_hash=None, *, operation_key=None):
            assert expected_hash == remote["source_hash"]
            remote["status"] = "publish"
            raise AmbiguousOutcome(
                "fixture publish response was lost",
                operation_key=operation_key,
            )

        async def restore(self, *_args, **_kwargs):
            calls["restore"] += 1
            remote["status"] = "draft"
            return dict(remote)

    connector = PublishTimeoutWordPress()

    async def fixture_client(_db, _site, kind="wordpress"):
        assert kind == "wordpress"
        return connector

    monkeypatch.setattr(workflows, "client_for", fixture_client)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(
        "app.intelligence.content.check_article",
        lambda *_args, **_kwargs: {"passed": True, "blockers": [], "warnings": []},
    )

    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        site.facts = {
            "business_name": "Independent test",
            "services": ["Repairs"],
            "authors": [{"id": "author-1", "name": "Fixture writer"}],
        }
        policy = create_policy(db, site, None, {
            "enabled": True,
            "allowed_actions": ["publish"],
            "author_id": "author-1",
            "posts_per_week": 1,
        })
        article = Article(
            site_id=site_id,
            title=remote["title"],
            slug=remote["slug"],
            body=remote["body"],
            author_id=remote["author_id"],
            status="checked",
            checks={"passed": True, "blockers": [], "warnings": []},
            managed=True,
            updated_at=now(),
        )
        db.add(article)
        db.add(Connection(
            site_id=site_id,
            kind="wordpress",
            encrypted_credentials="opaque-fixture-credentials",
            status="connected",
            capabilities={"authenticated": True, "native": {"create": True, "publish": True}},
        ))
        db.commit()

        with pytest.raises(AmbiguousOutcome):
            asyncio.run(workflows.publish(
                db,
                site,
                Job(
                    id="ambiguous-publish-no-rollback",
                    site_id=site_id,
                    kind="publish",
                    payload={"article_id": article.id},
                ),
            ))

        publication = db.scalar(select(Publication).where(Publication.article_id == article.id))
        assert calls["restore"] == 0
        assert remote["status"] == "publish"
        assert publication is not None
        assert publication.status == "ambiguous"
        assert publication.policy_version == policy.version
