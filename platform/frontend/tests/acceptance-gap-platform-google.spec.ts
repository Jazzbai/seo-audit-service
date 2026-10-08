import { expect, test, type Page, type Route } from '@playwright/test'

const siteId = 'site-google'
const setupMessage = 'Google setup is managed by your platform administrator. Ask them to finish Google OAuth setup before connecting this property.'

type MockOptions = {
  role?: 'owner' | 'viewer'
  config?: 'configured' | 'missing' | 'failed'
  holdConfig?: boolean
  connections?: Array<Record<string, unknown>>
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page, options: MockOptions = {}) {
  const role = options.role ?? 'owner'
  const config = options.config ?? 'configured'
  const connectionItems = [...(options.connections ?? [])]
  const configSites: string[] = []
  const connectionSaves: Array<{ siteId: string; kind: string; body: Record<string, unknown> }> = []
  let releaseConfig: () => void = () => undefined
  const configGate = new Promise<void>((resolve) => { releaseConfig = resolve })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, {
      user: { id: `${role}-1`, email: `${role}@example.test`, name: role },
      team: { id: 'team-google', name: 'Google fixture team' },
      role,
      csrf_token: 'test-csrf',
    })
    if (path === '/sites' && method === 'GET') return json(route, {
      items: [{ id: siteId, name: 'Google Test Site', origin: 'https://google.example', timezone: 'America/Chicago', language: 'en', paused: true, facts: {} }],
      total: 1,
    })
    if (path === '/settings' && method === 'GET') return json(route, { global_pause: false })
    if (path === `/sites/${siteId}` && method === 'GET') return json(route, {
      id: siteId, name: 'Google Test Site', origin: 'https://google.example', timezone: 'America/Chicago', language: 'en', paused: true, facts: {},
    })

    const configMatch = path.match(/^\/sites\/([^/]+)\/google-oauth\/config$/)
    if (configMatch && method === 'GET') {
      configSites.push(configMatch[1])
      if (options.holdConfig) await configGate
      if (config === 'failed') return json(route, { detail: 'internal oauth provider failure' }, 503)
      return json(route, {
        configured: config === 'configured',
        mode: 'platform',
        callback_url: 'https://platform.example/api/v1/oauth/google/callback',
        message: config === 'configured' ? 'Google is ready.' : 'Platform administrator setup is required.',
      })
    }

    if (path === `/sites/${siteId}/connections` && method === 'GET') {
      return json(route, { items: connectionItems, total: connectionItems.length })
    }
    const saveMatch = path.match(/^\/sites\/([^/]+)\/connections\/(gsc|ga4)$/)
    if (saveMatch && method === 'PUT') {
      const body = request.postDataJSON() as Record<string, unknown>
      const [, savedSiteId, kind] = saveMatch
      connectionSaves.push({ siteId: savedSiteId, kind, body })
      const savedSettings = body.settings as Record<string, unknown>
      const updated = { kind, status: 'needs_test', settings: savedSettings, safe_fields: savedSettings }
      const index = connectionItems.findIndex((item) => item.kind === kind)
      if (index >= 0) connectionItems[index] = updated
      else connectionItems.push(updated)
      return json(route, updated)
    }

    return json(route, { items: [], total: 0 })
  })

  return {
    configSites,
    connectionSaves,
    releaseConfig: () => releaseConfig(),
  }
}

function connectionCard(page: Page, name: string) {
  return page.locator('form.connection-card').filter({ hasText: name }).first()
}

test('configured platform OAuth lets an owner connect even when the site has no Google connection row', async ({ page }) => {
  const controls = await installMocks(page, { holdConfig: true })
  await page.goto(`/sites/${siteId}/settings/connections`)

  const gsc = connectionCard(page, 'Google Search Console')
  const ga4 = connectionCard(page, 'Google Analytics 4')
  await expect(gsc.getByText(setupMessage, { exact: true })).toBeVisible()
  await expect(gsc.getByRole('link', { name: 'Connect with Google' })).toHaveCount(0)
  await expect(ga4.getByRole('link', { name: 'Connect with Google' })).toHaveCount(0)

  controls.releaseConfig()
  await expect(gsc.getByRole('link', { name: 'Connect with Google' })).toHaveAttribute('href', `/api/v1/sites/${siteId}/connections/gsc/oauth/start`)
  await expect(ga4.getByRole('link', { name: 'Connect with Google' })).toHaveAttribute('href', `/api/v1/sites/${siteId}/connections/ga4/oauth/start`)
  expect(controls.configSites.length).toBeGreaterThanOrEqual(2)
  expect(controls.configSites.every((requestedSiteId) => requestedSiteId === siteId)).toBe(true)
})

for (const config of ['missing', 'failed'] as const) {
  test(`${config} platform Google setup hides connect links and shows the platform admin message`, async ({ page }) => {
    await installMocks(page, { config })
    await page.goto(`/sites/${siteId}/settings/connections`)

    await expect(page.getByText(setupMessage, { exact: true })).toHaveCount(2)
    await expect(page.getByRole('link', { name: 'Connect with Google' })).toHaveCount(0)
    await expect(page.getByText('internal oauth provider failure', { exact: true })).toHaveCount(0)
  })
}

for (const role of ['owner', 'viewer'] as const) {
  test(`${role} role receives the expected Google connect access`, async ({ page }) => {
    await installMocks(page, { role, connections: [] })
    await page.goto(`/sites/${siteId}/settings/connections`)

    const gsc = page.locator('.connection-card').filter({ hasText: 'Google Search Console' }).first()
    if (role === 'owner') {
      await expect(gsc.getByRole('link', { name: 'Connect with Google' })).toBeVisible()
    } else {
      await expect(page.getByRole('link', { name: 'Connect with Google' })).toHaveCount(0)
      await expect(gsc.getByText('Owner access required', { exact: true })).toBeVisible()
    }
  })
}

test('Google forms expose property settings only and save settings without a credentials payload', async ({ page }) => {
  const controls = await installMocks(page, {
    connections: [
      { kind: 'gsc', status: 'needs_test', safe_fields: { site_url: 'sc-domain:google.example' } },
      { kind: 'ga4', status: 'needs_test', safe_fields: { property_id: '123456789', conversion_event_names: ['purchase'], dimensions: ['date'], metrics: ['sessions'] } },
    ],
  })
  await page.goto(`/sites/${siteId}/settings/connections`)

  const gsc = connectionCard(page, 'Google Search Console')
  const ga4 = connectionCard(page, 'Google Analytics 4')
  for (const label of ['Client ID', 'Client secret', 'Refresh token']) {
    await expect(page.getByLabel(label, { exact: true })).toHaveCount(0)
  }
  await expect(gsc.getByLabel('Property')).toHaveValue('sc-domain:google.example')
  await expect(ga4.getByLabel('Property ID')).toHaveValue('123456789')
  await expect(ga4.getByLabel('Conversion event names')).toHaveValue('purchase')

  await gsc.getByLabel('Property').fill('sc-domain:updated.example')
  await gsc.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Google Search Console settings saved.', { exact: true })).toBeVisible()
  await ga4.getByLabel('Reporting dimensions').fill('date, eventName')
  await ga4.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Google Analytics 4 settings saved.', { exact: true })).toBeVisible()

  expect(controls.connectionSaves).toEqual([
    { siteId, kind: 'gsc', body: { settings: { site_url: 'sc-domain:updated.example' } } },
    { siteId, kind: 'ga4', body: { settings: { property_id: '123456789', conversion_event_names: ['purchase'], dimensions: ['date', 'eventName'], metrics: ['sessions'] } } },
  ])
})
