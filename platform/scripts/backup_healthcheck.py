"""Freshness and integrity probe for the encrypted backup service.

The backup container must not report healthy merely because an old archive is
still present. This probe selects the newest local archive, enforces a
freshness limit, and fully validates its authenticated contents without
opening the application database or artifact directory.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import os
import time
from pathlib import Path

from cryptography.fernet import InvalidToken

from scripts.backup import inspect_backup


DEFAULT_MAX_AGE_SECONDS = 172800


def _inspect_mirror(latest: Path, mirror_directory: str | Path) -> dict:
    """Require an exact copy of the newest local archive in the mirror."""

    mirror = Path(str(mirror_directory).strip())
    try:
        mirror_is_directory = mirror.is_dir() and not mirror.is_symlink()
        same_directory = mirror.resolve() == latest.parent.resolve()
    except OSError:
        raise RuntimeError("Configured backup mirror is unavailable") from None
    if not mirror_is_directory:
        raise RuntimeError("Configured backup mirror is unavailable")
    if same_directory:
        raise RuntimeError("Configured backup mirror must be separate from local backups")

    mirrored = mirror / latest.name
    try:
        mirrored_is_file = mirrored.is_file() and not mirrored.is_symlink()
        exact = mirrored_is_file and latest.stat().st_size == mirrored.stat().st_size and filecmp.cmp(
            latest,
            mirrored,
            shallow=False,
        )
    except (FileNotFoundError, OSError):
        raise RuntimeError("Configured backup mirror is unavailable") from None
    if not mirrored_is_file:
        raise RuntimeError("Latest encrypted backup is missing from configured mirror")
    if not exact:
        raise RuntimeError("Configured backup mirror does not match the latest local archive")
    return {"archive": mirrored.name}


def inspect_latest_backup(
    directory: str | Path,
    backup_key: str,
    credential_key: str,
    max_age_seconds: int | float,
    *,
    mirror_directory: str | Path | None = None,
    now: float | None = None,
) -> dict:
    """Return a validated summary or raise when the latest archive is unsafe."""

    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Backup directory is unavailable")
    try:
        max_age = float(max_age_seconds)
    except (TypeError, ValueError):
        raise RuntimeError("Backup freshness limit is invalid") from None
    if not math.isfinite(max_age) or max_age <= 0:
        raise RuntimeError("Backup freshness limit must be positive")

    archives = [
        path
        for path in root.glob("forgeseo-*.forge")
        if path.is_file() and not path.is_symlink()
    ]
    if not archives:
        raise RuntimeError("No encrypted backup archive is available")
    try:
        latest = max(archives, key=lambda path: path.stat().st_mtime_ns)
        modified_at = latest.stat().st_mtime
        current_time = time.time() if now is None else float(now)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        raise RuntimeError("Backup archive metadata is unavailable") from None
    if not math.isfinite(modified_at) or not math.isfinite(current_time):
        raise RuntimeError("Backup archive metadata is invalid")
    if modified_at > current_time:
        raise RuntimeError("Latest encrypted backup timestamp is in the future")
    age = current_time - modified_at
    if age > max_age:
        raise RuntimeError("Latest encrypted backup is stale")

    try:
        summary = inspect_backup(latest.read_bytes(), backup_key, credential_key)
    except (FileNotFoundError, OSError):
        raise RuntimeError("Latest encrypted backup is unavailable") from None
    summary.update({"archive": latest.name, "age_seconds": round(age, 3)})
    if mirror_directory is not None and str(mirror_directory).strip():
        summary["mirror"] = _inspect_mirror(latest, mirror_directory)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        default=os.environ.get("BACKUP_DIRECTORY", "/srv/backups"),
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=float(
            os.environ.get("BACKUP_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS)
        ),
    )
    args = parser.parse_args()
    backup_key = os.environ.get("BACKUP_KEY", "")
    credential_key = os.environ.get("ENCRYPTION_KEY", "")
    try:
        summary = inspect_latest_backup(
            args.directory,
            backup_key,
            credential_key,
            args.max_age_seconds,
            mirror_directory=os.environ.get("BACKUP_MIRROR_DIRECTORY"),
        )
    except (InvalidToken, RuntimeError, ValueError):
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
