# Platform-owned Google OAuth: local implementation

Status: local changes only; no deployment, Google consent, or live measurement
verification performed in this implementation batch. Auto1Stop and workspace
automation remain untouched and paused.

## Implemented

- Operator-only runtime client identity in both Compose definitions and Settings.
- Customer cards expose property/reporting settings, not client secrets or tokens.
- Site-scoped, authenticated readiness endpoint; missing configuration fails closed.
- First-time connection creation and explicit reconnection after local revocation.
- Owner/session/team/site/provider-bound, short-lived, one-use callback state.
- Encrypted per-site grants bound to the issuing client, no cross-client refresh reuse.
- Fixed Google token endpoint, read-only scope checks, and secret-safe readback.
- Same-client secret rotation works without copying the operator secret to site records.
- Concurrent revoke/client changes cannot install stale grants after code exchange.

## Remaining release checks

Deploy backend and frontend together from the reviewed release while paused.
Verify runtime readiness without printing secrets, then let the owner authorize
Search Console and GA4 using the intended Google account. Test actual read-only
access and property association. Property discovery/dropdowns are not included
in this batch; property values remain manual. Google consent alone is not a
verified property or measurement result.

External Testing is temporary: these refresh tokens expire after seven days.
Customer launch needs Google publishing/verification planning. This standard
client is for direct dashboard reporting, not new AI Google tools/MCP. No paid
provider calls, publishing, main-page edits, or visual website changes were made.

## Local validation

- Backend regression suite: **869 passed, 30 skipped**. Opt-in live integrations
  remain separate; this is not production Google verification.
- Focused existing/new Google OAuth checks: **24 passed**, including replay,
  cross-site state, missing configuration, client rotation, scope rejection,
  and revocation during exchange.
- Default Playwright browser suite: **172 passed**, using mock API responses.
  Six new platform Google tests cover readiness, roles, and settings-only saves.
- Frontend production build: passed; the existing approximately 606 KB bundle
  warning remains.
- Both Compose runtime-forwarding contracts and `git diff --check`: passed.

No production deployment, provider data collection, or customer Google consent
was performed by these tests.
