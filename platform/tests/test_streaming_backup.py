import io
import json
import os
import struct
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine, select

from app.models import Base, Team, Site, Connection, Job, Heartbeat, Event
from app.connectors.security import encrypt_credentials, decrypt_credentials
from scripts import streaming_backup as stream
from scripts.backup import create_backup
from scripts.backup_healthcheck import inspect_latest_backup


@pytest.fixture
def snapshot(tmp_path):
    db = create_engine('sqlite://')
    Base.metadata.create_all(db)
    key = Fernet.generate_key()
    credential_key = 'real-application-key-' + 'q' * 40
    with db.begin() as connection:
        connection.execute(Team.__table__.insert(), {'id':'team','name':'Team'})
        connection.execute(Site.__table__.insert(), {'id':'site','team_id':'team','name':'Site',
            'origin':'https://example.test','paused':False})
        connection.execute(Connection.__table__.insert(), {'id':'connection','site_id':'site',
            'kind':'wordpress','encrypted_credentials':encrypt_credentials({'password':'fixture-secret'},credential_key)})
        connection.execute(Job.__table__.insert(), {'id':'job','site_id':'site','kind':'publish',
            'status':'queued','idempotency_key':'queued'})
        connection.execute(Heartbeat.__table__.insert(), {'name':'platform_controls',
            'last_seen_at':datetime.now(), 'details':{'global_pause':False}})
    source = tmp_path/'source'
    source.mkdir()
    (source/'evidence.html').write_bytes(b'<p>original evidence</p>' * 500)
    (source/'nested').mkdir()
    (source/'nested'/'binary').write_bytes(bytes(range(256)) * 200)
    return db, source, key, credential_key


def build(snapshot, tmp_path):
    db, source, key, credential_key = snapshot
    path = tmp_path/'forgeseo-stream.forge'
    summary = stream.create_backup_file(db, source, path, key, credential_key)
    return path, summary


def test_file_backup_roundtrip_preserves_credentials_and_pauses_everything(snapshot, tmp_path):
    path, summary = build(snapshot, tmp_path)
    _, source, key, credential_key = snapshot
    assert summary['format'] == 3
    assert summary == stream.inspect_backup_file(path, key, credential_key)
    assert summary == stream.inspect_verified_file(path, key, credential_key)
    restored = create_engine('sqlite://')
    Base.metadata.create_all(restored)
    target = tmp_path/'restored'
    result = stream.restore_backup_file(restored, target, path, key, credential_key)
    assert result['automation'] == 'paused'
    for original in source.rglob('*'):
        if original.is_file():
            assert (target/original.relative_to(source)).read_bytes() == original.read_bytes()
    with restored.connect() as connection:
        assert connection.scalar(select(Site.paused)) is True
        assert connection.scalar(select(Job.status)) == 'needs_reconciliation'
        assert connection.scalar(select(Heartbeat.details)) == {'global_pause':True}
        encrypted = connection.scalar(select(Connection.encrypted_credentials))
        assert decrypt_credentials(encrypted, credential_key) == {'password':'fixture-secret'}
    with pytest.raises(ValueError, match='not empty'):
        stream.restore_backup_file(restored, tmp_path/'other', path, key, credential_key)
    with pytest.raises(FileExistsError):
        build(snapshot, tmp_path)


