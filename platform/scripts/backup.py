"""Encrypted, authenticated logical backups; restore ONLY into an empty database.

Run: python -m scripts.backup create backup.forge
     python -m scripts.backup inspect backup.forge
     python -m scripts.backup restore backup.forge --confirm-empty-target
Requires BACKUP_KEY (Fernet key) and the original ENCRYPTION_KEY in environment.
Stop scheduling/writes before capture. This tool refuses nonempty restore targets.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import tempfile
from hmac import compare_digest
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from cryptography.fernet import Fernet
from sqlalchemy import DateTime, func, select

from app.config import settings
from app.connectors.security import _fernet_key
from app.db import engine
from app.models import Base


MAX_RESTORE_BYTES = 1_000_000_000
MAX_ARCHIVE_ENTRIES = 100_000


def _fernet(key, label):
    """Validate a Fernet key and return a cipher without leaking its value."""

    if isinstance(key, str):
        raw = key.encode("ascii", "strict")
    elif isinstance(key, bytes):
        raw = key
    else:
        raise ValueError(f"{label} must be a valid Fernet key")
    try:
        return Fernet(raw)
    except (TypeError, UnicodeError, ValueError):
        raise ValueError(f"{label} must be a valid Fernet key") from None


def _application_fernet(key, label):
    """Use the application's established encryption-key derivation contract."""

    try:
        return Fernet(_fernet_key(key))
    except (TypeError, UnicodeError, ValueError):
        raise ValueError(f"{label} must be a valid application encryption key") from None


def _fingerprint(key):
    if isinstance(key, str):
        raw = key.encode("utf-8")
    elif isinstance(key, bytes):
        raw = key
    else:
        raise ValueError("The original credential key is required")
    return hashlib.sha256(raw).hexdigest()


def create_backup(database,artifact_root,key,credential_key):
    cipher = _fernet(key, 'BACKUP_KEY')
    _application_fernet(credential_key, 'ENCRYPTION_KEY')
    stream = io.BytesIO()
    manifest = {
        'format': 2,
        'backup_id': uuid4().hex,
        'created_at': datetime.now().astimezone().isoformat(),
        'credential_key_sha256': _fingerprint(credential_key),
        'tables': {},
        'artifacts': {},
    }
    with database.connect() as connection:
        if database.dialect.name == 'postgresql':
            connection = connection.execution_options(isolation_level='REPEATABLE READ')
        with connection.begin(),ZipFile(stream,'w',ZIP_DEFLATED) as archive:
            for table in Base.metadata.sorted_tables:
                if table.name == 'sessions':
                    continue  # Restored backups never revive browser sessions.
                rows = [dict(row) for row in connection.execute(select(table)).mappings()]
                manifest['tables'][table.name] = rows
            raw_root = Path(artifact_root)
            if raw_root.is_symlink():
                raise ValueError('Artifact root symlinks cannot be backed up')
            root = raw_root.resolve()
            if root.exists():
                for path in root.rglob('*'):
                    if path.is_symlink():
                        raise ValueError('Artifact symlinks cannot be backed up')
                    if not path.is_file():
                        continue
                    name = path.relative_to(root).as_posix()
                    body = path.read_bytes()
                    manifest['artifacts'][name] = hashlib.sha256(body).hexdigest()
                    archive.writestr('artifacts/'+name,body)
            archive.writestr('manifest.json',json.dumps(manifest,default=lambda value:value.isoformat()))
    return cipher.encrypt(stream.getvalue())


