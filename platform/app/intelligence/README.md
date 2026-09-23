# Intelligence runtime behavior

The intelligence package is plain-dictionary code.  It does not open database
sessions, create connector clients, authorize publication, or persist secrets.
The main workflow owns those responsibilities.

## Provider generation configuration

`generate_article(brief, facts, provider_config, transport=None)` requires:

```json
{
  "provider": "openai-compatible",
  "base_url": "https://provider.example/v1",
  "model": "configured-model-name",
  "api_key_env": "FORGESEO_AI_API_KEY",
  "estimated_cost_cents": 25,
  "max_cost_cents": 50,
  "request_format": "openai_chat"
}
```

`endpoint` (or `base_url`), `model`, `estimated_cost_cents`, and
`max_cost_cents` are required.
The estimate must be non-negative and no greater than the maximum.  An API key
may be supplied through `api_key_env` or a runtime `api_key` value; it is never
returned in provenance or error messages.  The provider receives the supplied
brief, supplied facts, and supplied sources as structured request data.  The
implementation does not perform external research or invent source records.

`request_format` defaults to `openai_chat`; `responses` and `generic_json` are
also supported.  A provider response must contain a JSON title and body.  A
malformed response, timeout, HTTP failure, missing connection, or cost above the
configured maximum returns an explicit error and never falls back to generated
copy.  Successful responses include `cost_cents`, input/provider provenance,
and `approved: false`, `publishable: false`, and `check_required: true`.

## Content planning inputs

`plan_topics(facts, pages, keywords=None, products=None, origin=None,
research_inputs=None)` produces at most eight four-week briefs. Search
observations may supply a bounded query topic; competitor observations remain
explicit planning context unless they include a separately supplied query.
Each retained observation keeps its source, provider, and observation time.
Credential-shaped fields, nested provider payloads, malformed timestamps, and
public-domain values masquerading as article topics are discarded before a
brief is built. These records inform editorial review only: they do not prove
rankings, support factual claims by themselves, or authorize publication.

Article checks also require every `<img>` source to match an explicit public
`brief.image_sources` record. Owner-provided records require confirmation,
licensed records require a licence and attribution, and generated
illustrations require disclosure plus `not_real: true`; none of these records
make an image a factual representation of a real product, premises, or
completed work.

## Visibility configuration

`collect` returns `{kind, source, observed_at, data, cost_cents, metadata}` on
success and the same measurement envelope with an `error` object on failure.
DataForSEO requires `estimated_cost_cents` before its HTTP request; optional
`max_cost_cents` marks an over-limit returned cost.  A setting marked `paid` or
`pricing_required` also requires a known estimate.  OAuth access tokens are
used first and refresh tokens are exchanged only when necessary.  Tokens and
passwords never appear in returned data.

DataForSEO competitor mode is a separate, budgeted observation. It normalizes
the site target and up to three public competitor domains, requires a location
and known pricing, and preserves the provider response under an explicit
competitor scope. The scheduler invokes it only when the versioned site policy
contains competitors; it never becomes a write authorization.

PageSpeed responses are reduced to an allowlisted performance record and carry
an explicit `measurement_context`: `lab` for Lighthouse-style controlled data,
`field_or_origin` for available loading-experience data, `both` when both
contexts are present, or `unknown` when the provider supplies neither. The
Chromium browser worker is labelled `lab` and `field_data: false`; its
navigation timings are not Core Web Vitals and never stand in for field-user
experience. The UI keeps these contexts visible instead of combining them into
one performance score.

AI samples succeed only when the remote response supplies explicit citations.
They are labelled `ranking_type: "ai_answer"` and
`consumer_rankings: false`; they are not treated as consumer search rankings.
Successful connected samples also preserve provider, model, question, locale,
answer, citations, and the observation timestamp when those values are supplied
by the provider or connection settings.
For `request_format: "openai_responses_web_search"` (and its supported aliases),
ForgeSEO calls the OpenAI Responses endpoint once per validated tracked question
using the required `web_search` tool, `store: false`, and a bounded output limit.
It accepts only a completed response with public `url_citation` annotations and
never stores the raw provider payload or API key. The configured estimated and
maximum costs are per question; the visibility workflow multiplies the reserved
amount by the question count, capped at 20, before dispatch.
The Settings connection test for this explicit format uses only the provider's
read-only `/models` endpoint and marks the connection verified only when the
configured model is listed. It does not run a web-search sample, reserve budget,
or persist the API key or provider response. Generic AI request formats remain
configured until their provider-specific collection is intentionally run.
The visibility workflow also requires a bounded, non-empty tracked-question
list before it creates a paid reservation; an empty AI queue is a setup state,
not a provider call.
`validate_measurement_import` accepts either an object containing `items` or the
direct list passed by `/measurements/import`. Each item declares or inherits a
measurement kind, preserves its source/provider provenance, and is stored as
an observation rather than being relabelled as a citation. Before persistence,
the whole envelope is recursively inspected and credential-shaped keys,
excessive nesting, and oversized inspection payloads are rejected. Ordinary
analytical fields such as `token_count` and `authorization_class` remain valid.
Backlink and business-listing imports are bounded recommendation evidence, not
ranking claims or write authorization. Competitor observations may preserve a
provider-reported position, but are explicitly scoped to that competitor,
query, provider, and observation date; they are not a universal ranking score
or write authorization.
