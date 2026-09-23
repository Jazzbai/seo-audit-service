"""Portable, read-only legacy transfer bundles.

The legacy database is an input only. Export uses a read-only transaction for
PostgreSQL, removes credential-shaped fields before anything is written to a
bundle, and writes the bundle atomically. Bundle imports are dry-run by
default at the CLI; ``--apply`` is required and the target site must remain
paused. Imported history never grants authority to write to a site.

Examples::

    python -m scripts.import_legacy --source-site 1 \
        --export-bundle ./transfer/auto1stopshop.json
    python -m scripts.import_legacy --bundle ./transfer/auto1stopshop.json \
        --target-site TARGET_ID
    python -m scripts.import_legacy --bundle ./transfer/auto1stopshop.json \
        --target-site TARGET_ID --apply
    python -m scripts.import_legacy --rollback-import CHECKSUM \
        --target-site TARGET_ID --apply

The direct ``--source-site ... --target-site ...`` mode remains available for
compatibility with the original importer. It still never writes to the
legacy database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import SessionLocal
from app.models import Candidate, Finding, Measurement, Page, Site
from app.operations import event, now


TABLES = (
    "observations",
    "evidence_artifacts",
    "findings",
    "opportunities",
    "decisions",
    "proposals",
    "approvals",
    "executions",
    "verifications",
    "audit_entries",
    "agent_runs",
)

BUNDLE_FORMAT = "forgeseo-legacy-transfer"
BUNDLE_VERSION = 1

_SENSITIVE_LEGACY_KEY_PARTS = (
    "credential",
    "password",
    "secret",
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "consumer_key",
    "consumer_secret",
    "webhook_secret",
    "bearer_token",
    "private_key",
    "authorization",
)


def _canonical_json(value) -> bytes:
    """Return the stable byte representation used for bundle checksums."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _normal_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


_NORMALIZED_SENSITIVE_PARTS = tuple(_normal_key(part) for part in _SENSITIVE_LEGACY_KEY_PARTS)


def _is_sensitive_key(key: object) -> bool:
    normalized = _normal_key(key)
    return any(part in normalized for part in _NORMALIZED_SENSITIVE_PARTS)


def redact_legacy(value):
    """Remove credential-shaped fields before legacy evidence is retained.

    This intentionally removes the whole field rather than attempting to
    preserve a masked value. It covers snake_case, camelCase, and mixed
    separator spellings found in older evidence payloads.
    """

    if isinstance(value, dict):
        output = {}
        for key, nested in value.items():
            if _is_sensitive_key(key):
                continue
            output[key] = redact_legacy(nested)
        return output
    if isinstance(value, list):
        return [redact_legacy(item) for item in value]
    if isinstance(value, tuple):
        return [redact_legacy(item) for item in value]
    return value


def _canonical_origin(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Site origin is missing from the legacy transfer")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Site origin is invalid") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Site origin must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Site origin must not contain credentials, query, or fragment")
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    authority = host if port in (None, default_port) else f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.casefold()}://{authority}{path}"


def _origin_authority(value: object) -> str:
    canonical = _canonical_origin(value)
    parsed = urlsplit(canonical)
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_payload(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Legacy transfer payload must be a JSON object")
    site = data.get("site")
    if not isinstance(site, dict):
        raise ValueError("Legacy transfer is missing its site record")
    if site.get("id") is None:
        raise ValueError("Legacy transfer site record is missing its ID")
    _canonical_origin(site.get("origin"))
    for table_name in TABLES:
        value = data.get(table_name, [])
        if not isinstance(value, list):
            raise ValueError(f"Legacy transfer table {table_name} must be a list")
    return data


def _payload_checksum(data: dict) -> str:
    return hashlib.sha256(_canonical_json(data)).hexdigest()


def build_bundle(data: dict) -> dict:
    """Build a versioned, redacted bundle without writing it to disk."""

    payload = redact_legacy(data)
    _validate_payload(payload)
    payload_bytes = _canonical_json(payload)
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "source_site_id": str(payload["site"]["id"]),
        "source_origin": _canonical_origin(payload["site"]["origin"]),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "payload_bytes": len(payload_bytes),
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "redacted": True,
    }
    return {"manifest": manifest, "payload": payload}


