# Split-server backup and recovery

This procedure protects the standalone platform's application records,
encrypted connection credentials and evidence artifacts. It does not back up
the WordPress site, the shared PostgreSQL server's other databases, PostgreSQL
roles/HBA rules, Coolify itself, or the shared edge proxy configuration. Keep
those configuration records and the original deployment commit separately.

## Configure backups before enabling schedules

The backend's optional `backup` profile adds a periodic encrypted logical
backup and a freshness/integrity health check. It uses the existing external
database over its verified TLS connection and mounts evidence read-only. It
does not receive broker credentials or the owner bootstrap token. The one-shot
root initializer only prepares its own backup volume; the backup process runs
as UID 10001.

1. Save the deployed application's original `ENCRYPTION_KEY` in a secure,
   independently recoverable password manager. Never rotate it as part of
   redeployment. Losing it makes saved site credentials unusable.
2. Generate a separate Fernet `BACKUP_KEY`, store it privately as a runtime-only
   variable, and retain it in the password manager as well. Use a valid Fernet
   key, not an arbitrary password. Do not put either key in Git, chat, a build
   argument, or a shell command/history. The application key and backup key
   must be different.
3. Enable only the `backup` Compose profile after migration and configuration
   checks pass. Compose supports `COMPOSE_PROFILES=backup` or `--profile backup`;
   verify that the actual Coolify build/start invocation receives the selected
   profile. Do not enable `monitoring` or all profiles as a side effect.
4. Verify successful `backup-init` exit, a healthy `backup` container, and its
   reported archive filename. The first capture occurs immediately; subsequent
   captures default to every 86,400 seconds. Health fails if the newest archive
   is corrupt or older than 172,800 seconds. These are configurable via
   `BACKUP_INTERVAL_SECONDS` and `BACKUP_MAX_AGE_SECONDS`.
5. Record the exact backup volume name from the running backup container's
   `/srv/backups` mount, plus its backend image ID and Git commit. Never assume
   that a local project's volume name matches Coolify's resource-scoped name.

Keep scheduling and platform writes stopped for the initial recovery
checkpoint, including outstanding jobs. Database reads use one PostgreSQL
repeatable-read snapshot, but that alone cannot make concurrent filesystem
changes transactional with the database. For a release/recovery checkpoint,
quiesce writers and verify there are no in-flight changes first.

Archives are encrypted and authenticated, written without replacing existing
files, and validated after writing. Default storage is **on the backend host
only**. The service explicitly reports `off_host_copy: not_configured`.
No automatic retention/deletion is configured. Monitor free space and select
an off-host destination and retention policy before an unattended pilot; a
Docker volume on the same disk is not protection from losing that disk.

## Isolated restoration drill

Use `compose.restore-drill.yaml` by itself on the backend host. Never merge it
with the production Compose files. It creates a separate PostgreSQL 16
database and artifact volume, starts no application workers/scheduler/API,
publishes no ports, and uses an internal-only network. The source encrypted
backup volume is mounted read-only.

Before running, prepare these variables privately in the operator shell:

| Variable | Required value |
| --- | --- |
| `RESTORE_IMAGE` | Exact already-present backend image ID for the archive's application schema |
| `FORGE_BACKUP_VOLUME` | Existing encrypted backup volume, resolved from the running deployment |
| `RESTORE_ARCHIVE` | One existing `forgeseo-...forge` filename, not a path |
| `RESTORE_ENCRYPTION_KEY` | Original application's encryption key from secure recovery storage |
| `RESTORE_BACKUP_KEY` | Original backup encryption key from secure recovery storage |
| `RESTORE_DB_PASSWORD` | New independent URL-safe drill-only secret of at least 32 characters |

Do not copy the production `DATABASE_URL`. The drill fixes the host, role and
database to `restore-db`, `restore`, and `forgeseo_restore_drill` and rejects
connection-query overrides. Do not render the fully resolved Compose model or
dump container environment values to a shared log.

From the checked-out `platform/` directory, with the variables already exported:

```bash
RESTORE_PROJECT="forgeseo-restore-$(date -u +%Y%m%d%H%M%S)"
docker compose --env-file /dev/null -f compose.restore-drill.yaml --project-name "$RESTORE_PROJECT" up --detach restore-check
docker compose --env-file /dev/null -f compose.restore-drill.yaml --project-name "$RESTORE_PROJECT" ps --all
docker compose --env-file /dev/null -f compose.restore-drill.yaml --project-name "$RESTORE_PROJECT" logs --no-log-prefix restore-check
```

Wait for `restore-check` to exit and verify exit code **0**, not merely a
successful detached `up`. Save the safe JSON verification report alongside the
commit/image ID, archive ID, UTC timestamp, and drill project name. A successful
report checks every archived application row and artifact hash, actually
decrypts restored site credentials, confirms sessions were not restored,
pauses all sites, and marks interrupted jobs for reconciliation. No site or
provider request is issued to test credential access.

The process refuses an existing schema, a nonempty artifact destination,
invalid archives and wrong keys. A failed attempt remains available for
inspection. Do not delete its data to make a retry pass; use a new uniquely
named drill after understanding the failure. Do not point this procedure at
the native production PostgreSQL server.

After recording results, stop only that exact drill project. Before deleting
anything, inspect its container/volume labels, prove that the targets belong
to that drill, and preserve the encrypted source archive. Do not use volume
pruning or production `down --volumes`. Keep recovered resources until the
operator explicitly accepts their disposal.

## Evidence and limits

The local automated drill uses synthetic data and a real disposable PostgreSQL
16 container. It exercises migrations, recovery, database/artifact comparisons,
credential decryption, paused state, session invalidation, and refusal to
overwrite a prior restoration. It is not evidence that a remote installation
has backups or that its recovery keys have been retained correctly.

Before accepting the deployed environment, perform the same drill with an
actual deployment archive and keys retrieved from recovery storage. Record
the latest archive timestamp (recovery point) and elapsed restoration time.
Restoration into a replacement production database and traffic cutover are
separate operator-controlled actions: retain global/site pause, reconcile
uncertain remote jobs, and establish a single schedule/write owner before
resuming anything. Never automatically downgrade migrations or overwrite the
shared database server.

References: [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/),
[internal networks](https://docs.docker.com/reference/compose-file/networks/#internal).
