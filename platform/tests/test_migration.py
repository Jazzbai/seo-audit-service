import hashlib
import json

import pytest

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import Base, Measurement, Page, Site, Team
from scripts.import_legacy import (
    export_bundle,
    import_history,
    load_bundle,
    rollback_import,
    write_bundle,
)


def test_legacy_history_import_is_paused_historical_and_idempotent(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    artifact_root = tmp_path / 'artifacts'
    data = {
        'site': {'id': 1, 'name': 'Legacy example', 'origin': 'https://example.test/'},
        'observations': [
            {
                'payload_json': {
                    'resources': [
                        {
                            'resource_key': 'post:1',
                            'resource_type': 'post',
                            'public_url': 'https://example.test/one',
                            'title': 'Legacy post',
                            'raw': {
                                'application_password': 'embedded-secret',
                                'apiKey': 'camel-api-secret',
                                'accessToken': 'camel-access-secret',
                                'content': 'keep this evidence',
                            },
                        },
                        {
                            'resource_key': 'page:2',
                            'resource_type': 'page',
                            'public_url': 'https://example.test/two',
                            'title': 'Legacy page',
                        },
                    ],
                },
            },
            {
                'payload_json': {
                    'requested_url': 'https://example.test/observed',
                    'status_code': 200,
                    'title': 'Observed legacy page',
                    'meta_description': 'Historical observation',
                },
            },
            {
                'payload_json': {
                    'requested_url': 'https://offsite.test/do-not-import',
                    'status_code': 200,
                    'title': 'Off-site link',
                },
            },
        ],
        'approvals': [{'id': 'historical-approval'}],
        'credentials': [{'secret': 'must-not-be-imported'}],
    }

    with Session(engine) as db:
        team = Team(name='Migration test')
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name='Legacy example', origin='https://example.test', paused=True)
        db.add(site)
        db.commit()

        report = import_history(db, site, data, str(artifact_root))
        pages = db.scalars(select(Page).where(Page.site_id == site.id)).all()
        measurement = db.scalar(select(Measurement).where(Measurement.site_id == site.id, Measurement.kind == 'legacy_import'))

        assert report['imported_pages'] == 3
        assert report['approval_authority_imported'] is False
        assert report['credentials_imported'] is False
        assert report['requires_fresh_inventory'] is True
        assert site.paused is True
        observed_key = 'legacy:url:' + hashlib.sha256(b'https://example.test/observed').hexdigest()[:24]
        assert {page.resource_key for page in pages} == {'post:1', 'page:2', observed_key}
        assert all(not page.enrolled and not page.managed for page in pages)
        assert measurement is not None
        archive = artifact_root / report['archive']
        archived = archive.read_text(encoding='utf-8')
        assert 'must-not-be-imported' not in archived
        assert 'embedded-secret' not in archived
        assert 'camel-api-secret' not in archived
        assert 'camel-access-secret' not in archived
        assert all('application_password' not in page.source['legacy'].get('raw', {}) for page in pages if 'legacy' in page.source)
        assert all('apiKey' not in page.source['legacy'].get('raw', {}) for page in pages if 'legacy' in page.source)
        assert all('accessToken' not in page.source['legacy'].get('raw', {}) for page in pages if 'legacy' in page.source)

        replay = import_history(db, site, data, str(artifact_root))
        assert replay['replayed'] is True
        assert db.query(Page).filter(Page.site_id == site.id).count() == 3


def _target_database(origin='https://bundle.example.test'):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        team = Team(name='Bundle test')
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name='Bundle target', origin=origin, paused=True)
        db.add(site)
        db.commit()
        return engine, site.id


def _bundle_data(origin='https://bundle.example.test/'):
    return {
        'site': {'id': 'legacy-site-1', 'name': 'Legacy bundle site', 'origin': origin},
        'observations': [{
            'payload_json': {
                'resources': [
                    {
                        'resource_key': 'posts:one',
                        'resource_type': 'posts',
                        'public_url': 'https://bundle.example.test/one',
                        'title': 'One',
                        'raw': {
                            'Application-Password': 'do-not-write',
                            'api_key': 'do-not-write',
                            'safe_note': 'retained evidence',
                        },
                    },
                    {
                        'resource_key': 'posts:two',
                        'resource_type': 'posts',
                        'public_url': 'https://bundle.example.test/two',
                        'title': 'Two',
                    },
                ],
            },
        }],
        'approvals': [{'id': 'old-approval', 'authorization': 'old-secret'}],
    }


def test_transfer_bundle_export_round_trip_is_atomic_and_redacted(tmp_path):
    source = create_engine('sqlite://')
    Base.metadata.create_all(source)
    with Session(source) as db:
        team = Team(name='Legacy source')
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name='Legacy source', origin='https://bundle.example.test', paused=True)
        db.add(site)
        db.commit()
        source_site_id = site.id
    with source.begin() as connection:
        connection.execute(text(
            'CREATE TABLE observations ('
            'id INTEGER PRIMARY KEY, site_id VARCHAR(32) NOT NULL, payload_json JSON NOT NULL)'
        ))
        connection.execute(
            text('INSERT INTO observations (site_id, payload_json) VALUES (:site_id, :payload_json)'),
            {
                'site_id': source_site_id,
                'payload_json': json.dumps({
                    'resources': [{
                        'resource_key': 'posts:one',
                        'public_url': 'https://bundle.example.test/one',
                        'title': 'One',
                        'apiKey': 'source-secret',
                    }],
                }),
            },
        )

    bundle_path = tmp_path / 'transfer' / 'legacy.json'
    manifest = export_bundle(source, source_site_id, bundle_path)
    assert bundle_path.is_file()
    assert manifest['format'] == 'forgeseo-legacy-transfer'
    assert manifest['version'] == 1
    assert manifest['redacted'] is True
    assert list(bundle_path.parent.glob('*.tmp')) == []
    raw = bundle_path.read_bytes()
    assert b'source-secret' not in raw
    payload, loaded_manifest, loaded_raw = load_bundle(bundle_path)
    assert loaded_manifest == manifest
    assert loaded_raw == raw
    assert payload['observations'][0]['payload_json']['resources'][0]['resource_key'] == 'posts:one'


