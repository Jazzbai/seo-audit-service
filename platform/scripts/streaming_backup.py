"""Disk-backed, authenticated chunk backups, with bounded restore validation.

The v3 envelope encrypts a ZIP in 8 MiB Fernet records. Every record binds the
archive UUID, type and ordinal; a mandatory authenticated footer commits the
record count, compressed byte count and SHA-256. Truncation, reordering, mixing
archives and trailing data fail before the restore target is opened. Database
rows are streamed as JSONL; a small index authenticates every table and file.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from app.models import Base
from scripts.backup import (_application_fernet, _fernet, _fingerprint,
    _manifest_object_pairs, _validate_artifact_name, _validate_manifest,
    inspect_backup, restore_backup, restore_validated_archive, write_backup)

MAGIC = b'JAZZB-BACKUP-3\n'
CHUNK_BYTES = 8 * 1024 * 1024
BUFFER_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 12 * 1024 * 1024
MAX_RESTORE_BYTES = 32 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ROW_BYTES = 16 * 1024 * 1024
MAX_DATABASE_ROWS = 20_000_000
MAX_ARCHIVE_ENTRIES = 100_000
DISK_RESERVE_BYTES = 64 * 1024 * 1024


def _space(directory, needed):
    if shutil.disk_usage(directory).free < needed + DISK_RESERVE_BYTES:
        raise ValueError('Insufficient scratch space for backup or restore')


def _sync_directory(directory):
    if os.name == 'posix':
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _copy(source, output, *, limit=MAX_RESTORE_BYTES):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(BUFFER_BYTES):
        size += len(chunk)
        if size > limit:
            raise ValueError('Backup exceeds supported restore size')
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    return digest.hexdigest(), size


def _file_hash(path):
    with Path(path).open('rb') as source:
        return _copy(source, None, limit=MAX_RESTORE_BYTES * 2)


def is_streamed_backup(path):
    with Path(path).open('rb') as source:
        return source.read(len(MAGIC)) == MAGIC


def _frame(output, cipher, value):
    token = cipher.encrypt(value)
    if len(token) > MAX_FRAME_BYTES:
        raise ValueError('Encrypted backup frame exceeds its bound')
    output.write(struct.pack('>I', len(token)))
    output.write(token)


def _read_frame(source, cipher):
    raw = source.read(4)
    if len(raw) != 4:
        raise ValueError('Backup is truncated')
    size = struct.unpack('>I', raw)[0]
    if not 1 <= size <= MAX_FRAME_BYTES:
        raise ValueError('Invalid encrypted backup frame size')
    token = source.read(size)
    if len(token) != size:
        raise ValueError('Backup is truncated')
    return cipher.decrypt(token)


def _header(source, cipher):
    if source.read(len(MAGIC)) != MAGIC:
        raise ValueError('Unsupported backup envelope')
    header = _read_frame(source, cipher)
    if len(header) != 21 or header[:1] != b'H' or struct.unpack('>I', header[17:])[0] != CHUNK_BYTES:
        raise ValueError('Invalid authenticated backup header')
    return header[1:17]


def _encrypt(source_path, destination, key, identity):
    cipher = _fernet(key, 'BACKUP_KEY')
    digest, size, count = hashlib.sha256(), 0, 0
    with Path(source_path).open('rb') as source, Path(destination).open('xb') as output:
        os.chmod(destination, 0o600)
        output.write(MAGIC)
        _frame(output, cipher, b'H' + identity + struct.pack('>I', CHUNK_BYTES))
        while chunk := source.read(CHUNK_BYTES):
            _space(Path(destination).parent, MAX_FRAME_BYTES)
            size += len(chunk)
            if size > MAX_RESTORE_BYTES:
                raise ValueError('Backup exceeds supported restore size')
            digest.update(chunk)
            _frame(output, cipher, b'C' + identity + struct.pack('>Q', count) + chunk)
            count += 1
        _frame(output, cipher, b'F' + identity + struct.pack('>QQ', count, size) + digest.digest())
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def decrypted_zip(path, key):
    """Yield only a completely authenticated plaintext ZIP in a private scratch dir."""
    cipher = _fernet(key, 'BACKUP_KEY')
    with tempfile.TemporaryDirectory(prefix='jazzb-backup-') as directory:
        plain = Path(directory) / 'authenticated.zip'
        with Path(path).open('rb') as source, plain.open('xb') as output:
            os.chmod(plain, 0o600)
            identity = _header(source, cipher)
            digest, size, count = hashlib.sha256(), 0, 0
            while True:
                value = _read_frame(source, cipher)
                if value[1:17] != identity:
                    raise ValueError('Backup records belong to different archives')
                if value[:1] == b'F':
                    expected = b'F' + identity + struct.pack('>QQ', count, size) + digest.digest()
                    if value != expected or source.read(1):
                        raise ValueError('Backup footer or record sequence is invalid')
                    break
                if (value[:1] != b'C' or len(value) <= 25 or len(value) > CHUNK_BYTES + 25
                        or struct.unpack('>Q', value[17:25])[0] != count):
                    raise ValueError('Backup record sequence is invalid')
                chunk = value[25:]
                size += len(chunk)
                if size > MAX_RESTORE_BYTES:
                    raise ValueError('Backup exceeds supported restore size')
                _space(directory, len(chunk))
                output.write(chunk)
                digest.update(chunk)
                count += 1
        yield plain, identity.hex()


def _build_zip(database, artifact_root, output_path, identity, credential_key):
    """Stream each table as ordered JSONL, then files, with a small index manifest."""
    root = Path(artifact_root)
    if root.is_symlink():
        raise ValueError('Artifact root symlinks cannot be backed up')
    manifest = {'format': 3, 'backup_id': identity.hex,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'credential_key_sha256': _fingerprint(credential_key), 'tables': {}, 'artifacts': {}}
    total, row_total, index_bytes = 0, 0, 0
    with ZipFile(output_path, 'w', ZIP_DEFLATED, allowZip64=True) as archive:
        with database.connect() as connection:
            if database.dialect.name == 'postgresql':
                connection = connection.execution_options(isolation_level='REPEATABLE READ')
            with connection.begin():
                for table in Base.metadata.sorted_tables:
                    if table.name == 'sessions':
                        continue
                    digest, size, count = hashlib.sha256(), 0, 0
                    with archive.open('database/' + table.name + '.jsonl', 'w', force_zip64=True) as target:
                        query = select(table).order_by(*table.primary_key.columns)
                        rows = connection.execution_options(yield_per=20).execute(query).mappings()
                        for row in rows:
                            data = json.dumps(dict(row), sort_keys=True, ensure_ascii=False,
                                default=lambda v: v.isoformat(), separators=(',', ':')).encode() + b'\n'
                            if len(data) > MAX_ROW_BYTES:
                                raise ValueError('Backup database row exceeds its bound')
                            count += 1
                            row_total += 1
                            size += len(data)
                            total += len(data)
                            if total > MAX_RESTORE_BYTES or row_total > MAX_DATABASE_ROWS:
                                raise ValueError('Backup exceeds supported restore size or row count')
                            target.write(data)
                            digest.update(data)
                    manifest['tables'][table.name] = {'rows': count, 'bytes': size,
                        'sha256': digest.hexdigest(), 'columns': list(table.columns.keys())}
        if root.exists():
            for path in root.rglob('*'):
                if path.is_symlink():
                    raise ValueError('Artifact symlinks cannot be backed up')
                if not path.is_file():
                    continue
                name = path.relative_to(root).as_posix()
                _validate_artifact_name(name)
                index_bytes += len(name.encode()) + 80
                if index_bytes > MAX_MANIFEST_BYTES:
                    raise ValueError('Backup artifact index exceeds its bound')
                if len(manifest['artifacts']) + len(manifest['tables']) >= MAX_ARCHIVE_ENTRIES - 1 or len(name.encode()) > 4096:
                    raise ValueError('Backup artifact manifest exceeds its bound')
                before = path.stat()
                if before.st_size > MAX_RESTORE_BYTES - total:
                    raise ValueError('Backup exceeds supported restore size')
                with path.open('rb') as source, archive.open('artifacts/' + name, 'w', force_zip64=True) as target:
                    digest, size = _copy(source, target, limit=MAX_RESTORE_BYTES - total)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError('Artifact changed during backup')
                manifest['artifacts'][name] = digest
                total += size
        data = json.dumps(manifest, separators=(',', ':')).encode()
        if len(data) > MAX_MANIFEST_BYTES or total + len(data) > MAX_RESTORE_BYTES:
            raise ValueError('Backup manifest or total size exceeds its bound')
        archive.writestr('manifest.json', data)


def _manifest3(manifest, credential_key):
    if not isinstance(manifest, dict) or manifest.get('format') != 3 or not isinstance(manifest.get('tables'), dict):
        raise ValueError('Unsupported streamed backup manifest')
    tables = manifest['tables']
    _, artifacts = _validate_manifest({**manifest, 'format': 2,
        'tables': {name: [] for name in tables}}, credential_key)
    if 'sessions' in tables or not set(tables).issubset(Base.metadata.tables):
        raise ValueError('Backup contains unsupported database tables')
    total_rows = 0
    for name, description in tables.items():
        if (not isinstance(description, dict)
                or any(type(description.get(k)) is not int or description[k] < 0 for k in ('rows', 'bytes'))
                or not isinstance(description.get('sha256'), str) or len(description['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in description['sha256'])
                or not isinstance(description.get('columns'), list)
                or any(not isinstance(c, str) for c in description['columns'])
                or len(description['columns']) != len(set(description['columns']))):
            raise ValueError('Backup table descriptor is invalid')
        if not set(description['columns']).issubset(Base.metadata.tables[name].columns.keys()):
            raise ValueError('Backup table schema is not supported')
        total_rows += description['rows']
    if total_rows > MAX_DATABASE_ROWS:
        raise ValueError('Backup database row count exceeds its bound')
    return tables, artifacts


def iter_table_rows(archive, name, description):
    digest, size, count = hashlib.sha256(), 0, 0
    with archive.open('database/' + name + '.jsonl') as source:
        while data := source.readline(MAX_ROW_BYTES + 1):
            if len(data) > MAX_ROW_BYTES or not data.endswith(b'\n'):
                raise ValueError('Backup database row exceeds its bound or is truncated')
            digest.update(data)
            size += len(data)
            count += 1
            if count > description['rows'] or size > description['bytes']:
                raise ValueError('Backup table size does not match its descriptor')
            row = json.loads(data, object_pairs_hook=_manifest_object_pairs)
            if not isinstance(row, dict) or set(row) != set(description['columns']):
                raise ValueError('Backup database row schema is invalid')
            yield row
    if count != description['rows'] or size != description['bytes'] or digest.hexdigest() != description['sha256']:
        raise ValueError('Backup table checksum or row count mismatch')


def _validate_zip(path, identity, credential_key):
    """Check entry bounds, the complete manifest and every artifact before restore."""
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > MAX_ARCHIVE_ENTRIES or len(names) != len(set(names)):
                raise ValueError('Backup contains duplicate or excessive archive entries')
            if (sum(info.file_size for info in infos) > MAX_RESTORE_BYTES
                    or any(info.file_size < 0 or stat.S_ISLNK(info.external_attr >> 16) for info in infos)):
                raise ValueError('Backup exceeds supported restore size or contains a symlink')
            if archive.getinfo('manifest.json').file_size > MAX_MANIFEST_BYTES:
                raise ValueError('Backup database manifest exceeds its bound')
            manifest = json.loads(archive.read('manifest.json'), object_pairs_hook=_manifest_object_pairs)
            tables, artifacts = _manifest3(manifest, credential_key)
            if manifest['backup_id'] != identity:
                raise ValueError('Backup manifest identity does not match its envelope')
            expected = ({'manifest.json'} | {'artifacts/' + name for name in artifacts}
                        | {'database/' + name + '.jsonl' for name in tables})
            if set(names) != expected:
                raise ValueError('Backup contains unexpected archive paths')
            for name, description in tables.items():
                if archive.getinfo('database/' + name + '.jsonl').file_size != description['bytes']:
                    raise ValueError('Backup table size does not match its descriptor')
                for _ in iter_table_rows(archive, name, description):
                    pass
            total = 0
            for name, expected in artifacts.items():
                with archive.open('artifacts/' + name) as source:
                    actual, size = _copy(source, None)
                if actual != expected:
                    raise ValueError('Artifact checksum mismatch')
                total += size
            summary = {'format': 3, 'backup_id': identity, 'created_at': manifest['created_at'],
                       'tables': {name: description['rows'] for name, description in tables.items()},
                       'artifacts': {'count': len(artifacts), 'bytes': total}}
            return manifest, tables, artifacts, summary
    except (BadZipFile, KeyError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Backup archive is invalid') from exc


def inspect_backup_file(path, key, credential_key):
    if not is_streamed_backup(path):
        if Path(path).stat().st_size > 2 * 1_000_000_000:
            raise ValueError('Legacy backup exceeds supported encrypted size')
        return inspect_backup(Path(path).read_bytes(), key, credential_key)
    with decrypted_zip(path, key) as (plain, identity):
        return _validate_zip(plain, identity, credential_key)[3]


def restore_backup_file(database, artifact_root, path, key, credential_key):
    if not is_streamed_backup(path):
        if Path(path).stat().st_size > 2 * 1_000_000_000:
            raise ValueError('Legacy backup exceeds supported encrypted size')
        return restore_backup(database, artifact_root, Path(path).read_bytes(), key, credential_key)
    with decrypted_zip(path, key) as (plain, identity):
        manifest, tables, artifacts, _ = _validate_zip(plain, identity, credential_key)
        root = Path(artifact_root)
        parent = root if root.exists() else root.parent
        while not parent.exists():
            parent = parent.parent
        with ZipFile(plain) as archive:
            _space(parent, sum(archive.getinfo('artifacts/' + name).file_size for name in artifacts))
            rows = {name: iter_table_rows(archive, name, description) for name, description in tables.items()}
            return restore_validated_archive(database, artifact_root, archive, manifest, rows, artifacts)


def receipt_path(path):
    return Path(str(path) + '.verified')


def create_backup_file(database, artifact_root, path, key, credential_key):
    target = Path(path)
    if target.parent.resolve().is_relative_to(Path(artifact_root).resolve()):
        raise ValueError('Backup output must be outside the artifact tree')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or receipt_path(target).exists():
        raise FileExistsError(str(target))
    _application_fernet(credential_key, 'ENCRYPTION_KEY')
    cipher = _fernet(key, 'BACKUP_KEY')
    identity = uuid4()
    with tempfile.TemporaryDirectory(prefix='.jazzb-backup-', dir=target.parent) as temporary:
        plain, encrypted = Path(temporary) / 'payload.zip', Path(temporary) / 'payload.encrypted'
        _build_zip(database, artifact_root, plain, identity, credential_key)
        _encrypt(plain, encrypted, key, identity.bytes)
        summary = inspect_backup_file(encrypted, key, credential_key)
        digest, size = _file_hash(encrypted)
        receipt = {'format': 1, 'archive': target.name, 'bytes': size, 'sha256': digest,
                   'credential_key_sha256': _fingerprint(credential_key), 'summary': summary}
        # Publish the receipt first: an archive is never visible to health checks
        # until its complete stored bytes have passed independent validation.
        written_receipt = False
        try:
            write_backup(receipt_path(target), cipher.encrypt(json.dumps(receipt).encode()))
            written_receipt = True
            os.link(encrypted, target)
            _sync_directory(target.parent)
        except Exception:
            if written_receipt:
                receipt_path(target).unlink(missing_ok=True)
            raise
    return summary


def inspect_verified_file(path, key, credential_key):
    """Cheap health probe: signed full-verification receipt plus streamed ciphertext hash."""
    path = Path(path)
    receipt_file = receipt_path(path)
    if receipt_file.is_symlink() or not receipt_file.is_file() or receipt_file.stat().st_size > 1024 * 1024:
        raise ValueError('Verified backup receipt is unavailable')
    receipt = json.loads(_fernet(key, 'BACKUP_KEY').decrypt(receipt_file.read_bytes()),
                         object_pairs_hook=_manifest_object_pairs)
    if (not isinstance(receipt, dict) or receipt.get('format') != 1 or receipt.get('archive') != path.name
            or receipt.get('credential_key_sha256') != _fingerprint(credential_key)):
        raise ValueError('Verified backup receipt does not match the archive or credential key')
    digest, size = _file_hash(path)
    if receipt.get('sha256') != digest or receipt.get('bytes') != size:
        raise ValueError('Verified backup ciphertext checksum mismatch')
    summary = receipt.get('summary', {})
    with path.open('rb') as source:
        identity = _header(source, _fernet(key, 'BACKUP_KEY')).hex()
    if not isinstance(summary, dict) or summary.get('format') != 3 or summary.get('backup_id') != identity:
        raise ValueError('Verified backup identity mismatch')
    return summary


def mirror_backup_file(path, mirror_directory, key, credential_key):
    if mirror_directory is None or not str(mirror_directory).strip():
        return None
    source, mirror = Path(path), Path(mirror_directory)
    if mirror.is_symlink() or not mirror.is_dir() or mirror.resolve() == source.parent.resolve():
        raise ValueError('Configured backup mirror must be a separate existing directory')
    destination = mirror / source.name
    if destination.exists() or receipt_path(destination).exists():
        raise FileExistsError(str(destination))
    inspect_verified_file(source, key, credential_key)
    signed_receipt = receipt_path(source).read_bytes()
    receipt = json.loads(_fernet(key, 'BACKUP_KEY').decrypt(signed_receipt))
    expected, size = receipt['sha256'], receipt['bytes']
    with tempfile.TemporaryDirectory(prefix='.jazzb-mirror-', dir=mirror) as temporary:
        staged = Path(temporary) / 'archive'
        with source.open('rb') as reader, staged.open('xb') as writer:
            os.chmod(staged, 0o600)
            observed = _copy(reader, writer, limit=MAX_RESTORE_BYTES * 2)
            writer.flush()
            os.fsync(writer.fileno())
        if observed != (expected, size) or _file_hash(staged) != (expected, size) or _file_hash(source) != (expected, size):
            raise ValueError('Backup mirror checksum mismatch')
        write_backup(receipt_path(destination), signed_receipt)
        try:
            os.link(staged, destination)
            _sync_directory(mirror)
        except Exception:
            receipt_path(destination).unlink(missing_ok=True)
            raise
    return destination
