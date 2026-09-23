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
            checked_at: '2026-09-22T12:00:00Z',
            capabilities: { native: { read: true }, last_connection_test: { status: 'degraded' } },
          },
          {
            kind: 'gsc',
            status: 'connected',
            checked_at: '2026-09-22T12:05:00Z',
            capabilities: { last_connection_test: { status: 'degraded' } },
          },
        ],
        total: 2,
      })
    }
    return json(route, { items: [], total: 0 })
  })
}

test('a degraded provider test cannot be presented as automatic access', async ({ page }) => {
  await installMocks(page)
  await page.goto('/sites/site-1/settings/connections')

  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  const gsc = page.locator('form.connection-card').filter({ hasText: 'Google Search Console' }).first()

  await expect(wordpress.getByRole('status', { name: 'WordPress connection readiness: Degraded' })).toBeVisible()
  await expect(wordpress.getByText('The last provider check failed. Review the connection and run Test again before relying on it.', { exact: true })).toBeVisible()
  await expect(gsc.getByRole('status', { name: 'Google Search Console connection readiness: Degraded' })).toBeVisible()
  await expect(gsc.getByText('The last provider check failed. Review the property and authorization, then run the connection test again.', { exact: true })).toBeVisible()
  await expect(gsc.locator('section').getByText('Degraded', { exact: true })).toBeVisible()
})
