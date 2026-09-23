import { expect, test, type Page, type Route } from '@playwright/test'

const ownerAuth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Alex Owner' },
  team: { id: 'team-1', name: 'Northstar team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const defaultSite = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Acme Studio',
  origin: 'https://acme.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: {
    business_name: 'Acme Studio',
    audience: 'Independent makers',
    locations: ['Austin, TX'],
    services: ['Design systems'],
    products: [],
    authors: [{ name: 'Alex Owner', id: 'author-1' }],
    confirmed_sources: [],
    brand_tone: 'Clear and practical',
  },
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installBusinessMocks(page: Page, options: { role?: 'owner' | 'editor' | 'viewer'; site?: typeof defaultSite; failSiteReads?: number } = {}) {
  const auth = { ...ownerAuth, role: options.role ?? 'owner' }
  let currentSite = structuredClone(options.site ?? defaultSite)
  let siteReadFailures = options.failSiteReads ?? 0
  const patches: unknown[] = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status' && method === 'GET') return json(route, { initialized: true })
    if (path === '/auth/me' && method === 'GET') return json(route, auth)
    if (path === '/sites' && method === 'GET') return json(route, { items: [currentSite], total: 1 })
    if (path === '/sites/site-1' && method === 'GET') {
      if (siteReadFailures > 0) {
        siteReadFailures -= 1
        return json(route, { detail: 'The site record is temporarily unavailable.' }, 503)
      }
      return json(route, currentSite)
    }
    if (path === '/sites/site-1' && method === 'PATCH') {
      const payload = request.postDataJSON() as { facts?: Record<string, unknown>; language?: string; timezone?: string }
      patches.push(payload)
      currentSite = { ...currentSite, ...payload, facts: { ...currentSite.facts, ...(payload.facts ?? {}) } }
      return json(route, currentSite)
    }
    if (path === '/sites/site-1/policy' && method === 'GET') return json(route, { id: 'policy-1', version: 3, settings: { enabled: false, allowed_actions: ['metadata'], protected_paths: ['/', '/contact*'], posts_per_week: 2, refreshes_per_week: 1, monthly_budget_cents: 30000, tracked_keywords: [], competitors: [], tracked_questions: [], publish_days: [1, 4], author_id: null } })
    if (path === '/settings' && method === 'GET') return json(route, { global_pause: true })
    if (path === '/sites/site-1/budgets' && method === 'GET') return json(route, { reservations: { items: [], total: 0 } })
    return json(route, { items: [], total: 0 })
  })

  return { patches }
}

test('owner can edit grounded business facts and verify the returned saved site', async ({ page }) => {
  const controls = await installBusinessMocks(page)
  await page.goto('/sites/site-1/settings/business')

  await expect(page.getByRole('heading', { name: 'Business facts' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Settings navigation' }).getByRole('link', { name: 'Business' })).toBeVisible()
  await expect(page.getByText('7/9 Provided', { exact: true })).toBeVisible()
  await page.getByLabel('Business name').fill('Acme Studio LLC')
  await page.getByLabel('Primary audience').fill('Independent product teams')
  await page.getByLabel('Language').fill('en-US')
  await page.getByLabel('Timezone').fill('America/Denver')
  await page.getByRole('textbox', { name: 'Locations', exact: true }).fill('Denver, CO')
  await page.getByRole('textbox', { name: 'Locations', exact: true }).press('Enter')
  await page.getByRole('textbox', { name: 'Products', exact: true }).fill('Research kits')
  await page.getByRole('textbox', { name: 'Products', exact: true }).press('Enter')
  await page.getByRole('textbox', { name: 'Source references', exact: true }).fill('https://acme.example/about')
  await page.getByRole('textbox', { name: 'Source references', exact: true }).press('Enter')
  await page.getByLabel('Author 1 name').fill('Jordan Lee')
  await page.getByLabel('Author 1 email').fill('jordan@example.com')
  await page.getByRole('button', { name: 'Save business facts' }).click()

  await expect(page.getByText('Business facts saved. The API returned the updated site state.')).toBeVisible()
  await expect(page.getByText('9/9 Provided', { exact: true })).toBeVisible()
  expect(controls.patches).toHaveLength(1)
  expect(controls.patches[0]).toMatchObject({
    language: 'en-US',
    timezone: 'America/Denver',
    facts: {
      business_name: 'Acme Studio LLC',
      audience: 'Independent product teams',
      locations: ['Austin, TX', 'Denver, CO'],
      products: ['Research kits'],
      authors: [{ name: 'Jordan Lee', id: 'author-1', email: 'jordan@example.com' }],
      confirmed_sources: ['https://acme.example/about'],
      brand_tone: 'Clear and practical',
    },
  })
})

test('viewer sees missing facts and read-only controls, while policy guardrails remain readable', async ({ page }) => {
  const blankSite = { ...structuredClone(defaultSite), language: '', timezone: '', facts: {} }
  await installBusinessMocks(page, { role: 'viewer', site: blankSite })
  await page.goto('/sites/site-1/settings/business')

  await expect(page.getByText('No provided facts are stored yet')).toBeVisible()
  await expect(page.getByText('Only the site owner can change business facts.')).toBeVisible()
  await expect(page.getByLabel('Business name')).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Owner access required' })).toBeDisabled()

  await page.goto('/sites/site-1/settings/policies')
  await expect(page.getByRole('heading', { name: 'Policy summary' })).toBeVisible()
  await expect(page.getByText('Automation is disabled. Review the summary below before an owner enables it.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Toggle automated workflows' })).toBeDisabled()
})

test('business facts recovers from a site read error with the real retry path', async ({ page }) => {
  await installBusinessMocks(page, { failSiteReads: 2 })
  await page.goto('/sites/site-1/settings/business')

  await expect(page.getByRole('alert')).toContainText('site record is temporarily unavailable')
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('heading', { name: 'Business facts' })).toBeVisible()
  await expect(page.locator('main').getByText('Acme Studio', { exact: true }).first()).toBeVisible()
})
