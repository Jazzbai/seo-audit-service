# Standalone platform publication

This branch preserves the original repository at its existing root and adds
the standalone platform exclusively under `platform/`. The parent legacy
commit is `91a5b09a4b8cc973133736be41c53729ba17d95d`.

The platform snapshot includes the local 0.1.0-rc.1 candidate plus reviewed
Responses-provider parsing and split-host/Coolify deployment corrections.
It does not include the private local development Git history, credentials,
database files, evidence artifacts, or site-specific operational ledgers.
Published proxy addresses are examples, not an activated installation.

Verification of this publication snapshot: **622 backend tests passed and
28 optional tests skipped**. The skipped real-service/container gates are not
claimed to have run. The prior frontend image build and local deployment checks
are supporting evidence, not proof of a successful remote deployment.

Publication correction: the legacy root's `lib/` ignore rule initially omitted
three frontend source modules. The platform now explicitly includes
`frontend/src/lib/`, and a regression test checks relative imports for missing
files. Always build the published checkout, not only the development workspace.

For installation use [the GitHub/Coolify guide](docs/COOLIFY_GITHUB.md) with Base
Directory `/platform` and the selected deployment branch. Keep automation paused
until certificate trust, access isolation, backup/restore and migration checks
pass. No seven-day unattended pilot acceptance is claimed.
