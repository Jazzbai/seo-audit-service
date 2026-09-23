import { expect, test, type Route } from '@playwright/test'

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
  facts: {},
}

const policy = {
  enabled: false,
  allowed_actions: ['metadata'],
  protected_paths: ['/', '/contact*'],
  posts_per_week: 2,
  refreshes_per_week: 1,
  monthly_budget_cents: 30000,
  tracked_keywords: [],
  competitors: [],
  tracked_questions: [],
  publish_days: [1, 4],
  author_id: null,
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

test('policy cadence fields prevent values above the API limits before save', async ({ page }) => {
  const policyUpdates: Array<Record<string, unknown>> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/policy' && request.method() === 'GET') return json(route, { id: 'policy-1', version: 4, settings: policy })
    if (path === '/sites/site-1/policy' && request.method() === 'PUT') {
      const body = request.postDataJSON() as { settings: Record<string, unknown> }
      policyUpdates.push(body.settings)
      return json(route, { id: 'policy-1', version: 5, settings: body.settings })
    }
    if (path === '/settings') return json(route, { global_pause: true })
    if (path === '/sites/site-1' && request.method() === 'GET') return json(route, site)
    if (path === '/sites/site-1/connections') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/pages') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/budgets') return json(route, { reservations: { items: [], total: 0 } })
    return json(route, { items: [], total: 0 })
  })

  await page.goto('/sites/site-1/settings/policies')

  const posts = page.locator('label.field').filter({ hasText: 'Posts per week' }).locator('input')
  const refreshes = page.locator('label.field').filter({ hasText: 'Refreshes per week' }).locator('input')
  const save = page.getByRole('button', { name: 'Save policy controls', exact: true })

  await expect(posts).toHaveAttribute('max', '2')
  await expect(refreshes).toHaveAttribute('max', '1')
  await expect(page.getByText('API limit: up to 2 posts per week.', { exact: true })).toBeVisible()
  await expect(page.getByText('API limit: up to 1 refresh per week.', { exact: true })).toBeVisible()

  await posts.fill('3')
  await refreshes.fill('2')
  await expect(page.getByText('The API allows no more than 2 posts per week.', { exact: true })).toBeVisible()
  await expect(page.getByText('The API allows no more than 1 refresh per week.', { exact: true })).toBeVisible()
  await expect(save).toBeDisabled()
  expect(policyUpdates).toHaveLength(0)

  await posts.fill('2')
  await refreshes.fill('1')
  await expect(save).toBeEnabled()
  await save.click()
  await expect(page.getByText('Policy and pause controls saved as new server state.', { exact: true })).toBeVisible()
  expect(policyUpdates).toHaveLength(1)
  expect(policyUpdates[0]).toMatchObject({ posts_per_week: 2, refreshes_per_week: 1 })
})
