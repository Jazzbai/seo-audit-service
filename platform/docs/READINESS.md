# Platform readiness

This is a development candidate, not a completed unattended production pilot.
See [local candidate acceptance](CANDIDATE_ACCEPTANCE.md) for the historical
local release checks and [operations](OPERATIONS.md) for recovery procedures.

The publication snapshot includes the standalone application, tests, dependency
locks, license notices, and split-server deployment definitions. Site-specific
operational ledgers, credentials, database records, evidence artifacts, and
private infrastructure notes are deliberately not published.

Current boundaries:

- New installations start globally paused. Split-server schedules additionally
  require an explicit monitoring profile.
- Paid connectors require actual credentials and budget authorization. Mocked
  tests do not prove successful paid-provider access or editorial quality.
- Split-host HTTPS, database certificate trust, runtime credentials, firewall
  restrictions, encrypted backup/restore and scheduler ownership must be
  verified in the selected environment before production acceptance.
- A seven-day unattended pilot requires seven real days of evidence. It has
  not been completed by the local release tests.

The split-host backup profile and isolated PostgreSQL recovery drill now have
local synthetic integration coverage. Remote archive creation, original-key
recovery, off-host retention, and restoration on the selected deployment remain
acceptance work. See [split recovery](SPLIT_RECOVERY.md).

Use the [Coolify guide](COOLIFY_GITHUB.md) for Git-based installation. Do not run
the single-host deployment gate against the split-server Compose definitions.