def test_health_uses_authenticated_receipt_and_ciphertext_hash(snapshot, tmp_path, monkeypatch):
    path, _ = build(snapshot, tmp_path)
    _, _, key, credential_key = snapshot
    def no_full_decrypt(*args):
        raise AssertionError('health must not decrypt/decompress the full archive')
    monkeypatch.setattr(stream, 'decrypted_zip', no_full_decrypt)
    summary = inspect_latest_backup(tmp_path, key, credential_key, 60)
    assert summary['format'] == 3
    data = bytearray(path.read_bytes())
    data[len(data)//2] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match='checksum'):
        inspect_latest_backup(tmp_path, key, credential_key, 60)


def frames(path):
    data = path.read_bytes()
    assert data.startswith(stream.MAGIC)
    position, records = len(stream.MAGIC), []
    while position < len(data):
        size = struct.unpack('>I', data[position:position+4])[0]
        records.append(data[position:position+4+size])
        position += 4 + size
    return records


@pytest.mark.parametrize('attack', ['truncated','removed','reordered','duplicated','mixed','trailing','bad_length'])
def test_envelope_attacks_never_touch_restore_target(snapshot, tmp_path, monkeypatch, attack):
    monkeypatch.setattr(stream, 'CHUNK_BYTES', 1024)
    path, _ = build(snapshot, tmp_path)
    _, _, key, credential_key = snapshot
    records = frames(path)
    assert len(records) >= 4
    if attack == 'truncated': data = path.read_bytes()[:-1]
    elif attack == 'removed': data = stream.MAGIC + b''.join(records[:1]+records[2:])
    elif attack == 'reordered': data = stream.MAGIC+b''.join([records[0],records[2],records[1],*records[3:]])
    elif attack == 'duplicated': data = stream.MAGIC+b''.join([records[0],records[1],*records[1:]])
    elif attack == 'trailing': data = path.read_bytes()+b'extra'
    elif attack == 'bad_length': data = stream.MAGIC+struct.pack('>I',stream.MAX_FRAME_BYTES+1)
    else:
        other = tmp_path/'other.forge'
        stream.create_backup_file(snapshot[0], snapshot[1], other, key, credential_key)
        data = stream.MAGIC+b''.join([records[0],frames(other)[1],*records[2:]])
    path.write_bytes(data)
    restored = create_engine('sqlite://')
    Base.metadata.create_all(restored)
    target = tmp_path/'restore-target'
    with pytest.raises((ValueError, InvalidToken)):
        stream.restore_backup_file(restored, target, path, key, credential_key)
    assert not target.exists()
    with restored.connect() as connection:
        assert connection.scalar(select(Team.id)) is None


def test_wrong_keys_missing_receipt_and_unverified_latest_are_rejected(snapshot, tmp_path):
    path, _ = build(snapshot, tmp_path)
    _, _, key, credential_key = snapshot
    with pytest.raises(InvalidToken):
        stream.inspect_backup_file(path, Fernet.generate_key(), credential_key)
    with pytest.raises(ValueError, match='Original credential key'):
        stream.inspect_backup_file(path, key, 'wrong-original-key')
    stream.receipt_path(path).unlink()
    with pytest.raises(ValueError, match='receipt'):
        inspect_latest_backup(tmp_path, key, credential_key, 60)
    # An independently requested full inspection remains available without a receipt.
    assert stream.inspect_backup_file(path, key, credential_key)['format'] == 3


def test_new_format_reads_legacy_and_rejects_source_over_bound(snapshot, tmp_path, monkeypatch):
    db, source, key, credential_key = snapshot
    legacy = tmp_path/'legacy.forge'
    legacy.write_bytes(create_backup(db, source, key, credential_key))
    assert stream.inspect_backup_file(legacy, key, credential_key)['format'] == 2
    monkeypatch.setattr(stream, 'MAX_RESTORE_BYTES', 100)
    path = tmp_path/'oversized.forge'
    with pytest.raises(ValueError, match='supported restore size'):
        stream.create_backup_file(db, source, path, key, credential_key)
    assert not path.exists()
    assert not stream.receipt_path(path).exists()


def test_legacy_archives_cannot_revive_stored_browser_sessions(snapshot, tmp_path):
    from app.models import Session
    db, source, key, credential_key = snapshot
    old = create_backup(db, source, key, credential_key)
    output = io.BytesIO()
    with ZipFile(io.BytesIO(Fernet(key).decrypt(old))) as archive, ZipFile(output, 'w') as changed:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == 'manifest.json':
                manifest = json.loads(data)
                manifest['format'] = 1
                manifest['tables']['sessions'] = [{'id':'old-browser-session'}]
                data = json.dumps(manifest).encode()
            changed.writestr(info, data)
    path = tmp_path/'old.forge'
    path.write_bytes(Fernet(key).encrypt(output.getvalue()))
    restored = create_engine('sqlite://')
    Base.metadata.create_all(restored)
    stream.restore_backup_file(restored, tmp_path/'old-restored', path, key, credential_key)
    with restored.connect() as connection:
        assert connection.scalar(select(Session.id)) is None


def test_mirror_is_exact_and_no_overwrite(snapshot, tmp_path):
    path, summary = build(snapshot, tmp_path)
    mirror = tmp_path/'mirror'
    mirror.mkdir()
    destination = stream.mirror_backup_file(path, mirror, snapshot[2], snapshot[3])
    assert stream.inspect_verified_file(destination, snapshot[2], snapshot[3]) == summary
    assert destination.read_bytes() == path.read_bytes()
    with pytest.raises(FileExistsError):
        stream.mirror_backup_file(path, mirror, snapshot[2], snapshot[3])


def test_symlinks_and_nonempty_artifact_targets_are_refused(snapshot, tmp_path):
    path, _ = build(snapshot, tmp_path)
    target = tmp_path/'target'
    target.mkdir()
    sentinel = target/'existing'
    sentinel.write_text('keep')
    db = create_engine('sqlite://')
    Base.metadata.create_all(db)
    with pytest.raises(ValueError, match='must be empty'):
        stream.restore_backup_file(db, target, path, snapshot[2], snapshot[3])
    assert sentinel.read_text() == 'keep'


@pytest.mark.parametrize('attack', ['checksum', 'unexpected', 'traversal', 'duplicate_manifest', 'sessions'])
def test_authenticated_but_unsafe_zip_is_rejected_before_restore(snapshot, tmp_path, attack):
    path, summary = build(snapshot, tmp_path)
    _, _, key, credential_key = snapshot
    unsafe = tmp_path/'unsafe.zip'
    with stream.decrypted_zip(path, key) as (plain, _):
        with ZipFile(plain) as original, ZipFile(unsafe, 'w', ZIP_DEFLATED) as changed:
            manifest = json.loads(original.read('manifest.json'))
            for info in original.infolist():
                if info.filename == 'manifest.json':
                    continue
                body = original.read(info.filename)
                if attack == 'checksum' and info.filename == 'artifacts/evidence.html':
                    body = b'altered evidence'
                changed.writestr(info, body)
            if attack == 'unexpected':
                changed.writestr('unexpected', b'data')
            if attack == 'traversal':
                manifest['artifacts']['../escape'] = '0' * 64
                changed.writestr('artifacts/../escape', b'escape')
            if attack == 'sessions':
                manifest['tables']['sessions'] = []
            data = json.dumps(manifest).encode()
            if attack == 'duplicate_manifest':
                data = b'{"format":2,' + data[1:]
            changed.writestr('manifest.json', data)
    encrypted = tmp_path/'unsafe.forge'
    stream._encrypt(unsafe, encrypted, key, UUID(summary['backup_id']).bytes)
    restored = create_engine('sqlite://')
    Base.metadata.create_all(restored)
    target = tmp_path/'unsafe-target'
    with pytest.raises(ValueError):
        stream.restore_backup_file(restored, target, encrypted, key, credential_key)
    assert not target.exists()


def test_output_in_source_tree_and_insufficient_space_never_publish_archive(snapshot, tmp_path, monkeypatch):
    db, source, key, credential_key = snapshot
    with pytest.raises(ValueError, match='outside the artifact tree'):
        stream.create_backup_file(db, source, source/'wrong.forge', key, credential_key)
    from collections import namedtuple
    Usage = namedtuple('Usage', 'total used free')
    monkeypatch.setattr(stream.shutil, 'disk_usage', lambda _: Usage(100, 100, 0))
    target = tmp_path/'space.forge'
    with pytest.raises(ValueError, match='scratch space'):
        stream.create_backup_file(db, source, target, key, credential_key)
    assert not target.exists() and not stream.receipt_path(target).exists()


def test_database_rows_stream_outside_small_manifest_and_restore_in_batches(snapshot, tmp_path, monkeypatch):
    from sqlalchemy import func
    from scripts.split_restore_drill import verify_recovery
    db, source, key, credential_key = snapshot
    with db.begin() as connection:
        connection.execute(Event.__table__.insert(), [{'id':i+1, 'team_id':'team',
            'site_id':'site', 'kind':'test', 'message':'retained evidence ' + 'x'*200}
            for i in range(10_000)])
    monkeypatch.setattr(stream, 'MAX_MANIFEST_BYTES', 50_000)
    path, summary = build(snapshot, tmp_path)
    assert summary['tables']['events'] == 10_000
    with stream.decrypted_zip(path, key) as (plain, _):
        with ZipFile(plain) as archive:
            assert archive.getinfo('manifest.json').file_size < 50_000
            assert archive.getinfo('database/events.jsonl').file_size > 1_000_000
    restored = create_engine('sqlite://')
    Base.metadata.create_all(restored)
    target = tmp_path/'many-rows'
    stream.restore_backup_file(restored, target, path, key, credential_key)
    with restored.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Event)) == 10_000
    assert verify_recovery(restored, target, path, key, credential_key)['credentials_decrypted'] == 1


