# ForgeSEO Platform

Independent WordPress SEO platform under development. There is no runtime dependency on Business Optimizer or the original ForgeSEO installation.

**Local candidate: 0.1.0-rc.1.** Start with the [plain-language local launch and login guide](docs/LOCAL_QUICKSTART.md).
See the [candidate acceptance matrix](docs/CANDIDATE_ACCEPTANCE.md) for this bounded handoff; the longer readiness ledger below records the broader product/production history.

**This is not yet the complete pilot release. Do not activate unattended live publishing.** See [readiness](docs/READINESS.md) for the remaining acceptance gates. New installations and restored sites start paused.

## Local verification

From this repository in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm ci
npm run build
npx playwright install chromium
npx playwright test tests/ui.spec.ts --config playwright.config.ts
npm run test:integration
npm run test:smoke

# Against a running standalone Compose deployment (global pause remains on)
$env:FORGE_COMPOSE_URL = "http://127.0.0.1:18080"
npm run test:compose

# Optional isolated connector gates (Docker must be running)
$env:FORGE_LIVE_WP = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_wordpress_live.py
$env:FORGE_LIVE_PG = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_postgres_live.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_postgres_worker_concurrency.py
$env:FORGE_SEO_LIVE = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_seo_plugins_live.py

# Read-only operator handoff (use a private server .env and real deployment URL)
.\.venv\Scripts\python.exe -m scripts.pilot_readiness --env-file .env `
  --health-url https://seo.example.com/health --compose --backup

# Platform-level live handoff (credentials stay in the shell environment;
# default mode makes no WordPress mutation)
$env:FORGE_PLATFORM_URL = "https://seo.example.com"
$env:FORGE_PLATFORM_EMAIL = "owner@example.com"
$env:FORGE_PLATFORM_PASSWORD = "use-a-private-shell-secret"
python -m scripts.platform_live_probe --site-id SITE_ID --run-inventory --run-audit --json

# Direct WordPress connector check (GET-only by default; no application .env is read)
$env:WORDPRESS_ORIGIN = "https://site.example.com"
$env:WORDPRESS_USERNAME = "wordpress-user"
$env:WORDPRESS_APPLICATION_PASSWORD = "use-a-private-shell-secret"
python -m scripts.live_wordpress_pilot

# Bounded deployment/startup gate (--start or --restart-check changes only the named standalone project)
.\.venv\Scripts\python.exe -m scripts.deployment_gate --env-file .env `
  --start --backup --restart-check --health-url https://seo.example.com/health
