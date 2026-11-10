# H-Town SEO activation — November 9, 2026

## Current outcome

**Publication is working through Jazzb SEO.** The first-visit article was published and verified on November 9 at 6:47 PM America/Chicago. H-Town is unpaused, with at most one post per week. The second reviewed article is scheduled for **November 17 at 9:00 AM America/Chicago**. Both use only Blog → Patient Education. No existing public page body or site template was edited during this work.

Live article: [Your First Visit to H-Town Star Family Dentistry: What to Bring and Ask](https://htownstarfamilydentistry.com/blog/patient-education/your-first-visit-to-h-town-star-family-dentistry-what-to-bring-and-ask/).

Search Console and GA4 collection works. Metadata writing through the existing Yoast setup remains unsupported; the audit backlog and Google production-verification work are separate remaining tasks. Baseline traffic and AI samples below do not establish an improvement from today's publication.

Site: `505f7404e64f48268606b19fb3cca37a` — https://htownstarfamilydentistry.com/

## Platform changes

- Deployment `98c3a6d` completed for backend and frontend. Connected Google accounts now expose accessible Search Console properties and GA4 properties in a dropdown. The operator can select, save and test a property; a manual ID remains available. H-Town's live dropdown correctly selects GA4 property **468885883**. This does not automatically choose a different customer's property.
- Publication policy now constrains WordPress category IDs and their live ancestry. The workflow sends categories in the actual draft request, verifies the returned assignment, checks policy again before publication, and verifies the published assignment. Retry reconciliation also compares categories. A missing, changed or unapproved assignment blocks publication.
- A first failed availability probe previously crashed when incrementing a database default that had not yet been inserted. The workflow now records the failed probe instead.
- Follow-up source commit `62087ef` rejects unrelated numeric-star discovery phrases such as "5 star dental near me" from automatic topic planning, while preserving genuine numeric-star business names. Its deployment completed and its actual production function returned false for the irrelevant phrase.
- A real read-only planning run then exposed three competitor-name queries accepted merely because they contained "dental" or "dentistry". Deployed follow-up `3a8cfa6` requires more specific offering relevance; industry nouns alone do not establish fit. All three competitor briefs are held.
- Deployed backend release `42cedca` supports exact operator-approved internal HTTPS routes. H-Town alone is configured, with fixed socket/Host/SNI and full certificate verification; see routing evidence below.
- Existing account creation remains closed. Auto1Stop's site policy was not changed.

Validation: 114 backend regression checks passed for the initial release; the additional category/API suite passed 22 checks. Frontend checks passed after correcting the new select's accessible name, and the production build passed. The final planning follow-up passed 74 intelligence/content-autopilot checks, including observed competitor queries and preservation of relevant service queries. The live property selector has 16 accessible GA4 properties plus its placeholder, selects H-Town's expected ID, and produced no captured page errors.

## H-Town configuration and content

The saved policy permits at most **one post per week** and no refreshes. Existing monthly budget ($300) and publication days (Tuesday/Friday) were preserved. Site pause and workspace pause are both off. Today's explicit publication used this week's one-post allowance; the second post is scheduled in the following week.

Only children of WordPress **Blog (61)** are allowed: IDs **62–70**. Default: **Patient Education (68)**. Services (47), Vlog (76), Uncategorized (1), Wellness (75), and similarly named categories outside the Blog branch are excluded. Service, vlog, about and billing paths are protected from automatic metadata changes.

Business facts now include the site's verified service names and public homepage/contact/billing sources. Guidance prohibits invented fees, offers, insurance coverage, hours, clinical outcomes or claims of clinician review.

Two drafts were written and saved through the authenticated editorial API:

| Draft | Platform article ID | Result |
| --- | --- | --- |
| Your First Visit to H-Town Star Family Dentistry: What to Bring and Ask | `162ab04f524743299a66ed880e719d9f` | Checks passed; published and publicly verified; WordPress ID 994298 |
| Questions to Ask About a Written Dental Treatment Estimate | `d3186fb834be4d769cf7f0eb88ff4a6c` | Checks passed; scheduled November 17, 9:00 AM Chicago (`2026-11-17T15:00:00Z`) |

Both use Patient Education (68). They contain practical appointment/estimate questions, source links and useful internal links. They were written and reviewed as editorial drafts, not generated by an unattended provider workflow. Publication of the first completed through the real worker. The second's future publication has not happened yet. No doctor approval was claimed; the verified configured WordPress author is the existing operator account (ID 1).

Three weak briefs created by the earlier planner were held: "4 star dental service guide", "5 star dental group guide", and "5 star dental near me guide". They have no completed body and must not be generated or published automatically.

The subsequent read-only cycle added seven brief records. Three competitor-name briefs were held as irrelevant. Two broad service topics were held pending a distinct Blog angle and overlap review. The remaining two clinical topics were held for research and appropriate clinical editorial review. These seven records are plans, not seven generated or published articles.

## Measurements and public audit

- Fresh GA4 test `ae6a43e9603146c6bc313a0281b69f00` verified property **468885883**. A real collection returned 29 daily rows for **October 12–November 9**, totaling **483 sessions across all channels**. This inclusive window contains a partial current day. Daily totalUsers were not summed into a claimed unique-user total.
- Search Console test `cb689ca607d14ca290ef0b920b9f83337` and collection `058989ece54b4932a87480b9aa58dfb4` completed. The collected measurement has **485 query/page rows**; summing these rows is not a reliable property-wide traffic total.
- A first two-question OpenAI web-search visibility sample completed as job `8f54e52c00f84ae78a0c0f8a416643a7`. H-Town was mentioned and cited in **one of two answers**. That is a small baseline sample, not a ranking, population estimate or demonstrated improvement. Estimated cost was 20 cents with a 100-cent maximum for the two questions; the estimate is not the provider invoice.
- Budget readback retains the $300 monthly limit, $0 settled and $1 reserved. The reservation remains held pending actual-cost reconciliation; $0 settled must not be described as no provider usage or no charge.
- Nine priority public URLs were actually fetched and audited locally; all returned HTTP 200. This is separate from the initially blocked platform crawl. Missing meta descriptions occurred on the homepage, contact, billing, doctor and blog archive pages. Other findings include long service titles/descriptions, missing social metadata and unnamed links. No existing-page metadata/body fixes were applied in this task.
- Five replacement description suggestions are prepared in private `metadata-review.csv`, marked `local_draft_not_applied`. They have not been imported as authenticated server observations or executed against WordPress.
- Before publication, public REST inventory returned 242 published posts; 134 were assigned to Blog or its children. Existing similar content must be considered before increasing publication volume. A fresh authenticated inventory after routing repair completed with 263 resources; the dashboard retains older history separately (397 stored page records).
- `search-opportunities.csv` preserves 20 relevant observed query/page rows for review. Existing Blog articles already receive impressions for implant comparison, Invisalign versus braces, and preventive dentistry. Those are opportunities to assess existing pages, not instructions to create duplicate guides. Public inventory also confirms an existing dental-impressions article. Both corresponding new clinical plans were explicitly held with their existing Blog URLs; refreshing existing bodies remains disabled.

The private evidence directory is `platform/artifacts/htown-activation-2026-11-09/`. It contains the two HTML drafts, real audit output, baseline measurements and a property-selector screenshot. Raw analytics and screenshots are intentionally not committed.

## Routing repair and application verification

Earlier from production **app-10**, HTTPS requests to `htownstarfamilydentistry.com` resolved to **139.138.153.14** but received a self-signed **unifi.local** certificate. Both ordinary HTTPS and the application's DNS-pinned fetch rejected it. Public access from the operator's Windows machine received the expected H-Town certificate and HTTP 200. This points to a network path/routing problem; the exact gateway rule has not been proven.

An earlier recheck after the owner's network change resolved the hostname to **10.0.1.14**. At that stage the connector rejected the private address before connecting. WordPress test `be7f3fd2ef44437a9177b9d1f76062cd` failed with `SSRFError`; the read-only crawl independently recorded "The URL resolves to a private or reserved network". Those failures are historical and were superseded by the scoped application route below.

The operator subsequently supplied a technical report proving normal verified HTTPS and authenticated WordPress reads through the persistent H-Town-only private route. Those authenticated checks were performed by a separate Django backend. Jazzb SEO's fresh test `bf94f931044a4816a1e02b3904228548` still rejected the private DNS answer before reaching TLS, exposing its additional application restriction.

The earlier request to restore public DNS is superseded by this deliberate internal route. Source release `42cedca` adds server-only `TRUSTED_HTTPS_ROUTES` configuration: exact public hostname, fixed RFC1918 IPv4 address, HTTPS port 443 only, fixed Host/SNI and full certificate verification. Site settings cannot supply this route. Other hostnames, ports, private literals and cross-authority redirects retain their rejection. Coolify saved only `{"htownstarfamilydentistry.com":"10.0.1.14"}` as runtime configuration, with build-time availability off. No all-sites rollout was requested or performed.

Route/connector/browser tests passed 66 checks; publication/config/deployment regressions passed 95 checks with 2 skipped. Deployment `iqokjiiixlfotsltp06upmbne` finished for `42cedca`. The real Jazzb SEO API container independently fetched WordPress HTTP 200 with TLS verification enabled. Native connection test `4fc97963ecd94713886d7e51bbc88dba` completed with authenticated create/publish capabilities and complete author discovery. Both article checks then passed. See `platform/docs/TRUSTED_HTTPS_ROUTES.md` for configuration and application rollback; the operator's separate hosts-file backup/rollback remains independent.

## WAF correction and publication recovery

The first publication job (`38fca59e26774011ad48b22a07591c28`) failed during creation and was conservatively held as ambiguous. H-Town was paused immediately. Read-only reconciliation (`3e23165e1c1747b782f1e8822d253049`) and authenticated exact-slug lookups found no draft. No blind duplicate create was sent.

A controlled comparison used an invalid post status to prevent creation. A short HTML request reached WordPress validation (HTTP 400), while the unchanged full article received an empty Caddy HTTP 403. Changing the client name or omitting the operation header did not help. Temporary H-Town-only Coraza audit diagnostics identified CRS 4.25.0 rules **932230, 932235 and 932250**, raising an inbound anomaly score of 15.

The permanent fix adds only three rule-target exclusions for **`ARGS:json.content`** to the existing rule chain (ID 1001105), already limited to source **10.0.1.10/32**, exact H-Town Host, exact `/wp-json/wp/v2/posts`, POST and JSON content type. The existing image-content exclusion (941160) remains. The WAF and anomaly-blocking rule were not disabled.

Observed controls:

- Unchanged full article + deliberately invalid status: **403 before → WordPress 400 afterward**.
- Command text in the title field: **403**.
- Full payload directed to the Pages endpoint: **403**.
- Full-content request from an external unauthenticated client: **403**.
- All these controls used an invalid status and created no post.

On **wp-14**, backups, hash manifest, private diagnostic evidence and a guarded rollback script are in `/root/backups/htown-jazzb-waf-20261109/`. Only one line changed in `/etc/caddy/sites/01-migration-preview-security.caddy`; the H-Town site file was restored byte-for-byte and the temporary diagnostic config was removed. No other site's route or WAF predicate was changed. `caddy validate` passed and Caddy was gracefully reloaded. `python3 /root/backups/htown-jazzb-waf-20261109/rollback.py --check` passed; `--apply` restores the prior WAF line and refuses to overwrite later edits. Application-route and host-DNS rollback remain separate operations.

After the scoped correction and another authenticated empty lookup, a one-time operator recovery recorded the rejected create as absent, preserved the original operation key and failed-job history, and reset only that local operation for an explicit retry. Event `publication_create_recovered` records this intervention. It was not an automatic timeout retry and did not invent a remote draft.

Retry job **`015a83bb02924f089f624314b040909e`** completed through the normal worker. Publication **`f8e30eeb1fdb4695ad2db7acadf80ada`** is published, with WordPress ID **994298** and captured public-verification evidence. The worker verified the approved body against WordPress raw content and the live page. Independent public HTTP/REST and controlled Chrome checks confirmed HTTP 200, the expected heading/body, self-canonical URL, indexable robots metadata, and **categories exactly `[68]`**, whose parent is **Blog (61)**. No duplicate post was found or created during recovery.

The new article is also present in the live `post-sitemap.xml` (HTTP 200). This verifies sitemap discovery, not that Google has already indexed or ranked the page.

## Remaining work

The current Yoast connection supports SEO reads but not metadata writes. Prepared descriptions and existing-page SEO candidates therefore remain unapplied until a supported WordPress metadata bridge is installed and verified. The subsequently deployed Auto1Stop follow-up `7f18575` fixes audit continuations to preserve their parent's `suppress_automation`, `skip_inventory` and `full_cycle_mode` controls; multi-batch regression tests passed. This resolves the continuation-control defect recorded earlier in this session, but does not add a Yoast writer. Unsupported-writer checks keep those metadata actions review-only. Clinical topics still need appropriate editorial review; ten unfinished briefs remain held.

Fresh audit job `fdbbe359b8604f64b8540742e8bc12d8` checked **100 pages with zero errors**, then returned partial coverage and queued continuation `48d5e9ab34574e06a7d53933cbc9a631`. Its initial batch explicitly suppressed metadata execution. The crawl is still underway; this is not complete-site coverage. The existing audit-cadence and incomplete-discovery incidents await actual fresh crawl results; rendered coverage is a separate measurement. They were not manually marked resolved to manufacture a healthy dashboard.

Google's app is still in External/Testing mode. The separate `seo.jazzb.com` domain/public-page/Google production-verification work remains unfinished; today's connection repair does not resolve testing-mode authorization expiry.

## Final execution verification

Before routing repair, read-only full cycle `90387322bc3741bb8f1e49106682ced8` completed with status **partial**. Availability recorded a failed probe without the old TypeError; inventory required a verified WordPress connection; public audit inspected zero pages because the private DNS result was rejected. Content planning completed with seven brief records (all subsequently held). This historical failure is not the final connection state.

Final article readback: **one published, one scheduled and ten review-needed** records. H-Town and workspace pause are both false; policy version 2 retains one post per week and Blog children 62–70. Availability job `56eb6b6a00614304bbe05c3ff075f378` completed with HTTP 200 and available=true. Inventory job `81750ddd17fb4750bffa17696ac8b489` completed with 263 resources. These were real production workflows after the repair.

Planner release `3a8cfa6` and routing release `42cedca` are deployed. Public authentication status returned `initialized:true, account_creation_enabled:false`; new platform accounts remain disabled. The private evidence folder includes the public REST verification, worker publication result, next-publication schedule, completion snapshot and controlled-browser screenshot. Scheduled future publication and organic/AI visibility improvement are not claimed as already observed.
