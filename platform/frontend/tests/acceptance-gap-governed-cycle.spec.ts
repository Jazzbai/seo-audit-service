import { expect, test, type Page, type Route } from '@playwright/test'

const baseSite = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Governed Studio',
  origin: 'https://governed.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: false,
  facts: {},
}

const baseOverview = {
  site: baseSite,
  counts: { pages: 8, open_findings: 2, pending_candidates: 3, published_articles: 1, open_incidents: 0 },
  monitoring: { status: 'healthy', last_seen_at: '2026-09-21T12:00:00Z' },
  budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
  recent_events: [],
  coverage: { status: 'complete', last_audit_at: '2026-09-21T11:00:00Z', error_count: 0, pending_url_count: 0 },
  connections: [],
  global_pause: false,
}

const governedResult = {
  workflow: 'full_cycle',
  mode: 'governed',
  complete: true,
  stages: [{ name: 'public_audit', status: 'complete' }],
  next_actions: [],
  execution_summary: {
    metadata: { candidates_authorized: 3, jobs_queued: 2 },
    content_publishing: { status: 'review_gated' },
    paid_visibility: { status: 'not_run' },
    remote_mutations: { status: 'not_run' },
  },
}

type MockOptions = {
  role?: 'owner' | 'editor' | 'viewer'
  sitePaused?: boolean
  globalPause?: boolean
  policyEnabled?: boolean
  allowedActions?: string[]
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installGovernedMocks(page: Page, options: MockOptions = {}) {
  const role = options.role ?? 'owner'
  const site = { ...baseSite, paused: options.sitePaused ?? false }
  const overview = { ...baseOverview, site, global_pause: options.globalPause ?? false }
  const policySettings = {
    enabled: options.policyEnabled ?? true,
    allowed_actions: options.allowedActions ?? ['metadata'],
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
  const jobs = new Map<string, { id: string; status: string; polls: number; kind: string; payload: Record<string, unknown>; result?: Record<string, unknown> }>()
  const jobRequests: Array<{ kind: string; payload: Record<string, unknown> }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, { user: { id: 'user-1', email: `${role}@example.com`, name: role }, team: { id: 'team-1', name: 'Governed team' }, role, csrf_token: 'csrf-test-token' })
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/overview' && method === 'GET') return json(route, overview)
    if (path === '/sites/site-1/policy' && method === 'GET') return json(route, { id: 'policy-1', version: 7, settings: policySettings })
    if (path === '/settings' && method === 'GET') return json(route, { global_pause: overview.global_pause })
    if (path === '/sites/site-1/jobs' && method === 'POST') {
      const body = (request.postDataJSON() ?? {}) as { kind?: string; payload?: Record<string, unknown> }
      const job = { id: `job-${jobRequests.length + 1}`, status: 'queued', polls: 0, kind: body.kind ?? 'unknown', payload: body.payload ?? {} }
      jobRequests.push({ kind: job.kind, payload: job.payload })
      jobs.set(job.id, job)
      return json(route, { ...job, site_id: 'site-1' })
    }
    const jobMatch = path.match(/^\/sites\/site-1\/jobs\/([^/]+)$/)
    if (jobMatch && method === 'GET') {
      const job = jobs.get(jobMatch[1])
      if (!job) return json(route, { detail: 'Job not found' }, 404)
      job.polls += 1
      if (job.polls === 1) return json(route, { ...job, site_id: 'site-1', status: 'running' })
      job.status = 'complete'
      job.result = governedResult
      return json(route, { ...job, site_id: 'site-1' })
    }
    return json(route, { items: [], total: 0 })
  })

  return { jobRequests }
}

test('owner can start governed mode with an explicit payload and review its execution summary', async ({ page }) => {
  const controls = await installGovernedMocks(page)
  await page.goto('/sites/site-1/overview')

  const runGoverned = page.getByRole('button', { name: 'Run governed cycle' })
  await expect(runGoverned).toBeEnabled()
  await expect(page.getByText('only activates site policy-approved metadata actions', { exact: false })).toBeVisible()

  await runGoverned.click()

  const summary = page.getByRole('region', { name: 'Policy-authorized execution summary' })
  await expect(summary).toBeVisible()
  await expect(summary).toContainText('Selected mode: Governed')
  await expect(summary).toContainText('Metadata candidates authorized')
  await expect(summary).toContainText('Metadata jobs queued')
  await expect(summary.getByText('3', { exact: true })).toBeVisible()
  await expect(summary.getByText('2', { exact: true })).toBeVisible()
  await expect(summary).toContainText('Review Gated')
  await expect(summary).toContainText('Paid visibility')
  await expect(summary).toContainText('Not Run')
  await expect(summary).toContainText('not an optimization or ranking result')
  expect(controls.jobRequests).toEqual(expect.arrayContaining([{ kind: 'full_cycle', payload: { mode: 'governed' } }]))
})

test('disabled site policy prevents governed execution and explains the gate', async ({ page }) => {
  const controls = await installGovernedMocks(page, { policyEnabled: false })
  await page.goto('/sites/site-1/overview')

  await expect(page.getByRole('button', { name: 'Run governed cycle' })).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'Site policy does not currently enable governed execution.' })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('site pause keeps governed execution disabled', async ({ page }) => {
  await installGovernedMocks(page, { sitePaused: true })
  await page.goto('/sites/site-1/overview')
  await expect(page.getByRole('button', { name: 'Run governed cycle' })).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'This site is paused.' })).toBeVisible()
})

test('workspace pause keeps governed execution disabled', async ({ page }) => {
  await installGovernedMocks(page, { globalPause: true })
  await page.goto('/sites/site-1/overview')
  await expect(page.getByRole('button', { name: 'Run governed cycle' })).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'workspace emergency pause is active' })).toBeVisible()
})

test('viewer sees governed mode as read-only even when policy allows metadata', async ({ page }) => {
  const controls = await installGovernedMocks(page, { role: 'viewer' })
  await page.goto('/sites/site-1/overview')

  await expect(page.getByRole('button', { name: 'Run governed cycle' })).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'Viewer role cannot start a governed cycle.' })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})
