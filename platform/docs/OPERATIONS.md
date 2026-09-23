# Operations and recovery

This deployment is not yet certified for unattended live writes. Keep the
global and site automation pauses in place until the release gates in
`READINESS.md` pass. The commands below target the standalone
`forgeseo-platform` Compose project only; they do not operate the legacy
installation.

## Startup and readiness

Create a private server `.env` from `.env.example`. Use independent random
values for `DB_PASSWORD`, `QUEUE_PASSWORD`, `ENCRYPTION_KEY`, and
`BOOTSTRAP_TOKEN`; production also requires an HTTPS `PUBLIC_URL`, the
matching `APP_ADDRESS`, and `COOKIE_SECURE=true`. Do not put site credentials
in this file. `BOOTSTRAP_TOKEN` is required by the API only for first-owner
creation and is never returned by the application.

The preflight rejects reuse of a valid secret across those trust domains. When
the encrypted-backup profile is enabled, `BACKUP_KEY` must also be different
from every application secret. Keep each value in the private server
environment and retain the backup key separately for recovery.

Generate the bootstrap value with a password generator or a command such as
`python -c "import secrets; print(secrets.token_urlsafe(32))"`. Start the
deployment, open the same-origin `/bootstrap` screen, and paste the value into
the **Deployment setup token** field. The API accepts it in the
`X-ForgeSEO-Bootstrap-Token` header, keeps the existing same-origin check, and
still atomically permits only the first owner. After the owner is created,
rotate `BOOTSTRAP_TOKEN` to a new random value in the private server
environment and restart the API. The database initialization lock remains the final one-time
guard, and existing logged-in sessions do not use this token.

Render the configuration before starting anything:

```powershell
python -m scripts.preflight
docker compose config --quiet
docker compose build
docker compose up -d --no-build
docker compose ps
```

For the encrypted-backup profile, run `python -m scripts.preflight --backup`
as well. This additionally verifies that `BACKUP_KEY` is a valid Fernet key;
the ordinary strong `ENCRYPTION_KEY` remains compatible with the application's
derived-key contract. The preflight checks only environment shape and
production safety flags; it never contacts a provider and never prints secret
values. A passing preflight does not replace the container health checks or
the backup/restore drill below.

For a bounded startup and restart check after preflight, use the deployment
gate. It requires an explicit private env file when it may start or restart
Compose, uses the exact project name supplied by the operator, and never
performs cleanup:

```powershell
python -m scripts.deployment_gate --env-file .env --start `
  --health-url https://your-host.example/health --backup --restart-check
```

Omit `--start` for a render-only check. `--restart-check` restarts only the
platform worker, scheduler-worker, Beat, and browser services in that project.
The gate builds and starts in separate Compose invocations under one deadline;
failed builds/startups prevent the restart check from touching older services.
The command reports the seven-day pilot as `NOT_STARTED`; it is a deployment
verification aid, not provider validation or live-site authorization.

If Docker commands hang, inspect the host before attempting any cleanup:

```powershell
wsl.exe -l -v
Get-Service -Name com.docker.service
docker version
```

The Linux engine must show `docker-desktop` as `Running` and `docker version`
must return a server version. Start Docker Desktop with the normal Windows
application or with administrator privileges if its service is stopped. Do
not manually delete or move `docker_data.vhdx`, manually force-start the
`docker-desktop` distribution, or run a broad prune while unrelated Compose
projects may be in use. Recheck the service and engine before running any
ForgeSEO Compose command. This recovery step is host administration, not a
ForgeSEO application operation.

The startup contract is deliberately ordered: PostgreSQL and RabbitMQ must be
healthy, the migration container must finish successfully, API/worker services
wait on that migration, and Caddy waits for the API readiness check. A healthy
container is not the same as a healthy workflow, so inspect all runtime
components after startup:

```powershell
docker compose exec api python /srv/deploy/healthcheck.py api
docker compose exec worker python /srv/deploy/healthcheck.py worker
docker compose exec scheduler-worker python /srv/deploy/healthcheck.py worker
docker compose exec browser python /srv/deploy/healthcheck.py worker
docker compose exec beat python /srv/deploy/healthcheck.py process
docker compose exec web wget -q -O /dev/null http://127.0.0.1:2019/config/

