"""Restore into the isolated drill database and verify recovered data and keys.

This command refuses production hosts/databases, overridden libpq parameters,
nonempty targets, and archives outside the read-only backup mount. It starts
no workers and contacts no websites or providers.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, func, inspect, select
from sqlalchemy.engine import make_url

from app.connectors.security import decrypt_credentials
from app.models import Base, Connection


def validate_target(environ):
    if environ.get("RESTORE_DRILL") != "true" or environ.get("GLOBAL_PAUSE") != "true":
        raise ValueError("An explicitly paused restore drill is required")
    url = make_url(environ.get("DATABASE_URL", ""))
    if (url.drivername != "postgresql+psycopg" or url.host != "restore-db"
            or url.port != 5432 or url.username != "restore" or url.database != "forgeseo_restore_drill"
            or url.query or not re.fullmatch(r"[A-Za-z0-9_-]{32,}", url.password or "")):
        raise ValueError("Only the isolated restore-db target is allowed")
    name = environ.get("BACKUP_ARCHIVE", "")
    if not re.fullmatch(r"forgeseo-[A-Za-z0-9_-]+\.forge", name):
        raise ValueError("Select an encrypted archive filename, not a path")
    if environ.get("ARTIFACT_ROOT") != "/srv/restore-artifacts":
        raise ValueError("Only the isolated artifact mount is allowed")
    return name


def normalized_rows(table, rows, *, restored=False):
    result = []
    for original in rows:
        row = dict(original)
        if not restored:
            if table.name == "sites":
                row["paused"] = True
            if table.name == "jobs" and row.get("status") in {"queued", "retry", "running"}:
                row["status"] = "needs_reconciliation"
            if table.name == "heartbeats" and row.get("name") == "platform_controls":
                row["details"] = {"global_pause": True}
        for column in table.columns:
            if isinstance(column.type, DateTime) and row.get(column.name):
                value = row[column.name]
                if isinstance(value, str):
                    value = datetime.fromisoformat(value)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                row[column.name] = value.astimezone(timezone.utc).isoformat()
        result.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    return sorted(result)


def verify_recovery(database, artifact_root, payload, backup_key, credential_key):
    from scripts.backup import _validate_archive, create_backup
    _, source, source_tables, source_artifacts, _ = _validate_archive(payload, backup_key, credential_key)
    recovered = create_backup(database, artifact_root, backup_key, credential_key)
    _, _, actual_tables, actual_artifacts, _ = _validate_archive(recovered, backup_key, credential_key)
    if set(source_tables) != set(actual_tables) or source_artifacts != actual_artifacts:
        raise RuntimeError("Recovered schema or artifact hashes do not match")
    for table in Base.metadata.sorted_tables:
        if table.name == "sessions":
            continue
        if normalized_rows(table, source_tables.get(table.name, [])) != normalized_rows(table, actual_tables.get(table.name, []), restored=True):
            raise RuntimeError("Recovered database rows do not match")
    with database.connect() as connection:
        if connection.scalar(select(func.count()).select_from(Base.metadata.tables["sessions"])):
            raise RuntimeError("Browser sessions must not survive recovery")
        credentials = list(connection.scalars(select(Connection.encrypted_credentials)))
        for ciphertext in credentials:
            decrypt_credentials(ciphertext, credential_key)
    return {"backup_id": source["backup_id"], "tables_verified": len(source_tables),
            "artifacts_verified": len(source_artifacts), "credentials_decrypted": len(credentials),
            "automation": "paused", "browser_sessions": "invalidated", "external_requests": 0}


def main() -> int:
    try:
        name = validate_target(os.environ)
        from app.config import settings
        from app.db import engine
        from scripts.backup import _validate_archive, restore_backup
        # A drill always starts with a fresh database; even prior drill schemas
        # are rejected instead of dropped or overwritten.
        if inspect(engine).get_table_names():
            raise ValueError("Drill database is not fresh; use a new project")
        path = Path("/srv/backups") / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Encrypted archive is unavailable")
        payload = path.read_bytes()
        key = os.environ.get("BACKUP_KEY", "")
        _validate_archive(payload, key, settings.ENCRYPTION_KEY)
        migration = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                                   capture_output=True, text=True, timeout=120)
        if migration.returncode:
            raise RuntimeError("Drill schema migration failed")
        restore_backup(engine, settings.ARTIFACT_ROOT, payload, key, settings.ENCRYPTION_KEY)
        print(json.dumps(verify_recovery(engine, settings.ARTIFACT_ROOT, payload, key, settings.ENCRYPTION_KEY)))
        return 0
    except Exception as error:
        print(f"Restore drill failed ({type(error).__name__}); values suppressed. Retain isolated drill resources for inspection.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
