# Local pilot candidate: start here

This is **0.1.0-rc.1**, for local evaluation. It is not permission to switch on
unattended publishing on Auto1StopShop. The existing installation stays separate.

## Open the application

With Docker Desktop running, open PowerShell and run these two lines:

```powershell
cd 'C:\Projects\ForgeSEO\platform'
.\scripts\start-local.ps1
```

Open **http://localhost:18080** in your browser. Keep using `localhost`, not a
different hostname, so the browser session's origin matches the configuration.
Subsequent starts can use `.\scripts\start-local.ps1 -NoBuild` if the code has
not changed. The script checks service health and preserves existing data.

On your first visit:

1. Open `.env.local-candidate` in the platform directory in your editor.
2. Copy **only the value after `BOOTSTRAP_TOKEN=`** into **Deployment setup token**.
3. Enter your name, workspace, email, and a password you choose. Click **Create workspace**.
4. Keep the private file safe. It contains encryption and database keys; do not
   post its contents in chat, delete it, or replace it when restarting.

On later visits, sign in with your chosen email/password. There is no shared
default account. WordPress credentials belong in the application's connection
form, **not** in this environment file. Routine work needs no PowerShell/API tokens.

The launcher reuses this repository's Compose services/build cache with separate
`forgeseo-platform-local` volumes. HTTP is bound to loopback, all writes start
paused, and no existing smoke/legacy project is stopped or replaced. It does not
copy Auto1StopShop credentials or import permissions. This local setup does not
run while Docker/the computer is off and is not the always-on production deployment.

## What to try

- Create a site, enter business facts and source URLs, then save/test its connection.
- Use **Run full cycle** to inventory, audit, plan content, and evaluate refreshes.
- Review Issues, Pages, the Content calendar, and Activity. Partial/stale coverage
  and missing connections are meaningful states, not signs the site is optimized.
- Keep live-site pauses on. Enable publishing only on an authorized test site with
  a verified author and explicit policy. Save a draft with sources, run **Check**,
  publish, inspect verification, and use **Roll back publication** to restore a draft.
- Automated research/generation requires a configured provider and pricing. The
  local release acceptance tests do not incur paid usage or certify article quality.

Production connectors deliberately reject private-network WordPress origins.
The isolated test command below uses a test-only allowlist; do not disable the
production network checks to connect a localhost WordPress instance.

## Replay the isolated demonstration

Requires the repository's Python virtual environment, Node/npm, Docker, and Chromium.
The current workspace already has these development dependencies installed.
For a new checkout: create `.venv` with Python 3.12; install
`pip install -c requirements.lock -r requirements.txt`; run `npm ci` and
`npx playwright install chromium` in `frontend`.

```powershell
cd 'C:\Projects\ForgeSEO\platform\frontend'
npm run test:pilot
```

This opens a disposable platform database and uses **real isolated WordPress and
WooCommerce containers** on ports 18090/18091. It onboards both sites through the
UI, tests connections, inventories/audits/plans, saves policy, checks and publishes
a manually authored fixture article on each, verifies HTTP content, and restores
each article to draft. It never uses live-site credentials or paid providers.
Its SQLite/in-process queue and source-HTML audit are explicit test substitutions;
PostgreSQL concurrency, Docker workers, and browser-rendering checks have separate gates.
Do not run multiple fixture suites simultaneously against these same ports.
Ports 4173 and 18082 must also be available. Fixture volumes retain prior test data;
larger catalogs legitimately use audit continuations. No live installation is modified.

## Stop or inspect

From the `platform` directory:

```powershell
docker compose --env-file .env.local-candidate -p forgeseo-platform-local ps
docker compose --env-file .env.local-candidate -p forgeseo-platform-local stop
```

Stopping preserves volumes and does not stop any other Compose project. If startup
fails, inspect `ps` and service logs, fix the reported cause, then rerun the launcher.
Do not use `down -v` as a repair step. See [operations](OPERATIONS.md) for backups;
production/off-site restore certification remains a separate gate.

For the release evidence and outstanding work, read [candidate acceptance](CANDIDATE_ACCEPTANCE.md).