```

The default Playwright suite runs only the mocked/recovery browser tests. `npm run test:integration` starts a real FastAPI server and Vite, creates a temporary database, signs up a test owner, onboards an independent fictional site, saves an encrypted test connection, creates and checks an article, visits operational screens, and checks the overview at desktop/mobile sizes. `npm run test:smoke` checks the same disposable backend's public health contract. `npm run test:compose` runs that onboarding journey against the Caddy-served frontend and API of an already-running standalone Compose deployment; it expects the disposable test bootstrap token used by the Compose verification setup. Neither path connects to or publishes on a real WordPress site.

The readiness command is also read-only: it checks local deployment state and
records external/provider and seven-day pilot gates without contacting them.
The deployment gate is the bounded operator command for a named standalone
Compose project: it validates the private environment before starting,
optionally verifies backup-profile startup and worker restart recovery, and
never removes containers, volumes, images, or caches. Neither command contacts
WordPress, Google, paid research, or AI providers.

The platform live probe logs into an already deployed ForgeSEO instance and
can queue only local inventory or public-audit jobs. It has no publishing or
candidate-execution option. The direct WordPress probe is GET-only unless both
`--allow-live-write` and `--confirm-live-write` are supplied; that optional
roundtrip creates one uniquely marked post, restores it to a non-public draft,
and retains it as evidence. Neither probe is the seven-day pilot gate.

## Application layout

- `app/`: authenticated FastAPI APIs, persistent workflows, scheduling, budgets, connectors, and intelligence.
- `frontend/`: standalone React/TypeScript web application.
- `wordpress/`: optional connector; isolated native/Yoast/Rank Math integration fixtures are covered, while live-site verification remains pending.
- `scripts/`: encrypted backup/inspect/restore, read-only legacy import, pilot readiness/deployment gates, and isolated browser test server.
- `deploy/`, `Dockerfile*`, `compose.yaml`: separate deployment configuration; production rollout is not yet verified.

The site Overview includes **Run full cycle** for the bounded, authenticated
site-understanding workflow. It records availability, verified WordPress
inventory when connected, public audit, content planning, and enrolled refresh
evaluation as one durable job. The result explains incomplete stages and next
actions. Publishing, metadata writes, paid visibility collection, and other
remote mutations remain separate policy-gated workflows; the command never
silently turns a connection or stored policy into an unreviewed write.

Owners can also use **Run site autopilot** from the same Overview. This is a
bounded six-stage command that adds one-article content autopilot between
content planning and refresh evaluation. It is owner-only, respects site and
workspace pauses, and stops visibly at policy, connection, budget, editorial,
failure, or reconciliation gates. A verified publication is reported only
when the server confirms it; this command does not mean the whole site is
optimized and does not replace the production seven-day pilot gate. Its public
audit stage can also queue page-specific metadata candidates through the same
bounded policy gate used by governed audits; each candidate worker rechecks
policy, freshness, protected resources, and the current remote value before a
write. Content publication remains a separate enrolled workflow.

The Content Calendar also exposes an explicit **Run content autopilot** action.
It can process at most one platform-managed planned article: research it,
generate a draft through the budget ledger, run editorial checks, and publish
only when the active policy and verified WordPress/AI/author prerequisites
pass. It reports gated, review-needed, failed, ambiguous, and verified states;
it does not mean the site is optimized and is not a substitute for the
production seven-day pilot gate. When automatic publishing is enabled, the
scheduler uses the same governed pipeline at most once per configured local
publish day after the 09:00 local start boundary. Active or uncertain parent
jobs reserve publication capacity, and unverified connections or paused sites
remain visibly blocked; the scheduler does not run a competing automatic
generation loop.

API routes use `/api/v1`. Credentials are encrypted per connection; they are never returned to the browser. Blank credential fields preserve existing values; revoke explicitly to remove a connection. Site settings do not inherit credentials from another installation.

WooCommerce product and category catalog edits are limited to verified editorial fields. Product and product-category SEO metadata can be read or written only through separately verified ForgeSEO routes with archive/rendering checks. Commerce data such as prices, stock, SKUs, orders, payments, and customers is never written.

## Server deployment prerequisites

Use a new server deployment/project and new volumes. Do not replace legacy containers. The compose project is `forgeseo-platform`, with PostgreSQL, RabbitMQ, API, platform worker, dedicated browser worker, scheduler worker, beat, and a Caddy same-origin frontend/proxy.

Copy `.env.example` to a private server `.env` and supply independent strong URL-safe database/queue passwords, a random encryption key of at least 32 bytes, and a separate strong `BOOTSTRAP_TOKEN`. Production requires a real HTTPS domain, `COOKIE_SECURE=true`, correct `PUBLIC_URL`, and reachable HTTP/HTTPS ports for certificate issuance. The first-owner screen asks for the deployment token once; after setup, rotate that environment value and restart the API. Site credentials belong in the UI after server setup.

The local example uses HTTP on port 18080. This is not a production security configuration. Compose intentionally defaults all automated writes to paused. Hosting/domain/OAuth/provider billing setup remains external work; no demo metrics substitute for missing connections.

## Costs, recovery, and import

Paid work requires configured pricing and a reservation. If a provider omits an actual charge, the maximum remains reserved rather than becoming fabricated measured spending. Owners can reconcile a charge using provider evidence under Settings → Policies & budget. Unknown outcomes must be reconciled before retrying paid or writing jobs.

Backup/restore instructions are in [operations](docs/OPERATIONS.md). The importer is a dry run by default and archives legacy approvals as historical evidence only. It never activates automation permissions or copies credentials. For a portable migration, export an atomic redacted bundle from the legacy read-only connection, preview it on the standalone target, then apply only while the target remains paused:

```powershell
python -m scripts.import_legacy --source-site 1 --export-bundle .\transfer\legacy.json
python -m scripts.import_legacy --bundle .\transfer\legacy.json --target-site TARGET_ID
python -m scripts.import_legacy --bundle .\transfer\legacy.json --target-site TARGET_ID --apply
```

The Policies & budget screen shows imported history and any checksum-scoped rollback as read-only evidence. Retain the legacy artifact store and installation until a complete migration and rollback drill passes.

## Upstream API references

Implementation targets the [WordPress posts API](https://developer.wordpress.org/rest-api/reference/posts/), [WooCommerce REST API](https://developer.woocommerce.com/docs/apis/rest-api/), and [DataForSEO API](https://docs.dataforseo.com/v3/serp/overview/). AI observations are provider samples, not universal ranking measurements; [Google's AI-feature guidance](https://developers.google.com/search/docs/appearance/ai-features) does not require special AI markup.
