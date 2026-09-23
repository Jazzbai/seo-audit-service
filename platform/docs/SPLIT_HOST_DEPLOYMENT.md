# Split-host deployment

Use [COOLIFY_GITHUB.md](COOLIFY_GITHUB.md) for this branch's source path, build
pack, runtime settings and deployment safeguards.

Topology: existing edge proxy and static frontend on one host; API, private
queue, workers and persistent artifacts on a backend host; a dedicated database
and role on an external PostgreSQL server. The templates are not merged with
the single-host `compose.yaml`.

Before activation verify certificate identity, required host files, unused
ports, firewall isolation, encrypted backups/restoration, paused defaults and
exclusive schedule ownership. Existing live applications and shared proxy
configuration must be preserved. Local test evidence is not production proof.
