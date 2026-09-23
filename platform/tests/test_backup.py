import io
import json
import os
import sys
from cryptography.fernet import Fernet, InvalidToken
import pytest
from sqlalchemy import create_engine, select
from pathlib import Path
from zipfile import ZipFile

from app.models import Base, Team, Site, Connection
from app.connectors.security import encrypt_credentials, decrypt_credentials
from scripts import backup as backup_module
from scripts.backup import create_backup,mirror_backup,restore_backup,write_backup
from scripts.backup_healthcheck import inspect_latest_backup


def _inspection_fixture(tmp_path):
    database = create_engine('sqlite://')
    Base.metadata.create_all(database)
    credential_key = 'application-encryption-key-' + 'x' * 40
    backup_key = Fernet.generate_key()
    with database.begin() as conn:
        conn.execute(Team.__table__.insert(), {'id': 'team', 'name': 'Inspection team'})
        conn.execute(Site.__table__.insert(), {
            'id': 'site',
            'team_id': 'team',
            'name': 'Inspection site',
            'origin': 'https://example.test',
            'paused': False,
        })
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'evidence.html').write_text('<h1>Saved evidence</h1>')
    (source / 'nested').mkdir()
    (source / 'nested' / 'second.txt').write_text('second artifact')
    return database, create_backup(database, source, backup_key, credential_key), backup_key, credential_key


def _rewrite_archive(payload, backup_key, add_entry=None, replace_artifact=False):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(Fernet(backup_key).decrypt(payload)), 'r') as source, ZipFile(output, 'w') as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if replace_artifact and info.filename == 'artifacts/evidence.html':
                body = b'tampered evidence'
            target.writestr(info, body)
        if add_entry:
            target.writestr(add_entry, b'unexpected')
    return Fernet(backup_key).encrypt(output.getvalue())


def test_backup_inspection_returns_json_summary_without_touching_targets(tmp_path, monkeypatch):
    _, payload, backup_key, credential_key = _inspection_fixture(tmp_path)
    target_database = create_engine('sqlite://')
    Base.metadata.create_all(target_database)
    with target_database.begin() as conn:
        conn.execute(Team.__table__.insert(), {'id': 'target', 'name': 'Must remain'})
    target_artifacts = tmp_path / 'target-artifacts'
    target_artifacts.mkdir()
    sentinel = target_artifacts / 'sentinel.txt'
    sentinel.write_text('must remain')

    class DatabaseMustNotBeOpened:
        def connect(self):
            raise AssertionError('inspection must not open a database')

    monkeypatch.setattr(backup_module, 'engine', DatabaseMustNotBeOpened())
    result = backup_module.inspect_backup(payload, backup_key, credential_key)

    assert result['format'] == 2
    assert result['backup_id']
    assert result['created_at']
    assert result['tables'][Team.__table__.name] == 1
    assert result['tables'][Site.__table__.name] == 1
    assert result['artifacts'] == {
        'count': 2,
        'bytes': len('<h1>Saved evidence</h1>') + len('second artifact'),
    }
    assert json.loads(json.dumps(result)) == result
    assert sentinel.read_text() == 'must remain'
    assert [path.relative_to(target_artifacts).as_posix() for path in target_artifacts.rglob('*')] == ['sentinel.txt']
    with target_database.connect() as conn:
        assert conn.scalar(select(Team.name).where(Team.id == 'target')) == 'Must remain'


def test_backup_inspection_cli_prints_summary_and_requires_both_keys(tmp_path, monkeypatch, capsys):
    _, payload, backup_key, credential_key = _inspection_fixture(tmp_path)
    archive = tmp_path / 'inspection.forge'
    archive.write_bytes(payload)
    monkeypatch.setenv('BACKUP_KEY', backup_key.decode('ascii'))
    monkeypatch.setenv('ENCRYPTION_KEY', credential_key)
    monkeypatch.setattr(sys, 'argv', ['backup.py', 'inspect', str(archive)])

    backup_module.main()

    output = json.loads(capsys.readouterr().out)
    assert output['format'] == 2
    assert 'application-encryption-key' not in json.dumps(output)


def test_backup_inspection_rejects_wrong_keys(tmp_path):
    _, payload, backup_key, credential_key = _inspection_fixture(tmp_path)
    with pytest.raises(InvalidToken):
        backup_module.inspect_backup(payload, Fernet.generate_key(), credential_key)
    with pytest.raises(ValueError, match='Original credential key'):
        backup_module.inspect_backup(payload, backup_key, 'different-credential-key')