# Verify the complete same-origin route from the host as well.
curl -fsS https://your-host.example/health
Invoke-WebRequest "$env:PUBLIC_URL/health"
```

The worker probe requires a Celery `pong` from the expected worker family.
Beat uses its pid file because it is not a worker. The application scheduler
heartbeat, queue delay, and missed-check count remain the authoritative
freshness signal for scheduling and are shown in the Monitoring area. A stale
heartbeat or excessive queue delay is degraded even when the containers are
running.

For a failed startup, inspect the dependency in order rather than retrying
writes blindly:

```powershell
docker compose ps
docker compose logs --tail=150 db queue migrate api worker scheduler-worker beat browser web
```

Do not expose a fresh installation publicly before the initial owner and HTTPS
configuration have been secured. The default Compose configuration keeps
automation paused.

## Inventory coverage and pagination failures

WordPress and WooCommerce inventory requests use 100 records per batch and a
maximum of 100 batches per collection (up to 10,000 records). This uses the
[WordPress pagination contract](https://developer.wordpress.org/rest-api/using-the-rest-api/pagination/).
Collections exceeding the bound fail with an `IncompleteInventory` result;
they are not reported as fully synchronized. Larger collections need a future
resumable inventory workflow before this limit can safely be expanded.

When total headers are absent, full batches continue until a short batch
establishes the end. Malformed or changing totals, early empty responses,
invalid records, duplicate identifiers, and mismatched final record counts
also fail visibly. Run history explains the failure without exposing remote
response text. A pagination limit is terminal for that job; inconsistent
responses use the existing bounded retry policy.

A failed collection does not refresh the connection's inventory evidence,
advance WordPress's polling cursor, or mark previously stored pages missing.
The affected connector must finish its read before its records are reconciled.
If WordPress completes and WooCommerce subsequently fails, the earlier
WordPress reconciliation remains valid, but the overall job fails and no
combined `inventory_complete` event is emitted. Existing findings and pending
candidates remain available for review. Inspect the affected site's REST
pagination behavior before retrying an inconsistent response.

## Google Search Console and GA4 OAuth

The Connections screen supports a bounded **Connect with Google** flow for
Search Console and GA4 only. An owner first saves the provider's OAuth client
ID and client secret in that site's connection card, then starts the flow from
the same card. The secret is encrypted with the site's existing credential
envelope; it is never returned to the browser after saving.

Register this exact redirect URI in the Google OAuth client configuration,
using the deployed `PUBLIC_URL`:

```text
https://your-host.example/api/v1/oauth/google/callback
```

Search Console requests the read-only Webmasters scope and GA4 requests the
read-only Analytics scope. The callback is bound to the initiating owner,
team, site, provider, and short-lived browser session state. It rejects
tampered, expired, cross-site, and sessionless callbacks. Successful token
responses are stored encrypted and redirect back to the site's Connections
screen with a non-secret result; provider tokens are never placed in the URL.

The existing manual refresh-token fields remain supported. If Google does not
return a replacement refresh token during reauthorization, the previously
stored refresh token is retained. Run the normal connection test after a
successful authorization before enabling measurement jobs. For GSC and GA4,
that test makes one read-only provider request and stores only a bounded
verified/error summary; it does not store provider response bodies or change
Google data. Tests for paid providers do not make an implicit paid request;
run their budgeted collection workflow explicitly after pricing is configured.

## WordPress targeted change notifications

The WordPress **Webhook secret** is optional, site-scoped, and stored encrypted
with the site's connection credentials. Configure the same secret in the
optional ForgeSEO WordPress connector only when it sends signed change
notifications. In WordPress, open **Settings → ForgeSEO Connector**, paste the
site-specific URL shown by ForgeSEO and the same secret, then save. Leave the
secret field blank to keep an existing secret unchanged; clear the URL to stop
delivery. Periodic polling remains the monitoring fallback.

The connector sends a `POST` to:

```text
/api/v1/webhooks/wordpress/<site_id>
```

The `X-ForgeSEO-Signature` header must contain the lowercase hexadecimal
HMAC-SHA256 digest of the exact raw request body, using the configured webhook
secret. The endpoint accepts payloads up to 64 KiB, requires an
`occurred_at` timestamp within five minutes, and accepts only supported target
IDs. It is non-authorizing: a valid notification queues a debounced targeted
audit and never authorizes a write. When notifications are unavailable, the
WordPress scheduler polls post-type collections every five minutes using an
overlapping `modified_after` cursor, stores the changed resources, and queues
the same targeted read-only audit. A full inventory reconciliation remains a
daily job, so a failed poll never advances its cursor. ForgeSEO mutation requests carry a
request-scoped operation key so the connector can identify and suppress the
platform's own notification echo; the marker is not persisted as human-edit
authority. Missing or unverified webhook capability does not disable the
normal periodic audit and inventory checks.

## Graceful restart and recovery

Routine worker or browser restarts are safe to perform one component at a
time:

```powershell
docker compose restart worker scheduler-worker beat browser
docker compose ps
```

The containers use an init process and a 30-second graceful stop window. Jobs
acknowledge late, and the scheduler reconciles expired leases after a worker
restart. Read-only work may retry within its bounded limit; publishing,
rollback, and paid work must become `needs_reconciliation` when a remote
outcome is uncertain. Never requeue a timed-out write merely because the
container restarted.

For maintenance that must prevent new work, pause all sites in the UI, stop
the scheduling and writing services, then verify the services are stopped:

```powershell
docker compose stop beat scheduler-worker worker browser api
docker compose ps
```

Bring them back only after the maintenance or checkpoint is complete:

```powershell
docker compose up -d api worker scheduler-worker beat browser web
docker compose ps
```

Only one installation may own schedules and writes. During migration, stop the
old owner first, capture a restorable checkpoint, and keep the new deployment
paused until its history, credentials, and policies have been verified.

## Encrypted backups

The backup service is an explicit `backup` profile so a deployment cannot
silently begin storing archives without an operator supplying a separate
`BACKUP_KEY`. The key must be a Fernet key and must be retained outside the
server, separately from `ENCRYPTION_KEY`. `ENCRYPTION_KEY` keeps the
application's existing contract: any strong value of at least 32 bytes is
accepted, and backup/restore derives the same application encryption key from
it; it does not need to be a padded Fernet string:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated value in the private `.env` as `BACKUP_KEY=...`, then start
the profile:

```powershell
docker compose --profile backup up -d backup
docker compose ps backup
docker compose logs --tail=50 backup
```

The profile creates a timestamped, authenticated, encrypted logical archive
once per `BACKUP_INTERVAL_SECONDS` (one day by default) and retains local
archives for `BACKUP_RETENTION_DAYS` (14 by default). The archive contains
database rows and artifact checksums; browser sessions are intentionally not
restored. The backup health check requires the newest archive to be both
decryptable and younger than `BACKUP_MAX_AGE_SECONDS` (48 hours by default).
Set that value to more than twice the configured `BACKUP_INTERVAL_SECONDS`
when changing the schedule; an old archive must make the backup service
unhealthy. The named Docker volume is not an off-site backup. An optional
operator-mounted mirror can retain the exact encrypted archive automatically:

```dotenv
BACKUP_MIRROR_DIRECTORY=/srv/backup-mirror
BACKUP_MIRROR_HOST_PATH=D:/secure-forgeseo-backups
```

Set both values before starting the backup profile. `BACKUP_MIRROR_HOST_PATH`
must be an existing writable host directory that the backup container can
mount and write as its non-root service user. After the local archive is
atomically committed, ForgeSEO atomically creates the same filename in that
directory without overwriting an existing file. If the mirror is missing,
unwritable, conflicting, or does not match the newest local archive, the
backup command fails and the backup health check reports failure; the local
archive is retained for local recovery. With `BACKUP_MIRROR_DIRECTORY` blank,
the mirror is disabled and the existing local-only behavior is unchanged.

The mounted directory is only a secondary copy. Its placement on another
host, disk, filesystem, or provider—and its retention and access controls—are
the operator's responsibility; this feature does not provide off-site storage
by itself and makes no network or cloud-provider calls. The default Compose
file keeps a dormant bind target so it remains renderable when the mirror is
disabled. Verify the configured copy through the same health probe:

```powershell
docker compose exec backup python -m scripts.backup_healthcheck
```

For manual handling when the mirror is not enabled, copy a recent archive off
the server explicitly:

```powershell
docker compose exec backup sh -c "ls -1t /srv/backups/forgeseo-*.forge | head -n 1"
docker compose cp "backup:/srv/backups/<archive-name>.forge" "D:\secure-forgeseo-backups\"
```

Before deleting the local archive, inspect the off-site copy with the same
`BACKUP_KEY` and `ENCRYPTION_KEY`. Inspection decrypts and validates the
manifest, credential-key binding, exact archive paths, and every artifact
checksum without opening a database or writing an artifact directory. Run it
from the deployment image or an isolated restore container:

```powershell
docker compose --profile restore up -d restore
docker compose cp "D:\secure-forgeseo-backups\<archive-name>.forge" "restore:/srv/backups/checkpoint.forge"
docker compose exec -T restore python -m scripts.backup inspect /srv/backups/checkpoint.forge
docker compose stop restore
```

Keep the inspection JSON with the off-site archive's retention record. A
successful inspection proves archive integrity and key compatibility; it does
not prove that a database or artifact restore has succeeded.

The scheduled profile is a best-effort logical snapshot. For a recovery-grade
checkpoint, pause sites, wait for active writes to finish, stop API/workers and
beat, start the backup profile long enough to create and copy an archive, then
stop it before bringing the application back:

```powershell
docker compose stop beat scheduler-worker worker browser api
docker compose --profile backup up -d backup
docker compose logs --tail=20 backup
docker compose exec backup sh -c "ls -1t /srv/backups/forgeseo-*.forge | head -n 1"
docker compose cp "backup:/srv/backups/<archive-name>.forge" "D:\secure-forgeseo-backups\"
docker compose stop backup
docker compose up -d api worker scheduler-worker beat browser web
```

Do not delete the local archive until the off-site copy has been inspected. The
logical archive is not PostgreSQL point-in-time/WAL recovery; retain a
provider-level database backup if that recovery objective is required.

## Restore drill

Restore only into a new, isolated Compose project with new empty database and
artifact volumes. Never restore over the live database. Use the original
`ENCRYPTION_KEY` and the separate `BACKUP_KEY`; the backup does not contain a
replacement for either secret.

A safe drill uses a distinct project name and the same image version:

```powershell
docker compose -p forgeseo-restore-drill --profile restore up -d restore
docker compose -p forgeseo-restore-drill cp "D:\secure-forgeseo-backups\<archive-name>.forge" "restore:/srv/backups/checkpoint.forge"
docker compose -p forgeseo-restore-drill stop restore
docker compose -p forgeseo-restore-drill --profile restore run --rm --no-deps restore python -m scripts.backup restore /srv/backups/checkpoint.forge --confirm-empty-target
docker compose -p forgeseo-restore-drill --profile backup up -d api worker scheduler-worker beat browser web
docker compose -p forgeseo-restore-drill ps
```

The manual `restore` profile is separate from the routine backup profile and
mounts artifacts read-write only for this deliberate recovery operation. The
restore command refuses a nonempty database or artifact directory,
validates the Fernet key fingerprint, rejects unexpected or unsafe archive
paths, verifies every artifact checksum, pauses restored sites, invalidates
browser sessions, and holds interrupted jobs for reconciliation. Artifacts are
staged before being committed; if materialization fails, the database
transaction and created files are cleaned up. An interrupted host or volume
operation still requires a fresh empty drill target—do not force a partial
restore forward.

After the drill, verify the restored evidence and paused state, then record the
result and the exact image, keys, and archive identifiers. Keep the original
deployment and artifact store until the restore and rollback procedures have
passed.

## Unknown remote outcomes and incidents

Do not force a job back to `queued` or create a second publication after a
timeout. Inspect the operation identifier, remote resource, captured source,
publication snapshot, and provider charge. Reconcile explicitly in the UI/API;
the platform must not infer that a timeout means no remote write or charge.

Use the in-app incident history and activity log for first-seen, recurrence,
affected-page, and recovery evidence. Container logs are supplementary and
must not be the only record of an incident.

### Owner-only publication reconciliation

An `ambiguous` or `needs_reconciliation` publication means that a previous
create or publish attempt ended without a trustworthy, source-matched remote
result. The remote site may already contain the article; it is not safe to
assume that the write failed. Automatic publishing remains held until the
outcome is resolved. Keep live publishing paused until the deployment gates in
`READINESS.md` pass, even when reconciliation reports a safe result.

Only an Owner may start reconciliation. The action is site-scoped and accepts
only the site and publication path parameters; it does not accept an operator
supplied operation key. It reads the known remote record, or searches for one
unique matching draft when the remote ID is not known, checks the saved source
snapshot, and verifies a published page. It never sends a create or publish
write, and it never retries either write. The reconciliation check itself may
be retried by the bounded read-only worker policy after a transient worker
failure.

Use an already authenticated same-origin session and its CSRF token. Replace
the placeholders locally; do not put cookies, credentials, CSRF values, or
operation keys in this runbook, shell history, or logs:

```powershell
$baseUrl = "https://<FORGESEO_HOST>"
$siteId = "<SITE_ID>"
$publicationId = "<PUBLICATION_ID>"

