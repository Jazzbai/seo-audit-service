# Controlled pilot: current authors and Microsoft 365

Status: preparation release deployed with follow-up verification outstanding;
**NO-GO for live publication**.
This checkpoint continues the retained one-article preparation goal. It does not
start the seven-day pilot, grant paid-work authority, or enroll existing pages.

## Implemented boundaries

- Authenticated WordPress author discovery refetches the connected account and
  the complete user collection. Ineligible users are excluded; missing access,
  partial results, revoked credentials and malformed responses fail closed.
- The article editor and policy settings use discovered users, not typed IDs or
  historical inventory. No author is selected automatically. A short-lived
  observation supports preflight; remote writes check the author again.
- Microsoft Graph credentials are encrypted per site. Settings, permission
  attestation, test-send approval and receipt confirmation are owner-only.
- Authentication sends no message. Exchange Application RBAC setup is an external
  admin dependency. A seven-day owner attestation binds to the exact saved
  configuration; it is not claimed as machine-verified mailbox isolation.
- A test sends only after explicit approval. Its durable operation preserves
  uncertain outcomes without automatic resend. Provider acceptance and a human's
  inbox-receipt confirmation remain separate evidence. Digests default off.

## Evidence before deployment

Read-only production check on September 25 at approximately 16:42 UTC:

- Site and global pauses true; policy version 3 disabled; allowed actions empty.
- Retained article `b1f0fb8a5cb647998fefc7c3b166f727`, 3,418 characters,
  `review_needed`, only recorded blocker `missing_author`, zero publications.
- 53 pages, 146 open findings, four pending candidates, one open incident.
- Monitoring honestly reports degraded coverage (`complete_with_errors`, one
  audit error); WordPress change polling healthy, no missed checks observed.
- Two prior cost reservations, 100 cents held, zero reconciled spend recorded.
  Recorded zero spend is not proof of zero provider charges.
- No new paid requests, live WordPress writes, or emails during these checks.

Final backend regression: 809 passed, 30 skipped, two existing dependency
warnings. This includes the absent-WordPress-capability regression. Skipped
environment-dependent tests and mocked Graph tests are not evidence of live
Microsoft delivery. Frontend production build passes with a 589 kB bundle-size
warning. Final browser regression: **123 passed** (mocked API; desktop/mobile,
permissions, failure states and the new author/Graph journeys).

## Launch checklist

| Gate | State / evidence needed |
| --- | --- |
| Deploy and verify this release | `fe36cbb` pushed and deployments requested; frontend success confirmed, public API/UI verified; private container/source checks interrupted by lost VPN access |
| Fresh real-site author lookup | Complete authenticated response on September 25 at 17:00 UTC: eligible IDs 1, 4, 3; not one account |
| Owner's author choice | Required, even if only one account is eligible |
| Source support | Three revision-bound reviews recorded September 24; recheck expiry and revision before launch |
| One-article policy and protections | Retained disabled; verify unchanged after deployment |
| Independent recovery keys | Not verified; original saved-key file path/private access still needed |
| Scoped Microsoft connection | Sender, tenant setup and credentials still needed privately |
| Approved test and actual receipt | Not performed; requires scope evidence, approved recipient and inbox confirmation |
| Explicit launch authorization | Not granted by this goal; publishing remains paused |

Owner instructions: [user guide](../USER_GUIDE.md) and
[Microsoft 365 setup](../MICROSOFT_365_NOTIFICATIONS.md). Never put credentials in
Git, chat, screenshots, article text, or permission-review notes.

## Production verification and remaining release work

- Backend deployment handle: `j4zpgkeagaiczof2ezen7h29`.
- Frontend deployment handle: `a40mo55qlmknjuwv3k0gzn8b`; Coolify reports success,
  16:52:47–16:53:38 UTC. Public JavaScript asset `index-CF3jen9c.js` matches the
  locally built release. New authenticated author endpoint returns HTTP 200.
- Current eligible author display names: ID 1 `auto1stophouston@gmail.com`, ID 4
  `ewservices`, ID 3 `snabbanalys`. No account is selected, created, modified or
  deleted. Owner must identify the intended byline; these are fresh observations,
  not the older inventory presented as current users.
- Public read-only assertions passed after deployment: exact original body
  SHA-256, retained three September 24 source-review timestamps, policy v3 with
  one article/one post/zero refreshes, disabled policy, both pauses, no remote post,
  no schedule, zero publications, and unchanged cost reservations.
- New Graph form is available with test-send disabled and recurring digest off.
  No Microsoft credentials, permission review, email send or receipt is proven.
- Live inspection found horizontal overflow on the new Graph card at 390 px.
  The scoped correction now passes a reproducing regression through the receipt
  controls state: **9/9 focused author/Graph browser tests**, production build
  and whitespace checks pass. This follow-up still needs frontend redeployment
  and live verification when VPN access returns. No backend change is needed.
- Both `10.0.1.12:8000` and `10.0.1.10:22` became unreachable. An attempted
  read-only container verification could not obtain the terminal connection.
  Do not resubmit deployments blindly; reconnect VPN and inspect existing handles.
- The suggested independent recovery file
  `C:\Users\alire\ForgeSEO-Secrets\recovery.env` does not exist at the checked
  path. No fallback to running-container keys is counted as independent custody.
