import { expect, test } from '@playwright/test'

test('incomplete inventory explains the cause in run history without echoing provider text', async ({ page }) => {
  const site = { id: 'site-1', name: 'Test Workshop', origin: 'https://workshop.example', paused: true }
  const items = ['limit', 'pagination', 'records'].map((issue, index) => ({
    id: `job-${index}`, kind: 'inventory', status: index === 0 ? 'failed' : 'retry', attempts: 1,
    result: { error_type: 'IncompleteInventory', inventory_issue: issue, reason: 'provider-secret-must-not-display' },
  }))
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = { items: [], total: 0 }
    if (path === '/api/v1/auth/status') body = { initialized: true }
    if (path === '/api/v1/auth/me') body = {
      user: { id: 'user-1', email: 'owner@example.test', name: 'Owner' },
      team: { id: 'team-1', name: 'Test' }, role: 'owner', csrf_token: 'fixture-csrf',
    }
    if (path === '/api/v1/sites') body = { items: [site], total: 1 }
    if (path === '/api/v1/sites/site-1') body = site
    if (path === '/api/v1/sites/site-1/jobs') body = { items, total: items.length }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.goto('/sites/site-1/jobs')
  await expect(page.getByRole('heading', { name: 'Run history' })).toBeVisible()
  await expect(page.getByText(/collection exceeds the current limit for one run/)).toBeVisible()
  await expect(page.getByText(/site returned inconsistent pagination/)).toBeVisible()
  await expect(page.getByText(/site returned invalid or repeated records/)).toBeVisible()
  await expect(page.getByText('provider-secret-must-not-display')).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: 'Run history' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
