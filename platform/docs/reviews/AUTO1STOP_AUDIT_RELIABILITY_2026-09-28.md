# Auto1StopShop audit reliability release

Status: implementation, local verification, backend deployment, and one
production read-only audit complete on September 28, 2026. This is a source
audit and monitoring correctness milestone, not authority to publish, spend,
or edit main-page content.

## Evidence before deployment

- Release source: `Jazzbai/seo-audit-service:platform-deployment`, commit
  `27c4fdb` (following `7201368` and `484b5c3`). Branch pushes succeeded and
  the existing deployment resources are retained.
- A continuation key now includes its parent job ID. A new root audit cannot
  attach to a completed continuation from an earlier run; replay of the same
  parent retains idempotency. A stalled frontier gets bounded retries.
- XML sitemap discovery reads direct page/sitemap `<loc>` children, not image
  extension locations. Fetched non-HTML responses are retained as bounded asset
  evidence, without creating page records or page-error findings. The per-batch
  request limit counts assets too.
- A reachable 404 URL remains a `page_unavailable` finding, but no longer
  makes an otherwise complete audit look like a failed scheduler check.
  Transport errors, rate limits, and server failures continue to degrade audit
  evidence. Operational errors from earlier batches are carried into the final
  result rather than hidden by a later clean batch. A genuinely clean audit
  resolves a prior bounded-retry incident.
- A later change uses a successful non-HTML response to reclassify any matching
  historical `discovered_page` row, resolve its false source finding, and
  exclude it from the page inventory/count while preserving the row as evidence.
- Full backend suite: **817 passed, 30 skipped**. Frontend production build
  passed with its existing bundle-size warning; browser suite: **124 passed**.
  Compose backend configuration rendered successfully. `git diff --check`
  passed before commit.
- A read-only local crawl of Auto1StopShop finished in four bounded batches:
  25, 25, 25, and 21 page records; one non-HTML asset; zero audit errors and
  zero pending URLs at the end. `/wpbc-bfb-preview/` returned HTTP 404 in
  the first batch and remained explicit page evidence. Production evidence is
  recorded separately below.

## Production acceptance

| Gate | Observed evidence | Result |
| --- | --- | --- |
| Backend deployment | Existing Coolify backend resource `o4jvrqvvjp9898j0efgvrsik` imported `27c4fdbbb4e0826932f2da639ea4f8bc8f186f7d`, finished around 20:11 UTC, and reported `Running (healthy)`. Deployment log shows API, worker, browser, beat, and scheduler-worker started. Public `/health` returned 200/`ok`. Frontend resource was not redeployed. | Pass |
| Fresh read-only audit | Root `d24756406ede4557acde632cd679fefb` and continuations `211dfa2bcd2b447daeffa3d3d170adbc`, `cbf294aaff6d4e41bfb2c2150ea39e63`, `1f687090d3174110b948ae5c779fa548` were all created Sep 28 after deployment. They did not reuse the Sep 24 continuations seen in the earlier broken run. Final job is `complete`, with zero pending URLs, zero operational errors, and one non-HTML asset. Across batches, 95 successful source-HTML page checks and one 404 page observation were recorded; these are checks, not 95 unique inventory pages. | Pass |
| Findings and monitoring | `/wpbc-bfb-preview/` remains an open `page_unavailable` finding with HTTP 404. The previous `.webp` image finding is `resolved`, and the image is absent from the page inventory. Overview reports source coverage `complete`, audit cadence `healthy`, monitoring `running`, zero open incidents, zero queue-delay seconds, and zero missed checks. This does not mean every SEO issue is resolved. | Pass for source audit |
| Rendered-browser coverage | All 11 sampled browser jobs returned `partial`: each navigation was HTTP 200 with no recorded script error, but together they counted 141 resource failures. The browser worker currently counts blocked cross-origin requests and fetch exceptions in the same bucket; this evidence cannot establish complete rendering or identify which failures are site faults. | Not ready |
| Safety and spending | Site and global pauses remain on. Policy v3 is disabled with no allowed actions and only the retained article selected. Publication count is zero; the article is still `checked` with no remote ID. September spent amount is $0.00. Two older Sep 24 reservations of $0.50 each remain open (`generate` and `visibility`); this audit made no new paid request. | Pass for this read-only milestone; reconcile old reservations before paid automation |
| Time-limited reviews | Three source reviews remain accepted for the retained article, whose editorial check passes and author ID remains 5. They were reviewed Sep 24 around 23:10 UTC and age out around Oct 1 at 23:10 UTC under the seven-day rule. | Pass now; recheck immediately before any later launch |

## Go/no-go and remaining work

**GO for the read-only source-audit reliability milestone. NO-GO for publishing,
autopilot, or a claim of complete rendered coverage.** Leave both pauses and
policy v3 unchanged. Next, distinguish intentional cross-origin browser blocks
from genuine resource failures without opening unsafe network access, then
verify a representative rendered sample. Reconcile the two old budget
reservations before any paid work. The real sitemap 404 should remain a tracked
site finding; determine whether that preview URL should exist or be removed
from the sitemap. Re-review time-limited sources before any separate launch
decision, which still requires owner approval.
