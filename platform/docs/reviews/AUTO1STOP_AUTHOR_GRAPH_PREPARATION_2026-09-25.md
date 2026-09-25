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
| Deploy and verify this release | Backend `fe36cbb`: all seven containers healthy, nine source files matched in API/worker/scheduler-worker; frontend mobile correction `a8f58dc` publicly verified at 17:14 UTC |
| Fresh real-site author lookup | Complete authenticated response on September 25 at 17:00 UTC: eligible IDs 1, 4, 3; not one account |
| Owner's author choice | Required, even if only one account is eligible |
| Source support | Three revision-bound reviews recorded September 24; recheck expiry and revision before launch |
| One-article policy and protections | Public read-only assertions passed after deployment; retained disabled and paused |
| Independent recovery keys | Owner's `recovery.env.txt` validated; both fingerprints match deployed keys; matching keys authenticated archive, 124 artifacts and two encrypted connection records at 17:13 UTC |
| Scoped Microsoft connection | Owner selected sender `forgeseo@eit.care`; mailbox type, Entra/Exchange setup and credentials remain unverified |
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

## Follow-up: saved keys and mobile release, 17:14 UTC

The owner supplied the same file with Windows' text-file extension:
`C:\Users\alire\ForgeSEO-Secrets\recovery.env.txt`. Both required assignments
passed format validation and were distinct. ACL inspection showed only the owner,
SYSTEM and Administrators. The plaintext copy was not uploaded, modified or
deleted; key values were not printed or committed.

Verification compared SHA-256 fingerprints computed locally from this independent
copy against the deployed keys, then used the matching keys to authenticate and
decrypt the retained archive and every encrypted connection record. All passed:

- Archive: `forgeseo-20260924T231123Z-bd7b12e64e074bb4ba23cfad3884c342.forge`.
- SHA-256: `7e7df8ec1caa9458c9892424472f7f056a92eced16d13898accf86f7eecb5216`.
- Verification time: `2026-09-25T17:13:38.531225+00:00`.
- 19 tables, 124 artifact checks, two credentials successfully decrypted.
- Zero production database writes or external-provider requests. This is key-copy
  and archive validation, not a new database restoration drill or a new backup.

The noninteractive Docker verification completed with exit 0 after the interactive
stdin diagnostic timed out. Container inspection also reported all seven services
healthy and release source hashes matched in the API, worker and scheduler worker.
Public assertions separately confirmed preserved body, reviews, policy and pauses.

The mobile correction was deployed once from `a8f58dc`. Public browser verification
at 17:14 UTC passed: real author dropdown, no automatic author choice, Graph card
visible, test-send disabled, digest off, and no 390px horizontal overflow on the
article or Connections page. Coolify history loading remains intermittent; no
duplicate deployment was submitted after local navigation timeouts.

`forgeseo@eit.care` is the owner's intended sender, not a proven connected mailbox.
No test recipient has been approved yet, and no email was sent. Remaining owner
decisions: mailbox/Entra setup, recipient and receipt, and article author selection.
