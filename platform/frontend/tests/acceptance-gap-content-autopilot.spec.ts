import { expect, test, type Page, type Route } from '@playwright/test'

const baseSite = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Pilot Workshop',
  origin: 'https://pilot.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: false,
  facts: { business_name: 'Pilot Workshop' },
}

const basePolicy = {
  enabled: true,
  allowed_actions: ['publish'],
  protected_paths: ['/contact*', '/checkout*'],
  posts_per_week: 2,
  refreshes_per_week: 1,
  monthly_budget_cents: 30000,
  tracked_keywords: [],
  competitors: [],
  tracked_questions: [],
  publish_days: [1, 4],
  author_id: '1',
}

const readyConnections = [
  {
    kind: 'wordpress',
    status: 'connected',
    checked_at: '2026-09-21T12:00:00Z',
    capabilities: {
      authenticated: true,
      native: { read: true, update: true, create: true, publish: true },
    },
  },
  { kind: 'ai', status: 'connected', checked_at: '2026-09-21T12:00:00Z', capabilities: { authenticated: true, research: true, settings: { endpoint: 'https://ai.example.test/generate', model: 'fixture-model', estimated_cost_cents: 1, max_cost_cents: 2 } } },
]

const authorPage = {
  id: 'author-page-1',
  site_id: 'site-1',
  resource_key: 'authors:1',
  url: 'https://pilot.example/?author=1',
  title: 'Taylor Writer',
  resource_type: 'authors',
  enrolled: false,
  managed: false,
}

const publishedResult = {
  workflow: 'content_autopilot',
  status: 'published',
  policy_version: 4,
  article_status: 'published',
  stages: [
    { name: 'research', status: 'complete' },
    { name: 'generate', status: 'complete' },
    { name: 'publish', status: 'complete' },
  ],
  blockers: [],
}

type MockOptions = {
  role?: 'owner' | 'editor' | 'viewer'
  sitePaused?: boolean
  globalPause?: boolean
  policyEnabled?: boolean
  connections?: unknown[]
  pages?: unknown[]
  articles?: unknown[]
  authorDiscovery?: unknown
  jobStatus?: string
  jobResult?: Record<string, unknown>
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installCalendarMocks(page: Page, options: MockOptions = {}) {
  const role = options.role ?? 'owner'
  const site = { ...baseSite, paused: options.sitePaused ?? false }
  const policy = { ...basePolicy, enabled: options.policyEnabled ?? true }
  const connections = options.connections ?? readyConnections
  const pages = options.pages ?? [authorPage]
  const articles = options.articles ?? [{ id: 'article-1', title: 'A grounded repair guide', body: '', status: 'planned', brief: { week: 1 }, sources: [], managed: true }]
  const jobs = new Map<string, { id: string; kind: string; status: string; polls: number; result?: Record<string, unknown> }>()
  const jobRequests: Array<{ kind: string; payload: Record<string, unknown>; idempotency_key?: string }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status' && method === 'GET') return json(route, { initialized: true })
    if (path === '/auth/me' && method === 'GET') return json(route, { user: { id: 'user-1', email: `${role}@example.com`, name: role }, team: { id: 'team-1', name: 'Pilot team' }, role, csrf_token: 'csrf-test-token' })
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1' && method === 'GET') return json(route, site)
    if (path === '/sites/site-1/articles' && method === 'GET') return json(route, { items: articles, total: articles.length })
    if (path === '/sites/site-1/connections' && method === 'GET') return json(route, { items: connections, total: connections.length })
    if (path === '/sites/site-1/authors' && method === 'GET') return json(route, options.authorDiscovery ?? {
      items: [{ id: '1', name: 'Taylor Writer' }],
      complete: true,
      checked_at: '2026-09-25T12:00:00Z',
      authenticated_user_id: '1',
      blockers: [],
    })
    if (path === '/sites/site-1/policy' && method === 'GET') return json(route, { id: 'policy-1', version: 4, settings: policy })
    if (path === '/settings' && method === 'GET') return json(route, { global_pause: options.globalPause ?? false })
    if (path === '/sites/site-1/pages' && method === 'GET') return json(route, { items: pages, total: pages.length })
    if (path === '/sites/site-1/jobs' && method === 'POST') {
      const body = (request.postDataJSON() ?? {}) as { kind?: string; payload?: Record<string, unknown>; idempotency_key?: string }
      const job = { id: `job-${jobRequests.length + 1}`, kind: body.kind ?? 'unknown', status: 'queued', polls: 0, result: options.jobResult ?? publishedResult }
      jobRequests.push({ kind: job.kind, payload: body.payload ?? {}, idempotency_key: body.idempotency_key })
      jobs.set(job.id, job)
      return json(route, { ...job, site_id: 'site-1' })
    }
    const jobMatch = path.match(/^\/sites\/site-1\/jobs\/([^/]+)$/)
    if (jobMatch && method === 'GET') {
      const job = jobs.get(jobMatch[1])
      if (!job) return json(route, { detail: 'Job not found' }, 404)
      job.polls += 1
      if (job.polls === 1) job.status = 'running'
      else job.status = options.jobStatus ?? 'complete'
      return json(route, { ...job, site_id: 'site-1' })
    }
    return json(route, { items: [], total: 0 })
  })
  return { jobRequests }
}

