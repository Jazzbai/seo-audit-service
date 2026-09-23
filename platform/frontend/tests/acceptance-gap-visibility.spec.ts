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
  facts: { business_name: 'Acme Studio', audience: 'Independent makers', locations: [], services: [], products: [], authors: [], confirmed_sources: [] },
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installVisibilityMocks(page: Page, options: { failMeasurementsCount?: number; emptyAfterFailure?: boolean; emptyConnections?: boolean; unsupportedSource?: string; trackedQuestions?: string[]; dataforseoReady?: boolean } = {}) {
  let failMeasurements = options.failMeasurementsCount ?? 0
  const daysAgo = (days: number) => new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString()
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status' && request.method() === 'GET') return json(route, { initialized: true })
    if (path === '/auth/me' && request.method() === 'GET') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/measurements' && request.method() === 'GET') {
      if (failMeasurements > 0) {
        failMeasurements -= 1
        return json(route, { detail: 'The visibility measurement service is temporarily unavailable.' }, 503)
      }
      if (options.emptyAfterFailure) return json(route, { items: [], total: 0 })
      return json(route, {
        items: [
          { id: 'measurement-stale', kind: 'pagespeed', source: 'pagespeed', observed_at: daysAgo(31), data: {} },
          { id: 'measurement-ai', kind: 'ai_sample', source: 'ai_sample', observed_at: daysAgo(2), data: { citations: [] } },
        ],
        total: 4,
      })
    }
    if (path === '/sites/site-1/connections' && request.method() === 'GET') {
      if (options.emptyConnections) return json(route, { items: [], total: 0 })
      return json(route, {
      items: [
        { kind: 'gsc', status: options.unsupportedSource === 'gsc' ? 'unsupported' : 'error', checked_at: new Date().toISOString(), error: 'The provider rejected the latest connection test.' },
        { kind: 'ga4', status: 'connected', checked_at: new Date().toISOString() },
        { kind: 'pagespeed', status: 'connected', checked_at: daysAgo(31) },
        ...(options.dataforseoReady ? [{ kind: 'dataforseo', status: 'configured', checked_at: new Date().toISOString(), capabilities: { credential_shape_verified: true, settings: { estimated_cost_cents: 2, max_cost_cents: 5 } } }] : []),
        { kind: 'ai', status: 'connected', checked_at: new Date().toISOString() },
      ],
      total: 4,
      })
    }
    if (path === '/sites/site-1/policy' && request.method() === 'GET') {
      return json(route, { settings: { tracked_questions: options.trackedQuestions ?? ['What does Acme Studio offer?'] } })
    }
    return json(route, { items: [], total: 0 })
  })
}

test('visibility distinguishes partial coverage and every source readiness state', async ({ page }) => {
  await installVisibilityMocks(page)
  await page.goto('/sites/site-1/visibility')

  await expect(page.getByRole('status').filter({ hasText: 'Visibility coverage is partial' })).toContainText('Showing 2 of 4 observations')
  await expect(page.getByRole('alert').filter({ hasText: 'Google Search Console: Error' })).toContainText('Review the connection and retry its test')
  await expect(page.getByRole('button', { name: 'Collect visibility' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Retry status check' })).toBeVisible()
  await expect(page.getByRole('row').filter({ hasText: 'Stale' }).getByText('Stale', { exact: true })).toBeVisible()

  await page.getByRole('tab', { name: 'Source settings' }).click()
  const sourceCard = (name: string) => page.locator('.source-card').filter({ hasText: name })
  await expect(sourceCard('Google Search Console').getByText('Error', { exact: true })).toBeVisible()
  await expect(sourceCard('Google Analytics 4').getByText('No Observations', { exact: true })).toBeVisible()
  await expect(sourceCard('PageSpeed').getByText('Stale', { exact: true })).toBeVisible()
  await expect(sourceCard('DataForSEO').getByText('Needs Connection', { exact: true })).toBeVisible()
  await expect(sourceCard('AI sample provider').getByText('Connected', { exact: true })).toBeVisible()
})

test('visibility keeps an empty response recoverable with a retry state', async ({ page }) => {
  await installVisibilityMocks(page, { failMeasurementsCount: 2, emptyAfterFailure: true, emptyConnections: true })
  await page.goto('/sites/site-1/visibility')

  await expect(page.getByRole('alert')).toContainText('visibility measurement service is temporarily unavailable')
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible()
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('heading', { name: 'No observations yet', exact: true })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: 'Google Search Console: Needs Connection' })).toContainText('No verified connection is available')
  await expect(page.getByRole('button', { name: 'Collect visibility' })).toBeDisabled()
})

test('visibility keeps unsupported sources distinct and blocks collection with a connection path', async ({ page }) => {
  await installVisibilityMocks(page, { unsupportedSource: 'gsc' })
  await page.goto('/sites/site-1/visibility')

  const unsupported = page.getByRole('alert').filter({ hasText: 'Google Search Console: Unsupported' })
  await expect(unsupported).toContainText('not supported for the current connection')
  await expect(page.getByRole('button', { name: 'Collect visibility' })).toBeDisabled()
  await expect(unsupported.getByRole('link', { name: 'Open source connections' })).toHaveAttribute('href', '/sites/site-1/settings/connections')

  await page.getByRole('tab', { name: 'Source settings' }).click()
  const sourceCard = page.locator('.source-card').filter({ hasText: 'Google Search Console' })
  await expect(sourceCard.getByText('Unsupported', { exact: true })).toBeVisible()
})

test('visibility gates an AI sample until tracked questions are configured', async ({ page }) => {
  await installVisibilityMocks(page, { trackedQuestions: [] })
  await page.goto('/sites/site-1/visibility')

  await page.getByLabel('Source to collect').selectOption('ai_sample')

  const notice = page.getByRole('status').filter({ hasText: 'AI sample provider: Needs Review' })
  await expect(notice).toContainText('Add at least one tracked question in Policies & budget')
  await expect(notice.getByRole('link', { name: 'Open Policies & budget' })).toHaveAttribute('href', '/sites/site-1/settings/policies')
  await expect(page.getByRole('button', { name: 'Collect visibility' })).toBeDisabled()
})

test('visibility allows the first priced DataForSEO observation while keeping provider verification visible', async ({ page }) => {
  await installVisibilityMocks(page, { dataforseoReady: true })
  await page.goto('/sites/site-1/visibility')

  await page.getByLabel('Source to collect').selectOption('dataforseo')

  const notice = page.getByRole('status').filter({ hasText: 'DataForSEO: Needs Review' })
  await expect(notice).toContainText('one bounded DataForSEO observation')
  await expect(page.getByRole('button', { name: 'Collect visibility' })).toBeEnabled()
})
