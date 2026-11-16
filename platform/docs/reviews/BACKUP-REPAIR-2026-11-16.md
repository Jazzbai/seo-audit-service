# Jazzb SEO backup repair — November 16, 2026

## Problem and implementation

The split-host backup service could not validate its current dataset: artifact
files alone occupied 1,563,517,708 bytes, exceeding the old 1,000,000,000-byte
restore bound. A read-only database estimate found 1,216,531 rows and about
499 MB of JSON before further monitoring activity. The old implementation
also collected all database rows and artifact ZIP contents in memory.

Release `320c97a` introduces disk-backed, chunk-authenticated v3 archives and
streamed per-table JSONL. Capture, verification, restore and the restore drill
process rows/files incrementally. Capture and restore enforce the same finite
limits. Older v1/v2 archives still have an explicit compatible recovery path.

Full stored-archive validation precedes atomic publication. An encrypted
verification receipt allows health checks to authenticate the header and
stream the ciphertext checksum without repeatedly decompressing the dataset.
The original encryption keys, daily schedule and freshness policy are retained.
No existing archive was removed to clear the failure.

See [Backup format and recovery design](../BACKUP-FORMAT-3.md) for framing,
limits, receipt handling, compatibility and recovery guards. Fernet is a
whole-message API; its [documented memory constraint](https://cryptography.io/en/latest/fernet/#limitations)
is why the encryption unit is now a bounded record.

## Tests before deployment

- 93 targeted backup, recovery and deployment checks passed; four optional
  gates were skipped in the ordinary run.
- The separately enabled 1.01 GB artifact roundtrip passed, with a guard against
  whole-file reads of large artifacts or encrypted archives.
- The separately enabled PostgreSQL test passed: restore into an internal
  isolated network, exact row/artifact/credential verification, capture from a
  real PostgreSQL repeatable-read snapshot, and refusal to restore again into
  the populated database.
- Tampering, truncation, reordered/missing/duplicate/mixed encrypted records,
  wrong keys, path traversal, unexpected ZIP entries, invalid table manifests,
  insufficient staging space and overwrite attempts are rejected.
- Restore preserves historical data while pausing sites/global controls,
  reconciling pending jobs and invalidating browser sessions.

## Live rollout

Coolify deployment: `njpdyenlz6mnognfqbf22pwl`.
The already-failing backup service was stopped for replacement; the existing
archives and application services were retained during that repair window.

The first live v3 archive was captured at `2026-11-16T20:24:45Z` and passed
full stored-archive validation before publication:

- Archive: `forgeseo-20261116T202445Z-18e3fbcdf7c4421da68e0374f86b7f26.forge`.
- 19 database tables, 1,216,860 rows and 5,973 artifact files.
- Artifact contents: 1,563,517,708 bytes; encrypted archive: 833,951,359 bytes.
- Backup container healthy, zero restarts after deployment. API, ordinary and
  scheduler workers, browser worker, Beat and queue also healthy with zero
  restarts when checked after rollout.
- Daily interval remains 86,400 seconds. Off-host mirroring is **not configured**;
  the existing archive volume is on the application server.

Full-dataset isolated recovery: **passed**. The drill was launched at
`2026-11-16T20:37:47Z`; its completed report and current service health were
retrieved at `2026-11-16T21:13:19Z` (3:13 PM Chicago).

- All 19 backed-up tables and all 5,973 artifact files matched the source,
  including the documented paused/reconciliation transformations.
- All nine saved connection credentials decrypted with the original
  application key. Recovered automations were paused and browser sessions
  invalidated; the drill made zero external/provider requests.
- A second restore into the now-populated test database was refused.
- The drill used the immutable deployed image, a fresh PostgreSQL database on
  an internal Docker network, no published ports and a read-only mount of the
  original backup volume. Production was never a recovery target.
- Its test containers, network and copied-data volumes were removed afterward;
  the original backup volume was preserved.
- All seven production services were still healthy with zero restarts at the
  final check. The newly created archive also passed its authenticated health
  inspection again.

The private machine-readable proof is
`platform/artifacts/backup-repair-2026-11-16/live-final-proof.json`. This verifies
the current archive and recovery path; off-host storage remains unconfigured,
and future captures still depend on available disk capacity and monitoring.

Existing signup remains disabled. H-Town's policy remains version 2 with one
post per week, a $300 monthly budget, Blog child categories only, and protected
main/service/vlog paths. The November 17, 9 AM Chicago article remains scheduled.

Private evidence is retained under
`platform/artifacts/backup-repair-2026-11-16/`; the server-side drill directory is
`/root/backups/jazzb-platform-backup-repair-20261116T202119`.

## Recommended next feature: independent structured-data maintenance

Add a business-facts panel and an automatic schema audit/repair workflow through
the Jazzb connector. On November 16 the H-Town homepage still emitted website
and organization references to `dto.atn.mybluehost.me`. This is a specific
existing inconsistency to fix before adding more markup.

The feature should:

1. Compare the public structured data against verified business facts, current
   domain and canonical page URLs. Explain discrepancies in plain language.
2. Maintain business, website, article, author and breadcrumb data with an
   explicit schema policy and a verified connector capability. Work without
   Yoast/Rank Math; detect existing graphs and avoid duplicate/conflicting data.
3. Use confirmed facts only; hold missing addresses, hours, credentials or other
   business claims for owner review.
4. Validate the rendered graph, preserve visible page content/design, retain a
   snapshot and roll back a failed write.
5. Recheck on the normal audit/change schedule once the site policy permits it.

Customer wording: “Keeps the business details search engines read accurate,
even when your website has no SEO plugin.”

Google documents how [LocalBusiness structured data](https://developers.google.com/search/docs/appearance/structured-data/local-business)
describes business details. Its [AI search guidance](https://developers.google.com/search/docs/appearance/ai-features)
calls for structured data to match visible content and says no special AI schema
is required. This feature would repair factual consistency; ranking or AI
citations still need separate measurement.
