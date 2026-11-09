# Closed account creation

The owner requested that Jazzb SEO remain a single-user pilot and that new-user registration be disabled completely. No existing users, passwords, sessions or memberships were deleted.

Code release: `a395f37`. Both production deployments finished successfully and the live API restriction is verified.

## Behavior

- Server setting `ACCOUNT_CREATION_ENABLED` defaults to `false`, including both deployment Compose definitions and the example environment.
- With that setting off, `POST /api/v1/auth/bootstrap` returns 403 before checking the setup token or creating users, teams, memberships or sessions. Even a valid first-owner token cannot bypass the gate.
- `POST /api/v1/team/members` is also blocked for an authenticated owner. The gate covers creation of a new user and attachment of an existing user to the team.
- Existing users can still log in and use existing sessions. Existing role checks, CSRF protection, last-owner protection and team-scoped access remain active.
- The login page no longer links to first-owner setup. Direct `/bootstrap` visits redirect to login unless initial setup has been explicitly enabled by an operator and the database is uninitialized.
- Team settings replace the account-creation form with a closed-access notice. An absent or unreadable creation flag does not expose a form.
- There is no dashboard control for reopening account creation. An operator would have to explicitly change the server setting and redeploy. Do not enable it during the closed pilot.
- Disposable local test harnesses explicitly opt in only to exercise initial setup and existing role/CSRF contracts.

## Local validation

- Frontend production build passed (existing bundle-size warning).
- 63 Playwright cases passed, including direct setup URL protection with initialized/uninitialized databases, missing/false flags, existing owner login, mobile/accessibility checks and owner team-form suppression.
- 142 selected backend/deployment tests passed, 2 skipped. New cases verify the closed default, valid/missing/wrong setup-token rejection, unchanged user/team/membership counts, existing session/password login, and blocking both new-user and existing-user team additions.

## Production deployments

- Backend: Coolify `jzfwcwferlp1yjsarcayuhyu`.
- Frontend: Coolify `9seigjm4ayemf37rz2rgb7kz`.

## Live verification

- `/health` returned HTTP 200 after the restart. The API briefly returned 502 during replacement, then recovered.
- `/api/v1/auth/status` returned `initialized: true` and `account_creation_enabled: false`.
- Anonymous `POST /api/v1/auth/bootstrap` returned HTTP 403, `New account creation is disabled`.
- Authenticated owner `POST /api/v1/team/members`, with a valid session and CSRF token, returned the same HTTP 403 and explanation.
- Probe payloads deliberately used empty email/name fields. They pass body-shape validation so the gate is exercised, but could not create a real account even if the flag were misconfigured. No temporary live account was created.
- The roster contained one member before and after the probes; that member matched the current signed-in user. `/auth/me` remained available through the existing session. No password or token was requested from the owner.
- A fresh anonymous browser visiting `/bootstrap` reached `/login`, displaying the Jazzb SEO sign-in heading, with no account-creation link or setup-token field.
- The live Team page displayed the closed-access notice, with no Add member button or temporary-password field.
- Existing-user password login with account creation disabled is covered by the isolated backend tests; the live owner was not signed out for verification.
