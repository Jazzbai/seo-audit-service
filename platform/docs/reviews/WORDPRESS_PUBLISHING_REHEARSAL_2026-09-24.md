# WordPress publishing rehearsal — active acceptance ledger

This is the separate publishing-readiness goal, not the completed deployment
milestone and not the seven-day live pilot. No new paid requests are authorized.

## Boundaries

- Source: `Jazzbai/seo-audit-service`, `platform-deployment`; starting revision
  `a651518aa12cfa42825bb97d8a7be35dd009c476`.
- Reuse isolated WordPress `forgeseo-integration-wp-1` (loopback port 18090),
  after verifying its state and suitable network access. Never use Auto1Stop as
  the publishing target. Existing fixture data is retained.
- Reuse the empty `forgeseo-platform-local` application when suitable. Initial
  read-only DB inspection found zero sites. Do not restart the old Auto1Stop
  smoke workers/schedulers or the legacy ForgeSEO services.
- Auto1Stop remains paused, with disabled publishing policy. Its remote runtime,
  credentials, backups, content and scheduler ownership must be reverified before
  any compatible production deployment.

## Completion evidence required

| Requirement | Required proof | Current state |
| --- | --- | --- |
| Real isolated UI lifecycle | Authenticated browser, stored checks/schedule, worker publication, real WordPress REST and public HTML, UI rollback to draft | Passed fresh complete UI rehearsal at 22:03 UTC; prior interrupted rehearsal also recovered |
| Exactly-once outcome | Retained attempt history, replay/retry evidence, one remote operation/post identity | UI retry returned original job; real REST query found one post; same publication row retained after rollback |
| Safety regressions | Pauses, protected pages, missing author/source, concurrent edits, revoked credentials, ambiguous timeout, restart and exhausted-budget tests | Passing automated tests; fault/evidence boundaries below |
| Honest metering UI | Numeric counts visible; estimates, reservations and unknown actual costs distinguished; credential-redaction regressions | Passing API and desktop/mobile browser tests; cents no longer rounded to dollars |
| Relevant tests | Reviewed test scope, passing backend/browser suites and actual rehearsal evidence; simulated failures labeled | 715 backend passed/30 optional skipped; 109 UI passed; 8 real PostgreSQL passed; five real-WP lifecycle cases and actual process-death recovery passed |
| Delivery | Reviewed branch commit, compatible Coolify deployment preserving pauses, concise user guide and pilot go/no-go report | Pending |

Initial infrastructure checks show the WordPress fixture is running. Local
Auto1Stop smoke workers and schedulers are stopped; no queued/running/retry jobs
for that site. Other fixture stacks have no Auto1Stop record. No production site
mutation, additional paid provider call, or deployment has been performed by this
goal yet. Isolated fixture writes are recorded below.

## Retained rehearsal checkpoint

- Isolated WordPress article `d1a2f8b084574c22ba6277b5132a733c`, remote post
  `686`, publication `258a8f34e3d54d3fac554ac32ae33706`.
- Real scheduler selected the UI-scheduled article at 21:39 UTC. Publication job
  `7ef2038602174abf8e6c22e6c475cc6f` completed with source HTML evidence.
- The first test stopped on typographic-apostrophe handling in its rendered-link
  assertion. Resuming the same operation exposed a real scheduler/manual replay
  conflict. Fixed narrowly: return the canonical article job, preserve its
  original authorization and status, never redispatch a failed or uncertain job.
  Generic idempotency collisions remain rejected.
- Resume browser test passed: actual rendered heading/source link, exactly one
  matching WordPress post after retry, UI rollback, remote draft status and
  original publication-history row retained. Screenshots are retained under
  `frontend/test-results/wordpress-rehearsal-1790286666803/` locally.
- Retained DB/artifacts: `artifacts/publishing-rehearsals/run-ei2e0kfv` (ignored,
  not a production database). A lost ephemeral fixture key required revoking and
  reconnecting only the fixture through the UI. Retained harnesses now save a
  test-only key so evidence can survive a test-process restart.
- This earlier fixture run still had metadata enabled: it also applied six
  isolated metadata actions, with three failed attempts. The current rehearsal
  explicitly enables only publication. Those records were not erased.
- Harness uses the real scheduler and worker handlers but an in-process queue
  and SQLite, not the production RabbitMQ/PostgreSQL topology. Separate real
  Playwright inspection proves rendering; queued dedicated-browser jobs are not
  claimed as completed by this harness.
- New replay regressions reproduced five failures before the fix; the replay
  and scheduler-quota suites now pass (15 tests). New protected-target tests
  reproduced three unsafe paths. Intended slug and WordPress sample permalink
  are now checked against policy before creation/publication; further safety
  regression and final delivery checks remain outstanding.

