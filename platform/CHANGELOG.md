# Changelog

## 0.1.0-rc.1 — 2026-09-23

Local pilot candidate, not an unattended production release.

- Integrated complete-collection WordPress/WooCommerce inventory handling;
  truncated or inconsistent reads fail visibly without discarding prior evidence.
- Fixed article editing to preserve research and structured sources and record
  authenticated editor provenance without bypassing editorial or policy checks.
- Added a real browser-to-isolated-WordPress/WooCommerce acceptance journey,
  following planner-created briefs through publication, public verification and rollback.
- Added a private-key-preserving local launcher, loopback-only access, dependency
  constraints, pinned application base images and a consistent candidate version.
- Added [launch/login instructions](docs/LOCAL_QUICKSTART.md) and an
  [acceptance matrix](docs/CANDIDATE_ACCEPTANCE.md) with verified results,
  explicit test substitutions, unfinished capabilities and external gates.

Live-site publishing, paid-provider acceptance and the seven-day unattended pilot
remain separate subsequent goals. No live site was changed for this release.
