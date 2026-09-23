# ForgeSEOPlatform 0.1.0-rc.1 — local candidate acceptance

Scope: the revised **local pilot candidate** goal, not production certification.
Release date: 2026-09-23. No live WordPress site or paid provider was used by this
release verification. The original ForgeSEO installation was not modified.

## Requirement-to-evidence matrix

| Requirement | Evidence and boundary |
|---|---|
| Independent local application, usable launch/login | `scripts/start-local.ps1` starts the existing Compose design under a separate local project with persisted private keys, loopback ports and paused defaults. `LOCAL_QUICKSTART.md` explains first-owner setup and later login. API/frontend version is `0.1.0-rc.1`. |
| UI onboarding and site policies on independent WP/Woo sites | `frontend/tests/pilot.spec.ts` uses browser forms and real REST credentials from loopback Docker fixtures, saves verified-author policies, and reads the persisted state back. No Auto1StopShop-specific assumptions. |
| Inventory and auditing | The same journey runs Full cycle, asserts completed inventory and source-HTML audit observations. Larger catalogs retain partial parent status while continuation jobs finish. `test_inventory_pagination.py`, `test_wordpress_resource_discovery.py` and `test_platform.py` cover pagination failures, completeness, and preserved prior evidence/cursors. |
| Planning, drafts and editorial checking | The journey obtains planner-created briefs, edits one with fixture-authored content, and checks it through the API. `test_editor_release.py` and `ui.spec.ts` verify that save preserves research/citations, records editor origin, and does not waive missing-source/author checks. This does not certify AI-written article quality. |
| Authorized publish, public verification and rollback | Browser controls publish on both actual fixture installations, inspect persisted public evidence, independently GET the published text, then roll back to draft and verify public HTTP 404. `test_wordpress_live.py` additionally checks duplicate/lost-response reconciliation and concurrent edits. |
| Protected bodies/layouts and commerce fields unchanged | `test_wordpress_live.py` checks metadata round-trip protected HTML/body equality and WooCommerce description rollback with exact price/SKU/stock/variation preservation. `test_connectors.py` tests builder guards, nested protected-field rejection and source conflicts. These fixtures are not a certification of every theme/builder/plugin. |
| Partial candidate selection preserves unselected work | `test_platform.py::test_partial_selection_and_finding_recurrence`, the live single-candidate metadata test, PostgreSQL sibling/site-scope tests, and `acceptance-gap-candidate-accounting.spec.ts` verify separate candidate states and visibility. Historical supersession is not resolution. |
| Recoverable failures, budgets and visible exceptions | Backend tests cover unknown outcomes, expired leases, revocation, stale sources and paused writes. Real PostgreSQL tests exercise atomic concurrent reservations and worker locks. Browser tests cover incomplete inventory, reconciliation, paused controls, failed runs and budget/connection gates. Paid-provider responses and cost outcomes are simulated. |
| Local restoration | `test_postgres_encrypted_backup_restore_preserves_artifacts_and_safety` restores isolated database records, artifacts and encrypted credential access; restored sites are paused and sessions invalidated. Production/off-site restore remains unverified. |
| Versioned reproducible handoff | Release tag `v0.1.0-rc.1`, Python dependency constraints, npm lockfile, pinned application base images, local launcher, test commands below and this matrix. Reproducibility means the supported local workflow/configuration; it is not a promise of byte-identical OS package builds. |

## Verification record

These are observed command results from the release checkpoint. A skipped or
simulated check is never counted as live acceptance.

- Backend: **570 passed, 26 skipped**. The skipped gates are opt-in Docker tests
  (listed below) and two host-PHP lint checks; the backend result is not a claim
  that those integration tests ran in that command.
- PostgreSQL migrations, concurrent budgets/policies/candidates, worker locking
  and encrypted restore: **8 passed** against the isolated PostgreSQL fixture.
- Yoast/Rank Math rendered-HTML/connector integration: **8 passed, 2 skipped**
  (host PHP lint unavailable). Runtime plugin paths did run in Docker.
- Frontend production build: passed; a non-blocking ~563 kB bundle warning remains.
- WordPress/WooCommerce connector and recovery gate: **12 passed**, including
  real publication rollback, dropped replies, preserved commerce data and
  single-candidate execution without sibling/body drift.
- Mocked-API frontend regression: **102 passed**, including editor research and
  structured citation preservation, mobile layouts, failure states and budgets.
- Real-API onboarding/content/report/mobile browser integration: **1 passed**;
  this separate test uses a fictional, unconnected site and does not publish.
- Packaging/deployment regression after dependency pinning: **7 passed**.
- Browser-to-WP/Woo: **1 passed** journey covering both installations, continuing
  planner-created briefs through actual publication and rollback on each.
- Installed Docker candidate: **1 passed** read-only browser smoke, including
  API health, accessible desktop setup and mobile overflow checks. Eight
  long-running services healthy; database migration exited successfully. The
  running API's image ID matches the rebuilt image and contains the dependency
  lock and `0.1.0-rc.1` version. First-owner setup remains untouched (zero users).

```powershell
# Repository root; Docker must be running for the opt-in fixture gates.
.\.venv\Scripts\python.exe -m pytest -q
$env:FORGE_LIVE_WP='1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_wordpress_live.py
$env:FORGE_LIVE_PG='1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_postgres_live.py tests/test_postgres_worker_concurrency.py
$env:FORGE_SEO_LIVE='1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_seo_plugins_live.py
cd frontend
npm ci
npm run build
npm test -- --workers=4
npm run test:integration
npm run test:pilot
npm run test:local
```

The pilot browser harness uses a temporary SQLite database and in-process job
delivery, **not** RabbitMQ. It uses real WordPress/WooCommerce HTTP, but leaves
browser-rendering jobs visibly queued; its audit evidence is source HTML.
The ordinary frontend regression suite mocks API responses. AI generation,
paid research, visibility providers, failure injection and virtual-clock cadence
tests use explicit stubs/simulations. None establishes a real seven-day result.

## Unfinished implementation / supported-scope limits

These are not missing credentials and are not declared complete by this release:

- Inventories beyond 100 batches / 10,000 records **per collection** fail visibly;
  resumable support above that limit remains unfinished.
- Internal-link and structured-data planning recommendations remain review-only;
  a suggestion is not an automatically applied improvement.
- Writes for unimplemented SEO plugins, arbitrary builder-managed bodies and
  additional CMSs remain unsupported. Existing bodies require explicit enrollment.
- Automated editorial checks are rule/provenance checks, not proof that every
  assertion is factually correct or that content is competitively useful. Real
  generated articles still need provider-connected editorial acceptance.
- No universal ranking/LLM visibility score, placement guarantee or external
  backlink/listing write authority is implemented or implied.

## External setup and subsequent acceptance goals

- Select/verify the always-on deployment, HTTPS origin, production secrets and
  operational ownership. Only one deployment may own live schedules/writes.
- Connect and verify each required paid research/AI service and actual pricing;
  validate Google OAuth/domain configuration, real media rights and email delivery.
  Existing read-only Auto1StopShop evidence is historical, not re-certified here.
- Exercise real-provider content quality, cost reconciliation and limited enrolled
  publishing after those connections are ready; do not count fixture articles.
- Verify production/off-site backup restoration and operational recovery.
- Run **seven actual unattended days** with the required publications, costs,
  monitoring and protection evidence. Status: **not started**.

The local development goal ends at this candidate handoff. These subsequent
gates do not justify an endless repetition of already-passing local tests.