# Existing authenticated WebRequestSession from the ForgeSEO browser session.
# Do not print or serialize this session.
$authenticatedSession = $existingAuthenticatedSession
$headers = @{
    "Origin" = $baseUrl
    "X-CSRF-Token" = "<CSRF_TOKEN_FROM_AUTHENTICATED_SESSION>"
}

$queued = Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/v1/sites/$siteId/publications/$publicationId/reconcile" `
    -WebSession $authenticatedSession `
    -Headers $headers `
    -ContentType "application/json" `
    -Body "{}" `
    -ErrorAction Stop

$jobId = $queued.id
if (-not $jobId) { throw "The reconciliation action did not return a job id." }
```

The endpoint returns `202 Accepted`. Poll the returned, site-scoped job until
it reaches a terminal job status. Read `job.result.status` for the
reconciliation outcome when present; a failed or blocked job may expose only
a safe job-level error result:

```powershell
$terminal = @("complete", "partial", "failed", "blocked",
              "needs_reconciliation", "ambiguous", "rolled_back")
$job = $null

for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $job = Invoke-RestMethod `
        -Method Get `
        -Uri "$baseUrl/api/v1/sites/$siteId/jobs/$jobId" `
        -WebSession $authenticatedSession `
        -ErrorAction Stop

    if ($terminal -contains $job.status) { break }
    Start-Sleep -Seconds 2
}