def _atomic_write(path: Path, content: bytes) -> None:
    """Write a file with a same-directory temporary and atomic replacement."""

    path = Path(path)
    if path.exists() and path.is_dir():
        raise ValueError("Transfer bundle destination is a directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Directory fsync is useful on POSIX. Windows does not support
        # opening a directory this way, so failure here is harmless after the
        # atomic replace has completed.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_bundle(data: dict, destination: str | Path) -> dict:
    """Redact, checksum, and atomically write a transfer bundle."""

    bundle = build_bundle(data)
    _atomic_write(Path(destination), _canonical_json(bundle))
    return bundle["manifest"]


def load_bundle(path: str | Path) -> tuple[dict, dict, bytes]:
    """Load and validate a bundle, returning payload, manifest, and bytes.

    The redaction check is repeated at import time. A bundle cannot merely
    claim to be redacted; a credential-shaped key makes it invalid.
    """

    bundle_path = Path(path)
    try:
        raw = bundle_path.read_bytes()
        bundle = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Legacy transfer bundle was not found") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Legacy transfer bundle is not valid UTF-8 JSON") from exc
    if not isinstance(bundle, dict):
        raise ValueError("Legacy transfer bundle must be a JSON object")
    manifest = bundle.get("manifest")
    payload = bundle.get("payload")
    if not isinstance(manifest, dict) or not isinstance(payload, dict):
        raise ValueError("Legacy transfer bundle must contain manifest and payload objects")
    if manifest.get("format") != BUNDLE_FORMAT:
        raise ValueError("Unsupported legacy transfer bundle format")
    if manifest.get("version") != BUNDLE_VERSION:
        raise ValueError("Unsupported legacy transfer bundle version")
    if manifest.get("redacted") is not True:
        raise ValueError("Legacy transfer bundle is not marked as redacted")
    expected = manifest.get("payload_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("Legacy transfer bundle has no valid payload checksum")
    if any(character not in "0123456789abcdef" for character in expected.casefold()):
        raise ValueError("Legacy transfer bundle has no valid payload checksum")
    redacted_payload = redact_legacy(payload)
    if redacted_payload != payload:
        raise ValueError("Legacy transfer bundle contains credential-shaped fields")
    _validate_payload(payload)
    actual = _payload_checksum(payload)
    if actual != expected.casefold():
        raise ValueError("Legacy transfer bundle checksum mismatch")
    source_origin = _canonical_origin(payload["site"]["origin"])
    if manifest.get("source_origin") != source_origin:
        raise ValueError("Legacy transfer bundle source origin does not match its payload")
    if str(manifest.get("source_site_id")) != str(payload["site"]["id"]):
        raise ValueError("Legacy transfer bundle source site does not match its payload")
    payload_bytes = _canonical_json(payload)
    if manifest.get("payload_bytes") != len(payload_bytes):
        raise ValueError("Legacy transfer bundle payload length does not match its manifest")
    return payload, manifest, raw


def export_source(database, source_site):
    """Read a legacy site and return its redacted transfer payload."""

    names = set(inspect(database).get_table_names())
    metadata = MetaData()
    output = {}
    with database.connect() as connection:
        if database.dialect.name == "postgresql":
            # This is deliberately the only transaction mode used for the
            # legacy connection. The source is never updated by this tool.
            connection.execute(text("SET TRANSACTION READ ONLY"))
        if "sites" not in names:
            raise ValueError("Legacy database is missing its sites table")
        sites = Table("sites", metadata, autoload_with=database)
        row = connection.execute(select(sites).where(sites.c.id == source_site)).mappings().first()
        if row is None:
            raise ValueError("Legacy site was not found")
        output["site"] = {key: row[key] for key in ("id", "name", "origin") if key in row}
        for name in TABLES:
            if name not in names:
                continue
            table = Table(name, metadata, autoload_with=database)
            if "site_id" not in table.c:
                continue
            output[name] = [
                dict(item)
                for item in connection.execute(select(table).where(table.c.site_id == source_site)).mappings()
            ]
    return redact_legacy(json.loads(json.dumps(output, default=str)))


def export_bundle(database, source_site, destination: str | Path) -> dict:
    """Export a source site to an atomic bundle and return its manifest."""

    return write_bundle(export_source(database, source_site), destination)


def resources(value):
    """Yield explicit page-shaped resources from legacy evidence."""

    if isinstance(value, dict):
        key = value.get("resource_key")
        url = value.get("public_url") or value.get("url")
        if isinstance(key, str) and isinstance(url, str) and url.startswith("https://"):
            yield value
        elif (
            isinstance(value.get("requested_url"), str)
            and isinstance(value.get("status_code"), (int, float))
            and value["requested_url"].startswith("https://")
        ):
            # Older ForgeSEO observations did not carry a resource key; they
            # represented a crawled page directly. Convert only that explicit
            # observation shape, not arbitrary nested link dictionaries.
            observed_url = value["requested_url"]
            yield {
                "resource_key": "legacy:url:" + hashlib.sha256(observed_url.encode()).hexdigest()[:24],
                "resource_type": "discovered_page",
                "public_url": observed_url,
                "title": value.get("title", ""),
                "legacy_observation": {
                    "requested_url": observed_url,
                    "status_code": value.get("status_code"),
                    "title": value.get("title", ""),
                    "meta_description": value.get("meta_description", ""),
                    "visible_text_hash": value.get("visible_text_hash", ""),
                },
            }
        for nested in value.values():
            yield from resources(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from resources(nested)


def _same_origin(url: str, origin: str) -> bool:
    try:
        return _origin_authority(url) == _origin_authority(origin)
    except ValueError:
        return False


def _import_plan(db, site: Site, data: dict, checksum: str) -> tuple[list[dict], list[str]]:
    source_origin = data["site"]["origin"]
    pending = []
    existing_keys = []
    seen = set()
    for observation in data.get("observations", []):
        if not isinstance(observation, dict):
            continue
        for resource in resources(observation.get("payload_json", {})):
            key = resource.get("resource_key")
            if not isinstance(key, str) or key in seen:
                continue
            seen.add(key)
            resource_url = resource.get("public_url") or resource.get("url")
            if not isinstance(resource_url, str) or not _same_origin(resource_url, source_origin):
                continue
            if db.scalar(select(Page.id).where(Page.site_id == site.id, Page.resource_key == key)):
                existing_keys.append(key)
            else:
                pending.append(resource)
    return pending, existing_keys


def _base_import_report(data: dict, checksum: str, archive: str | None, pending: list[dict], existing_keys: list[str]) -> dict:
    return {
        "sha256": checksum,
        "archive": archive,
        "history_counts": {key: len(data.get(key, [])) for key in TABLES},
        "candidate_pages": len(pending) + len(existing_keys),
        "would_import_pages": len(pending),
        "already_present_pages": len(existing_keys),
        "approval_authority_imported": False,
        "credentials_imported": False,
        "requires_fresh_inventory": True,
        "evidence_bytes": "Legacy evidence references preserved; original artifact store must remain retained until separately copied and verified",
    }


def _archive_path(artifact_root: str | Path, site: Site, checksum: str) -> tuple[Path, str]:
    root = Path(artifact_root)
    relative = Path(str(site.id)) / "legacy" / f"{checksum}.json"
    return root / relative, str(relative).replace("\\", "/")


def import_history(
    db,
    site: Site,
    data: dict,
    artifact_root: str | Path,
    *,
    dry_run: bool = False,
    bundle_manifest: dict | None = None,
    archive_bytes: bytes | None = None,
):
    """Import redacted history, or return a no-write preview when requested.

    ``dry_run`` is explicit for callers; the command-line interface defaults
    to it. The legacy direct function keeps its original applying behavior
    for compatibility, while every CLI path requires ``--apply`` to mutate.
    """

    if not isinstance(data, dict):
        raise ValueError("Legacy transfer payload must be an object")
    data = redact_legacy(data)
    _validate_payload(data)
    source_origin = _canonical_origin(data["site"]["origin"])
    target_origin = _canonical_origin(site.origin)
    if source_origin != target_origin:
        raise ValueError("Source and target site origins do not match")

    checksum = _payload_checksum(data)
    if bundle_manifest is not None:
        if bundle_manifest.get("format") != BUNDLE_FORMAT or bundle_manifest.get("version") != BUNDLE_VERSION:
            raise ValueError("Unsupported legacy transfer bundle")
        if str(bundle_manifest.get("payload_sha256", "")).casefold() != checksum:
            raise ValueError("Legacy transfer bundle checksum mismatch")
    pending, existing_keys = _import_plan(db, site, data, checksum)
    archive_path, archive_relative = _archive_path(artifact_root, site, checksum)
    report = _base_import_report(data, checksum, archive_relative, pending, existing_keys)
    report["dry_run"] = bool(dry_run)
    report["target_paused"] = bool(site.paused)
    if dry_run:
        report["would_archive"] = str(archive_path)
        return report
    if not site.paused:
        raise ValueError("Target site must be paused before applying a legacy transfer")

    existing = db.scalar(
        select(Measurement).where(
            Measurement.site_id == site.id,
            Measurement.kind == "legacy_import",
            Measurement.source == checksum,
        )
    )
    if existing is not None:
        replay = dict(existing.data or {})
        replay.update({"replayed": True, "sha256": checksum, "dry_run": False})
        return replay

    if archive_bytes is None:
        archive_bytes = _canonical_json(data)
    # The payload was validated and redacted above. Bundle bytes are retained
    # with their manifest; direct mode retains a canonical redacted payload.
    _atomic_write(archive_path, archive_bytes)

    imported_page_ids = []
    imported_resource_keys = []
    for resource in pending:
        title = resource.get("title", "")
        if not isinstance(title, str):
            title = title.get("rendered", "") if isinstance(title, dict) else ""
        page = Page(
            site_id=site.id,
            resource_key=resource["resource_key"],
            url=resource.get("public_url") or resource.get("url"),
            title=title,
            resource_type=resource.get("resource_type") or resource["resource_key"].split(":")[0],
            source={
                "legacy": resource,
                "requires_fresh_inventory": True,
                "legacy_import_sha256": checksum,
                "legacy_import_created": True,
            },
            source_hash="",
            enrolled=False,
            managed=False,
        )
        db.add(page)
        db.flush()
        imported_page_ids.append(page.id)
        imported_resource_keys.append(resource["resource_key"])

    report.update(
        {
            "dry_run": False,
            "imported_pages": len(imported_page_ids),
            "imported_page_ids": imported_page_ids,
            "imported_resource_keys": imported_resource_keys,
            "archive_retained": True,
        }
    )
    db.add(Measurement(site_id=site.id, kind="legacy_import", source=checksum, data=report, observed_at=now()))
    event(db, site, "legacy_imported", "Legacy history archived; no approvals or automation permissions activated", report)
    db.commit()
    return report


def _safe_archive_for_report(artifact_root: str | Path, site: Site, report: dict) -> Path:
    relative = report.get("archive")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Legacy import has no retained evidence archive")
    root = Path(artifact_root).resolve()
    archive = (root / relative).resolve()
    try:
        archive.relative_to(root)
    except ValueError as exc:
        raise ValueError("Legacy import archive path is outside the artifact root") from exc
    if not archive.is_file():
        raise ValueError("Legacy import evidence archive is missing; rollback stopped")
    return archive


def rollback_import(
    db,
    site: Site,
    checksum: str,
    artifact_root: str | Path,
    *,
    dry_run: bool = True,
):
    """Remove only pages marked as created by one exact import checksum.

    The target must be paused for both preview and apply. The import
    measurement and archive are retained. A page with later dependent
    findings/candidates is not removed; the operation fails closed rather
    than deleting unrelated evidence.
    """

    if not isinstance(checksum, str) or len(checksum) != 64 or any(
        character not in "0123456789abcdef" for character in checksum.casefold()
    ):
        raise ValueError("Rollback requires the exact 64-character import checksum")
    checksum = checksum.casefold()
    if not site.paused:
        raise ValueError("Target site must remain paused before rolling back a legacy transfer")

    prior = db.scalar(
        select(Measurement).where(
            Measurement.site_id == site.id,
            Measurement.kind == "legacy_rollback",
            Measurement.source == checksum,
        )
    )
    if prior is not None:
        replay = dict(prior.data or {})
        replay["replayed"] = True
        return replay

    imported = db.scalar(
        select(Measurement).where(
            Measurement.site_id == site.id,
            Measurement.kind == "legacy_import",
            Measurement.source == checksum,
        )
    )
    if imported is None:
        raise ValueError("No matching legacy import was found for that checksum")
    import_report = imported.data or {}
    archive = _safe_archive_for_report(artifact_root, site, import_report)
    page_ids = import_report.get("imported_page_ids")
    if not isinstance(page_ids, list):
        raise ValueError("This legacy import has no exact page rollback metadata")

    pages = db.scalars(select(Page).where(Page.site_id == site.id, Page.id.in_(page_ids))).all() if page_ids else []
    safe_pages = []
    skipped_pages = []
    dependent_pages = []
    for page in pages:
        source = page.source if isinstance(page.source, dict) else {}
        if source.get("legacy_import_sha256") != checksum or source.get("legacy_import_created") is not True:
            skipped_pages.append(page.id)
            continue
        if (
            db.scalar(select(Finding.id).where(Finding.page_id == page.id)) is not None
            or db.scalar(select(Candidate.id).where(Candidate.page_id == page.id)) is not None
        ):
            dependent_pages.append(page.id)
            continue
        safe_pages.append(page)

    if dependent_pages:
        raise ValueError("Rollback stopped because an imported page has later findings or candidates")

    report = {
        "sha256": checksum,
        "archive": import_report.get("archive"),
        "archive_retained": archive.is_file(),
        "candidate_pages": len(page_ids),
        "would_remove_pages": len(safe_pages),
        "skipped_pages": skipped_pages,
        "dry_run": bool(dry_run),
        "approval_authority_imported": False,
        "credentials_imported": False,
    }
    if dry_run:
        return report

    for page in safe_pages:
        db.delete(page)
    report["rolled_back_pages"] = len(safe_pages)
    db.add(Measurement(site_id=site.id, kind="legacy_rollback", source=checksum, data=report, observed_at=now()))
    event(db, site, "legacy_import_rolled_back", "Pages created by one legacy import were removed; evidence archive retained", report)
    db.commit()
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-site", type=int, help="Legacy site ID for direct export compatibility")
    parser.add_argument("--target-site", help="Existing standalone ForgeSEO site ID")
    parser.add_argument("--bundle", type=Path, help="Validated legacy transfer bundle to import")
    parser.add_argument("--export-bundle", type=Path, help="Destination for an atomic legacy transfer bundle")
    parser.add_argument("--rollback-import", metavar="SHA256", help="Exact import checksum to roll back")
    parser.add_argument("--apply", action="store_true", help="Apply an import or rollback; otherwise preview only")
    return parser


def _cli_error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    modes = sum(bool(value) for value in (args.bundle, args.export_bundle, args.rollback_import))
    if modes > 1:
        return _cli_error("choose exactly one of --bundle, --export-bundle, or --rollback-import")
    if args.export_bundle is not None:
        if args.source_site is None:
            return _cli_error("--export-bundle requires --source-site")
        if args.target_site or args.apply:
            return _cli_error("bundle export does not accept --target-site or --apply")
        try:
            legacy_url = os.environ["LEGACY_DATABASE_URL"]
            source = create_engine(legacy_url)
            manifest = export_bundle(source, args.source_site, args.export_bundle)
        except KeyError:
            return _cli_error("LEGACY_DATABASE_URL is not configured")
        except SQLAlchemyError:
            return _cli_error("legacy database export failed; verify the read-only connection")
        except (OSError, ValueError):
            return _cli_error("legacy bundle export failed; verify the destination and source site")
        finally:
            if "source" in locals():
                source.dispose()
        print(json.dumps({"mode": "export", "bundle": str(args.export_bundle), "manifest": manifest}, sort_keys=True))
        return 0

    if args.rollback_import is not None:
        if not args.target_site:
            return _cli_error("--rollback-import requires --target-site")
        if args.source_site is not None or args.bundle is not None:
            return _cli_error("rollback cannot be combined with a source site or bundle")
        try:
            with SessionLocal() as db:
                site = db.get(Site, args.target_site)
                if site is None:
                    raise ValueError("Target site was not found")
                report = rollback_import(
                    db,
                    site,
                    args.rollback_import,
                    settings.ARTIFACT_ROOT,
                    dry_run=not args.apply,
                )
        except SQLAlchemyError:
            return _cli_error("legacy rollback could not be completed")
        except (OSError, ValueError):
            return _cli_error("legacy rollback stopped; verify the checksum, paused target, and retained archive")
        print(json.dumps({"mode": "rollback", **report}, sort_keys=True))
        return 0

    if not args.target_site:
        return _cli_error("import requires --target-site")
    if args.bundle is not None and args.source_site is not None:
        return _cli_error("--bundle cannot be combined with --source-site")
    try:
        bundle_manifest = None
        archive_bytes = None
        if args.bundle is not None:
            data, bundle_manifest, archive_bytes = load_bundle(args.bundle)
        else:
            if args.source_site is None:
                return _cli_error("direct import requires --source-site, or provide --bundle")
            legacy_url = os.environ["LEGACY_DATABASE_URL"]
            source = create_engine(legacy_url)
            try:
                data = export_source(source, args.source_site)
            finally:
                source.dispose()
        with SessionLocal() as db:
            site = db.get(Site, args.target_site)
            if site is None:
                raise ValueError("Target site was not found")
            report = import_history(
                db,
                site,
                data,
                settings.ARTIFACT_ROOT,
                dry_run=not args.apply,
                bundle_manifest=bundle_manifest,
                archive_bytes=archive_bytes,
            )
    except KeyError:
        return _cli_error("LEGACY_DATABASE_URL is not configured")
    except SQLAlchemyError:
        return _cli_error("legacy transfer failed; verify the database and paused target")
    except (OSError, ValueError):
        return _cli_error("legacy transfer rejected; verify the bundle, origin, and paused target")
    print(json.dumps({"mode": "import", **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
