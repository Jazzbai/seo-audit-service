import { expect, test, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

type Site = { id: string; name: string; origin: string; paused: boolean; archived_at: string | null; timezone: string; language: string; facts: object }
const initialSites: Site[] = [
  { id: 'auto', name: 'Auto One Stop', origin: 'https://auto.example', paused: true, archived_at: null, timezone: 'America/Chicago', language: 'en', facts: {} },
  { id: 'dental', name: 'Dental Practice', origin: 'https://dental.example', paused: true, archived_at: null, timezone: 'America/Chicago', language: 'en', facts: {} },
]

async function mockWorkspace(page: Page, options: { sites?: Site[]; role?: string; archiveFails?: boolean } = {}) {
  let sites = (options.sites ?? initialSites).map((s) => ({ ...s }))
  const writes: string[] = []
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1', '')
    const send = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/auth/status') return send({ initialized: true })
    if (path === '/auth/me') return send({ user: { id: 'owner', email: 'owner@example.com', name: 'Owner' }, team: { id: 'team', name: 'My team' }, role: options.role ?? 'owner', csrf_token: 'test-csrf' })
    if (path === '/sites' && request.method() === 'GET') {
      const visible = sites.filter((s) => url.searchParams.get('include_archived') === 'true' || !s.archived_at)
      const offset = Number(url.searchParams.get('offset') ?? 0), limit = Number(url.searchParams.get('limit') ?? 50)
      return send({ items: visible.slice(offset, offset + limit), total: visible.length })
    }
    const change = path.match(/^\/sites\/([^/]+)\/(archive|restore)$/)
    if (change && request.method() === 'POST') {
      writes.push(path)
      if (options.archiveFails) return send({ detail: 'This site has work in progress. Try again when its job finishes.' }, 409)
      sites = sites.map((s) => s.id === change[1] ? { ...s, paused: true, archived_at: change[2] === 'archive' ? '2026-10-07T12:00:00Z' : null } : s)
      return send(sites.find((s) => s.id === change[1]))
    }
    const overview = path.match(/^\/sites\/([^/]+)\/overview$/)
    if (overview) return send({ site: sites.find((s) => s.id === overview[1]), counts: {}, monitoring: { status: 'not_running', site_paused: true }, budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 }, recent_events: [], coverage: { status: 'not_checked' }, connections: [] })
    return send({ items: [], total: 0 })
  })
  return writes
}

test('owner can find setup from the sidebar and switch between dashboards', async ({ page }) => {
  await mockWorkspace(page)
  await page.goto('/sites/auto/overview')
  await page.getByRole('combobox', { name: 'Choose a site' }).selectOption('dental')
  await expect(page).toHaveURL(/\/sites\/dental\/overview$/)
  await page.getByRole('link', { name: 'Add site', exact: true }).click()
  await expect(page).toHaveURL(/\/sites\/new$/)
  await page.getByRole('link', { name: 'Back to Manage sites' }).click()
  await expect(page.getByRole('heading', { name: 'Manage sites' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Dental Practice' })).toBeVisible()
})

test('last active site can be archived and restored without leaving the management screen', async ({ page }) => {
  const writes = await mockWorkspace(page, { sites: [initialSites[0]] })
  await page.goto('/sites')
  await page.getByRole('button', { name: 'Archive site', exact: true }).click()
  expect(writes).toEqual([])
  await expect(page.getByRole('group', { name: 'Archive Auto One Stop' })).toContainText('Your website stays online')
  await page.getByRole('button', { name: 'Confirm archive' }).click()
  await expect(page.getByText('Add your first active site', { exact: true })).toBeVisible()
  await expect(page).toHaveURL(/\/sites$/)
  await page.getByRole('button', { name: 'Archived sites (1)', exact: true }).click()
  await page.getByRole('button', { name: 'Restore site' }).click()
  await expect(page.getByText('Auto One Stop restored. Automation is paused.', { exact: false })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Open dashboard' })).toBeVisible()
  expect(writes).toEqual(['/sites/auto/archive', '/sites/auto/restore'])
})

test('an unsuccessful archive leaves the site available and explains why', async ({ page }) => {
  await mockWorkspace(page, { sites: [initialSites[0]], archiveFails: true })
  await page.goto('/sites')
  await page.getByRole('button', { name: 'Archive site', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm archive' }).click()
  await expect(page.getByRole('alert')).toContainText('work in progress')
  await expect(page.getByRole('link', { name: 'Open dashboard' })).toBeVisible()
})

test('viewer can switch and open sites but cannot add, archive, or restore them', async ({ page }) => {
  await mockWorkspace(page, { role: 'viewer' })
  await page.goto('/sites')
  await expect(page.getByRole('link', { name: 'Open dashboard' })).toHaveCount(2)
  await expect(page.getByRole('link', { name: 'Add site', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Archive site' })).toHaveCount(0)
  await page.goto('/sites/new')
  await expect(page).toHaveURL(/\/sites$/)
})

test('management loads every site beyond the first API page and supports search', async ({ page }) => {
  const sites = Array.from({ length: 205 }, (_, n) => ({ ...initialSites[0], id: `site-${n}`, name: `Practice ${n}`, origin: `https://practice${n}.example` }))
  await mockWorkspace(page, { sites })
  await page.goto('/sites')
  await page.getByRole('searchbox', { name: 'Search sites' }).fill('Practice 204')
  await expect(page.getByRole('heading', { name: 'Practice 204', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Active sites (205)', exact: true })).toBeVisible()
})

test('mobile navigation exposes site controls and management is accessible', async ({ page }) => {
  await mockWorkspace(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/sites')
  await expect(page.getByRole('heading', { name: 'Manage sites' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await expect(page.getByLabel('Primary navigation').getByRole('link', { name: 'Add site', exact: true })).toBeVisible()
  await expect(page.getByLabel('Primary navigation').getByRole('link', { name: 'Manage sites', exact: true })).toBeVisible()
})