def write_backup(path, payload):
    """Write an encrypted archive atomically and refuse accidental overwrite."""

    target = Path(path)
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise ValueError('Backup payload must be non-empty bytes')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(str(target))
    temporary = target.with_name(f'.{target.name}.{uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        # A hard link promotes the fully written file without replacing an
        # archive that appeared after the existence check.  The temporary
        # file stays in the destination directory, so the link is atomic on
        # the same filesystem.
        if target.exists():
            raise FileExistsError(str(target))
        os.link(temporary, target)
        temporary.unlink()
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def mirror_backup(path, mirror_directory):
    """Atomically copy one local archive to an operator-mounted mirror.

    A blank mirror directory keeps the existing local-only behavior.  A
    configured mirror must already be an accessible, non-symlink directory;
    treating a missing mount as a local directory would make the copy look
    more durable than it is.  ``write_backup`` provides the same no-overwrite
    and atomic promotion guarantees for the mirror as it does locally.
    """

    if mirror_directory is None or not str(mirror_directory).strip():
        return None

    source = Path(path)
    mirror = Path(str(mirror_directory).strip())
    try:
        source_is_file = source.is_file() and not source.is_symlink()
        mirror_is_directory = mirror.is_dir() and not mirror.is_symlink()
        same_directory = mirror.resolve() == source.parent.resolve()
    except OSError:
        raise RuntimeError("Configured backup mirror is unavailable") from None
    if not source_is_file:
        raise RuntimeError("Local backup archive is unavailable")
    if not mirror_is_directory:
        raise RuntimeError("Configured backup mirror is unavailable")
    if same_directory:
        raise RuntimeError("Configured backup mirror must be separate from local backups")

    try:
        payload = source.read_bytes()
    except (OSError, ValueError):
        raise RuntimeError("Local backup archive is unavailable") from None
    destination = mirror / source.name
    try:
        write_backup(destination, payload)
    except FileExistsError:
        # Preserve the writer's no-overwrite contract so a conflicting mirror
        # archive is visible to the operator rather than silently replaced.
        raise
    except (OSError, ValueError):
        raise RuntimeError("Configured backup mirror write failed") from None

    try:
        # Verify both sides still contain the exact bytes captured for the
        # mirror.  This also makes an unexpected concurrent source mutation a
        # failed mirror operation instead of a false success.
        if destination.read_bytes() != payload or source.read_bytes() != payload:
            raise RuntimeError("Configured backup mirror write failed")
    except (OSError, ValueError):
        raise RuntimeError("Configured backup mirror write failed") from None
    return destination


def _manifest_object_pairs(pairs):
    """Reject duplicate JSON keys instead of silently choosing one value."""

    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Backup manifest contains duplicate keys')
        result[key] = value
    return result


def _validate_artifact_name(name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('Unsafe artifact path')
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or not path.parts
        or any(part in ('', '.', '..') for part in path.parts)
        or '\\' in name
        or ':' in name
    ):
        raise ValueError('Unsafe artifact path')
    return path


def _validate_manifest(manifest, credential_key):
    version = manifest.get('format') if isinstance(manifest, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
        raise ValueError('Unsupported backup version')
    if (
        not isinstance(manifest.get('backup_id'), str)
        or not manifest['backup_id']
        or not isinstance(manifest.get('created_at'), str)
        or not manifest['created_at']
    ):
        raise ValueError('Backup manifest is invalid')

    try:
        credential_fingerprint = _fingerprint(credential_key)
    except ValueError:
        raise ValueError('Original credential key required; backup does not contain a substitute') from None
    stored_fingerprint = manifest.get('credential_key_sha256')
    if (
        not isinstance(stored_fingerprint, str)
        or len(stored_fingerprint) != hashlib.sha256().digest_size * 2
        or any(character not in '0123456789abcdefABCDEF' for character in stored_fingerprint)
        or not compare_digest(stored_fingerprint, credential_fingerprint)
    ):
        raise ValueError('Original credential key required; backup does not contain a substitute')
    _application_fernet(credential_key, 'ENCRYPTION_KEY')

    tables = manifest.get('tables')
    artifact_manifest = manifest.get('artifacts')
    if not isinstance(tables, dict) or not isinstance(artifact_manifest, dict):
        raise ValueError('Backup manifest is invalid')
    for table_name, rows in tables.items():
        if not isinstance(table_name, str) or not table_name or not isinstance(rows, list):
            raise ValueError('Backup table data is invalid')
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('Backup table data is invalid')

    file_paths = set()
    path_prefixes = set()
    for name, expected in artifact_manifest.items():
        path = _validate_artifact_name(name)
        if (
            not isinstance(expected, str)
            or len(expected) != hashlib.sha256().digest_size * 2
            or any(character not in '0123456789abcdefABCDEF' for character in expected)
        ):
            raise ValueError('Backup artifact manifest is invalid')
        parts = tuple(path.parts)
        if parts in path_prefixes:
            raise ValueError('Duplicate artifact paths')
        if any(parts[:index] in file_paths for index in range(1, len(parts))):
            raise ValueError('Unsafe artifact path')
        file_paths.add(parts)
        path_prefixes.update(parts[:index] for index in range(1, len(parts) + 1))

    return tables, artifact_manifest


def _validate_archive(payload, key, credential_key):
    """Decrypt and fully validate an archive without touching application state."""

    cipher = _fernet(key, 'BACKUP_KEY')
    content = cipher.decrypt(payload)
    try:
        archive = ZipFile(io.BytesIO(content))
    except (BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise ValueError('Backup archive is invalid') from exc

    try:
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError('Backup contains too many archive entries')
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError('Backup contains duplicate archive paths')
            if any(info.file_size < 0 for info in infos):
                raise ValueError('Backup contains invalid archive sizes')
            if sum(info.file_size for info in infos) > MAX_RESTORE_BYTES:
                raise ValueError('Backup exceeds supported restore size')

            try:
                raw_manifest = archive.read('manifest.json')
                manifest = json.loads(raw_manifest, object_pairs_hook=_manifest_object_pairs)
            except (KeyError, BadZipFile, EOFError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise ValueError('Backup manifest is invalid') from None

            try:
                tables, artifact_manifest = _validate_manifest(manifest, credential_key)
            except ValueError:
                raise
            expected_names = {'manifest.json'} | {f'artifacts/{name}' for name in artifact_manifest}
            if set(names) != expected_names:
                raise ValueError('Backup contains unexpected archive paths')

            artifact_bytes = 0
            for name, expected in artifact_manifest.items():
                try:
                    body = archive.read(f'artifacts/{name}')
                except (KeyError, BadZipFile, EOFError, OSError, RuntimeError):
                    raise ValueError('Backup archive is invalid') from None
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError('Artifact checksum mismatch')
                artifact_bytes += len(body)
            return content, manifest, tables, artifact_manifest, artifact_bytes
    except ValueError:
        raise
    except (BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise ValueError('Backup archive is invalid') from exc


def inspect_backup(payload, key, credential_key):
    """Return a JSON-safe archive summary without opening a database or root."""

    _, manifest, tables, artifact_manifest, artifact_bytes = _validate_archive(
        payload,
        key,
        credential_key,
    )
    return {
        'format': manifest['format'],
        'backup_id': manifest['backup_id'],
        'created_at': manifest['created_at'],
        'tables': {name: len(rows) for name, rows in tables.items()},
        'artifacts': {
            'count': len(artifact_manifest),
            'bytes': artifact_bytes,
        },
    }


def restore_backup(database,artifact_root,payload,key,credential_key):
    content, manifest, tables, artifact_manifest, _ = _validate_archive(
        payload,
        key,
        credential_key,
    )
    raw_root = Path(artifact_root)
    if raw_root.is_symlink():
        raise ValueError('Restore artifact directory cannot be a symlink')
    root = raw_root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Restore artifact directory must be empty')
    root.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(io.BytesIO(content)) as archive:
        assets = {}
        root_created = False
        if not root.exists():
            root.mkdir(parents=True)
            root_created = True
        # Stage inside the destination volume. Docker volumes can be mounted
        # separately from the container layer, so a staging directory under
        # root.parent would make os.replace fail with EXDEV.
        staging = Path(tempfile.mkdtemp(prefix=f'.{root.name}.restore-', dir=str(root)))
        created_files = []
        created_dirs = []
        try:
            for name,expected in artifact_manifest.items():
                path = _validate_artifact_name(name)
                body = archive.read('artifacts/'+name)
                if hashlib.sha256(body).hexdigest()!=expected:
                    raise ValueError('Artifact checksum mismatch')
                staged = staging.joinpath(*path.parts)
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(body)
                assets[name] = staged

            with database.begin() as connection:
                for table in Base.metadata.sorted_tables:
                    if connection.scalar(select(func.count()).select_from(table)):
                        raise ValueError('Restore target database is not empty')
                for table in Base.metadata.sorted_tables:
                    rows = tables.get(table.name,[])
                    if not isinstance(rows, list):
                        raise ValueError('Backup table data is invalid')
                    for row in rows:
                        if not isinstance(row, dict):
                            raise ValueError('Backup table data is invalid')
                        for column in table.columns:
                            if isinstance(column.type,DateTime) and row.get(column.name):
                                row[column.name] = datetime.fromisoformat(row[column.name])
                        if table.name=='sites':
                            row['paused']=True
                        if table.name=='jobs' and row['status'] in ('queued','retry','running'):
                            row['status']='needs_reconciliation'
                        if table.name=='heartbeats' and row['name']=='platform_controls':
                            row['details']={'global_pause':True}
                    if rows:
                        connection.execute(table.insert(),rows)
                if database.dialect.name=='postgresql':
                    from sqlalchemy import text
                    connection.execute(text("SELECT setval(pg_get_serial_sequence('events','id'), COALESCE((SELECT MAX(id) FROM events),1), EXISTS(SELECT 1 FROM events))"))

                for name, staged in assets.items():
                    path = PurePosixPath(name)
                    target = root.joinpath(*path.parts)
                    if target.exists():
                        raise ValueError('Restore artifact directory must be empty')
                    parent = target.parent
                    missing = []
                    while not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.extend(reversed(missing))
                    os.replace(staged, target)
                    created_files.append(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            for target in reversed(created_files):
                target.unlink(missing_ok=True)
            for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if root_created:
                try:
                    root.rmdir()
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {'tables':len(manifest['tables']),'artifacts':len(assets),'automation':'paused','browser_sessions':'invalidated'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['create','inspect','restore'])
    parser.add_argument('path',type=Path)
    parser.add_argument('--confirm-empty-target',action='store_true')
    args=parser.parse_args()
    key=os.environ.get('BACKUP_KEY','')
    if args.operation=='create':
        body=create_backup(engine,settings.ARTIFACT_ROOT,key,settings.ENCRYPTION_KEY)
        written = write_backup(args.path, body)
        mirrored = mirror_backup(written, os.environ.get('BACKUP_MIRROR_DIRECTORY'))
        if mirrored is None:
            print('Encrypted backup created; retain BACKUP_KEY and ENCRYPTION_KEY separately.')
        else:
            print('Encrypted backup created and mirrored; retain BACKUP_KEY and ENCRYPTION_KEY separately.')
    elif args.operation=='inspect':
        credential_key=os.environ.get('ENCRYPTION_KEY', settings.ENCRYPTION_KEY)
        if not key:
            parser.error('BACKUP_KEY is required for inspect')
        if not credential_key:
            parser.error('ENCRYPTION_KEY is required for inspect')
        print(json.dumps(inspect_backup(args.path.read_bytes(), key, credential_key)))
    else:
        if not args.confirm_empty_target:
            parser.error('--confirm-empty-target is required; run migrations on an empty destination first')
        print(json.dumps(restore_backup(engine,settings.ARTIFACT_ROOT,args.path.read_bytes(),key,settings.ENCRYPTION_KEY)))


if __name__=='__main__':
    main()
