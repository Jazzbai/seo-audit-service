import { expect, test, type Page, type Route } from '@playwright/test'

const auth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Alex Owner' },
  team: { id: 'team-1', name: 'Northstar team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Acme Studio',
  origin: 'https://acme.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: {},
}

type Scenario = 'healthy' | 'attention' | 'stale' | 'failure'

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

function overviewFor(scenario: Scenario) {
  const current = new Date().toISOString()
  const old = new Date(Date.now() - 45 * 24 * 60 * 60 * 1000).toISOString()
  const base = {
    site,
    counts: { pages: 12, open_findings: 3, pending_candidates: 0, published_articles: 0, open_incidents: 0 },
    monitoring: { status: 'running', last_seen_at: current, queue_delay_seconds: 0, missed_checks: 0, wordpress_change_poll: { status: 'healthy', last_success_at: current, missed_window: false } },
    budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
    recent_events: [],
    coverage: { status: 'complete', last_audit_at: current, error_count: 0, pending_url_count: 0 },
    connections: [
      { kind: 'wordpress', status: 'connected', checked_at: current },
      { kind: 'gsc', status: 'connected', checked_at: current },
      { kind: 'ga4', status: 'connected', checked_at: current },
    ],
  }

  if (scenario === 'attention') return {
    ...base,
    monitoring: { ...base.monitoring, status: 'degraded', queue_delay_seconds: 90, missed_checks: 2, wordpress_change_poll: { status: 'not_connected', last_success_at: null, missed_window: false } },
    coverage: { status: 'partial', last_audit_at: current, error_count: 1, pending_url_count: 2 },
    connections: [{ kind: 'wordpress', status: 'needs_connection', checked_at: null }, { kind: 'ai', status: 'unsupported', checked_at: current }],
  }

  if (scenario === 'stale') return {
    ...base,
    counts: { ...base.counts, pending_candidates: 1 },
    monitoring: { ...base.monitoring, wordpress_change_poll: { status: 'stale', last_success_at: old, missed_window: true } },
    coverage: { ...base.coverage, last_audit_at: old },
    connections: [{ kind: 'wordpress', status: 'connected', checked_at: old }],
  }

  if (scenario === 'failure') return {
    ...base,
    monitoring: { ...base.monitoring, status: 'failed', wordpress_change_poll: { status: 'error', last_success_at: null, missed_window: false } },
    coverage: { status: 'failed', last_audit_at: current, error_count: 0, pending_url_count: 0 },
    connections: [{ kind: 'wordpress', status: 'connected', checked_at: current }, { kind: 'ai', status: 'error', checked_at: current }],
  }

  return base
}

async function installMocks(page: Page, getScenario: () => Scenario) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/overview') return json(route, overviewFor(getScenario()))
    if (path === '/sites/site-1/jobs' && request.method() === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/incidents' && request.method() === 'GET') return json(route, { items: [], total: 0 })
    return json(route, { items: [], total: 0 })
  })
}

test('overview distinguishes monitoring, coverage, connection, and result states', async ({ page }) => {
  let scenario: Scenario = 'healthy'
  await installMocks(page, () => scenario)

  await page.goto('/sites/site-1/overview')
  const status = page.getByRole('region', { name: 'Monitoring and coverage status' })
  await expect(status.getByRole('listitem', { name: /Scheduler and queue: automatic/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /WordPress change checks: automatic/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Audit coverage: automatic/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Candidate queue: empty_results/i })).toBeVisible()

  scenario = 'attention'
  await page.reload()
  await expect(status.getByRole('listitem', { name: /Scheduler and queue: needs_review/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /WordPress change checks: needs_connection/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Audit coverage: partial_coverage/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Search and AI measurements: unsupported/i })).toBeVisible()

  scenario = 'stale'
  await page.reload()
  await expect(status.getByRole('listitem', { name: /WordPress change checks: stale/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Audit coverage: stale/i })).toBeVisible()

  scenario = 'failure'
  await page.reload()
  await expect(status.getByRole('listitem', { name: /Scheduler and queue: failure/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /WordPress change checks: failure/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Audit coverage: failure/i })).toBeVisible()
  await expect(status.getByRole('listitem', { name: /Search and AI measurements: failure/i })).toBeVisible()
})

test('empty run history is explicit and never implies optimization', async ({ page }) => {
  await installMocks(page, () => 'healthy')
  await page.goto('/sites/site-1/jobs')

  await expect(page.getByRole('heading', { name: 'No jobs yet' })).toBeVisible()
  await expect(page.getByText('No runs have been recorded for this site. An empty run history is not proof that the site is optimized; check audit coverage and findings.', { exact: true })).toBeVisible()
})
