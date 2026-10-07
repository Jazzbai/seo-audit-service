import { expect, test, type Page, type Route } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

test.use({ timezoneId: 'America/Chicago' })

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
  let siteCreateBody: Record<string, unknown> | null = null
  let connectionSave: Record<string, unknown> | null = null

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, siteCreated ? { items: [site], total: 1 } : { items: [], total: 0 })
    if (path === '/sites' && request.method() === 'POST') {
      siteCreateRequests += 1
      siteCreateBody = request.postDataJSON() as Record<string, unknown>
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

  return { getSiteCreateRequests: () => siteCreateRequests, getSiteCreateBody: () => siteCreateBody, getConnectionSave: () => connectionSave }
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

for (const width of [1440, 390]) {
  test(`timezone dropdown offers worldwide choices and submits the selected identifier at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const controls = await installMocks(page)
    await page.goto('/sites/new')
    const timezone = page.getByRole('combobox', { name: 'Timezone' })
    await expect(timezone).toHaveValue('America/Chicago')
    await expect(timezone.locator('option[value="America/Chicago"]')).toContainText('Central Time')
    expect(await timezone.locator('option').count()).toBeGreaterThan(400)
    for (const id of ['Europe/London', 'Asia/Tokyo', 'UTC']) {
      await timezone.selectOption(id)
      await expect(timezone).toHaveValue(id)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
    await fillRequiredSiteFacts(page)
    await page.getByRole('button', { name: 'Create site' }).click()
    await expect(page).toHaveURL(/\/sites\/site-new\/overview$/)
    expect(controls.getSiteCreateBody()).toMatchObject({ timezone: 'UTC' })
  })
}

test('timezone dropdown retains worldwide choices without the Intl enumeration API', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(Intl, 'supportedValuesOf', { value: undefined, configurable: true }))
  await installMocks(page)
  await page.goto('/sites/new')
  const timezone = page.getByRole('combobox', { name: 'Timezone' })
  await expect.poll(() => timezone.locator('option').count()).toBeGreaterThan(400)
  await timezone.selectOption('Australia/Sydney')
  await expect(timezone).toHaveValue('Australia/Sydney')
})

async function pasteList(page: Page, label: string, text: string) {
  const input = page.getByRole('textbox', { name: label, exact: true })
  await input.focus()
  await input.evaluate((element, value) => {
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', value)
    element.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }))
  }, text)
}

test('onboarding accepts pasted comma and multiline lists across every chip field', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/new')
  await fillRequiredSiteFacts(page)
  await pasteList(page, 'Brand tone', 'Friendly, Relaxing\nReliable, Friendly')
  await pasteList(page, 'Locations', ' Houston, Katy\r\nSugar Land, , Houston ')
  await pasteList(page, 'Services', 'Family dentistry,\nTeeth cleaning\r\nWhitening')
  await pasteList(page, 'Products', 'Night guards\nToothbrushes, Night guards')
  await pasteList(page, 'Authors', 'Dr. Alex Morgan, Dr. Jordan Lee\nDr. Alex Morgan')
  await pasteList(page, 'Confirmed sources', 'https://northstar.example/about\nOwner interview, https://northstar.example/about')
  await expect(page.getByRole('button', { name: 'Remove Houston', exact: true })).toHaveCount(1)
  await page.getByRole('button', { name: 'Create site' }).click()
  await expect(page).toHaveURL(/\/sites\/site-new\/overview$/)
  expect(controls.getSiteCreateBody()).toMatchObject({ facts: {
    brand_tone: 'Friendly, Relaxing, Reliable',
    locations: ['Houston', 'Katy', 'Sugar Land'],
    services: ['Family dentistry', 'Teeth cleaning', 'Whitening'],
    products: ['Night guards', 'Toothbrushes'],
    authors: [{ name: 'Dr. Alex Morgan' }, { name: 'Dr. Jordan Lee' }],
    confirmed_sources: ['https://northstar.example/about', 'Owner interview'],
  } })
})

test('typed separators, Enter, plus and blur preserve individual items without submitting early', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/new')
  await fillRequiredSiteFacts(page)
  const input = page.getByRole('textbox', { name: 'Services', exact: true })
  await input.pressSequentially('Cleaning,')
  await expect(page.getByRole('button', { name: 'Remove Cleaning', exact: true })).toBeVisible()
  await input.fill('Whitening')
  await input.press('Enter')
  expect(controls.getSiteCreateRequests()).toBe(0)
  await input.fill('Implants')
  await page.getByRole('button', { name: 'Add services', exact: true }).click()
  await input.fill('Crowns')
  await input.press('Shift+Enter')
  await input.pressSequentially('Bridges')
  await page.getByRole('button', { name: 'Remove Whitening', exact: true }).click()
  // Clicking Create site commits the remaining multiline draft before submission.
  await page.getByRole('button', { name: 'Create site' }).click()
  await expect(page).toHaveURL(/\/sites\/site-new\/overview$/)
  expect(controls.getSiteCreateBody()).toMatchObject({ facts: { services: ['Cleaning', 'Implants', 'Crowns', 'Bridges'] } })
})

test('quoted values retain commas and pasted text replaces the current selection', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/new')
  await fillRequiredSiteFacts(page)
  await pasteList(page, 'Locations', '"Houston, TX", "Austin, TX"\n"Houston, TX"')
  const input = page.getByRole('textbox', { name: 'Locations', exact: true })
  await input.pressSequentially('"Denver, CO"')
  await input.press('Enter')
  await expect(page.getByRole('button', { name: 'Remove Denver, CO', exact: true })).toBeVisible()
  const services = page.getByRole('textbox', { name: 'Services', exact: true })
  await services.fill('Old draft')
  await services.selectText()
  await pasteList(page, 'Services', 'Cleaning, Whitening')
  await page.getByRole('button', { name: 'Create site' }).click()
  await expect(page).toHaveURL(/\/sites\/site-new\/overview$/)
  expect(controls.getSiteCreateBody()).toMatchObject({ facts: {
    locations: ['Houston, TX', 'Austin, TX', 'Denver, CO'], services: ['Cleaning', 'Whitening'],
  } })
})

for (const width of [1440, 390]) {
  test(`populated chip fields remain accessible without horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await installMocks(page)
    await page.goto('/sites/new')
    await pasteList(page, 'Locations', 'Houston, Katy\nSugar Land')
    await pasteList(page, 'Confirmed sources', `https://northstar.example/${'long-reference-'.repeat(18)}\nOwner interview`)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  })
}
