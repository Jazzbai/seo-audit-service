import { expect, test, type Page, type Route } from '@playwright/test'

const auth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Owner' },
  team: { id: 'team-1', name: 'Pilot team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Pilot Workshop',
  origin: 'https://pilot.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: { business_name: 'Pilot Workshop' },
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/connections' && request.method() === 'GET') {
      return json(route, {
        items: [
          {
            kind: 'wordpress',
            status: 'connected',
            checked_at: '2026-09-21T12:00:00Z',
            capabilities: { native: { read: true }, editorial: { read: true } },
          },
          {
            kind: 'gsc',
            status: 'error',
            checked_at: '2026-09-21T12:05:00Z',
            capabilities: { last_connection_test: { status: 'error', error_code: 'remote_error' } },
          },
          {
            kind: 'ga4',
            status: 'needs_test',
            safe_fields: { property_id: '123456789' },
          },
          {
            kind: 'woocommerce',
            status: 'unsupported',
            checked_at: '2026-09-21T12:10:00Z',
            capabilities: {},
          },
        ],
        total: 4,
      })
    }
    return json(route, { items: [], total: 0 })
  })
}

test('connection settings expose accessible readiness states and a degraded recovery path', async ({ page }) => {
  await installMocks(page)
  await page.goto('/sites/site-1/settings/connections')

  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  const gsc = page.locator('form.connection-card').filter({ hasText: 'Google Search Console' }).first()
  const ga4 = page.locator('form.connection-card').filter({ hasText: 'Google Analytics 4' }).first()
  const woocommerce = page.locator('form.connection-card').filter({ hasText: 'WooCommerce' }).first()
  const ai = page.locator('form.connection-card').filter({ hasText: 'AI sample provider' }).first()

  await expect(page.getByRole('status', { name: 'WordPress connection readiness: Automatic' })).toBeVisible()
  await expect(page.getByRole('status', { name: 'Google Search Console connection readiness: Degraded' })).toBeVisible()
  await expect(page.getByRole('status', { name: 'Google Analytics 4 connection readiness: Needs Review' })).toBeVisible()
  await expect(page.getByRole('status', { name: 'WooCommerce connection readiness: Unsupported' })).toBeVisible()
  await expect(page.getByRole('status', { name: 'AI sample provider connection readiness: Needs Connection' })).toBeVisible()

  await expect(gsc.getByText('The last provider check failed. Review the connection and run Test again before relying on it.', { exact: true })).toBeVisible()
  await expect(gsc.getByText('Last checked:', { exact: false })).toBeVisible()
  await expect(gsc.getByRole('button', { name: 'Test', exact: true })).toBeEnabled()
  await expect(gsc.getByText('remote_error', { exact: true })).toHaveCount(0)
  await expect(wordpress.getByText('Access was verified. Workflows remain subject to policy and editorial checks.', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('The latest WordPress check did not report content-type coverage. Page reading alone does not prove that the whole site inventory is complete.', { exact: true })).toBeVisible()
  await expect(ga4.getByText('Credentials or authorization are present, but access is not verified yet. Run Test before relying on this source.', { exact: true })).toBeVisible()
  await expect(woocommerce.getByText('This source is not supported for this site.', { exact: true })).toBeVisible()
  await expect(ai.getByText('Connect this source before ForgeSEO can verify access.', { exact: true })).toBeVisible()
})
