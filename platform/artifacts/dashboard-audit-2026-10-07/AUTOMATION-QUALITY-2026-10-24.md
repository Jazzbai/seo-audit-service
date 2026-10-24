# Auto1Stop automation follow-up — October 24, 2026

## Local-only follow-up while VPN is unavailable

At the owner's request, further work stays local: no deployment, remote push, paid provider call, WordPress write or Google listing change in this follow-up. The prior release `9b90cfb` remains queued for deployment.

- Hardened topic-source validation: malformed URLs, non-HTTP URLs, URLs with embedded credentials and the site's `www` homepage alias do not pass the minimum research guard. Valid topic-page references still pass; URL shape alone is not proof of adequate research.
- Service suggestions collapse whitespace and case-insensitive duplicates without changing saved business facts. Duplicate source URLs are removed, while unrelated and non-HTTPS sources remain excluded.
- The service-review card now exposes source links and explains their business-published evidence limitation. Missing facts link directly to Business settings. Component state resets for a new site so old suggestions cannot be reused by that card.
- Full backend suite after these changes: **909 passed, 30 skipped**. Final full local Playwright UI suite: **177 passed**. Frontend production build passed with the existing bundle-size warning. All browser checks use local/mocked data; these are not live deployment acceptance results.

Remaining Google management authorization, listing writes, backup/restore evidence, bill reconciliation and deployment are not supplied by local tests.

## Release state

Development is tested locally, not deployed. Production was still at `13b5866` when checked. Coolify `10.0.1.12:8000` timed out in controlled Chrome and two TCP checks failed; VPN/private-network access is required before deployment. Do not confuse the live settings changes below with the pending code release.

## Live ForgeSEO settings and actions completed

- Auto1Stop is unpaused; H-Town Star Family Dentistry remains paused. Existing one-post-per-week Friday 9 AM America/Chicago publication window, author 5, protected paths and $300 monthly ceiling were preserved.
- Recorded five services supported by the published business website, Houston location and matching service source URLs: collision/auto body repair, car painting, bumper repair, scratch repair and insurance claim assistance. These are business-published claims, not independent certification of capabilities.
- Corrected Search Console collection property to `https://auto1stopshop.com/`, matching the public canonical destination. Read-only access test `b2d3bf50d3f54c3e83bf631c76d6fa9f` passed; real collection `a805095d1c8a4abdbabe456368723e6d` completed.
- Added two local discovery questions to policy version 6 and aligned AI connection questions with them. Read-only AI connection test `427dc8394a384173ab147723ca91fa6c` passed.
- Collected the two-question AI visibility baseline: job `d309b75b606d4876ab5aa8807bf67a99`, measurement `3b5538e71175483f9c8c0a692ad604ec`. Neither response mentioned Auto1Stop or cited its domain. This is one OpenAI model/web-grounded sample, not a universal LLM ranking. An earlier scheduled one-question sample used the old research prompt and is not this baseline.
- Held nine legacy keyword-only planned articles for editorial/business-fit review. No articles were deleted or approved automatically.
- Created source-backed planned article `b370875c1c3d423582d50f43fcc2c89e`: “Scratch touch-up or panel repaint? Questions to ask a Houston body shop.” It has not been generated or published; next scheduled execution still needs verification.
- October budget ledger readback: $300 limit, $2.50 reserved, $0 settled. Settled zero is NOT evidence of zero provider charges. Actual costs remain unreconciled; estimates were not substituted for bills.

## ForgeSEO code changes awaiting deployment

1. Visibility measurements are filtered by source before pagination, excluding uptime samples by default. Added source picker and previous/next pagination so older organic/analytics/AI observations remain reachable.
2. Failed Search Console/GA4 collection updates connection readiness instead of leaving a stale successful check visible. Policy questions override stale connection research prompts for AI sampling.
3. Draft instructions require safe HTML fragments with no H1/document wrappers. A leading attribute-free plain-text H1 can be demoted to H2 without another paid request; other markup still goes through checks, and this repair does not approve publication.
4. Automatic article selection holds availability/emergency claims and legal topics for review. Empty or homepage-only research stops before paid drafting. This is a minimum quality guard, not proof that every non-homepage source is adequate.
5. Alt-text auditing excludes explicit 1×1 images and Facebook tracking endpoints, while retaining real image checks. Existing historical findings will need a fresh audit; they were not bulk dismissed.
6. Every site's Connections page gains a Google Business Profile service-review card, and planning emits a local-service review action. Suggestions use that site's saved facts/sources. Eligibility, correct location, manager access and existing-service preservation remain explicit prerequisites.

Google Business Profile remote writes are **not implemented** in this release. The action explicitly reports `remote_write_supported: false`; it does not pretend Search Console authorization grants Business Profile management. Full automation still needs a supported Google management integration, separate authorization and safe existing-service merge/readback handling. See [Google's services API guide](https://developers.google.com/my-business/content/services): a service update replaces the `serviceItems` array, so preserving existing entries matters.

## Actual public website and Google listing changes

No WordPress article, main-page content, theme, layout, navigation or metadata was changed during this follow-up. No Google Business Profile services were changed. Friday October 23's draft remains unpublished after its markup failure; historical totals of one published article and one applied metadata change are not new work from today.

The new settings, source-backed brief and visibility baseline therefore cannot yet demonstrate an SEO or LLM visibility improvement. Check subsequent real collections and published changes over time; do not attribute historical organic fluctuations to this pending release.

## Verification

- Full backend suite: **903 passed, 30 skipped**. Skipped tests do not establish live infrastructure or provider acceptance.
- Targeted Playwright UI checks: **8 passed**, including visibility pagination, manual service review, readiness controls and OAuth return states. These use mocked APIs, not a production deployment.
- Frontend production build passed; existing bundle-size warning remains.
- Plugin discovery confirmed no connected Google Business Profile writer was available. Official OpenAI documentation informed draft-format constraints; no model/pricing migration was performed.

## Remaining work

- Restore private-network/VPN access; deploy backend and frontend, verify release identifiers and exercise the new screens against production. Do not report a push alone as deployment.
- Verify the next scheduled article runs with topic evidence, passes checks, respects protected paths and records any actual publication. No forced extra paid generation/publication was used as a shortcut.
- Manually review the matching Auto1Stop Google listing in normal Chrome; add only missing confirmed services, preserve existing services and verify the saved list. User requested screenshot-guided steps after development because Google blocked login in controlled Chrome.
- Implement separately authorized Business Profile management if unattended Google service synchronization is required; current release supplies review actions only.
- Reconcile provider bills; verify isolated backup restore and publication rollback. These remain unproven, not silently accepted.
- Run a fresh audit after deployment to check tracking-pixel findings and continue measuring organic search and AI visibility. A first two-question sample is a baseline, not a completed visibility program.

## Manual Google Business Profile handoff

Use normal Chrome with the Google account that manages Auto1Stop. Open `https://business.google.com/locations`, select the matching business/address and open **Edit services**. Before editing, send a screenshot of existing service categories and items so duplicates and wrong categories can be avoided. Candidate names, supported by the website: Collision and auto body repair; Car painting; Bumper repair; Scratch repair; Insurance claim assistance. Do not add unverified prices, hours, certifications or guarantees.
