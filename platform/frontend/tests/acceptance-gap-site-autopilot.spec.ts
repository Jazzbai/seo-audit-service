import { expect, test, type Page, type Route } from '@playwright/test'

const baseSite = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Autopilot Workshop',
  origin: 'https://autopilot.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: false,
  facts: {},
}

const basePolicy = {
  enabled: true,
  allowed_actions: ['publish', 'refresh'],
  protected_paths: ['/contact*', '/checkout*'],
  posts_per_week: 2,
  refreshes_per_week: 1,
  monthly_budget_cents: 30000,
  tracked_keywords: [],
  competitors: [],
  tracked_questions: [],
  publish_days: [1, 4],
  author_id: 'author-1',
}

const completeAutopilotResult = {
  workflow: 'full_cycle',
  mode: 'autopilot',
  complete: true,
  stages: [
    { name: 'availability', status: 'complete' },
    { name: 'inventory', status: 'complete' },
    { name: 'public_audit', status: 'complete' },
    { name: 'content_plan', status: 'complete' },
    { name: 'one_article_content_autopilot', status: 'published' },
    { name: 'refresh_evaluation', status: 'complete' },
  ],
  next_actions: [],
  execution_summary: {
    metadata: { status: 'queued', authorized_count: 2, queued_count: 2 },
  },
  provider_payload: 'must never be rendered',
  job_id: 'internal-job-id',
}

type MockOptions = {
  role?: 'owner' | 'editor' | 'viewer'
  sitePaused?: boolean
  globalPause?: boolean
  omitOverviewGlobalPause?: boolean
  policyError?: boolean
  settingsError?: boolean
  jobStatus?: string
  jobResult?: Record<string, unknown>
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page, options: MockOptions = {}) {
  const role = options.role ?? 'owner'
  const site = { ...baseSite, paused: options.sitePaused ?? false }
  const overview = {
    site,
    counts: { pages: 18, open_findings: 4, pending_candidates: 6, published_articles: 2, open_incidents: 0 },
    monitoring: { status: 'healthy', last_seen_at: '2026-09-21T12:00:00Z' },
    budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
    recent_events: [],
    coverage: { status: 'complete', last_audit_at: '2026-09-21T11:00:00Z', error_count: 0, pending_url_count: 0 },
    connections: [{ kind: 'wordpress', status: 'connected', checked_at: '2026-09-21T12:00:00Z' }],
    ...(options.omitOverviewGlobalPause ? {} : { global_pause: options.globalPause ?? false }),
  }
  const jobs = new Map<string, { id: string; kind: string; status: string; polls: number; result: Record<string, unknown> }>()
  const jobRequests: Array<{ kind: string; payload: Record<string, unknown> }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status' && method === 'GET') return json(route, { initialized: true })
    if (path === '/auth/me' && method === 'GET') return json(route, {
      user: { id: 'user-1', email: `${role}@example.com`, name: role },
      team: { id: 'team-1', name: 'Autopilot team' },
      role,
      csrf_token: 'csrf-test-token',
    })
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/overview' && method === 'GET') return json(route, overview)
    if (path === '/sites/site-1/policy' && method === 'GET') {
      if (options.policyError) return json(route, { detail: 'Policy readiness is unavailable' }, 503)
      return json(route, { id: 'policy-1', version: 8, settings: basePolicy })
    }
    if (path === '/settings' && method === 'GET') {
      if (options.settingsError) return json(route, { detail: 'Workspace pause state is unavailable' }, 503)
      return json(route, { global_pause: options.globalPause ?? false })
    }
    if (path === '/sites/site-1/jobs' && method === 'POST') {
      const body = (request.postDataJSON() ?? {}) as { kind?: string; payload?: Record<string, unknown> }
      const job = {
        id: `job-${jobRequests.length + 1}`,
        kind: body.kind ?? 'unknown',
        status: 'queued',
        polls: 0,
        result: options.jobResult ?? completeAutopilotResult,
      }
      jobRequests.push({ kind: job.kind, payload: body.payload ?? {} })
      jobs.set(job.id, job)
      return json(route, { ...job, site_id: 'site-1' }, 202)
    }
    const jobMatch = path.match(/^\/sites\/site-1\/jobs\/([^/]+)$/)
    if (jobMatch && method === 'GET') {
      const job = jobs.get(jobMatch[1])
      if (!job) return json(route, { detail: 'Job not found' }, 404)
      job.polls += 1
      job.status = job.polls === 1 ? 'running' : options.jobStatus ?? 'complete'
      return json(route, { ...job, site_id: 'site-1' })
    }
    return json(route, { items: [], total: 0 })
  })

  return { jobRequests }
}

