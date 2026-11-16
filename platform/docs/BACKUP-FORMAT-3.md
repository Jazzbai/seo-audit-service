# Streaming backup and recovery

The scheduled external-PostgreSQL backup and `python -m scripts.backup create`
write v3 archives. File-based inspect/restore automatically recognize older
v1/v2 Fernet archives. Original `BACKUP_KEY` and `ENCRYPTION_KEY` values remain
required; neither is rotated or embedded in the archive by this change.

## Resource bounds and authentication

- Database tables are ordered by primary key and streamed as JSONL from one
  repeatable-read PostgreSQL snapshot. Each table has a row count, byte count,
  column list and SHA-256 in a small manifest.
- Artifact files are streamed into ZIP64 entries and hashed. Symlinks, changing
  source files, duplicate paths and traversal paths are refused.
- A disk-backed ZIP is encrypted in 8 MiB Fernet records. Every record binds its
  archive UUID, type and ordinal. The mandatory authenticated footer binds the
  record count, compressed byte count and complete ZIP checksum. Mixed archives,
  missing/reordered/duplicate records, truncation and trailing bytes are refused.
- Capture and restore share a 32 GiB total uncompressed bound, 100,000 archive
  entries, a 16 MiB index, 16 MiB per database row and 20 million database rows.
  Hitting a bound fails capture before publishing an archive. These are finite
  safety limits, not a claim of unlimited storage.
- Plaintext scratch files live in private temporary directories and are removed
  after use. Backups and receipts are mode 0600 on Linux. Archive promotion is
  atomic and refuses an existing filename. Restore checks available staging
  space, stages on the destination volume, and uses a database transaction.

Fernet's whole-message API is used only for bounded records, not a multi-GB
archive. See the [cryptography limitations](https://cryptography.io/en/latest/fernet/#limitations).
This envelope uses the existing authenticated primitive; it has not had a
separate external cryptographic audit.

## Verification and health

Creation independently decrypts and validates the complete stored archive before
publishing it. It publishes an encrypted `.forge.verified` receipt binding the
archive name, size, ciphertext SHA-256, original credential-key fingerprint and
verified summary. The archive is made visible only after this receipt exists.

Routine health checks authenticate the receipt and header, stream the full
ciphertext checksum, and enforce both file and authenticated creation-time
freshness. They do not repeatedly decompress GBs every minute. Corrupt ciphertext,
wrong keys, missing receipts, stale backups or missing configured mirrors fail
health. Explicit `inspect` and every restore still fully decrypt and validate
all table rows and artifacts; a receipt never bypasses restore validation.

Copy both `<name>.forge` and `<name>.forge.verified` to retain quick health checks.
Full inspect and restore remain possible with the archive alone. Configured
mirrors receive and verify both files without reading the entire archive into
memory. A local volume or mirror on the same host is not an off-host recovery
copy. The split-host loop retains archives until an operator defines retention;
the standalone Compose profile keeps its existing configured retention window.

## Recovery drill

Use `compose.restore-drill.yaml` with a unique project name, the deployed image
ID, the backup volume mounted read-only, original keys and an independent drill
database password. `split_restore_drill` refuses production database addresses,
nonempty targets, arbitrary archive paths and non-paused environments. Its Docker
network is internal; it starts no scheduler or publishing worker.

The drill migrates a fresh PostgreSQL database, restores the archive, and checks
every normalized database row, artifact checksum and encrypted credential.
All sites and global controls are paused, pending/running jobs require
reconciliation, and browser sessions are invalidated, including sessions present
in older-format archives. No website or provider request is part of this drill.

Do not infer a successful restore from a healthy backup container. Retain the
drill's separate success report and encrypted archive for the release review.