def test_backup_inspection_rejects_tampered_artifact_and_archive(tmp_path):
    _, payload, backup_key, credential_key = _inspection_fixture(tmp_path)
    tampered_artifact = _rewrite_archive(payload, backup_key, replace_artifact=True)
    with pytest.raises(ValueError, match='Artifact checksum mismatch'):
        backup_module.inspect_backup(tampered_artifact, backup_key, credential_key)

    tampered_archive = _rewrite_archive(payload, backup_key, add_entry='unexpected.txt')
    with pytest.raises(ValueError, match='unexpected archive paths'):
        backup_module.inspect_backup(tampered_archive, backup_key, credential_key)


def test_backup_healthcheck_requires_a_fresh_valid_archive(tmp_path):
    _, payload, backup_key, credential_key = _inspection_fixture(tmp_path)
    archive = tmp_path / 'forgeseo-checkpoint.forge'
    archive.write_bytes(payload)
    os.utime(archive, (995, 995))

    summary = inspect_latest_backup(
        tmp_path,
        backup_key,
        credential_key,
        max_age_seconds=10,
        now=1000,
    )
    assert summary['archive'] == archive.name
    assert summary['age_seconds'] == 5.0

    with pytest.raises(RuntimeError, match='stale'):
        inspect_latest_backup(tmp_path, backup_key, credential_key, 4, now=1000)

    archive.write_bytes(_rewrite_archive(payload, backup_key, replace_artifact=True))
    os.utime(archive, (999, 999))
    with pytest.raises(ValueError, match='Artifact checksum mismatch'):
        inspect_latest_backup(tmp_path, backup_key, credential_key, 10, now=1000)


def test_encrypted_restore_recovers_data_artifacts_and_credentials(tmp_path):
    original=create_engine('sqlite://')
    restored=create_engine('sqlite://')
    Base.metadata.create_all(original)
    Base.metadata.create_all(restored)
    # The application accepts any strong value and derives its Fernet key.
    credential_key='application-encryption-key-' + 'x' * 40
    backup_key=Fernet.generate_key()
    with original.begin() as conn:
        conn.execute(Team.__table__.insert(),{'id':'team','name':'Test team'})
        conn.execute(Site.__table__.insert(),{'id':'site','team_id':'team','name':'Test','origin':'https://example.test','paused':False})
        conn.execute(Connection.__table__.insert(),{'id':'connection','site_id':'site','kind':'wordpress','encrypted_credentials':encrypt_credentials({'password':'private-test-value'},credential_key)})
    source=tmp_path/'source'
    source.mkdir()
    (source/'evidence.html').write_text('<h1>Saved evidence</h1>')
    payload=create_backup(original,source,backup_key,credential_key)
    assert b'private-test-value' not in payload
    with pytest.raises(InvalidToken):
        restore_backup(restored,tmp_path/'bad',payload,Fernet.generate_key(),credential_key)
    with pytest.raises(ValueError,match='Original credential key'):
        restore_backup(restored,tmp_path/'bad',payload,backup_key,'different-key')
    result=restore_backup(restored,tmp_path/'restored',payload,backup_key,credential_key)
    assert result['automation']=='paused'
    assert (tmp_path/'restored'/'evidence.html').read_text()=='<h1>Saved evidence</h1>'
    with restored.connect() as conn:
        assert conn.scalar(select(Site.paused)) is True
        encrypted=conn.scalar(select(Connection.encrypted_credentials))
        assert decrypt_credentials(encrypted,credential_key)['password']=='private-test-value'
    with pytest.raises(ValueError,match='not empty'):
        restore_backup(restored,tmp_path/'another',payload,backup_key,credential_key)


def test_backup_writer_is_atomic_and_refuses_overwrite(tmp_path):
    path=tmp_path/'nested'/'checkpoint.forge'
    write_backup(path,b'encrypted-payload')
    assert path.read_bytes()==b'encrypted-payload'
    with pytest.raises(FileExistsError):
        write_backup(path,b'new-payload')


def test_backup_writer_does_not_clobber_destination_that_appears_during_commit(tmp_path,monkeypatch):
    path=tmp_path/'checkpoint.forge'
    real_link=backup_module.os.link

    def create_racing_destination(source,target):
        Path(target).write_bytes(b'older-archive')
        return real_link(source,target)

    monkeypatch.setattr(backup_module.os,'link',create_racing_destination)
    with pytest.raises(FileExistsError):
        write_backup(path,b'new-payload')
    assert path.read_bytes()==b'older-archive'
    assert not list(path.parent.glob(f'.{path.name}.*.tmp'))


def test_backup_mirror_is_atomic_exact_and_refuses_overwrite(tmp_path):
    local = tmp_path / 'local' / 'forgeseo-checkpoint.forge'
    mirror = tmp_path / 'mounted-mirror'
    mirror.mkdir()
    write_backup(local, b'exact-encrypted-archive')

    destination = mirror_backup(local, mirror)

    assert destination == mirror / local.name
    assert destination.read_bytes() == local.read_bytes() == b'exact-encrypted-archive'
    if os.name != 'nt':
        assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        mirror_backup(local, mirror)
    assert destination.read_bytes() == b'exact-encrypted-archive'
    assert not list(mirror.glob(f'.{destination.name}.*.tmp'))