if ($null -eq $job -or $terminal -notcontains $job.status) {
    Write-Warning "The check is still running; poll the same site and job later."
} else {
    [pscustomobject]@{
        job_status = $job.status
        reconciliation_status = $job.result.status
        reason = $job.result.reason
        next_action = $job.result.next_action
    }
}
```

Interpret the safe result as follows:

- `published`: the remote record matched the saved snapshot and the public
  page was verified. No new write was sent; continue monitoring it.
- `draft_reconciled`: one matching remote draft was found and source-locked.
  It is not published. Review the policy and deployment gates before using the
  normal, explicitly authorized publication workflow.
- `held`: ForgeSEO could not prove a safe match or verification, for example
  because the remote record was unavailable, the snapshot differed, or the
  local article changed. Do not retry create or publish; review the incident
  and resolve the underlying condition first.

A `409` response means the publication is no longer eligible for
reconciliation (for example, it is already resolved); do not create another
publication. A transient API/worker failure can leave the reconciliation job
retryable, but that retry is still read-only.

## Legacy import and ownership cutover

The portable transfer workflow reads the old installation through a
read-only `LEGACY_DATABASE_URL`. It never updates the legacy database. Export
redacts credential-shaped fields, records a versioned manifest, and writes the
JSON bundle by atomic replacement:

```text
python -m scripts.import_legacy --source-site 1 \
  --export-bundle ./transfer/auto1stopshop.json
