"""Focused offline coverage for a workflow rollback safety boundary."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import select

from app import workflows
from app.config import settings
from app.models import Candidate, Connection, Incident, Job, Page, Publication, Site
from app.policies import create_policy
from test_platform import platform


def test_metadata_rollback_preserves_unrelated_external_editorial_edit(platform, monkeypatch):
    """A failed metadata verification must not replay a full source snapshot."""

    remote = None

    class MetadataClient:
        def __init__(self):
            self.before = {
                'resource_key': 'products:5',
                'resource_type': 'product',
                'id': '5',
                'url': 'https://example.test/product',
                'title': 'Original product',
                'body': '<p>Original product guidance.</p>',
                'status': 'publish',
                'metadata': {'seo': {'forgeseo': {'title': '', 'description': 'Old description'}}},
                'source_hash': 'product-before',
                'raw': {
                    'name': 'Original product',
                    'description': '<p>Original product guidance.</p>',
                },
            }
            self.current = deepcopy(self.before)
            self.update_calls = 0
            self.restore_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def read(self, resource_key):
            assert resource_key == self.current['resource_key']
            return deepcopy(self.current)

        async def update(self, resource_key, changes, expected_hash, **kwargs):
            assert resource_key == self.current['resource_key']
            assert expected_hash == self.current['source_hash']
            assert set(changes) == {'seo'}
            assert set(changes['seo']) == {'description'}
            self.update_calls += 1
            description = changes['seo']['description']
            self.current['metadata']['seo']['forgeseo']['description'] = description
            if self.update_calls == 1:
                # The metadata write succeeds, but an unrelated external edit
                # lands before rendered verification completes.
                self.current['body'] = '<p>External editor guidance.</p>'
                self.current['raw']['description'] = self.current['body']
                self.current['source_hash'] = 'product-with-external-edit'
            else:
                self.current['source_hash'] = 'product-after-metadata-rollback'
            return deepcopy(self.current)

        async def restore(self, resource_key, snapshot, expected_hash=None, **kwargs):
            self.restore_calls += 1
            assert resource_key == self.current['resource_key']
            assert expected_hash == self.current['source_hash']
            self.current = deepcopy(snapshot)
            return deepcopy(self.current)

    remote = MetadataClient()

    async def fixture_client(db, site, kind='wordpress'):
        assert kind == 'woocommerce'
        return remote

    async def public_fetch(url):
        body = remote.current['body']
        return {
            'status_code': 200,
            'url': url,
            'html': (
                '<html><head><link rel="canonical" href="https://example.test/">'
                '<meta name="robots" content="index,follow"><style>.entry{color:black}</style>'
                '</head><body><main class="entry"><h1>Original product</h1>'
                f'{body}</main></body></html>'
            ),
        }

    def audit_once(*args, **kwargs):
        return {
            'signals': {
                'meta_description': remote.current['metadata']['seo']['forgeseo']['description'],
            },
            'findings': [],
        }

    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    monkeypatch.setattr(workflows, 'client_for', fixture_client)
    monkeypatch.setattr(workflows, 'fetch', public_fetch)
    monkeypatch.setattr('app.intelligence.audit.audit_page', audit_once)

    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['metadata'],
            'protected_paths': [],
        })
        page = Page(
            site_id=site_id,
            resource_key='products:5',
            resource_type='products',
            url='https://example.test/product',
            title='Original product',
            source=deepcopy(remote.before),
            source_hash='product-before',
        )
        candidate = Candidate(
            site_id=site_id,
            page_id='',
            field='meta_description',
            before_value='Old description',
            after_value='New description',
            source_hash='product-before',
            status='approved',
            details={},
        )
        db.add(page)
        db.flush()
        candidate.page_id = page.id
        db.add(candidate)
        db.add(Connection(
            site_id=site_id,
            kind='woocommerce',
            encrypted_credentials='offline-test-credentials',
            status='connected',
            capabilities={
                'seo': {
                    'write': True,
                    'writable_fields': ['description'],
                    'resource_types': ['product'],
                },
            },
        ))
        db.commit()

        with pytest.raises(ValueError, match='Rendered verification failed'):
            asyncio.run(workflows.candidate(
                db,
                site,
                Job(
                    id='metadata-rollback-gap-2',
                    site_id=site_id,
                    kind='candidate',
                    payload={'candidate_id': candidate.id},
                ),
            ))

        publication = db.scalar(select(Publication).where(Publication.candidate_id == candidate.id))
        incident = db.scalar(select(Incident).where(Incident.key == f'rollback:{publication.id}'))
        assert remote.restore_calls == 0
        assert remote.update_calls == 2
        assert remote.current['body'] == '<p>External editor guidance.</p>'
        assert remote.current['metadata']['seo']['forgeseo']['description'] == 'Old description'
        assert candidate.status == 'failed'
        assert publication.status == 'failed'
        assert incident is not None


def test_rolled_back_candidate_does_not_hide_a_recurring_proposal(platform):
    """A restored source must receive a new actionable proposal."""

    _, factory, site_id = platform
    observation = {
        'signals': {},
        'findings': [],
        'candidates': [{
            'field': 'seo_title',
            'before_value': 'Old title',
            'after_value': 'Complete useful title',
        }],
    }

    with factory() as db:
        site = db.get(Site, site_id)
        page = Page(
            site_id=site_id,
            resource_key='posts:recurring-candidate',
            resource_type='posts',
            url='https://example.test/recurring-candidate',
            title='Recurring candidate page',
            source_hash='restored-source',
        )
        db.add(page)
        db.flush()

        workflows.upsert_observation(db, site, page, observation)
        db.flush()
        first = db.scalar(select(Candidate).where(Candidate.page_id == page.id))
        assert first is not None
        first.status = 'rolled_back'
        db.commit()

        workflows.upsert_observation(db, site, page, observation)
        db.commit()

        rows = db.scalars(
            select(Candidate)
            .where(Candidate.page_id == page.id)
            .order_by(Candidate.created_at, Candidate.id)
        ).all()
        assert len(rows) == 2
        historical = next(row for row in rows if row.id == first.id)
        fresh = next(row for row in rows if row.id != first.id)
        assert historical.status == 'rolled_back'
        assert fresh.status == 'pending'
        assert fresh.after_value == first.after_value


def test_automatic_candidate_gate_does_not_authorize_cross_site_page_reference(platform, monkeypatch):
    """A corrupted/imported cross-site candidate must stay unselected."""

    async def fake_crawl(*args, **kwargs):
        return {
            'pages': [],
            'complete': True,
            'pending_urls': [],
            'visited_urls': [],
            'errors': [],
        }

    monkeypatch.setattr('app.intelligence.audit.crawl', fake_crawl)
    monkeypatch.setattr(settings, 'GLOBAL_PAUSE', False)
    _, factory, site_id = platform
    with factory() as db:
        site = db.get(Site, site_id)
        site.paused = False
        create_policy(db, site, None, {
            'enabled': True,
            'allowed_actions': ['metadata'],
            'protected_paths': [],
        })
        other_site = Site(
            team_id=site.team_id,
            name='Other isolated site',
            origin='https://other.example.test',
        )
        db.add(other_site)
        db.flush()
        other_page = Page(
            site_id=other_site.id,
            resource_key='posts:cross-site',
            url=other_site.origin + '/post',
            source_hash='other-source',
        )
        db.add(other_page)
        db.flush()
        candidate = Candidate(
            site_id=site.id,
            page_id=other_page.id,
            field='seo_title',
            before_value='Old title',
            after_value='A complete isolated title',
            source_hash=other_page.source_hash,
        )
        job = Job(
            id='cross-site-candidate-audit',
            site_id=site.id,
            kind='audit',
            payload={'suppress_automation': False},
            idempotency_key='cross-site-candidate-audit',
        )
        db.add_all([candidate, job])
        db.commit()

        result = asyncio.run(workflows.audit(db, site, job))
        db.commit()

        assert result['complete'] is True
        assert candidate.status == 'pending'
        assert db.scalars(
            select(Job).where(Job.site_id == site.id, Job.kind == 'candidate')
        ).all() == []
