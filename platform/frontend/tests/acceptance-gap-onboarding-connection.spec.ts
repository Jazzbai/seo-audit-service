import { expect, test, type Page, type Route } from '@playwright/test'

const auth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Owner' },
  team: { id: 'team-1', name: 'Pilot team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-new',
  team_id: 'team-1',
  name: 'Northstar Studio',
  origin: 'https://northstar.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: { business_name: 'Northstar Studio', audience: 'Independent makers' },
}

const overview = {
  site,
  counts: { pages: 0, open_findings: 0, pending_candidates: 0, published_articles: 0, open_incidents: 0 },
  monitoring: { status: 'not_running', last_seen_at: null, wordpress_change_poll: { status: 'not_running', last_success_at: null } },
  budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
  recent_events: [],
  coverage: { status: 'unknown', last_audit_at: null, error_count: 0, pending_url_count: 0 },
  connections: [{ kind: 'wordpress', status: 'needs_test', checked_at: null }],
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page, options: { failConnection?: boolean } = {}) {
  let siteCreated = false
  let siteCreateRequests = 0
  let connectionSave: Record<string, unknown> | null = null

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, siteCreated ? { items: [site], total: 1 } : { items: [], total: 0 })
    if (path === '/sites' && request.method() === 'POST') {
      siteCreateRequests += 1
      siteCreated = true
      return json(route, site)
    }
    if (path === '/sites/site-new/connections/wordpress' && request.method() === 'PUT') {
      connectionSave = request.postDataJSON() as Record<string, unknown>
      if (options.failConnection) return json(route, { detail: 'The WordPress connection service is unavailable.' }, 503)
      return json(route, { kind: 'wordpress', status: 'needs_test', safe_fields: { username: 'wp-editor' } })
    }
    if (path === '/sites/site-new/overview') return json(route, overview)
    return json(route, { items: [], total: 0 })
  })

  return { getSiteCreateRequests: () => siteCreateRequests, getConnectionSave: () => connectionSave }
}

async function fillRequiredSiteFacts(page: Page) {
  await page.getByLabel('Site name').fill('Northstar Studio')
  await page.getByLabel('Site origin').fill('https://northstar.example')
  await page.getByLabel('Business name').fill('Northstar Studio')
  await page.getByLabel('Primary audience').fill('Independent makers')
}

test('onboarding saves an optional WordPress connection without exposing its secret after setup', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/new')
  await fillRequiredSiteFacts(page)
  await page.getByLabel('WordPress username').fill('wp-editor')
  await page.getByLabel('Application password').fill('app-password-not-for-display')

  await page.getByRole('button', { name: 'Create site' }).click()

  await expect(page).toHaveURL(/\/sites\/site-new\/overview$/)
  await expect(page.getByRole('heading', { name: 'Northstar Studio' })).toBeVisible()
  expect(controls.getSiteCreateRequests()).toBe(1)
  expect(controls.getConnectionSave()).toEqual({ credentials: { username: 'wp-editor', application_password: 'app-password-not-for-display' }, settings: {} })
  await expect(page.getByText('app-password-not-for-display', { exact: true })).toHaveCount(0)
})

test('onboarding explains that WordPress credentials must be supplied together or skipped', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/new')
  await fillRequiredSiteFacts(page)
  await page.getByLabel('WordPress username').fill('wp-editor')

  await page.getByRole('button', { name: 'Create site' }).click()

  await expect(page.getByRole('alert')).toContainText('Enter both the WordPress username and application password')
  expect(controls.getSiteCreateRequests()).toBe(0)
  await expect(page).toHaveURL(/\/sites\/new$/)
})

test('onboarding keeps a created site recoverable when saving its connection fails', async ({ page }) => {
  const controls = await installMocks(page, { failConnection: true })
  await page.goto('/sites/new')
  await fillRequiredSiteFacts(page)
  await page.getByLabel('WordPress username').fill('wp-editor')
  await page.getByLabel('Application password').fill('app-password')

  await page.getByRole('button', { name: 'Create site' }).click()

  await expect(page.getByRole('alert')).toContainText('The site was created, but the WordPress connection could not be saved.')
  await expect(page.getByRole('link', { name: 'Open connection settings' })).toHaveAttribute('href', '/sites/site-new/settings/connections')
  await expect(page.getByRole('button', { name: 'Site created' })).toBeDisabled()
  expect(controls.getSiteCreateRequests()).toBe(1)
})
