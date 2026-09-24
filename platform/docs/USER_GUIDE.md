# ForgeSEO: review and controlled publishing

This guide covers the WordPress publishing-rehearsal release. It is not permission
to enable an unattended live pilot. Routine use is through the web application;
no PowerShell, API keys in requests, or environment-file editing is required.

## What to do now

1. Sign in to [your deployed ForgeSEO application](https://forgeseo.139.138.153.13.sslip.io)
   using your existing workspace. You can start reviewing now; you do not have to
   enable automation or edit environment files.
2. Choose the correct site from the site selector.
3. Use **Overview** for monitoring freshness and coverage. A completed audit can
   still have errors. An empty queue does not mean the whole site is optimized.
4. Open **Issues** and **Pages** to review the evidence behind proposed work.
5. Open **Content → Articles** to read drafts. Review the actual claims and cited
   sources; a successful generation is not editorial approval.
6. Use **Activity**, **Jobs**, **Incidents**, and **Publications** to inspect work
   and recovery states. **Settings → Connections** reports verified access versus
   missing/revoked connections.

For Auto1StopShop, keep **Site paused** and **Global pause** on, **Allow automated
workflows** off, and publishing unselected. Read-only monitoring can continue.
Do not press **Generate draft**, paid visibility controls, or an autopilot button
as an experiment: these can request paid work when their prerequisites allow it.

As of the 2026-09-24 preparation checkpoint, the existing article's three source
reviews are recorded; its remaining editorial blocker is the genuine WordPress
author. Independent recovery-key access and notification delivery still need
verification. Do not invent an author or enable live publication to clear a queue.

## Reviewing a flagged source

Save the draft first, then use its **Source review** panel. Open the source,
compare it with the article and explain exactly what it supports and what it
does not. Confirm that review, then choose **Record source review**. The server
fetches public HTML, retains evidence and records your authenticated identity.
It rechecks the draft and refuses a review if someone changed it meanwhile.

A review can clear a source flag only for that saved revision. It never erases
the original model flag, invents an author or authorizes publication. Reviews
expire after seven days; changing the title, body, source list or generation
requires another review. A successful fetch alone is not factual validation.

For a one-article pilot, **Settings → Policies & budget → Article publishing
scope** lets an owner select the specific draft by title. Save using **Save policy
controls**. A restricted scope with no selections blocks all publication;
unselected articles cannot publish. Keep both pauses on and allowed actions off
until a separate launch is approved.

## Reading usage and spending

An article's **Provider usage** panel separates recorded numeric input/output
tokens, the configured estimate, and the request's reservation ceiling. Missing
values say **Not recorded** or **Unknown**, not zero. Manually written articles
need not have provider usage.

In **Settings → Policies**, **Provider cost reconciliation** distinguishes held
reservations, released amounts and known actual charges. A held dollar is not
necessarily a spent dollar. Only an owner should enter an actual charge, using
provider billing evidence. Do not enter an estimate as an actual charge simply
to release budget. The monthly limit does not authorize unlimited trial requests.

## Publishing on an approved isolated test site

These steps were demonstrated against the disposable WordPress installation.
Do not repeat them on a live site until its separate publishing pilot is approved.

1. Connect WordPress through onboarding or **Settings → Connections**. Use an
   application password privately in the form, then **Test**. The connection must
   return authenticated access and a genuine author.
2. Confirm the test site's business facts and source references.
3. Open **Content → New article**. Supply a meaningful title, supported body,
   source URLs and a verified author. **Save article**, then **Check**. Resolve
   blockers; scheduling does not bypass editorial checks.
4. Review **Settings → Policies**. Enable only the approved actions, protected
   paths, limits and author. For a publication-only rehearsal, leave metadata,
   refresh and other writes off. Review workspace pause carefully: it affects
   other sites too. Isolated tests must use a separate workspace/deployment.
5. In the editor, choose a future **Schedule time** and press **Schedule**. The
   time input uses your browser's local timezone. Watch **Jobs** and reload the
   article to see the stored state. Saving a schedule is not proof of publication.
6. Require a published record with successful source/public verification. Open
   the resulting WordPress page separately and inspect the visible content,
   author and links. Source-HTML evidence and browser rendering are distinct.
7. An owner can use **Roll back publication** and confirm. For a newly created
   article this restores WordPress draft status, not deletion. Require successful
   remote verification and retain the publication history.

Repeating **Publish** for the same operation returns its existing job rather than
creating another post. After rollback, that old operation cannot be presented as
a new successful publication. A deliberate new publication requires fresh review.

## When something fails

- **Paused:** review the site and workspace controls. Queued publication stays
  held; do not disable safeguards just to clear a queue.
- **Missing author/source:** resolve the actual editorial information, save,
  then check again. Model confidence is not authorization.
- **Protected path or unknown permalink:** keep the article in draft. A private
  draft can exist before WordPress reveals its final URL. Review policy/URL
  support; never remove a protection just to force a publication. Plain query
  permalinks may conservatively match a protected root path.
- **Outside edit/source conflict:** the change is preserved. Inspect both versions
  before requesting further work. Do not overwrite the editor's content.
- **Revoked credentials:** an owner reconnects and tests WordPress. Reconnection
  does not authorize an uncertain write to be repeated.
- **Timeout or interrupted worker:** open **Publications → Check remote outcome**
  as an owner. This reads WordPress; it does not publish. An unavailable/held check
  can be tried again after the connection is restored. If a matching draft is
  reconciled, return to its article and explicitly press **Publish**. The new
  linked attempt retains the earlier failed job and same remote post identity;
  current policy, checks and freshness still apply. Never clone the article to
  work around an uncertain outcome.
- **Budget exhausted/unknown pricing:** the affected paid work stops. Supply
  verified pricing or reconcile actual billing; do not bypass reservations.

If a result remains uncertain, leave that action stopped and inspect its incident.
Do not delete its WordPress post, database row, snapshot or job history.

## Before the live pilot

Approve a narrowly scoped publishing policy, real author, verified sources and
specific article enrollment. Confirm a recent encrypted off-host backup and
recovery-key custody, one scheduler/write owner, monitoring and alert delivery,
and a separately approved paid-work budget. Keep main pages and builder layouts
protected. A seven-day pilot must then collect seven real days of evidence; this
rehearsal does not start that clock or promise rankings/AI citations.

The next bounded goal should be **live-pilot preparation**, not immediate
unattended publication: resolve the real author/source blockers, agree on the
exact article and allowed actions, verify independently held recovery keys and
alert delivery, and produce an explicit go/no-go checklist. Keep live publishing
disabled until those prerequisites and the owner's launch approval are recorded.

See the [rehearsal evidence and remaining gates](reviews/WORDPRESS_PUBLISHING_REHEARSAL_2026-09-24.md).
