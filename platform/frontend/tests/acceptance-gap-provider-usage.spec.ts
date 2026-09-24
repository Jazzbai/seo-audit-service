import { expect, test, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

// Explicit browser fixtures; no provider or WordPress calls are made here.
async function mocks(page: Page, generation: unknown = {}) {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    const site = { id: 'site-usage', name: 'Usage fixture', origin: 'https://usage.example', facts: {}, paused: true }
    let body: unknown = { items: [], total: 0 }
    if (path === '/auth/status') body = { initialized: true }
    if (path === '/auth/me') body = { user: { id: 'u', name: 'Reviewer', email: 'viewer@example.test' }, team: { id: 't', name: 'Fixture' }, role: 'viewer', csrf_token: 'test-csrf' }
    if (path === '/sites') body = { items: [site], total: 1 }
    if (path === '/sites/site-usage') body = site
    if (path === '/settings') body = { global_pause: true }
    if (path === '/sites/site-usage/policy') body = { version: 1, settings: { enabled: false, allowed_actions: [], protected_paths: ['/'], tracked_keywords: [], competitors: [], tracked_questions: [], publish_days: [], monthly_budget_cents: 30000, posts_per_week: 2, refreshes_per_week: 1 } }
    if (path === '/sites/site-usage/articles/article-usage') body = { id: 'article-usage', site_id: site.id, title: 'Source-backed review draft', body: '<p>Draft.</p>', brief: { generation }, sources: [], status: 'review_needed', author_id: null, checks: { passed: false, blockers: ['missing_author'] } }
    if (path === '/sites/site-usage/budgets') body = {
      accounts: { items: [], total: 0 }, reservations: { items: [
        { id: 'held', status: 'reserved', estimated_cents: 50, actual_cents: null },
        { id: 'released', status: 'released', estimated_cents: 75, actual_cents: null },
        { id: 'settled', status: 'settled', estimated_cents: 50, actual_cents: 3 },
        { id: 'free', status: 'settled', estimated_cents: 50, actual_cents: 0 },
      ], total: 4 },
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}

for (const width of [1440, 390]) {
  test(`article displays numeric usage separately from estimated costs at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mocks(page, { usage: { input_tokens: 4279, output_tokens: 856, total_tokens: 5135 }, estimated_cost_cents: 10, max_cost_cents: 50 })
    await page.goto('/sites/site-usage/content/articles/article-usage')
    const usage = page.getByRole('region', { name: 'Provider usage' })
    await expect(usage.getByText('4,279', { exact: true })).toBeVisible()
    await expect(usage.getByText('856', { exact: true })).toBeVisible()
    await expect(usage.getByText('5,135', { exact: true })).toBeVisible()
    await expect(usage.getByText('$0.10', { exact: true })).toBeVisible()
    await expect(usage.getByText('$0.50', { exact: true })).toBeVisible()
    await expect(usage).toContainText('not actual charges')
    await expect(usage.getByRole('link')).toHaveAttribute('href', '/sites/site-usage/settings/policies')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect((await new AxeBuilder({ page }).include('[aria-label="Provider usage"]').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()).violations).toEqual([])
  })
}

test('missing or malformed metering does not become zero usage or expose provider strings', async ({ page }) => {
  await mocks(page, { usage: { input_tokens: '[redacted]', output_tokens: true, total_tokens: 'credential-shaped-value' }, estimated_cost_cents: -1 })
  await page.goto('/sites/site-usage/content/articles/article-usage')
  const usage = page.getByRole('region', { name: 'Provider usage' })
  await expect(usage.getByText('Not recorded', { exact: true })).toHaveCount(3)
  await expect(usage.getByText('Unknown', { exact: true })).toHaveCount(2)
  await expect(usage).not.toContainText('credential-shaped-value')
  await expect(usage).not.toContainText('$0.00')
})

test('held, released, settled and explicitly zero billing remain distinct', async ({ page }) => {
  await mocks(page)
  await page.goto('/sites/site-usage/settings/policies')
  const rows = page.getByRole('list', { name: 'Provider reservations' }).getByRole('listitem')
  await expect(rows.nth(0)).toContainText('held $0.50 (not measured spending); actual charge: Unknown')
  await expect(rows.nth(1)).toContainText('no longer held; actual charge: Unknown')
  await expect(rows.nth(1)).not.toContainText('actual charge: $0.75')
  await expect(rows.nth(2)).toContainText('actual charge: $0.03')
  await expect(rows.nth(3)).toContainText('actual charge: $0.00')
})
