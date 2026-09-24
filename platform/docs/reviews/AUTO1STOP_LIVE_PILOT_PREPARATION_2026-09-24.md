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

The original source flag could not legitimately be cleared through the old UI:
it was permanently attached to protected provider provenance. The prepared fix
adds a separate authenticated, revision-bound source review with reviewer/time,
notes, public-HTML fetch evidence and a SHA-256. It never deletes the model's
original flag or accepts a model confidence score as approval. Ordinary article
creation/editing cannot forge these records. Reviews expire after seven days and
are invalidated by title, body, source-list or generation changes.

## Narrow policy to stage while paused

Keep `enabled=false`, `allowed_actions=[]`, site pause and global pause true.
Stage a policy with:

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
| Source review recorded in application | Primary pages reviewed; audited-review implementation tested locally | Deployment and real review pending |
| Narrow policy saved, still disabled | Versioned article allowlist implemented locally; runtime remains v2 | Deployment and staging pending |
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
accessibility checks. Full UI regression: **115 passed**. Compatible deployment
is pending at this checkpoint. Existing >500 KB frontend build warning remains.
