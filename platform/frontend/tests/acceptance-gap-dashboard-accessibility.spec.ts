import { expect, test, type Page, type Route } from '@playwright/test'

const widths = [1440, 1280, 390] as const

const auth = {
  user: { id: 'owner-1', email: 'owner@example.test', name: 'Audit Owner' },
  team: { id: 'team-1', name: 'Audit Workspace' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Auto1Stop QA Fixture',
  origin: 'https://fixture.example.test',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: {},
}

const policy = {
  enabled: false,
  allowed_actions: ['metadata'],
  protected_paths: ['/', '/contact*'],
  posts_per_week: 2,
  refreshes_per_week: 1,
  monthly_budget_cents: 30000,
  tracked_keywords: ['fixture keyword'],
  competitors: [],
  tracked_questions: [],
  publish_days: [1, 4],
  author_id: null,
}

const scheduledAt = (() => {
  const date = new Date()
  date.setHours(12, 0, 0, 0)
  return date.toISOString()
})()

const article = {
  id: 'article-1',
  site_id: 'site-1',
  title: 'A scheduled accessibility audit article',
  slug: 'scheduled-accessibility-audit',
  body: '<p>Fixture article content.</p>',
  status: 'scheduled',
  brief: {},
  checks: { passed: true, blockers: [], warnings: [] },
  sources: [],
  author_id: null,
  scheduled_at: scheduledAt,
  managed: true,
  updated_at: scheduledAt,
}

const findings = [{
  id: 'finding-1',
  key: 'fixture-title-missing',
  code: 'missing_title',
  severity: 'medium',
  title: 'A populated finding for accessibility checks',
  details: { summary: 'The fixture page has a reviewable title finding.' },
  status: 'open',
  first_seen_at: scheduledAt,
  last_seen_at: scheduledAt,
}]

const candidates = [{
  id: 'candidate-1',
  page_id: 'page-1',
  field: 'title',
  before_value: 'Current fixture title',
  after_value: 'Suggested fixture title',
  source_hash: 'fixture-source-hash',
  status: 'pending',
  details: { review_only_reasons: ['SEO metadata write is unsupported in this fixture.'] },
  created_at: scheduledAt,
  page: { id: 'page-1', url: 'https://fixture.example.test/service', resource_key: '/service', title: 'Fixture service page' },
}]

const report = {
  generated_at: scheduledAt,
  period_start: scheduledAt,
  period_end: scheduledAt,
  site: { id: 'site-1', name: site.name, origin: site.origin },
  overview: { counts: { pages: 60, open_findings: 3, pending_candidates: 1, published_articles: 0, open_incidents: 0 } },
  measurements: { total: 2, items: [{ kind: 'fixture', value: 12 }, { kind: 'fixture', value: 18 }] },
  events: { total: 4, items: Array.from({ length: 12 }, (_, index) => ({ id: `event-${index}`, detail: `Fixture report event ${index} ${'evidence '.repeat(12)}` })) },
  publications: { total: 0, items: [] },
  budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function mockDashboardApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/settings') return json(route, { global_pause: true })
    if (path === '/sites/site-1' && request.method() === 'GET') return json(route, site)
    if (path === '/sites/site-1/policy') return json(route, { id: 'policy-1', version: 4, settings: policy })
    if (path === '/sites/site-1/connections') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/authors') {
      return json(route, { items: [], complete: true, checked_at: scheduledAt, authenticated_user_id: 'writer-1', blockers: [], warnings: [] })
    }
    if (path === '/sites/site-1/budgets') return json(route, { reservations: { items: [], total: 0 } })
    if (path === '/sites/site-1/articles') return json(route, { items: [article], total: 1 })
    if (path === '/sites/site-1/findings') return json(route, { items: findings, total: findings.length })
    if (path === '/sites/site-1/candidates') return json(route, { items: candidates, total: candidates.length })
    if (path === '/sites/site-1/reports/weekly') return json(route, report)
    return json(route, { items: [], total: 0 })
  })
}

async function expectNoDocumentOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }))
  expect(dimensions.scrollWidth, `document width ${dimensions.scrollWidth}px should fit viewport ${dimensions.clientWidth}px`).toBeLessThanOrEqual(dimensions.clientWidth)
}