```

Move the bundle through the approved transfer boundary, then validate it on
the standalone deployment. A bundle import is a preview unless `--apply` is
present; applying requires an existing target site whose pause is still on:

```text
python -m scripts.import_legacy --bundle ./transfer/auto1stopshop.json \
  --target-site NEW_SITE_ID
python -m scripts.import_legacy --bundle ./transfer/auto1stopshop.json \
  --target-site NEW_SITE_ID --apply
```

The direct database form remains available for compatibility, but it is less
portable and still requires `--apply` before it writes:

```text
python -m scripts.import_legacy --source-site 1 --target-site NEW_SITE_ID
python -m scripts.import_legacy --source-site 1 --target-site NEW_SITE_ID --apply
```

The import stores a checksum, the exact IDs it created, and a retained
evidence archive. Historical approvals, proposals, executions, and evidence
are informational only; they never become policy permissions or credentials.
Reusing the same checksum is idempotent. If a transfer must be undone, keep
the target paused and preview the exact checksum first, then apply the
rollback:

```text
python -m scripts.import_legacy --rollback-import IMPORT_SHA256 \
  --target-site NEW_SITE_ID
python -m scripts.import_legacy --rollback-import IMPORT_SHA256 \
  --target-site NEW_SITE_ID --apply
```

Rollback removes only pages marked as created by that exact import and leaves
the evidence archive and rollback record in place. It stops rather than
deleting a page with later findings or candidates. It cannot roll back an old
import that lacks exact page-ID metadata. The source installation remains
read-only throughout.

Production cutover still requires a verified read-only TCP database credential,
an independently backed-up artifact archive, a fresh paused standalone target,
an explicit comparison against fresh inventory, and one deployment owning
schedules and writes. Do not enable live publishing as part of migration.
