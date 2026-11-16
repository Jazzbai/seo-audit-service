# H-Town independent metadata automation — November 16, 2026

## Scope and outcome

H-Town Star Family Dentistry now has Jazzb SEO Connector 0.5.0 installed and
active. The connector stores managed SEO titles and descriptions in its own
WordPress fields. Yoast and Rank Math are not required. On H-Town, the existing
Yoast installation remains active and its supported output filter is used to
avoid duplicate tags. Native output without an SEO plugin passed separate live
WordPress tests.

Installation is a one-time connection prerequisite. Subsequent eligible metadata
suggestions can be authorized by the site's existing metadata policy, queued by
audits, applied by the normal worker, and checked against the public page. This
does not automate arbitrary theme changes, schema, redirects, robots or sitemaps.

Actions submitted during this activation target H-Town only. No connector was
installed on Auto1Stop or any other customer site.

## Verified changes to H-Town

Forty-two real policy-authorized metadata updates completed during activation.
Thirty ran from the governed cycle and its audit continuations. After the page
route correction, release verification requested five fresh targeted audits
and invoked the regular policy gate for two further batches (10 + 2 updates).
Those updates still used the ordinary candidate jobs, source locks and public
verification; the release-verification trigger was operator initiated.
The following two descriptions were also independently checked in a public
Chrome context:

| WordPress post | Page | Result |
| --- | --- | --- |
| 992850 | [The Mouth-Body Connection](https://htownstarfamilydentistry.com/blog/the-mouth-body-connection-how-healthy-teeth-boost-your-whole-body-wellness/) | HTTP 200; one expected description; worker protected-drift check empty |
| 992869 | [Confident Smile, Confident Life](https://htownstarfamilydentistry.com/uncategorized/confident-smile-confident-life-how-healthy-teeth-improve-self-esteem-relationships/) | HTTP 200; one expected description; worker protected-drift check empty |

These are metadata changes to existing resources, not new publications or
category changes. An authoritative WordPress comparison of all 260 published
posts/pages after all 42 updates found no added, missing or changed native
bodies, titles, authors, slugs or category assignments. The two rows above are
independently checked examples; the private evidence includes the full job
results for the additional page changes.

A temporary noindex/nofollow Blog → Patient Education post verified a metadata
apply and metadata-only rollback while retaining native content and layout.
Temporary posts 994309 and 994311 were deleted after the checks. No permanent
article was created during this activation.

## Platform fixes

- Package the independent connector, capability declaration and installation
  instructions (`4e93133`).
- Verify metadata around recommendation cards that WordPress demonstrably
  rotates between read-only requests. Learn the exact permitted regions before
  writing; preserve surrounding attributes and reject other HTML drift
  (`cf7a822`). This changes verification only, not the served theme or content.
- Scan past blocked archive suggestions instead of allowing the first ten
  entries to starve eligible metadata. Keep the write batch at ten and the scan
  bounded at 1,000 (`6fd0103`).
- Hold known stale suggestions before queueing; protect normalized native
  attribution and reject context-dependent description fragments (`7715cac`).
- Queue/execute only resource types with implemented metadata writers:
  posts, pages, products and supported commerce categories (`2e21856`).
- Honor the packaged connector's explicit `seo_resource_types` declaration for
  its shared post/page metadata endpoint. A posts-only or malformed declaration
  still cannot authorize page writes (`e51c68d`).

The first live cycle exposed four author-archive suggestions that were incorrectly
queued before the last filter fix. The connector refused the unsupported resource
before any write. Two failed jobs were retained and two waiting jobs cancelled.
All four suggestions were returned to `review_needed` with the explicit
`verified_writable_resource_required` reason and a scoped operator audit event.
They were not marked applied, and no author pages were changed.

The live cycle also exposed the shared page-route mismatch on five existing
pages (eight suggestions). Those writes were refused before changing metadata.
Their original source hashes were independently verified unchanged after the
deployment. Fresh targeted audits and policy authorization produced replacement
changes, which passed real worker and public-page verification. The eight old
suggestions are now stale because their source hashes have been superseded by
verified changes. Their failed publication/job history remains intact. The two
release-verification batches also picked up other eligible page fields already
in the queue; all 12 updates completed successfully.

Two article updates encountered a recommendation widget that did not rotate
in the initial pair of baseline reads, then changed later. The public guard
refused to certify the result. Independent read-only checks confirmed that
both selected metadata values had been restored, native content was unchanged,
and the remaining HTML difference was confined to strict recommendation-card
regions. These checks do not retroactively broaden the pre-write profile or
mark the failed jobs successful. The two suggestions remain held for review.
Fresh read-only checks also confirmed their public descriptions match the
original empty values. Their rollback alarms were manually resolved with this
evidence; the jobs, candidates and publication attempts retain failed status.

## Safeguards and recovery

The existing site policy remains version 2: one post per week, $300 monthly
budget, Blog-child publication categories only, and protected home, service,
Vlog, contact, legal and account paths. The reviewed treatment-estimate article
remains scheduled for November 17 at 9:00 AM America/Chicago. No policy expansion
or schedule reset was needed. Signup remains disabled.

WordPress installation backup and connector-only rollback are on wp-14 at
`/root/backups/jazzb-htown-connector-20261116T172811`. The existing compressed
database backup passed gzip integrity; a database restore was not performed.
The connector ZIP SHA-256 is
`6ca1b341b38e5c9e66efd5ccbb862673b89ec2237f9f3c895fe454af24251744`.

The final resource-filter regression run passed 122 tests. Earlier validation
also passed live no-SEO-plugin WordPress apply/restore and candidate-workflow
tests, connector regressions and PHP syntax checks. Repeated suites are not
summed as unique test coverage.

The page-route correction additionally passed a 53-test targeted regression
suite and three live WordPress checks: native post metadata, native page
metadata, and the actual platform candidate workflow, all without a third-party
SEO plugin. The page fixture uses an explicit draft slug so publication does
not introduce WordPress's unrelated automatic slug change.

## Deployment and remaining operational work

The resource-filter release `2e21856` completed in Coolify deployment
`vcmokhc9ilo0ytunr2dcsprs`; the final page-route release `e51c68d` completed in
`bz36bzssdrbqstttuiu6rnqb`. API, queue, Beat and all three worker families were
healthy after startup. A fresh scheduler heartbeat and disabled signup were
verified. Workers initially retried broker connections during restart;
release-verification writes were held until the health checks passed.

Two article suggestions remain held by public HTML verification. Their original
descriptions were restored and independently checked; improving cache-aware
recommendation baselines is a remaining automation refinement.

The platform encrypted-backup service is a separate unresolved operational
issue. It restarted with a validation `ValueError`. Its backup key was present,
valid and independent of the application key. The artifact tree contained
5,973 regular files totaling 1,563,517,708 bytes with no symlinks; this already
exceeds the backup/restore implementation's 1,000,000,000-byte safety limit,
before database manifest data is included. This explains a concrete validation
barrier; no successful current encrypted platform archive was verified. Do not
treat container startup as backup certification. Fix the bounded backup/restore
design and test a full restore before expanding the unattended rollout.

The separate H-Town WordPress database backup and connector rollback remain
available at the locations recorded above.

## Evidence and interpretation

Private local evidence is under
`platform/artifacts/htown-metadata-activation-2026-11-16/`, including connection
verification, deployment IDs, the governed cycle, worker job results, public
checks, native integrity and author-suggestion reconciliation. The governed
cycle ID is `f2947428e5a8436eacd4eddacfc2e7f2`.

In the dashboard, select H-Town and open **Publications** for verified mutation
receipts or **Activity** for `candidate_applied` events. **Jobs** retains the
original failed attempts as well as their successful replacement work. Existing
failed history does not mean the same change is waiting to be retried.

Audits may continue in batches and held suggestions still need valid sources,
editorial quality and a supported writer. A completed metadata write establishes
technical correctness, not a search ranking, traffic or LLM visibility gain.
Those effects require subsequent Google and visibility measurements.