function colorContrast(foreground: string, background: string) {
  const channels = (value: string) => value.match(/[\d.]+/g)?.slice(0, 3).map(Number) ?? []
  const luminance = (value: string) => {
    const rgb = channels(value).map((channel) => {
      const normalized = channel / 255
      return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
  }
  const [lighter, darker] = [luminance(foreground), luminance(background)].sort((a, b) => b - a)
  return (lighter + 0.05) / (darker + 0.05)
}

for (const width of widths) {
  test(`populated policy page fits ${width}px and keeps weekday checkboxes keyboard usable`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockDashboardApi(page)
    await page.goto('/sites/site-1/settings/policies')

    await expect(page.getByRole('heading', { name: 'Policy & budget' })).toBeVisible()
    await expect(page.getByText('fixture keyword', { exact: true })).toBeVisible()
    await expectNoDocumentOverflow(page)

    const weekday = page.locator('.day-picker input').first()
    await expect(weekday).not.toBeChecked()
    for (let step = 0; step < 120 && !(await weekday.evaluate((element) => element === document.activeElement)); step += 1) {
      await page.keyboard.press('Tab')
    }
    await expect(weekday).toBeFocused()
    const focusIndicator = await weekday.evaluate((element) => {
      const style = getComputedStyle(element.nextElementSibling as HTMLElement)
      return { width: style.outlineWidth, style: style.outlineStyle }
    })
    expect(focusIndicator).toEqual({ width: '3px', style: 'solid' })
    await page.keyboard.press('Space')
    await expect(weekday).toBeChecked()
  })

  test(`populated Issues page fits ${width}px, preserves diff contrast, and exposes keyboard table scrolling`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockDashboardApi(page)
    await page.goto('/sites/site-1/issues')

    await expect(page.getByRole('heading', { name: 'Issues', exact: true })).toBeVisible()
    await expect(page.getByText('A populated finding for accessibility checks')).toBeVisible()
    await expect(page.locator('.candidate-diff .diff-box.after')).toContainText('Suggested fixture title')
    await expectNoDocumentOverflow(page)

    const labelContrasts = await page.locator('.diff-label').evaluateAll((labels) => labels.map((label) => {
      const foreground = getComputedStyle(label).color
      const background = getComputedStyle(label.parentElement as HTMLElement).backgroundColor
      return { foreground, background }
    }))
    expect(labelContrasts).toHaveLength(2)
    for (const colors of labelContrasts) expect(colorContrast(colors.foreground, colors.background)).toBeGreaterThanOrEqual(4.5)

    const tableRegion = page.getByRole('region', { name: 'Open findings table' })
    await expect(tableRegion).toHaveAttribute('tabindex', '0')
    await expect(tableRegion).toBeVisible()
    if (width === 390) {
      await tableRegion.focus()
      await page.keyboard.press('ArrowRight')
      await expect.poll(() => tableRegion.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0)
    }
  })

  test(`populated calendar fits ${width}px and exposes horizontal scrolling to the keyboard`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockDashboardApi(page)
    await page.goto('/sites/site-1/content')

    await expect(page.getByRole('heading', { name: 'Calendar', exact: true })).toBeVisible()
    await expect(page.getByText(article.title, { exact: true }).first()).toBeVisible()
    await expectNoDocumentOverflow(page)

    const calendar = page.getByRole('region', { name: 'This week' })
    await expect(calendar).toHaveAttribute('role', 'region')
    await expect(calendar).toHaveAttribute('aria-label', 'This week')
    await expect(calendar).toHaveAttribute('tabindex', '0')
    if (width === 390) {
      await calendar.focus()
      await page.keyboard.press('ArrowRight')
      await expect.poll(() => calendar.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0)
    }
  })

  test(`populated weekly report fits ${width}px and exposes raw data scrolling to the keyboard`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockDashboardApi(page)
    await page.goto('/sites/site-1/reports/weekly')

    await expect(page.getByRole('heading', { name: 'Weekly report', exact: true })).toBeVisible()
    await expect(page.getByText('60', { exact: true }).first()).toBeVisible()
    await expectNoDocumentOverflow(page)

    const rawReport = page.getByRole('region', { name: 'Raw report response' })
    await expect(rawReport).toContainText('Fixture report event 11')
    await expect(rawReport).toHaveAttribute('role', 'region')
    await expect(rawReport).toHaveAttribute('aria-label', 'Raw report response')
    await expect(rawReport).toHaveAttribute('tabindex', '0')
    await rawReport.focus()
    await page.keyboard.press('PageDown')
    await expect.poll(() => rawReport.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
  })
}

for (const { path, initialized, heading } of [
  { path: '/login', initialized: true, heading: 'Sign in to ForgeSEO' },
  { path: '/bootstrap', initialized: false, heading: 'Create the first owner account' },
]) {
  for (const width of [1440, 390]) {
    test(`${path} skip link focuses main content at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.route('**/api/v1/**', async (route) => {
        const pathName = new URL(route.request().url()).pathname.replace('/api/v1', '')
        if (pathName === '/auth/status') return json(route, { initialized })
        if (pathName === '/auth/me') return json(route, { detail: 'Not authenticated' }, 401)
        return json(route, { detail: 'Unexpected request' }, 404)
      })
      await page.goto(path)

      await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible()
      await page.keyboard.press('Tab')
      const skipLink = page.getByRole('link', { name: 'Skip to content' })
      await expect(skipLink).toBeFocused()
      await page.keyboard.press('Enter')

      const main = page.locator('#main-content')
      await expect(main).toBeFocused()
      await expect(main).toHaveAttribute('tabindex', '-1')
      await expect(page).toHaveURL(/#main-content$/)
    })
  }
}