WordPress permalink behavior is grounded in the official
[posts schema](https://developer.wordpress.org/rest-api/reference/posts/) and
[sample permalink implementation](https://developer.wordpress.org/reference/functions/get_sample_permalink/).

## Complete UI rehearsal after fixes

`frontend/test-results/rehearsal-reports/release-20260924-1.json` records one
passing test, zero unexpected/skipped/flaky results (49.6 seconds). Screenshots
show scheduling, the actual rendered WordPress article and completed rollback.
These local artifacts and fixture keys are ignored, not published in Git.

- Site `916dd1b24ce548c284d678cf59ce88b1`; retained DB
  `artifacts/publishing-rehearsals/run-evryu5_z/pilot.db`.
- Article `2a008d435b4a423397e897fbf7c747ec`; publication
  `f6cbb19e9547494b944933af8af74d93`; publish job
  `c3f29d27c86d4ce79921471906160429`; WordPress post `702`.
- One remote post after UI retry; final remote status **draft**; local article
  and publication **rolled_back**; no scheduler errors.
- Published HTML evidence SHA-256:
  `d4dd3c4233e4814176160631b0d4cedff081c1052881e230e5abb88efd408245`.
- Source-backed, explicitly labeled fixture article adapted from retained FTC
  research. No new paid research/generation. This is not proof of autonomous
  fact-checking or production-quality writing without editorial review.

## Failure and regression coverage

| Gate | Evidence | Boundary |
| --- | --- | --- |
| Pause and policy | `test_worker_retries.py`, `test_platform.py`, scheduler long-run and UI pause tests | Worker holds jobs before writes; publication policy rechecked; simulated application state |
| Protected targets/enrollment | `test_publication_protection.py`, foundation normalized-path/enrollment tests | Reproduced unsafe intended and resolved paths before fix; unknown/off-origin permalinks fail closed |
| Missing author/source | Real UI missing-author check, editor release and content tests | Confirmed-source fixture reused; live draft still has genuine blockers |
| Concurrent edits | Real-WP external-title case, connector/source-conflict and refresh tests | Actual external WordPress edit preserved; snapshot fallback cannot ignore a mismatch |
| Revoked access | Platform revocation test and connector permission-recheck test | Provider revocation responses simulated; no real production credential revoked |
| Timeout | Real-WP create/publish lost-response cases | Real HTTP write, deliberately dropped successful response; no duplicate |
| Worker restart | `test_wordpress_process_recovery.py` | Real `os._exit(73)` after real remote draft creation, before local ID commit; fresh processes reconcile, publish once and rollback; test-only SQLite and accelerated lease expiry |
| Reconciliation UX | API repeated-held-check and explicit linked resume tests; UI reconciliation tests | Prior jobs preserved; no blind retry; paused resume remains queued |
| Budget/usage | Budget atomic/foundation tests; browser usage safety/provider-usage tests | Concurrent real PostgreSQL reservations also pass; no additional paid requests |
| Browser/build | 109 mocked-API UI tests plus real WordPress rehearsal; `npm run build` | Desktop/mobile/a11y coverage where specified; existing >500KB bundle warning remains |
| Database/locking | Eight isolated PostgreSQL integration tests | Real migrations, reservations and worker advisory locks; disposable test DBs cleaned, fixture volume retained |

The process-death test found two additional real gaps: the interrupted publication
was not exposed to reconciliation, and native WordPress slug/author values were
read from the wrong normalized fields when no draft snapshot survived. Both are
fixed. A resumed publication now gets a distinct linked job while retaining its
original remote operation and original failed/uncertain job. Held read-only
reconciliation is no longer permanently cached as the only result.

## Production preservation and delivery gate

At 22:03 UTC: Auto1StopShop site/global pause both true; policy v2 disabled with
no allowed actions; zero publications; review article unchanged at 3,418
characters, still blocked on `missing_author` and `unverified_sources`. Monthly
ceiling $300, two retained reservations holding $1, no new provider work.
Scheduler heartbeat fresh, zero queue delay/missed checks; audit coverage remains
partial/degraded, not relabeled healthy.

At 22:05 UTC: seven existing backend services healthy. Off-host transfer timer
active with successful transfer checks; six archives retained. The previous
populated isolated restore evidence remains valid and its DB stays stopped.
No restore/key rotation or migration/schema change is introduced by this release.

**Branch delivery and compatible deployment remain pending at this checkpoint.**
Do not equate passing local tests with completed production deployment.

## Live-pilot decision and owner prerequisites

The rehearsal supports a controlled-pilot readiness decision after delivery, not
automatic activation. Auto1StopShop remains **NO-GO for publishing now** until:

- A real author and source-validation/editorial blockers are resolved.
- The owner approves precise article enrollment, protected paths, publishing
  limits and budget, and explicitly authorizes the separate live pilot.
- Provider invoice-actual costs are reconciled when available; estimates are not
  invoices. Backup-transfer alerting/email delivery and operational response
  ownership are verified (host transfer health is not yet integrated in the UI).
- Monitoring, backup freshness and one schedule/write owner are checked at launch.

The seven-day clock has not started. Other CMSs, broader WooCommerce acceptance,
all SEO findings, rankings and guaranteed AI citations are outside this goal.
The [plain-language user guide](../USER_GUIDE.md) explains review, publishing,
rollback, costs and recovery.

Each checkpoint must name changed files, test evidence and the next unresolved
acceptance gate. Do not treat a mock transport test as the real browser rehearsal.
