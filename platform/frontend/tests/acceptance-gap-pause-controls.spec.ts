import { expect, test, type Page, type Route } from '@playwright/test'

const baseSite = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Pause Control Pilot',
  origin: 'https://pilot.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: false,
  facts: {},
}

const basePolicy = {
  enabled: false,
  allowed_actions: ['metadata'],
  protected_paths: ['/'],
  posts_per_week: 2,
  refreshes_per_week: 1,
  monthly_budget_cents: 30000,
  tracked_keywords: [],
  competitors: [],
  tracked_questions: [],
  publish_days: [1, 4],
  author_id: null,
}

type Role = 'owner' | 'editor' | 'viewer'

type PauseControls = {
  policyUpdates: Array<Record<string, unknown>>
  siteUpdates: Array<Record<string, unknown>>
  globalPauseUpdates: Array<Record<string, unknown>>
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installPauseMocks(page: Page, role: Role): Promise<PauseControls> {
  const state = {
    sitePaused: false,
    globalPause: false,
    policy: { ...basePolicy },
    policyVersion: 4,
  }
  const controls: PauseControls = { policyUpdates: [], siteUpdates: [], globalPauseUpdates: [] }

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    const site = { ...baseSite, paused: state.sitePaused }

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') {
      return json(route, {
        user: { id: `${role}-1`, email: `${role}@example.com`, name: role },
        team: { id: 'team-1', name: 'Pause Control Team' },
        role,
        csrf_token: 'csrf-test-token',
      })
    }
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/policy' && method === 'GET') {
      return json(route, { id: 'policy-1', version: state.policyVersion, settings: state.policy })
    }
    if (path === '/sites/site-1/policy' && method === 'PUT') {
      const body = (request.postDataJSON() ?? {}) as { settings?: Record<string, unknown> }
      const settings = body.settings ?? {}
      controls.policyUpdates.push(settings)
      state.policy = { ...state.policy, ...settings }
      state.policyVersion += 1
      return json(route, { id: 'policy-1', version: state.policyVersion, settings: state.policy })
    }
    if (path === '/settings' && method === 'GET') return json(route, { global_pause: state.globalPause })
    if (path === '/settings' && method === 'PATCH') {
      const body = (request.postDataJSON() ?? {}) as Record<string, unknown>
      controls.globalPauseUpdates.push(body)
      state.globalPause = body.global_pause === true
      return json(route, { global_pause: state.globalPause })
    }
    if (path === '/sites/site-1' && method === 'GET') return json(route, site)
    if (path === '/sites/site-1' && method === 'PATCH') {
      const body = (request.postDataJSON() ?? {}) as Record<string, unknown>
      controls.siteUpdates.push(body)
      if (typeof body.paused === 'boolean') state.sitePaused = body.paused
      return json(route, { ...baseSite, paused: state.sitePaused })
    }
    if (path === '/sites/site-1/connections' && method === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/pages' && method === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/budgets' && method === 'GET') return json(route, { reservations: { items: [], total: 0 } })
    return json(route, { items: [], total: 0 })
  })

  return controls
}

function policySummary(page: Page) {
  return page.locator('section.panel').filter({ hasText: 'Policy summary' }).first()
}

test('owner can persist site and global pause controls and sees the saved state', async ({ page }) => {
  const controls = await installPauseMocks(page, 'owner')
  await page.goto('/sites/site-1/settings/policies')

  const sitePause = page.getByRole('button', { name: 'Toggle site pause' })
  const globalPause = page.getByRole('button', { name: 'Toggle global pause' })
  await expect(sitePause).toHaveAttribute('aria-pressed', 'false')
  await expect(globalPause).toHaveAttribute('aria-pressed', 'false')

  await sitePause.click()
  await globalPause.click()
  await expect(sitePause).toHaveAttribute('aria-pressed', 'true')
  await expect(globalPause).toHaveAttribute('aria-pressed', 'true')
  await expect(policySummary(page).locator('.metric-row').filter({ hasText: 'Site pause' })).toContainText('Paused')
  await expect(policySummary(page).locator('.metric-row').filter({ hasText: 'Workspace emergency pause' })).toContainText('Paused')

  await page.getByRole('button', { name: 'Save policy controls', exact: true }).click()
  await expect(page.getByText('Policy and pause controls saved as new server state.', { exact: true })).toBeVisible()

  expect(controls.policyUpdates).toHaveLength(1)
  expect(controls.siteUpdates).toEqual([{ paused: true }])
  expect(controls.globalPauseUpdates).toEqual([{ global_pause: true }])
  await expect(sitePause).toHaveAttribute('aria-pressed', 'true')
  await expect(globalPause).toHaveAttribute('aria-pressed', 'true')
  await expect(policySummary(page).locator('.metric-row').filter({ hasText: 'Site pause' })).toContainText('Paused')
  await expect(policySummary(page).locator('.metric-row').filter({ hasText: 'Workspace emergency pause' })).toContainText('Paused')
})

test('non-owner cannot use the global emergency pause and no pause update is sent', async ({ page }) => {
  const controls = await installPauseMocks(page, 'editor')
  await page.goto('/sites/site-1/settings/policies')

  await expect(page.getByText('Read-only for your role', { exact: true })).toBeVisible()
  const sitePause = page.getByRole('button', { name: 'Toggle site pause' })
  const globalPause = page.getByRole('button', { name: 'Toggle global pause' })
  await expect(sitePause).toBeDisabled()
  await expect(globalPause).toBeDisabled()
  await expect(globalPause).toHaveAttribute('aria-pressed', 'false')

  await globalPause.evaluate((button) => (button as HTMLButtonElement).click())

  expect(controls.globalPauseUpdates).toHaveLength(0)
  expect(controls.siteUpdates).toHaveLength(0)
  expect(controls.policyUpdates).toHaveLength(0)
  await expect(globalPause).toHaveAttribute('aria-pressed', 'false')
  await expect(policySummary(page).locator('.metric-row').filter({ hasText: 'Workspace emergency pause' })).toContainText('Not paused')
})
