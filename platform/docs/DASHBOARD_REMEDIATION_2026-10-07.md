# Dashboard audit follow-up: October 7, 2026

## Deployment boundary

These are local changes on `platform-deployment-jazzbai`. They have not been pushed or deployed. The public dashboard has not received them. Auto1Stop's live content, styles, connection credentials, automation policy, site pause and workspace pause were not changed during this follow-up.

The private checklist and original report in `artifacts/dashboard-audit-2026-10-07/` retain the historical results for all 148 requirements. Local test passes below do not replace those live results or certify the complete pilot.

## Implemented changes

- Policy weekday checkboxes no longer stretch the policy layout at desktop or mobile widths; keyboard focus remains visible.
- Issues comparison labels meet contrast checks. Tables, calendar and raw reports have named, keyboard-focusable scrolling areas. Login and initial-owner setup have working skip-link targets.
- Article generation and publication are visibly disabled when site/workspace controls are paused or unavailable. Draft editing and editorial checks remain available. The API rejects new paused generation/publication requests with an explicit 409 before queuing a job; workers retain their execution-time checks.
- Owners and editors can cancel a waiting job through its site-scoped API/UI. Conditional updates prevent overwriting a worker claim; late broker delivery ignores the cancelled job. Running or completed work is not cancellable through this action. Cancellation retains history, does not roll back remote changes, and does not disable recurring schedules or automatically create a replacement operation.
- Queued browser samples no longer count as successful rendering. A successful sample needs matching complete evidence, HTTP 200 and zero resource failures.
- Overview reports source-HTML coverage separately from recent browser sample outcomes. Browser outcomes use the latest job per page among at most 200 jobs in a seven-day window; even all successful samples are not whole-site rendering certification.
- Real WordPress recovery tests now use a persisted encrypted connection, authenticated capabilities and discovered authors. Fault scenarios assert that the intended failure injection was reached. Integration Docker cleanup preserves pre-existing resources.

## Verification

| Check | Result | Evidence limit |
| --- | --- | --- |
| Backend regression suite | 855 passed, 30 skipped | Local database/fixtures; opt-in integrations are separate |
| Frontend regression suite | 163 passed | Mock API browser tests, including populated accessibility/mobile cases |
| Additional coverage-label browser tests | 3 passed | Mock API; missing, partial and successful sample states |
| Real local API browser journey | 1 passed | Disposable platform database, not production |
| WordPress/WooCommerce integration and recovery suite | 12 passed | Isolated real WordPress installations; no Auto1Stop writes |
| WordPress/WooCommerce browser publishing rehearsal | 1 passed, both site types | UI onboarding, verified connections, planning handoff, manual editorial checks, publication, public-page verification and rollback to draft; not AI research or a seven-day run |
| Frontend production build | Passed | Existing bundle-size warning remains |
| Whitespace validation | Passed | `git diff --check` |

Backend dependency deprecation warnings remain. Rendering resource restrictions were not relaxed merely to make tests green.

The browser publishing rehearsal initially missed the WooCommerce save message because it started the next operation after the backend connection changed but before the WordPress UI waiter finished. The test now waits for the visible WordPress completion before proceeding; the rerun passed without bypassing a product safeguard.

## Still outstanding

1. Push/deploy a reviewed release and re-run the affected browser tests against the public dashboard. Keep writes paused during regression testing.
2. Resolve Auto1Stop's incomplete browser renders with a securely bounded external-resource strategy. The template reconciliation caller still supplies queued URLs, not completed render evidence; recent actual outcomes are exposed separately in Overview rather than falsely marking those template summaries complete.
3. Verify a supported metadata write surface on Auto1Stop. The currently deployed connection reports unsupported metadata writes; connection success does not imply metadata editing support. Any optional connector installation needs its own backup and capability verification.
4. Connect missing search/analytics/research sources and exercise their real measurements, editorial research and cost accounting. Local mocks do not establish Google authorization, paid-provider execution or article quality.
5. Renew the owner's expired mail-scope review only after checking the current external permissions; prove delivery with recipient confirmation if testing email.
6. Complete the remaining acceptance requirements, restore rehearsal and seven-day unattended pilot. Do not describe this follow-up as a fully autonomous production release or as evidence of ranking/AI-citation improvements.

## What the owner needs to do

For now, keep automation paused and identify whether Auto1Stop has Search Console and GA4, which Google account has access, and its GA4 property ID if available. Do not paste passwords, tokens or client secrets into chat. No PowerShell or environment-file edits are needed for these local fixes.

After deployment, Google connection setup uses the site's Connections cards and requires a registered OAuth client plus account authorization; see [operations instructions](OPERATIONS.md#google-search-console-and-ga4-oauth). Missing Search Console/GA4 properties must be created/verified rather than represented by demo data.

Email is optional for auditing. The owner must personally review the current scope, record the review, send one explicitly approved test and confirm actual receipt; see [notification instructions](MICROSOFT_365_NOTIFICATIONS.md). Saving Graph settings invalidates the previous review, so save the connection before recording that review.

Paid research keys belong only in the encrypted Connections settings when that feature is ready. WooCommerce setup is unnecessary for a site without a store. Publishing and enrolled body edits remain paused until a specific, reviewed pilot is ready; protected main pages and builder layouts remain outside that pilot.
