import { expect, test, type Page, type Route } from '@playwright/test'

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
  facts: { business_name: 'Pilot Workshop' },
}

const basePolicy = {
  enabled: false,
  allowed_actions: ['metadata'],
  protected_paths: ['/', '/contact*', '/checkout*'],
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

async function installSettingsMocks(page: Page, options: { connections?: unknown[]; policy?: Record<string, unknown>; authors?: unknown[] } = {}) {
  const connections = options.connections ?? []
  const policy = { ...basePolicy, ...(options.policy ?? {}) }
  const authors = options.authors ?? []
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/policy') return json(route, { id: 'policy-1', version: 4, settings: policy })
    if (path === '/settings') return json(route, { global_pause: true })
    if (path === '/sites/site-1' && request.method() === 'GET') return json(route, site)
    if (path === '/sites/site-1/connections') return json(route, { items: connections, total: connections.length })
    if (path === '/sites/site-1/pages') return json(route, { items: authors, total: authors.length })
    if (path === '/sites/site-1/budgets') return json(route, { reservations: { items: [], total: 0 } })
    return json(route, { items: [], total: 0 })
  })
}

test('settings explains external setup work before automation is enabled', async ({ page }) => {
  await installSettingsMocks(page)
  await page.goto('/sites/site-1/settings/policies')

  const checklist = page.getByRole('list', { name: 'Automation readiness checklist' })
  await expect(page.getByRole('heading', { name: 'Before enabling automation' })).toBeVisible()
  await expect(page.getByText('Operator Checks Pending', { exact: true })).toBeVisible()
  await expect(checklist.getByRole('listitem').filter({ hasText: 'Always-on server' })).toContainText('Not Verified')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'Encrypted backups and restore' })).toContainText('Not Verified')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'WordPress access' })).toContainText('Needs Setup')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'AI answer provider' })).toContainText('Optional')
  await expect(page.getByText('Do not enable automation yet', { exact: true })).toBeVisible()
  await expect(page.getByText('Confirm HTTPS, the API, web app, queue, workers, scheduler, and browser worker are healthy on the deployment server. This page cannot verify those processes.', { exact: true })).toBeVisible()
  await expect(checklist.getByRole('link', { name: 'Configure connections' }).first()).toHaveAttribute('href', '/sites/site-1/settings/connections')
})

test('settings blocks a monthly budget above the API ceiling before submission', async ({ page }) => {
  await installSettingsMocks(page)
  await page.goto('/sites/site-1/settings/policies')

  const budget = page.locator('#monthly-budget')
  await expect(budget).toHaveAttribute('max', '300')
  await expect(page.getByText('Hard API ceiling: $300 per site per month.', { exact: false })).toBeVisible()
  await expect(budget).toHaveAttribute('aria-describedby', 'monthly-budget-limit')

  await budget.fill('300.01')

  await expect(budget).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByText('The API allows no more than $300 per site per month.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save policy controls', exact: true })).toBeDisabled()
})

test('settings blocks more than twenty tracked AI questions before submission', async ({ page }) => {
  await installSettingsMocks(page)
  await page.goto('/sites/site-1/settings/policies')

  const questions = Array.from({ length: 21 }, (_, index) => `Question ${index + 1}`).join('\n')
  await page.getByLabel('Tracked questions').fill(questions)

  await expect(page.getByText('The API allows no more than 20 tracked questions.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save policy controls', exact: true })).toBeDisabled()
})

test('settings distinguishes application-ready pilot safeguards from unverified deployment work', async ({ page }) => {
  await installSettingsMocks(page, {
    connections: [
      {
        kind: 'wordpress',
        status: 'connected',
        checked_at: '2026-09-18T12:00:00Z',
        capabilities: { native: { read: true, update: true, create: true, publish: true }, editorial: { read: true, write: true } },
      },
      { kind: 'dataforseo', status: 'connected', checked_at: '2026-09-18T12:00:00Z', capabilities: { authenticated: true } },
    ],
    policy: { allowed_actions: ['metadata', 'publish'], author_id: 'author-7' },
    authors: [{ resource_key: 'authors:author-7', resource_type: 'authors', title: 'Taylor Writer' }],
  })
  await page.goto('/sites/site-1/settings/policies')

  const checklist = page.getByRole('list', { name: 'Automation readiness checklist' })
  await expect(checklist.getByRole('listitem').filter({ hasText: 'WordPress access' })).toContainText('Ready')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'DataForSEO' })).toContainText('Ready')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'Pilot safeguards' })).toContainText('Ready')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'Google Search Console' })).toContainText('Optional')
  await expect(checklist.getByRole('listitem').filter({ hasText: 'Always-on server' })).toContainText('Not Verified')
  await expect(page.getByText('Keep the site and workspace pauses on until the “not verified” server and backup checks are complete', { exact: false })).toBeVisible()
})
