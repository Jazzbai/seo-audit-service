import { expect, test, type Route } from '@playwright/test'

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

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

test('site-list failure is an explicit recoverable error instead of an indefinite loading state', async ({ page }) => {
  let siteListRequests = 0

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') {
      siteListRequests += 1
      if (siteListRequests === 1) return json(route, { detail: 'The workspace site list is temporarily unavailable.' }, 503)
      return json(route, { items: [site], total: 1 })
    }
    if (path === '/sites/site-1/overview') {
      return json(route, {
        site,
        counts: { pages: 0, open_findings: 0, pending_candidates: 0, published_articles: 0, open_incidents: 0 },
        monitoring: { status: 'unknown', last_seen_at: null },
        budget: { limit_cents: 0, spent_cents: 0, reserved_cents: 0 },
        recent_events: [],
        coverage: { status: 'unknown', last_audit_at: null, error_count: 0, pending_url_count: 0 },
        connections: [],
      })
    }
    return json(route, { items: [], total: 0 })
  })

  await page.goto('/sites/site-1/overview')

  const error = page.getByRole('alert')
  await expect(error).toContainText('The workspace site list is temporarily unavailable.')
  await expect(error.getByRole('button', { name: 'Try again' })).toBeVisible()

  await error.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('heading', { name: 'Acme Studio' })).toBeVisible()
  expect(siteListRequests).toBe(2)
})

test('weekly report treats nested measures and returned incident or spending sections as complete', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/reports/weekly') {
      return json(route, {
        site,
        generated_at: '2026-09-21T12:00:00Z',
        period_start: '2026-09-14T12:00:00Z',
        period_end: '2026-09-21T12:00:00Z',
        overview: {
          site,
          counts: { pages: 48, open_findings: 6, pending_candidates: 2, published_articles: 4, open_incidents: 0 },
          monitoring: { status: 'healthy', last_seen_at: '2026-09-21T11:59:00Z' },
          budget: { limit_cents: 30000, spent_cents: 1200, reserved_cents: 0 },
          coverage: { status: 'complete', last_audit_at: '2026-09-21T11:00:00Z', error_count: 0, pending_url_count: 0 },
          recent_events: [],
          connections: [],
        },
        measurements: { items: [{ id: 'measurement-1', kind: 'gsc', source: 'fixture', observed_at: '2026-09-21T11:30:00Z', data: { clicks: 12 } }], total: 1 },
        events: { items: [], total: 2 },
        publications: { items: [], total: 0 },
        incidents: { items: [], total: 0 },
        spending: {
          budget_account: { limit_cents: 30000, spent_cents: 1200, reserved_cents: 0 },
          reservations: { items: [], total: 0 },
        },
        note: 'Observed changes and measurements; not proof of ranking causality.',
      })
    }
    return json(route, { items: [], total: 0 })
  })

  await page.goto('/sites/site-1/reports/weekly')

  await expect(page.getByRole('heading', { name: 'Weekly report' })).toBeVisible()
  await expect(page.getByText('Partial weekly report', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'No scalar measures' })).toHaveCount(0)
  await expect(page.locator('#main-content .report-hero .stat-label').filter({ hasText: 'Pages' })).toBeVisible()
  await expect(page.locator('#main-content .report-hero .report-number').filter({ hasText: '48' })).toBeVisible()
  await expect(page.getByText('Spending Budget Account Spent Cents', { exact: true })).toBeVisible()
  await expect(page.getByText('Period end', { exact: true })).toHaveCount(0)
  await expect(page.locator('pre.json-preview')).toContainText('"measurements"')
  await expect(page.locator('pre.json-preview')).toContainText('"total": 1')
})

test('weekly report labels empty or partial data and keeps CSV failures visible', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/reports/weekly' && url.searchParams.get('format') === 'csv') {
      return json(route, { detail: 'The weekly CSV export is temporarily unavailable.' }, 503)
    }
    if (path === '/sites/site-1/reports/weekly') {
      return json(route, {
        site,
        generated_at: '2026-09-21T12:00:00Z',
        period_start: '2026-09-14T12:00:00Z',
        note: 'Only an envelope was returned; report sections are unavailable.',
      })
    }
    return json(route, { items: [], total: 0 })
  })

  await page.goto('/sites/site-1/reports/weekly')

  await expect(page.getByRole('heading', { name: 'Weekly report' })).toBeVisible()
  await expect(page.getByRole('status')).toContainText('Partial weekly report')
  await expect(page.getByRole('status')).toContainText('no scalar measures')
  await expect(page.getByRole('status')).toContainText('not evidence that the site is optimized')
  await expect(page.getByRole('heading', { name: 'No scalar measures' })).toBeVisible()

  await page.getByRole('button', { name: 'Download CSV' }).click()
  await expect(page.getByRole('alert')).toContainText('Service Unavailable')
})