def test_transfer_bundle_rejects_tampering_and_origin_mismatch(tmp_path):
    bundle_path = tmp_path / 'legacy.json'
    write_bundle(_bundle_data(), bundle_path)
    tampered = json.loads(bundle_path.read_text(encoding='utf-8'))
    tampered['payload']['site']['name'] = 'Changed after export'
    bundle_path.write_text(json.dumps(tampered), encoding='utf-8')
    with pytest.raises(ValueError, match='checksum'):
        load_bundle(bundle_path)

    write_bundle(_bundle_data(), bundle_path)
    engine, site_id = _target_database(origin='https://other.example.test')
    with Session(engine) as db:
        site = db.get(Site, site_id)
        payload, manifest, raw = load_bundle(bundle_path)
        with pytest.raises(ValueError, match='origins'):
            import_history(
                db,
                site,
                payload,
                tmp_path / 'artifacts',
                dry_run=True,
                bundle_manifest=manifest,
                archive_bytes=raw,
            )


def test_bundle_import_is_dry_run_idempotent_and_exactly_rollbackable(tmp_path):
    bundle_path = tmp_path / 'legacy.json'
    write_bundle(_bundle_data(), bundle_path)
    payload, manifest, raw = load_bundle(bundle_path)
    engine, site_id = _target_database()
    artifact_root = tmp_path / 'artifacts'
    with Session(engine) as db:
        site = db.get(Site, site_id)
        preexisting = Page(
            site_id=site.id,
            resource_key='preexisting:1',
            url='https://bundle.example.test/preexisting',
            title='Do not remove',
            resource_type='page',
            source={'source': 'fresh-inventory'},
        )
        db.add(preexisting)
        db.commit()

        preview = import_history(
            db,
            site,
            payload,
            artifact_root,
            dry_run=True,
            bundle_manifest=manifest,
            archive_bytes=raw,
        )
        assert preview['dry_run'] is True
        assert preview['would_import_pages'] == 2
        assert not (artifact_root / preview['archive']).exists()
        assert db.query(Page).filter(Page.site_id == site.id).count() == 1

        report = import_history(
            db,
            site,
            payload,
            artifact_root,
            bundle_manifest=manifest,
            archive_bytes=raw,
        )
        assert report['imported_pages'] == 2
        assert report['imported_page_ids']
        assert all(not page.enrolled and not page.managed for page in db.scalars(select(Page)).all())
        replay = import_history(
            db,
            site,
            payload,
            artifact_root,
            bundle_manifest=manifest,
            archive_bytes=raw,
        )
        assert replay['replayed'] is True
        assert db.query(Page).filter(Page.site_id == site.id).count() == 3

        rollback_preview = rollback_import(db, site, report['sha256'], artifact_root)
        assert rollback_preview['dry_run'] is True
        assert rollback_preview['would_remove_pages'] == 2
        rolled_back = rollback_import(db, site, report['sha256'], artifact_root, dry_run=False)
        assert rolled_back['rolled_back_pages'] == 2
        assert (artifact_root / report['archive']).is_file()
        remaining = db.scalars(select(Page).where(Page.site_id == site.id)).all()
        assert [page.resource_key for page in remaining] == ['preexisting:1']
        assert db.scalar(select(Measurement).where(
            Measurement.site_id == site.id,
            Measurement.kind == 'legacy_rollback',
            Measurement.source == report['sha256'],
        )) is not None
        rollback_replay = rollback_import(db, site, report['sha256'], artifact_root, dry_run=False)
        assert rollback_replay['replayed'] is True


def test_apply_import_and_rollback_require_paused_target(tmp_path):
    bundle_path = tmp_path / 'legacy.json'
    write_bundle(_bundle_data(), bundle_path)
    payload, manifest, raw = load_bundle(bundle_path)
    engine, site_id = _target_database()
    with Session(engine) as db:
        site = db.get(Site, site_id)
        site.paused = False
        db.commit()
        with pytest.raises(ValueError, match='paused'):
            import_history(
                db,
                site,
                payload,
                tmp_path / 'artifacts',
                bundle_manifest=manifest,
                archive_bytes=raw,
            )
        site.paused = True
        db.commit()
        report = import_history(
            db,
            site,
            payload,
            tmp_path / 'artifacts',
            bundle_manifest=manifest,
            archive_bytes=raw,
        )
        site.paused = False
        db.commit()
        with pytest.raises(ValueError, match='paused'):
            rollback_import(db, site, report['sha256'], tmp_path / 'artifacts', dry_run=False)