test('viewer can review the calendar but cannot start content autopilot', async ({ page }) => {
  const controls = await installCalendarMocks(page, { role: 'viewer' })
  await page.goto('/sites/site-1/content')

  await expect(page.getByRole('button', { name: 'Run content autopilot', exact: true })).toBeDisabled()
  await expect(page.getByText('Viewer access can review the calendar, but cannot start content autopilot.', { exact: true })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('a paused site keeps content autopilot disabled with a plain-language explanation', async ({ page }) => {
  const controls = await installCalendarMocks(page, { sitePaused: true })
  await page.goto('/sites/site-1/content')

  await expect(page.getByRole('button', { name: 'Run content autopilot', exact: true })).toBeDisabled()
  await expect(page.getByText('This site is paused. Resume it in Settings only after the pilot safeguards are ready.', { exact: true })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('missing policy, connections, and verified author are shown as readiness gates', async ({ page }) => {
  const controls = await installCalendarMocks(page, {
    policyEnabled: false,
    connections: [],
    pages: [],
    authorDiscovery: {
      items: [],
      complete: false,
      checked_at: '2026-09-25T12:00:00Z',
      authenticated_user_id: null,
      blockers: ['wordpress_connection_required'],
    },
  })
  await page.goto('/sites/site-1/content')

  await expect(page.getByRole('button', { name: 'Run content autopilot', exact: true })).toBeDisabled()
  await expect(page.getByText('The site policy is disabled. An owner must enable the policy before this workflow can run.', { exact: true })).toBeVisible()
  await expect(page.getByText('Needs Connection', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('Author discovery is incomplete, blocked, or malformed. Refresh the authenticated WordPress author check before starting this workflow.', { exact: true })).toBeVisible()
  expect(controls.jobRequests).toHaveLength(0)
})

test('owner can run one content autopilot job and see its safe publication result', async ({ page }) => {
  const controls = await installCalendarMocks(page)
  await page.goto('/sites/site-1/content')

  const button = page.getByRole('button', { name: 'Run content autopilot', exact: true })
  await expect(button).toBeEnabled()
  await expect(page.getByText('The configured author was returned by the latest complete authenticated WordPress author check.', { exact: true })).toBeVisible()
  await button.click()
  await expect(page.getByText('Content autopilot is running one bounded article through research, generation, editorial checks, and publication verification.', { exact: true })).toBeVisible()
  await expect(page.getByText('One article was published and verified', { exact: true })).toBeVisible()
  await expect(page.getByText(/This does not mean the whole site is optimized\./)).toBeVisible()
  expect(controls.jobRequests).toEqual([expect.objectContaining({ kind: 'content_autopilot', payload: { max_articles: 1 }, idempotency_key: expect.stringMatching(/^content-autopilot-site-1-\d{4}-\d{2}-\d{2}$/) })])
})

test('partial content autopilot results remain review-only and expose the next action', async ({ page }) => {
  await installCalendarMocks(page, {
    articles: [],
    jobStatus: 'partial',
    jobResult: {
      workflow: 'content_autopilot',
      status: 'needs_review',
      article_status: 'review_needed',
      blockers: ['editorial_checks_required'],
      next_action: 'Review the article before publishing.',
    },
  })
  await page.goto('/sites/site-1/content')
  await page.getByRole('button', { name: 'Run content autopilot', exact: true }).click()

  await expect(page.getByText('Content autopilot needs review', { exact: true })).toBeVisible()
  await expect(page.getByText(/No successful publication is claimed until the listed checks are resolved\./)).toBeVisible()
  await expect(page.getByText('editorial_checks_required', { exact: true })).toBeVisible()
  await expect(page.getByText('Next action: Review the article before publishing.', { exact: true })).toBeVisible()
  await expect(page.getByText('job-1', { exact: true })).toHaveCount(0)
})

test('failed content autopilot results explain that no publication was confirmed', async ({ page }) => {
  await installCalendarMocks(page, {
    articles: [],
    jobStatus: 'failed',
    jobResult: {
      workflow: 'content_autopilot',
      status: 'failed',
      blockers: ['ai_provider_unavailable'],
      next_action: 'Review the AI connection before trying again.',
    },
  })
  await page.goto('/sites/site-1/content')
  await page.getByRole('button', { name: 'Run content autopilot', exact: true }).click()

  await expect(page.getByText('Content autopilot failed safely', { exact: true })).toBeVisible()
  await expect(page.getByText('The workflow did not confirm a successful publication.', { exact: false })).toBeVisible()
  await expect(page.getByText('ai_provider_unavailable', { exact: true })).toBeVisible()
  await expect(page.getByText('Next action: Review the AI connection before trying again.', { exact: true })).toBeVisible()
})

test('mobile calendar keeps the no-content state and explicit control usable', async ({ page }) => {
  await installCalendarMocks(page, { articles: [] })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/sites/site-1/content')

  await expect(page.getByRole('heading', { name: 'One-article content autopilot' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run content autopilot', exact: true })).toBeEnabled()
  await expect(page.getByText('No qualifying topic yet.', { exact: true })).toHaveCount(4)
  await expect(page.getByRole('heading', { name: 'Nothing waiting for a date' })).toBeVisible()
})
