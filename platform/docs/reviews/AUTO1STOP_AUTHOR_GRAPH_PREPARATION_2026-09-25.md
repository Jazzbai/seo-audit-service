# Controlled pilot: current authors and Microsoft 365

Status: implementation verification in progress; **NO-GO for live publication**.
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
| Deploy and verify this release | Local verification passed; deployment pending |
| Fresh real-site author lookup | Pending deployed endpoint verification; old inventory is not current authority |
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