def test_backup_mirror_is_opt_in_and_requires_a_separate_mounted_directory(tmp_path):
    local = tmp_path / 'forgeseo-checkpoint.forge'
    write_backup(local, b'local-only')

    assert mirror_backup(local, None) is None
    with pytest.raises(RuntimeError, match='separate'):
        mirror_backup(local, local.parent)
    with pytest.raises(RuntimeError, match='unavailable'):
        mirror_backup(local, tmp_path / 'not-mounted')


def test_backup_create_cli_mirrors_only_after_local_write(tmp_path, monkeypatch, capsys):
    local = tmp_path / 'local' / 'forgeseo-created.forge'
    mirror = tmp_path / 'mounted-mirror'
    mirror.mkdir()
    payload = b'exact-cli-archive'
    monkeypatch.setenv('BACKUP_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setenv('BACKUP_MIRROR_DIRECTORY', str(mirror))
    monkeypatch.setattr(backup_module, 'create_backup', lambda *args: payload)
    monkeypatch.setattr(sys, 'argv', ['backup.py', 'create', str(local)])

    backup_module.main()

    assert local.read_bytes() == (mirror / local.name).read_bytes() == payload
    assert 'mirrored' in capsys.readouterr().out


def test_backup_healthcheck_requires_the_newest_archive_in_the_mirror(tmp_path):
    _, payload, backup_key, credential_key = _inspection_fixture(tmp_path)
    local = tmp_path / 'local'
    local.mkdir()
    archive = local / 'forgeseo-checkpoint.forge'
    archive.write_bytes(payload)
    mirror = tmp_path / 'mounted-mirror'
    mirror.mkdir()

    with pytest.raises(RuntimeError, match='missing from configured mirror'):
        inspect_latest_backup(
            local,
            backup_key,
            credential_key,
            max_age_seconds=10,
            mirror_directory=mirror,
            now=archive.stat().st_mtime + 1,
        )

    (mirror / archive.name).write_bytes(payload + b'changed')
    with pytest.raises(RuntimeError, match='does not match'):
        inspect_latest_backup(
            local,
            backup_key,
            credential_key,
            max_age_seconds=10,
            mirror_directory=mirror,
            now=archive.stat().st_mtime + 1,
        )

    (mirror / archive.name).write_bytes(payload)
    summary = inspect_latest_backup(
        local,
        backup_key,
        credential_key,
        max_age_seconds=10,
        mirror_directory=mirror,
        now=archive.stat().st_mtime + 1,
    )
    assert summary['mirror'] == {'archive': archive.name}


def test_restore_cleans_artifacts_when_materialization_fails(tmp_path,monkeypatch):
    original=create_engine('sqlite://')
    restored=create_engine('sqlite://')
    Base.metadata.create_all(original)
    Base.metadata.create_all(restored)
    credential_key=Fernet.generate_key().decode()
    backup_key=Fernet.generate_key()
    source=tmp_path/'source'
    source.mkdir()
    (source/'nested').mkdir()
    (source/'nested'/'evidence.html').write_text('saved')
    payload=create_backup(original,source,backup_key,credential_key)
    target=tmp_path/'restored'
    real_replace=backup_module.os.replace

    def fail_for_target(source_path,target_path):
        if str(target_path).startswith(str(target)):
            raise OSError('simulated artifact commit failure')
        return real_replace(source_path,target_path)

    monkeypatch.setattr(backup_module.os,'replace',fail_for_target)
    with pytest.raises(OSError,match='simulated'):
        restore_backup(restored,target,payload,backup_key,credential_key)
    assert not target.exists() or not any(target.iterdir())
    with restored.connect() as conn:
        assert conn.scalar(select(Site.id)) is None


def test_restore_stages_artifacts_inside_destination_root(tmp_path,monkeypatch):
    original=create_engine('sqlite://')
    restored=create_engine('sqlite://')
    Base.metadata.create_all(original)
    Base.metadata.create_all(restored)
    credential_key=Fernet.generate_key().decode()
    backup_key=Fernet.generate_key()
    source=tmp_path/'source'
    source.mkdir()
    (source/'evidence.html').write_text('saved')
    payload=create_backup(original,source,backup_key,credential_key)
    target=tmp_path/'restored'
    real_mkdtemp=backup_module.tempfile.mkdtemp

    def record_staging(*args,**kwargs):
        staging=real_mkdtemp(*args,**kwargs)
        assert Path(staging).parent==target
        return staging

    monkeypatch.setattr(backup_module.tempfile,'mkdtemp',record_staging)
    restore_backup(restored,target,payload,backup_key,credential_key)
    assert (target/'evidence.html').read_text()=='saved'