test('owner can run site autopilot and review its bounded result', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/site-1/overview')

  const button = page.getByRole('button', { name: 'Run site autopilot', exact: true })
  await expect(button).toBeEnabled()
  await expect(page.getByText('It runs availability, inventory, public audit, content planning, policy-authorized metadata checks, one-article content autopilot, and refresh evaluation.', { exact: false })).toBeVisible()

  await button.click()

  const result = page.getByRole('region', { name: 'Site autopilot complete' })
  await expect(result).toBeVisible()
  await expect(result).toContainText('Workflow stages')
  await expect(result).toContainText('One-article content autopilot')
  await expect(result).toContainText('Published')
  await expect(result).toContainText('Policy-authorized metadata')
  await expect(result).toContainText('Candidates authorized')
  await expect(result).toContainText('Jobs queued')
  await expect(result).toContainText('2')
  await expect(result).toContainText('This does not mean the whole site is optimized.')
  await expect(page.getByText('must never be rendered', { exact: true })).toHaveCount(0)
  await expect(page.getByText('internal-job-id', { exact: true })).toHaveCount(0)
  expect(controls.jobRequests).toEqual([{ kind: 'full_cycle', payload: { mode: 'autopilot' } }])
})

for (const role of ['editor', 'viewer'] as const) {
  test(`${role} sees site autopilot as owner-only and read-only`, async ({ page }) => {
    const controls = await installMocks(page, { role })
    await page.goto('/sites/site-1/overview')

    await expect(page.getByRole('button', { name: 'Run site autopilot', exact: true })).toBeDisabled()
    await expect(page.getByText('Editor and Viewer roles can review this workflow, but only the site owner can run site autopilot.', { exact: true })).toBeVisible()
    expect(controls.jobRequests).toHaveLength(0)
  })
}

test('site pause keeps site autopilot disabled', async ({ page }) => {
  const controls = await installMocks(page, { sitePaused: true })
  await page.goto('/sites/site-1/overview')

  await expect(page.getByRole('button', { name: 'Run site autopilot', exact: true })).toBeDisabled()
  await expect(page.getByText('Site autopilot is unavailable while site-level automation is paused. Resume it in the policy controls only after the pilot safeguards are ready.', { exact: true })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('workspace pause keeps site autopilot disabled', async ({ page }) => {
  const controls = await installMocks(page, { globalPause: true })
  await page.goto('/sites/site-1/overview')

  await expect(page.getByRole('button', { name: 'Run site autopilot', exact: true })).toBeDisabled()
  await expect(page.getByText('A workspace-wide emergency stop currently holds site autopilot. An owner must resume it before starting this workflow.', { exact: true })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('unavailable policy readiness keeps site autopilot disabled and retryable', async ({ page }) => {
  const controls = await installMocks(page, { policyError: true })
  await page.goto('/sites/site-1/overview')

  await expect(page.getByRole('button', { name: 'Run site autopilot', exact: true })).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'Site readiness is unavailable' })).toContainText('The current site policy and budget controls could not be verified. Retry readiness checks before starting site autopilot.')
  await expect(page.getByRole('button', { name: 'Retry site autopilot readiness', exact: true })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('gated site autopilot result is explained without raw provider data', async ({ page }) => {
  const controls = await installMocks(page, {
    jobResult: {
      workflow: 'full_cycle',
      mode: 'autopilot',
      complete: false,
      stages: [{ name: 'one_article_content_autopilot', status: 'gated' }],
      execution_summary: { content_publishing: { status: 'gated', blockers: ['verified_wordpress_connection_required'] } },
      blockers: ['verified_wordpress_connection_required'],
      provider_payload: 'secret provider response',
      job_id: 'internal-job-id',
    },
  })
  await page.goto('/sites/site-1/overview')
  await page.getByRole('button', { name: 'Run site autopilot', exact: true }).click()

  const result = page.getByRole('region', { name: 'Site autopilot is gated' })
  await expect(result).toBeVisible()
  await expect(result).toContainText('The server held this workflow behind a policy, pause, connection, budget, or article prerequisite.')
  await expect(result).toContainText('Gated')
  await expect(page.getByText('secret provider response', { exact: true })).toHaveCount(0)
  await expect(page.getByText('internal-job-id', { exact: true })).toHaveCount(0)
  expect(controls.jobRequests).toHaveLength(1)
})

test('needs-review site autopilot result stays review-only', async ({ page }) => {
  await installMocks(page, {
    jobStatus: 'partial',
    jobResult: {
      workflow: 'full_cycle',
      mode: 'autopilot',
      complete: false,
      status: 'needs_review',
      stages: [
        { name: 'public_audit', status: 'complete' },
        { name: 'one_article_content_autopilot', status: 'needs_review' },
      ],
      next_action: 'Review editorial checks before publishing.',
    },
  })
  await page.goto('/sites/site-1/overview')
  await page.getByRole('button', { name: 'Run site autopilot', exact: true }).click()

  const result = page.getByRole('region', { name: 'Site autopilot needs review' })
  await expect(result).toBeVisible()
  await expect(result).toContainText('The workflow recorded partial results that need review.')
  await expect(result).toContainText('Needs Review')
  await expect(result).toContainText('One-article content autopilot')
  await expect(result).not.toContainText('Review editorial checks before publishing.')
})

test('site autopilot remains usable on a narrow overview layout', async ({ page }) => {
  await installMocks(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/sites/site-1/overview')

  const button = page.getByRole('button', { name: 'Run site autopilot', exact: true })
  await expect(button).toBeVisible()
  await expect(button).toBeEnabled()
  await expect(page.getByRole('heading', { name: 'Autopilot Workshop' })).toBeVisible()
})
