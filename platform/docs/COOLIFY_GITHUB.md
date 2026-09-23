# Deploy this branch with Coolify

This branch adds the standalone platform in `platform/` without replacing the
original application at the repository root. Do not deploy the root legacy
Dockerfile or docker-compose.yml when installing this platform.

Create two Git-backed **Applications** using the **Docker Compose** build pack:

| Setting | Frontend | Backend |
| --- | --- | --- |
| Repository | `https://github.com/Jazzbai/seo-audit-service` | Same repository |
| Branch | `platform-deployment` | `platform-deployment` |
| Base Directory | `/platform` | `/platform` |
| Compose Location | `/compose.frontend.yaml` | `/compose.backend.yaml` |
| Server | Selected frontend server | Selected backend server |
| Raw Compose Deployment | Enabled | Enabled |
| Inject Build Args to Dockerfile | Disabled | Disabled |
| Domains | Blank when using an existing external Caddy | Blank |
| Auto Deploy / previews | Disabled during verification | Disabled during verification |
| Connect To Predefined Network | Disabled | Disabled |

Do not select **Docker Compose Empty**: it creates a service without a Git
source. The repository branch must be pushed and readable before these
applications can load their definitions. Keep the same resource IDs on later
redeployments to retain the same resource-scoped persistent volumes.

## Private runtime configuration

Backend runtime variables (not build-time arguments):

- `FORGE_HOSTNAME`: the platform's public HTTPS hostname.
- `API_BIND_IP`: backend private IPv4 address; restrict inbound API access to
  the frontend proxy with a verified network/host firewall.
- `TRUSTED_PROXY_IP`: the actual private IPv4 source of the existing proxy.
- `API_PORT`: an unused private host port, default `18001`.
- `DATABASE_URL`: SQLAlchemy `postgresql+psycopg` URL for a dedicated PostgreSQL
  database/role, URL-encoded password, `sslmode=verify-full`, and
  `sslrootcert=/run/forgeseo/db-ca.crt`. If needed, a certificate-matching DNS
  hostname can be combined with a private `hostaddr` parameter.
- `QUEUE_PASSWORD`, `ENCRYPTION_KEY`, `BOOTSTRAP_TOKEN`: independent, private
  secrets satisfying the existing 32-byte minimum. Queue passwords use URL-safe
  letters, numbers, underscores and hyphens. The database password must also
  satisfy preflight. Preserve encryption keys securely for recovery.

Install the verified public database CA certificate at
`/etc/forgeseo/db-ca.crt` on the backend host, readable by container UID 10001,
**before** creating/deploying the backend resource. Never copy a database
private key. Coolify may prepare absent bind paths as directories; check this
exact file first. TLS encryption without certificate validation is not enough.

Frontend has no secrets. `FRONTEND_PORT` defaults to `18080`, bound to host
loopback only. The static image listens on container port `8080`. It neither
binds public ports 80/443 nor starts a second HTTPS proxy. An existing host
Caddy can use the additive example in `deploy/split/Caddyfile.edge.example`;
replace its documentation hostname and illustrative private address. Do not
replace a shared Caddyfile. Validate the complete existing configuration before
a graceful reload and verify existing applications afterward.

The private API hop is HTTP; require an isolated trusted network or add an
encrypted tunnel/internal TLS. Docker-published ports need verified firewall
coverage, not an assumption that a private bind address is an access list.

## Build and activation boundaries

The Compose files render/build without runtime secrets. Blank build values
cannot activate the app: startup preflight gates migration, broker and workers.
Global pause stays enabled; the `monitoring` profile is not enabled by default.
The migration container must exit 0. Its successful exit is not an app crash.

After verification, use Deploy on each existing resource to deploy the selected
Git commit. Verify image IDs and health, and coordinate frontend/API versions.
Enable automatic push deployments only after manual redeployment succeeds.
Never delete volumes as part of redeployment. An image rollback does not reverse
a schema migration: retain a tested encrypted backup/restore plan.

See [readiness](READINESS.md) and [operations](OPERATIONS.md). No production
installation or seven-day pilot success is implied by these source files.

References: [Coolify Git-backed Compose](https://coolify.io/docs/applications/builds/docker-compose),
[PostgreSQL TLS](https://www.postgresql.org/docs/16/libpq-ssl.html).
