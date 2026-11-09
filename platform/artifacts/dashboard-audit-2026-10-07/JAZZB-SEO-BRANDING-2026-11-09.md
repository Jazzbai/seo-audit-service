# Jazzb SEO dashboard branding

Date: November 9, 2026. The owner selected `seo.jazzb.com` as the intended public hostname and requested removal of ForgeSEO branding from the dashboard. The dashboard now uses **Jazzb SEO**.

## Release

- Frontend code: `b60ce80` (Rename customer dashboard to Jazzb SEO).
- Coolify frontend deployment: `6qejf6k9wunckjjd64bqrw58`, finished successfully on `ui-13`.
- Production serves `/assets/index-Cfr34vUS.js`; the document title is `Jazzb SEO` and the root returns HTTP 200.
- Backend code was not changed or redeployed for this dashboard-only release.

## Changes

- Desktop sidebar, mobile header, login/bootstrap branding and site onboarding.
- Browser title and description.
- Connection guidance, calendar and article messages, overview copy, store guidance, mailbox-confirmation copy and site-management prompts.
- Weekly CSV downloads now use `jazzb-seo-weekly-report.csv`.
- Migration guidance uses a neutral historical-platform description.
- Existing authentication headers, connector capability keys, route namespaces and deployment identifiers retain their compatibility contracts. Historical records were not rewritten.

## Verification

- Production build passed; the existing JavaScript bundle-size warning remains.
- The five affected Playwright suites ran 72 cases: 71 passed initially. The audit-progress completion assertion timed out in the parallel run, then passed on an isolated rerun. No audit-workflow code or assertion timeout was changed to mask this.
- Live authenticated Auto1Stop overview: Jazzb SEO document title and sidebar, no old product name in rendered text, no JavaScript page errors.
- Live mobile viewport: Jazzb SEO header, no document overflow, navigation drawer reached its settled position at x=0. Screenshot taken after its existing animation completed.
- Live Connections page: nine connection cards rendered; no old product name in rendered text and no JavaScript page errors. An initial check incorrectly looked for a Google card heading; these card titles are not heading elements. The subsequent card/text readback passed.
- Live unauthenticated login: Jazzb SEO title, brand and sign-in heading; no old product name in rendered text.
- Local evidence (not pushed): `jazzb-seo-desktop-2026-11-09.png`, `jazzb-seo-mobile-2026-11-09.png`, `jazzb-seo-login-2026-11-09.png`.

No WordPress content, site policies, provider grants or Google publishing status were changed by this release.

## Next launch stage

The selected hostname is authorization to prepare the Jazzb SEO launch; it is not evidence that DNS or Google verification has already changed.

1. Prepare a public product homepage, app-specific privacy policy, terms and support information. Proposed locations on the selected hostname are `/`, `/privacy`, `/terms` and `/support`; `/login` and existing authenticated routes remain the dashboard entry points. The current root still redirects to the dashboard/sign-in and is not yet the required public product homepage.
2. Confirm the actual operating company and monitored support identity. Disclose the implemented Google-data access, storage, sharing, retention, revocation and deletion behavior accurately. Review existing Jazzb terms for customer publication rights.
3. Complete remaining public app branding, including backend email templates/API metadata and the Google consent-screen name, in that launch stage. This frontend release does not change the name shown on Google's consent screen.
4. Configure DNS **A record `seo` -> `139.138.153.13`** under `jazzb.com`. No DNS record was changed today.
5. Add the hostname to the existing edge routing, obtain HTTPS and verify both static assets and backend API routing before switching users.
6. Set the backend public origin to `https://seo.jazzb.com` and register the exact callback `https://seo.jazzb.com/api/v1/oauth/google/callback`. Preserve the working old callback during migration until the new flow passes.
7. Verify `jazzb.com` ownership in Search Console using an account associated with the Cloud project; complete authorized domains, branding and data-access verification for the current read-only Google scopes. Publishing, brand verification and sensitive-scope verification are distinct statuses.
8. Obtain fresh Google grants after the transition and verify actual GSC/GA4 collections. Continue the existing unattended publication acceptance work separately.

No new Google Cloud project or API key is required for each customer.