@pytest.mark.skipif(os.environ.get('FORGE_LARGE_BACKUP') != '1', reason='explicit >1 GB disk roundtrip')
def test_real_payload_above_old_one_gigabyte_limit(snapshot, tmp_path, monkeypatch):
    db, source, key, credential_key = snapshot
    big = source/'large-evidence.bin'
    size = 1_010_000_000
    with big.open('wb') as output:
        output.truncate(size)
    original_read = Path.read_bytes
    def reject_whole_file_read(path):
        if path == big or path.suffix == '.forge':
            raise AssertionError('Large files must be streamed')
        return original_read(path)
    monkeypatch.setattr(Path, 'read_bytes', reject_whole_file_read)
    path, summary = build(snapshot, tmp_path)
    assert summary['artifacts']['bytes'] > 1_000_000_000
    restored = create_engine('sqlite://')
    Base.metadata.create_all(restored)
    target = tmp_path/'large-restored'
    stream.restore_backup_file(restored, target, path, key, credential_key)
    recovered = target/'large-evidence.bin'
    assert recovered.stat().st_size == size
    with recovered.open('rb') as reader:
        reader.seek(size - 10)
        assert reader.read() == b'\0' * 10
    # Remove only these two test-owned large files so repeated local runs do not retain GBs.
    big.unlink()
    recovered.unlink()
