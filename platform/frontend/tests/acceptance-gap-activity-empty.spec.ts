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

const overview = {
  site,
  counts: { pages: 0, open_findings: 0, pending_candidates: 0, published_articles: 0, open_incidents: 0 },
  monitoring: { status: 'running', last_seen_at: '2026-09-21T12:00:00Z', queue_delay_seconds: 0, missed_checks: 0 },
  budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
  recent_events: [],
  coverage: { status: 'unknown', last_audit_at: null, error_count: 0, pending_url_count: 0 },
  connections: [],
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/activity') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/overview') return json(route, overview)
    if (path === '/sites/site-1/policy') return json(route, { id: 'policy-1', version: 1, settings: { enabled: false, allowed_actions: [] } })
    if (path === '/settings') return json(route, { global_pause: true })
    return json(route, { items: [], total: 0 })
  })
}

test('empty activity gives the operator a truthful path to the first audit', async ({ page }) => {
  await installMocks(page)
  await page.goto('/sites/site-1/activity')

  await expect(page.getByRole('heading', { name: 'No activity recorded', exact: true })).toBeVisible()
  await expect(page.getByText(/an empty activity stream is not proof that the site is optimized\./)).toBeVisible()

  const auditPath = page.getByRole('link', { name: 'Open overview to run an audit', exact: true })
  await expect(auditPath).toHaveAttribute('href', '/sites/site-1/overview')
  await auditPath.click()
  await expect(page).toHaveURL(/\/sites\/site-1\/overview$/)
  await expect(page.getByRole('button', { name: 'Run audit', exact: true })).toBeVisible()

  await expect(page.getByText('internal-job-id', { exact: true })).toHaveCount(0)
})
