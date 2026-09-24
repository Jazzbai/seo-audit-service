# Auto1StopShop controlled-pilot preparation

Status: **in progress; live publication is not authorized**. This goal prepares
one retained article, not a seven-day pilot or a general automation rollout.

## Scope and baseline

- Site: `b9b9b32f9bba41949f93a7e3c103fde4`, `https://auto1stopshop.com`.
- Retained article: `b1f0fb8a5cb647998fefc7c3b166f727`, **Before Your Body Shop
  Estimate: Photos and Information to Gather**. Intended slug:
  `before-your-body-shop-estimate-photos-and-information-to-gather`.
- Read-only production inspection at 22:46 UTC: body 3,418 characters, no remote
  post ID, no schedule, zero publications. Original body SHA-256:
  `8fc07e7410b64a5a7c5e1cc6e61bb47b10a79fa592bc2bde8cb35a6d0536019e`.
- Site/global pauses true; policy v2 disabled, no allowed actions. All existing
  page records are unenrolled. Elementor constraints remain active.
- No new paid request or WordPress mutation is authorized by preparation.

## Source and editorial review

The existing text was read against these current primary pages on 2026-09-24:

| Source | What it supports | Limits |
| --- | --- | --- |
| [FTC Auto Repair Basics](https://consumer.ftc.gov/articles/0211-auto-repair-basics) | Written-estimate guidance and clarification of additional-work approval | Consumer guidance, not a claim about Texas law, coverage or this shop's process |
| [Progressive Guided Photo](https://www.progressive.com/claims/faq/guided-photo/) | Its invitation-based app submission and subsequent insurer review | Only that insurer's workflow; no claim that Auto1StopShop uses it |
| [Auto1StopShop contact page](https://auto1stopshop.com/contact-us/) | A valid internal destination for asking the shop about intake | No inference that photographs replace inspection or that a particular intake service is offered |

General preparation suggestions are clearly distinguished from sourced rules.
The draft makes no price, timing, coverage, safety-to-drive, certification or
partnership promise. It contains no images needing a new license. The retained
provider history and metering must remain unchanged.

Focused overlap review also inspected retained headings and relevant paragraphs
from posts 1384 (repair costs), 1389 (accident-response checklist) and 1511
(insurance supplements). The preparation-before-contact angle is distinct from
those topics. Older posts contain inconsistent phone/location claims; those are
not copied into this article. This was a focused comparison, not a claim to have
audited all duplication or repaired the older content.

The original source flag could not legitimately be cleared through the old UI:
it was permanently attached to protected provider provenance. The prepared fix
adds a separate authenticated, revision-bound source review with reviewer/time,
notes, public-HTML fetch evidence and a SHA-256. It never deletes the model's
original flag or accepts a model confidence score as approval. Ordinary article
creation/editing cannot forge these records. Reviews expire after seven days and
are invalidated by title, body, source-list or generation changes.

## Narrow policy staged while paused

Policy **version 3** was saved at approximately 23:10 UTC with `enabled=false`,
`allowed_actions=[]`, site pause and global pause true, and:

- `publication_article_ids=["b1f0fb8a5cb647998fefc7c3b166f727"]`;
- `posts_per_week=1`, `refreshes_per_week=0`, `publish_days=[]`;
- no tracked questions/keywords/competitors or new paid-work permission;
- the current $300 monthly ceiling retained as a ceiling, not new authorization;
- a real author selected only after owner direction;
- protected `/`, `/services*`, `/contact*`, `/landing-page*`, `/blog*`,
  `/privacy*`, `/terms*`, `/checkout*`, `/cart*`, `/my-account*`, `/wp-*`,
  `/author*` and `/category*`.

An explicit future launch would change only the approved publication action,
enabled state and relevant pauses after a fresh check. It is **not** part of this
goal. Metadata, existing-body refresh, store writes, links and alt text remain
disabled. Restricted policies prevent automatic creation-and-publication cycles
and reject every unselected article at write time, including already queued work.
No existing pages are enrolled or builder layouts edited.

## Open launch checklist

| Requirement | Current evidence | State |
| --- | --- | --- |
| Retain one real draft | Production record and body read at 22:46 UTC | Proven |
| Genuine author approved | WordPress account 1 is authenticated; accounts 3 and 4 are also in inventory; owner choice requested | Needs owner input |
| Source review recorded in application | Three actual public-HTML fetches and authenticated reviews recorded on the retained revision; original provider flag preserved; only `missing_author` remains | Proven at 23:10 UTC; expires in seven days or on relevant edits |
| Narrow policy saved, still disabled | Version 3 names only this article; owner UI confirms selected title, both pauses and no allowed actions | Proven |
| Independent recovery-key access | Prior restore used keys from a running container; owner previously attested a saved copy | Saved-copy path/private access requested; unverified |
| Notification receipt | No SMTP connection exists; test recipient/service requested | Needs connection and actual delivery evidence |
| No paid work/live publication | Current pauses, zero publications and unchanged budget reservations | Maintained; recheck before handoff |

Do not call the article approval-ready or the goal complete while any required
verification above is missing. SMTP server acceptance alone is not proof that
the recipient received a message. Saved-copy key access must be exercised without
printing keys or substituting the live-container copy. Use an isolated restore
target if a new restore is needed; never overwrite the production database.

## Implementation checkpoint

Local backend suite: **750 passed, 30 optional skipped**. New scope/source gates
cover forged reviews, invalid/unavailable sources, expiry, concurrent edits,
missing authors, viewer/site isolation, selected/unselected scheduling, and
restricted autopilot. Six new desktop/mobile browser cases passed, including
accessibility checks. Full UI regression: **115 passed**. Existing >500 KB
frontend build warning remains.

Release `f0d1d5b` was pushed to the existing `platform-deployment` branch and
deployed through the existing resources, with no schema/key/environment changes:

- Backend deployment `gww9lgvu7s6io8xwgk353mzy`: completed; seven services healthy;
  running API/worker/scheduler hashes match the reviewed source.
- Frontend deployment `j694o8yhpla9zc6w1dbjvp7g`: completed at 23:09:28 UTC.
  Public authenticated screens verified `/assets/index-CekCr4OM.js`, three
  current source reviews, the selected article and both active pause controls.
- Provider history unchanged: the original flagged contact URL remains in
  generation provenance; recorded usage remains 4,279 input / 856 output tokens.
  The article body hash is unchanged. No author was guessed or assigned.
- At 23:10 UTC: zero publications, zero schedule/remote post ID, monthly ceiling
  $300, two unchanged reservations totaling $1. Heartbeat advanced after release;
  no queue delay or missed checks; WordPress poll healthy. Overall audit coverage
  still honestly reports its existing degraded/partial condition.

## Backed-up preparation checkpoint

New encrypted archive at 23:11 UTC includes policy v3 and all three reviews:
`forgeseo-20260924T231123Z-bd7b12e64e074bb4ba23cfad3884c342.forge`.
It contains 19 tables and 124 artifacts. The host transfer service copied it to
`10.0.1.6:/srv/forgeseo-backups` and verified 15,242,444 bytes with SHA-256
`7e7df8ec1caa9458c9892424472f7f056a92eced16d13898accf86f7eecb5216`.
Nine archives are retained; none were deleted. The application backup profile's
`off_host_copy=not_configured` refers to its own transfer option; the independent
host timer performs the verified transfer.

This capture used deployed keys. It is **not** the outstanding independent-key
verification, and the machine evidence remains `independent_keys_verified=false`.
No new restore or notification-delivery success is claimed.

## Inputs still required from the owner

1. Select the genuine WordPress author. Available inventory IDs: 1 (business
   account, currently displaying its email), 3 (`snabbanalys`), 4 (`ewservices`).
   Do not silently rename a public profile or invent a person.
2. Supply the path/private access method for the independently saved original
   `ENCRYPTION_KEY` and `BACKUP_KEY`. Never paste secrets into chat or Git.
3. Supply an approved test inbox and SMTP connection privately in Settings.
   Leave recurring email digests disabled until delivery has been tested.

These questions were sent together during this goal turn. The goal remains
active and incomplete; no launch approval is inferred from successful tests,
source clearance or policy staging.
