"""Periodic encrypted backups for the external-PostgreSQL deployment.

Never logs database exceptions or credential values. Archives are kept until
an explicit retention/off-host policy is configured; no automatic deletion.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.db import engine
from scripts.backup import create_backup, inspect_backup, write_backup


def interval_seconds(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or not 60 <= interval <= 604800:
        raise ValueError("Backup interval must be between one minute and one week")
    return interval


def capture(database, artifacts, directory, backup_key, credential_key):
    if backup_key == credential_key:
        raise ValueError("Backup and application keys must be independent")
    payload = create_backup(database, artifacts, backup_key, credential_key)
    summary = inspect_backup(payload, backup_key, credential_key)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(directory) / f"forgeseo-{stamp}-{uuid4().hex}.forge"
    write_backup(path, payload)
    # Validate the actual stored bytes, not only the in-memory archive.
    if inspect_backup(path.read_bytes(), backup_key, credential_key) != summary:
        raise RuntimeError("Stored backup verification failed")
    return {"archive": path.name, "backup_id": summary["backup_id"],
            "tables": len(summary["tables"]), "artifacts": summary["artifacts"]["count"],
            "off_host_copy": "not_configured"}


def main() -> int:
    try:
        interval = interval_seconds(os.environ.get("BACKUP_INTERVAL_SECONDS", "86400"))
        while True:
            result = capture(engine, settings.ARTIFACT_ROOT, "/srv/backups",
                             os.environ.get("BACKUP_KEY", ""), settings.ENCRYPTION_KEY)
            print(json.dumps(result), flush=True)
            time.sleep(interval)
    except Exception as error:
        print(f"Encrypted backup failed ({type(error).__name__}); inspect private configuration. Values suppressed.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
