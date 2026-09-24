import { expect, test, type Page, type Route } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const auth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Alex Owner' },
  team: { id: 'team-1', name: 'Northstar team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-1', team_id: 'team-1', name: 'Acme Studio', origin: 'https://acme.example', timezone: 'America/Chicago', language: 'en', paused: true,
  facts: { business_name: 'Acme Studio', audience: 'Independent makers', locations: ['Austin, TX'], services: ['Design systems'], products: [], authors: [], confirmed_sources: [] },
}

const overview = {
  site,
  counts: { pages: 12, open_findings: 3, pending_candidates: 2, published_articles: 4, open_incidents: 0 },
  monitoring: { status: 'healthy', last_seen_at: '2026-09-14T15:00:00Z', wordpress_change_poll: { status: 'healthy', last_success_at: '2026-09-14T14:55:00Z' } },
  budget: { limit_cents: 30000, spent_cents: 4500, reserved_cents: 1000 },
  recent_events: [{ id: 1, kind: 'audit', message: 'Inventory refreshed', created_at: '2026-09-14T14:00:00Z' }],
  coverage: { status: 'complete_with_errors', last_audit_at: '2026-09-14T14:00:00Z', error_count: 2, pending_url_count: 1 },
  connections: [{ kind: 'wordpress', status: 'connected', checked_at: '2026-09-14T14:00:00Z' }],
}

const completeFullCycleResult: Record<string, unknown> = {
  workflow: 'full_cycle',
  complete: true,
  stages: [
    { name: 'availability', status: 'complete', stage_job_id: 'internal-stage-availability' },
    { name: 'inventory', status: 'complete', stage_job_id: 'internal-stage-inventory' },
    { name: 'public_audit', status: 'complete', stage_job_id: 'internal-stage-audit' },
    { name: 'content_plan', status: 'complete', stage_job_id: 'internal-stage-plan' },
    { name: 'refresh_evaluation', status: 'complete', stage_job_id: 'internal-stage-refresh' },
  ],
  next_actions: [
    { action: 'review_results', status: 'ready', reason: 'Review the evidence and proposed work before authorizing any governed action' },
    { action: 'publish', status: 'policy_gated', reason: 'Publishing was not attempted by the read-only full cycle' },
    { action: 'metadata_writes', status: 'policy_gated', reason: 'Metadata writes were not attempted by the read-only full cycle' },
    { action: 'paid_visibility', status: 'not_run', reason: 'Paid visibility collection requires a separate budgeted workflow' },
    { action: 'remote_mutations', status: 'not_run', reason: 'No remote mutation is performed by the full cycle' },
  ],
}

const partialFullCycleResult: Record<string, unknown> = {
  workflow: 'full_cycle',
  complete: false,
  stages: [
    { name: 'availability', status: 'complete', stage_job_id: 'stage-secret-availability' },
    { name: 'inventory', status: 'needs_connection', reason: 'verified_wordpress_connection_required', stage_job_id: 'stage-secret-inventory' },
    { name: 'public_audit', status: 'complete', stage_job_id: 'stage-secret-audit' },
    { name: 'content_plan', status: 'complete', stage_job_id: 'stage-secret-plan' },
    { name: 'refresh_evaluation', status: 'complete', stage_job_id: 'stage-secret-refresh' },
  ],
  next_actions: [
    { action: 'connect_wordpress', status: 'needs_connection', reason: 'Verify a WordPress connection before importing the authenticated inventory' },
    { action: 'review_incomplete_stages', status: 'needs_review', stages: ['inventory'] },
    { action: 'publish', status: 'policy_gated', reason: 'Publishing was not attempted by the read-only full cycle' },
    { action: 'metadata_writes', status: 'policy_gated', reason: 'Metadata writes were not attempted by the read-only full cycle' },
    { action: 'paid_visibility', status: 'not_run', reason: 'Paid visibility collection requires a separate budgeted workflow' },
    { action: 'remote_mutations', status: 'not_run', reason: 'No remote mutation is performed by the full cycle' },
  ],
}

type MockJobRequest = { kind: string; payload?: Record<string, unknown>; idempotency_key?: string }
type MockJob = MockJobRequest & { id: string; site_id: string; status: string; polls: number; result?: Record<string, unknown> }
type MockConnectionSave = { kind: string; body: { credentials: Record<string, unknown>; settings: Record<string, unknown> } }

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installWorkspaceMocks(page: Page, options: { initialized?: boolean; authorized?: boolean; emptySites?: boolean; failOverview?: boolean; failJobs?: boolean; failIncidents?: boolean; jobs?: unknown[]; incidents?: unknown[]; measurements?: unknown[]; connectionItems?: unknown[]; articles?: unknown[]; pages?: unknown[]; fullCycleStatus?: string; fullCycleResult?: Record<string, unknown>; monitoring?: Partial<typeof overview.monitoring>; coverage?: Partial<typeof overview.coverage> } = {}) {
  let authorized = options.authorized ?? true
  let failOverview = options.failOverview ?? false
  let failJobs = options.failJobs ?? false
  let failIncidents = options.failIncidents ?? false
  let createdSite = false
  let overviewRequestCount = 0
  const jobRequests: MockJobRequest[] = []
  const connectionSaves: MockConnectionSave[] = []
  let connectionItems = [...(options.connectionItems ?? [])] as Array<Record<string, unknown>>
  const jobs = new Map<string, MockJob>()
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status' && method === 'GET') return json(route, { initialized: options.initialized ?? true })
    if (path === '/auth/me' && method === 'GET') return authorized ? json(route, auth) : json(route, { detail: 'Not authenticated' }, 401)
    if (path === '/auth/login' && method === 'POST') { authorized = true; return json(route, auth) }
    if (path === '/auth/bootstrap' && method === 'POST') { authorized = true; return json(route, auth) }
    if (!authorized) return json(route, { detail: 'Not authenticated' }, 401)
    if (path === '/sites' && method === 'GET') return json(route, { items: options.emptySites && !createdSite ? [] : [site], total: options.emptySites && !createdSite ? 0 : 1 })
    if (path === '/sites' && method === 'POST') { createdSite = true; return json(route, { ...site, name: 'Northstar Studio' }) }
    if (path === '/sites/site-1' && method === 'GET') return json(route, site)
    if (path === '/sites/site-1/overview' && method === 'GET') {
      overviewRequestCount += 1
      const responseOverview = createdSite ? {...overview,site:{...site,name:'Northstar Studio'}} : overview
      return failOverview ? json(route, { detail: 'The monitoring service is temporarily unavailable.' }, 503) : json(route, { ...responseOverview, monitoring: { ...responseOverview.monitoring, ...options.monitoring }, coverage: { ...responseOverview.coverage, ...options.coverage } })
    }
    if (path === '/sites/site-1/jobs' && method === 'GET') {
      return failJobs ? json(route, { detail: 'The run history service is temporarily unavailable.' }, 503) : json(route, { items: options.jobs ?? [], total: options.jobs?.length ?? 0 })
    }
    if (path === '/sites/site-1/incidents' && method === 'GET') {
      return failIncidents ? json(route, { detail: 'The incident service is temporarily unavailable.' }, 503) : json(route, { items: options.incidents ?? [], total: options.incidents?.length ?? 0 })
    }
    if (path === '/sites/site-1/jobs' && method === 'POST') {
      const body = (request.postDataJSON() ?? {}) as Partial<MockJobRequest>
      const requestRecord: MockJobRequest = { kind: body.kind ?? 'unknown', payload: body.payload ?? {}, idempotency_key: body.idempotency_key }
      const id = `job-${jobRequests.length + 1}`
      const job: MockJob = { ...requestRecord, id, site_id: 'site-1', status: 'queued', polls: 0 }
      jobRequests.push(requestRecord)
      jobs.set(id, job)
      return json(route, job)
    }
    const jobMatch = path.match(/^\/sites\/site-1\/jobs\/([^/]+)$/)
    if (jobMatch && method === 'GET') {
      const job = jobs.get(jobMatch[1])
      if (!job) return json(route, { detail: 'Job not found' }, 404)
      job.polls += 1
      job.status = job.polls === 1 ? 'running' : job.kind === 'full_cycle' ? (options.fullCycleStatus ?? 'complete') : 'complete'
      if (job.kind === 'full_cycle' && job.status !== 'running') job.result = options.fullCycleResult ?? completeFullCycleResult
      return json(route, job)
    }
    if (path === '/sites/site-1/articles' && method === 'GET') return json(route, { items: options.articles ?? [], total: options.articles?.length ?? 0 })
    if (path === '/sites/site-1/measurements' && method === 'GET') return json(route, { items: options.measurements ?? [], total: options.measurements?.length ?? 0 })
    if (path === '/sites/site-1/connections' && method === 'GET') return json(route, { items: connectionItems, total: connectionItems.length })
    const connectionMatch = path.match(/^\/sites\/site-1\/connections\/([^/]+)$/)
    if (connectionMatch && method === 'PUT') {
      const requestBody = (request.postDataJSON() ?? {}) as { credentials?: Record<string, unknown>; settings?: Record<string, unknown> }
      const body = { credentials: requestBody.credentials ?? {}, settings: requestBody.settings ?? {} }
      const kind = connectionMatch[1]
      connectionSaves.push({ kind, body })
      connectionItems = connectionItems.map((item) => item.kind === kind ? { ...item, safe_fields: { ...((item.safe_fields ?? {}) as Record<string, unknown>), ...body.settings }, settings: { ...((item.settings ?? {}) as Record<string, unknown>), ...body.settings } } : item)
      return json(route, connectionItems.find((item) => item.kind === kind) ?? { kind, status: 'needs_test', safe_fields: body.settings })
    }
    if (path === '/sites/site-1/measurements/import' && method === 'POST') return json(route, { imported: 1 })
    if (path === '/sites/site-1/issues' && method === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/findings' && method === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/candidates' && method === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/pages' && method === 'GET') return json(route, { items: options.pages ?? [], total: options.pages?.length ?? 0 })
    return json(route, { items: [], total: 0 })
  })
  return {
    setOverviewFailure: (value: boolean) => { failOverview = value },
    setJobsFailure: (value: boolean) => { failJobs = value },
    setIncidentsFailure: (value: boolean) => { failIncidents = value },
    getOverviewRequestCount: () => overviewRequestCount,
    jobRequests,
    connectionSaves,
  }
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`populated page table meets WCAG contrast at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await installWorkspaceMocks(page, { pages: [{
      id: 'page-1', site_id: site.id, title: 'Design services', url: `${site.origin}/services/`,
      resource_type: 'pages', enrolled: false, last_seen_at: new Date().toISOString(), signals: {},
    }] })
    await page.goto('/sites/site-1/pages')
    await expect(page.getByRole('columnheader', { name: 'Page', exact: true })).toBeVisible()
    const scan = await new AxeBuilder({ page }).include('table').withRules(['color-contrast']).analyze()
    expect(scan.violations).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false)
  })
}

test('mocked API: owner can sign in and see returned overview data', async ({ page }) => {
  await installWorkspaceMocks(page, { authorized: false })
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Sign in to ForgeSEO' })).toBeVisible()
  await page.getByLabel('Work email').fill('owner@example.com')
  await page.getByLabel('Password').fill('correct horse battery staple')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/\/sites\/site-1\/overview$/)
  await expect(page.getByRole('heading', { name: 'Acme Studio' })).toBeVisible()
  await expect(page.getByText('12', { exact: true })).toBeVisible()
  await expect(page.getByText('Inventory refreshed')).toBeVisible()
  await expect(page.getByText('2 audit errors')).toBeVisible()
  await expect(page.getByText('WordPress changes: Healthy · last successful poll')).toBeVisible()
})

test('overview runs an authenticated full cycle, shows progress, and refreshes its data', async ({ page }) => {
  const controls = await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/overview')
  await expect(page.getByRole('button', { name: 'Run full cycle' })).toBeVisible()
  await expect(page.getByText('This command runs availability, inventory/audit, content planning, and refresh evaluation in one authenticated job. Writes and paid services remain policy/connection-gated.')).toBeVisible()

  await page.getByRole('button', { name: 'Run full cycle' }).click()
  await expect(page.getByRole('button', { name: 'Running…' })).toBeVisible()
  await expect(page.getByText(/Full cycle is running\./)).toBeVisible()
  await expect(page.getByText('Full cycle complete. The overview and recorded coverage have been refreshed. Writes and paid services remain policy/connection-gated.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'What ForgeSEO found and what happens next' })).toBeVisible()
  expect(controls.jobRequests).toEqual(expect.arrayContaining([expect.objectContaining({ kind: 'full_cycle', payload: {} })]))
  expect(controls.getOverviewRequestCount()).toBeGreaterThan(1)
})

test('overview retains partial full-cycle coverage and explains governed follow-ups', async ({ page }) => {
  await installWorkspaceMocks(page, { fullCycleStatus: 'partial', fullCycleResult: partialFullCycleResult })
  await page.goto('/sites/site-1/overview')
  await page.getByRole('button', { name: 'Run full cycle' }).click()

  await expect(page.getByRole('heading', { name: 'What ForgeSEO found and what happens next' })).toBeVisible()
  await expect(page.getByText('Partial / Needs Review', { exact: true })).toBeVisible()
  await expect(page.getByText('WordPress inventory', { exact: true })).toBeVisible()
  await expect(page.getByText('Needs Connection', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('Connect WordPress', { exact: true })).toBeVisible()
  await expect(page.getByText('Review incomplete stages', { exact: true })).toBeVisible()
  await expect(page.getByText('Policy Gated', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('Not Run', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('The full cycle is read-only.', { exact: false })).toBeVisible()
  await expect(page.getByText('stage-secret-inventory', { exact: true })).toHaveCount(0)
  await expect(page.getByText('job-1', { exact: true })).toHaveCount(0)
})

test('overview keeps the audit action separate and completes the existing audit journey', async ({ page }) => {
  const controls = await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/overview')
  await page.getByRole('button', { name: 'Run audit' }).click()
  await expect(page.getByRole('button', { name: 'Starting…' })).toBeVisible()
  await expect(page.getByText(/Audit is running\./)).toBeVisible()
  await expect(page.getByText('Audit complete. Coverage and findings have been refreshed from the server.')).toBeVisible()
  expect(controls.jobRequests).toEqual(expect.arrayContaining([expect.objectContaining({ kind: 'audit', payload: {} })]))
  expect(controls.getOverviewRequestCount()).toBeGreaterThan(1)
})

test('overview labels audit coverage age separately from monitoring health', async ({ page }) => {
  await installWorkspaceMocks(page, { coverage: { last_audit_at: new Date(Date.now() - 45 * 24 * 60 * 60 * 1000).toISOString() } })
  await page.goto('/sites/site-1/overview')

  const freshness = page.getByRole('status', { name: 'Audit coverage freshness: Stale' })
  await expect(freshness).toContainText('Audit data: Stale')
  await expect(freshness).toContainText('run an audit before relying on this coverage')
  await expect(page.getByText('All systems steady', { exact: true })).toBeVisible()
})

test('overview labels automation state separately from monitoring health', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/overview')

  const automation = page.getByRole('status', { name: 'Automation state: Paused' })
  await expect(automation).toBeVisible()
  await expect(automation).toContainText('Automation')
  await expect(page.getByText('Site-level automation is paused. Observations and reviews remain available; governed writes are held.', { exact: true })).toBeVisible()
  await expect(page.getByText('All systems steady', { exact: true })).toBeVisible()
})

test('overview makes missing audit age explicit', async ({ page }) => {
  await installWorkspaceMocks(page, { coverage: { last_audit_at: null } })
  await page.goto('/sites/site-1/overview')

  const freshness = page.getByRole('status', { name: 'Audit coverage freshness: Unknown' })
  await expect(freshness).toContainText('No valid audit timestamp is available.')
})

test('new owner can onboard a site with business facts and land on its overview', async ({ page }) => {
  await installWorkspaceMocks(page, { emptySites: true })
  await page.goto('/sites/new')
  await expect(page.getByRole('heading', { name: 'Tell ForgeSEO what good work looks like.' })).toBeVisible()
  await page.getByLabel('Site name').fill('Northstar Studio')
  await page.getByLabel('Site origin').fill('https://northstar.example')
  await page.getByLabel('Business name').fill('Northstar Studio')
  await page.getByLabel('Primary audience').fill('Independent makers')
  await page.getByRole('textbox', {name:'Services',exact:true}).fill('Design systems')
  await page.getByRole('textbox', {name:'Services',exact:true}).press('Enter')
  await page.getByRole('button', { name: 'Create site' }).click()
  await expect(page).toHaveURL(/\/sites\/site-1\/overview$/)
  await expect(page.getByRole('heading', { name: 'Northstar Studio' })).toBeVisible()
})

test('mobile navigation exposes the SEO workspaces', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/sites/site-1/overview')
  await expect(page.getByRole('heading', { name: 'Acme Studio' })).toBeVisible()
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
  await page.getByRole('link', { name: 'Issues' }).click()
  await expect(page).toHaveURL(/\/sites\/site-1\/issues$/)
  await expect(page.getByRole('heading', { name: 'Issues' })).toBeVisible()
})

test('issues keeps an empty candidate queue from implying optimization', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/issues')

  await expect(page.getByRole('heading', { name: 'Candidate history' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No candidates waiting' })).toBeVisible()
  await expect(page.getByText('No candidate changes are currently waiting for a decision. This only means the queue is empty; it does not mean the site is fully optimized. Check audit coverage and findings for unresolved or unmeasured work.', { exact: true })).toBeVisible()
})

test('pages classifies inventory freshness conservatively at the daily cadence', async ({ page }) => {
  await installWorkspaceMocks(page, {
    pages: [
      { id: 'page-fresh', resource_key: 'pages:fresh', url: 'https://acme.example/services', title: 'Services', resource_type: 'page', enrolled: false, managed: false, last_seen_at: new Date(Date.now() - 23 * 60 * 60 * 1000).toISOString() },
      { id: 'page-stale', resource_key: 'pages:stale', url: 'https://acme.example/about', title: 'About', resource_type: 'page', enrolled: false, managed: false, last_seen_at: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString() },
      { id: 'page-missing', resource_key: 'pages:missing', url: 'https://acme.example/contact', title: 'Contact', resource_type: 'page', enrolled: false, managed: false, last_seen_at: null },
      { id: 'page-invalid', resource_key: 'pages:invalid', url: 'https://acme.example/faq', title: 'FAQ', resource_type: 'page', enrolled: false, managed: false, last_seen_at: 'not-a-timestamp' },
      { id: 'page-future', resource_key: 'pages:future', url: 'https://acme.example/locations', title: 'Locations', resource_type: 'page', enrolled: false, managed: false, last_seen_at: new Date(Date.now() + 60 * 60 * 1000).toISOString() },
    ],
  })
  await page.goto('/sites/site-1/pages')

  await expect(page.getByRole('heading', { name: 'Page inventory', exact: true })).toBeVisible()
  const freshRow = page.getByRole('row').filter({ hasText: 'Services' })
  const staleRow = page.getByRole('row').filter({ hasText: 'About' })
  const missingRow = page.getByRole('row').filter({ hasText: 'Contact' })
  const invalidRow = page.getByRole('row').filter({ hasText: 'FAQ' })
  const futureRow = page.getByRole('row').filter({ hasText: 'Locations' })

  await expect(freshRow.getByText('Within Daily Cadence', { exact: true })).toBeVisible()
  await expect(staleRow.getByText('Stale', { exact: true })).toBeVisible()
  await expect(missingRow.getByText('Freshness Unknown', { exact: true })).toBeVisible()
  await expect(invalidRow.getByText('Freshness Unknown', { exact: true })).toBeVisible()
  await expect(futureRow.getByText('Freshness Unknown', { exact: true })).toBeVisible()
  await expect(staleRow).toContainText('Observed more than 24 hours ago')
  await expect(missingRow).toContainText('freshness is unknown, not current')
  await expect(futureRow).toContainText('invalid or in the future')
  await expect(page.getByRole('status').filter({ hasText: 'Observation freshness only' })).toContainText('do not prove data quality, complete coverage, or optimization')
})

test('overview presents a recoverable API error and retry path', async ({ page }) => {
  const controls = await installWorkspaceMocks(page, { failOverview: true })
  await page.goto('/sites/site-1/overview')
  await expect(page.getByRole('alert')).toContainText('monitoring service is temporarily unavailable')
  controls.setOverviewFailure(false)
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('heading', { name: 'Acme Studio' })).toBeVisible()
})

test('overview makes an absent monitoring heartbeat explicit', async ({ page }) => {
  await installWorkspaceMocks(page, { monitoring: { status: 'not_running', last_seen_at: null } })
  await page.goto('/sites/site-1/overview')

  await expect(page.getByText('Not Running', { exact: true })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: 'Monitoring is not running' })).toBeVisible()
  await expect(page.getByText('Monitoring has not reported a heartbeat yet. Audits and change checks may be stale until the scheduler starts.', { exact: true })).toBeVisible()
  await expect(page.getByText('Last seen Not yet observed', { exact: true })).toBeVisible()
})

test('jobs run history shows safe summaries and recovers after an API error', async ({ page }) => {
  const controls = await installWorkspaceMocks(page, {
    failJobs: true,
    jobs: [{
      id: 'job-history-1',
      site_id: 'site-1',
      kind: 'full_cycle',
      status: 'complete',
      attempts: 2,
      created_at: '2026-09-16T14:00:00Z',
      updated_at: '2026-09-16T14:05:00Z',
      idempotency_key: 'secret-idempotency-key',
      result: { stages: [{ stage_job_id: 'secret-stage-job-id' }] },
    }],
  })
  await page.goto('/sites/site-1/jobs')
  await expect(page.getByRole('link', { name: 'Jobs', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Run history', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('run history service is temporarily unavailable')

  controls.setJobsFailure(false)
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('table', { name: 'Run history' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Kind' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Status' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Created' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Updated' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Attempts' })).toBeVisible()
  await expect(page.getByText('Full Cycle', { exact: true })).toBeVisible()
  await expect(page.getByText('Complete', { exact: true })).toBeVisible()
  await expect(page.getByText('2', { exact: true })).toBeVisible()
  await expect(page.getByText('secret-idempotency-key', { exact: true })).toHaveCount(0)
  await expect(page.getByText('secret-stage-job-id', { exact: true })).toHaveCount(0)
})

test('incidents disclose safe evidence and preserve redaction boundaries', async ({ page }) => {
  await installWorkspaceMocks(page, {
    incidents: [
      {
        id: 'incident-scheduler',
        key: 'monitoring:scheduler',
        kind: 'monitoring',
        severity: 'high',
        title: 'Scheduler heartbeat is stale',
        status: 'open',
        failure_count: 3,
        first_seen_at: '2026-09-17T12:00:00Z',
        last_seen_at: '2026-09-17T12:15:00Z',
        details: {
          reason: 'No scheduler heartbeat has been received',
          affected_resource: { type: 'site', key: 'site-1', url: 'https://acme.example' },
          queue_delay_seconds: 720,
          provider_response: 'must-not-render-provider-response',
          credentials: { access_token: 'must-not-render-token' },
        },
      },
      {
        id: 'incident-resolved',
        key: 'publication:resolved',
        kind: 'workflow',
        severity: 'medium',
        title: 'Publication rollback needs attention',
        status: 'resolved',
        failure_count: 1,
        first_seen_at: '2026-09-17T10:00:00Z',
        last_seen_at: '2026-09-17T10:05:00Z',
        resolved_at: '2026-09-17T10:10:00Z',
        details: { reason: 'The saved draft was restored', publication_id: 'publication-1' },
      },
    ],
  })
  await page.goto('/sites/site-1/incidents')
  await expect(page.getByRole('heading', { name: 'Incidents', exact: true })).toBeVisible()
  await expect(page.getByRole('table', { name: 'Incidents' })).toBeVisible()

  const scheduler = page.getByRole('row').filter({ hasText: 'Scheduler heartbeat is stale' })
  await expect(scheduler.getByText('View safe evidence', { exact: true })).toBeVisible()
  await scheduler.getByText('View safe evidence', { exact: true }).click()
  await expect(scheduler.getByText('No scheduler heartbeat has been received', { exact: true })).toBeVisible()
  await expect(scheduler.getByText('Affected resource', { exact: true })).toBeVisible()
  await expect(scheduler.getByText('site · site-1 · https://acme.example', { exact: true })).toBeVisible()
  await expect(scheduler.getByText('Queue delay (seconds)', { exact: true })).toBeVisible()
  await expect(scheduler.getByText('720', { exact: true })).toBeVisible()
  await expect(page.getByText('must-not-render-provider-response', { exact: true })).toHaveCount(0)
  await expect(page.getByText('must-not-render-token', { exact: true })).toHaveCount(0)

  const resolved = page.getByRole('row').filter({ hasText: 'Publication rollback needs attention' })
  await resolved.getByText('View safe evidence', { exact: true }).click()
  await expect(resolved.locator('dt').filter({ hasText: 'Resolved' })).toBeVisible()
  await expect(resolved.getByText('Publication', { exact: true })).toBeVisible()
})

test('incidents preserve the recoverable error and empty states', async ({ page }) => {
  const controls = await installWorkspaceMocks(page, { failIncidents: true })
  await page.goto('/sites/site-1/incidents')
  await expect(page.getByRole('alert')).toContainText('incident service is temporarily unavailable')

  controls.setIncidentsFailure(false)
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('heading', { name: 'Incidents', exact: true })).toBeVisible()
  await expect(page.getByText('No open incidents', { exact: true })).toBeVisible()
  await expect(page.getByRole('table', { name: 'Incidents' })).toHaveCount(0)
})

test('visibility import reports the server validation count', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/visibility')
  await expect(page.getByRole('heading', { name: 'Visibility' })).toBeVisible()
  await page.getByRole('tab', { name: 'Imports' }).click()
  await page.getByLabel('JSON array').fill('[{"url":"https://source.example/a"}]')
  await page.getByRole('button', { name: 'Validate & import' }).click()
  await expect(page.getByText('Imported 1 validated measurement.')).toBeVisible()
})

test('visibility labels lab performance separately from field traffic and imported observations', async ({ page }) => {
  await installWorkspaceMocks(page, {
    measurements: [
      { id: 'measurement-lab', kind: 'pagespeed', source: 'pagespeed', observed_at: '2026-09-17T12:00:00Z', data: { measurement_context: 'both', performance_score: 92, largest_contentful_paint_ms: 1800 } },
      { id: 'measurement-field-performance', kind: 'pagespeed', source: 'pagespeed', observed_at: '2026-09-17T12:30:00Z', data: { measurement_context: 'field_or_origin', loading_experience: { overall_category: 'FAST' } } },
      { id: 'measurement-field', kind: 'ga4', source: 'ga4', observed_at: '2026-09-17T13:00:00Z', data: { sessions: 42, conversions: 3 } },
      { id: 'measurement-imported', kind: 'citation_import', source: 'owner_import', observed_at: '2026-09-17T14:00:00Z', data: { url: 'https://source.example/reference' } },
    ],
  })
  await page.goto('/sites/site-1/visibility')

  await expect(page.getByRole('heading', { name: 'How to read observations' })).toBeVisible()
  const table = page.getByRole('table', { name: 'Visibility measurements' })
  await expect(table.getByText('Lab + Field Performance', { exact: true })).toBeVisible()
  await expect(table.getByText('Field Performance', { exact: true })).toBeVisible()
  await expect(table.getByText('Field / Traffic', { exact: true })).toBeVisible()
  await expect(table.getByText('Unknown / Imported', { exact: true })).toBeVisible()
  const legend = page.getByRole('list', { name: 'Observation basis legend' })
  await expect(legend.getByText('Controlled PageSpeed or Lighthouse measurement; it is not field-user experience.', { exact: true })).toBeVisible()
  await expect(legend.getByText('Observed search or traffic activity from GSC, GA4, or referral data.', { exact: true })).toBeVisible()
  await expect(legend.getByText('Available real-user or origin loading-experience data; it is separate from controlled lab measurements.', { exact: true })).toBeVisible()
  await expect(legend.getByText('Imported or unclassified evidence; confirm its provenance before relying on it.', { exact: true })).toBeVisible()
})

test('visibility labels observation freshness without implying data quality or ranking', async ({ page }) => {
  const daysAgo = (days: number) => new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString()
  await installWorkspaceMocks(page, {
    measurements: [
      { id: 'measurement-fresh', kind: 'fresh_observation', source: 'fresh-source', observed_at: daysAgo(2), data: {} },
      { id: 'measurement-aging', kind: 'aging_observation', source: 'aging-source', observed_at: daysAgo(8), data: {} },
      { id: 'measurement-stale', kind: 'stale_observation', source: 'stale-source', observed_at: daysAgo(31), data: {} },
      { id: 'measurement-missing', kind: 'missing_observation', source: 'missing-source', data: {} },
      { id: 'measurement-invalid', kind: 'invalid_observation', source: 'invalid-source', observed_at: 'not-a-timestamp', data: {} },
    ],
  })
  await page.goto('/sites/site-1/visibility')

  const table = page.getByRole('table', { name: 'Visibility measurements' })
  await expect(page.getByRole('heading', { name: 'Observation freshness', exact: true })).toBeVisible()
  await expect(page.getByText('Freshness describes only elapsed time since the source timestamp. It does not measure accuracy, data quality, rankings, or every visitor\'s experience.', { exact: true })).toBeVisible()
  await expect(table.locator('tbody tr').filter({ hasText: 'Fresh Observation' }).getByText('Fresh', { exact: true })).toBeVisible()
  await expect(table.locator('tbody tr').filter({ hasText: 'Aging Observation' }).getByText('Aging', { exact: true })).toBeVisible()
  await expect(table.locator('tbody tr').filter({ hasText: 'Stale Observation' }).getByText('Stale', { exact: true })).toBeVisible()
  await expect(table.locator('tbody tr').filter({ hasText: 'Missing Observation' }).getByText('Unknown', { exact: true })).toBeVisible()
  await expect(table.locator('tbody tr').filter({ hasText: 'Invalid Observation' }).getByText('Unknown', { exact: true })).toBeVisible()
})

test('visibility import guidance distinguishes backlink and business-listing observations from ranking proof and automatic changes', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/visibility')
  await page.getByRole('tab', { name: 'Imports' }).click()

  const backlinkGuidance = page.locator('.notice').filter({ hasText: 'Backlink observations and business-listing observations' })
  await expect(backlinkGuidance).toBeVisible()
  await expect(backlinkGuidance).toContainText('They are not proof of rankings and do not make automatic changes.')
  await expect(page.getByLabel('JSON array')).toHaveAttribute('placeholder', /backlink-observation/)
  await expect(page.getByLabel('JSON array')).toHaveAttribute('placeholder', /business-listing-observation/)
  await expect(page.locator('.notice').filter({ hasText: 'Competitor observations' })).toContainText("provider-specific observations with a source and date. They are not a guarantee of this site's ranking and do not make automatic changes.")
})

test('visibility import guidance warns that credentials are rejected before storage', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/visibility')
  await page.getByRole('tab', { name: 'Imports' }).click()

  const credentialGuidance = page.locator('.notice').filter({ hasText: 'Keep credentials out.' })
  await expect(credentialGuidance).toBeVisible()
  await expect(credentialGuidance).toContainText('Never paste API keys, passwords, tokens, cookies, or other login details into an import.')
  await expect(credentialGuidance).toContainText('The API rejects credentials before anything is stored.')
})

test('content calendar shows the rolling four-week plan without treating briefs as scheduled publications', async ({ page }) => {
  await installWorkspaceMocks(page, {
    connectionItems: [
      { kind: 'wordpress', status: 'connected', checked_at: '2026-09-14T14:00:00Z' },
    ],
    articles: [
      { id: 'article-1', title: 'Week one topic', body: '', status: 'planned', brief: { week: 1 }, sources: [], managed: true },
      { id: 'article-4', title: 'Week four topic', body: '', status: 'planned', brief: { week: 4 }, sources: [], managed: true },
    ],
  })
  await page.goto('/sites/site-1/content')
  await expect(page.getByRole('heading', { name: 'Rolling four-week plan' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Planned brief Week one topic' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Planned brief Week four topic' })).toBeVisible()
  await expect(page.getByText('They are not publication dates until an editor checks and schedules them.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Connection readiness' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'WordPress connection' })).toBeVisible()
  await expect(page.getByText('Connected', { exact: true })).toBeVisible()
  await expect(page.getByText(/Connection checked: Sep 14/)).toBeVisible()
  await expect(page.getByRole('heading', { name: 'AI research provider' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'AI research provider' }).getByText('Needs Connection', { exact: true })).toBeVisible()
  await expect(page.getByText('Connection checked: Not yet checked', { exact: true })).toBeVisible()
  await expect(page.getByText(/Calendar view last refreshed:/)).toBeVisible()
})

test('article editor restores persisted editorial check state', async ({ page }) => {
  await installWorkspaceMocks(page)
  const article = {
    id: 'article-1',
    site_id: 'site-1',
    title: 'A useful repair guide',
    slug: 'a-useful-repair-guide',
    body: '<p>Bring your repair questions.</p>',
    status: 'review_needed',
    brief: {
      research_evidence: [
        { kind: 'search_observation', query: 'windshield repair Houston', source: 'gsc', observed_at: '2026-09-17T12:00:00Z' },
        { kind: 'competitor_observation', competitor_url: 'https://rival.example/', source: 'dataforseo' },
      ],
    },
    checks: { passed: false, blockers: ['missing_author'], warnings: [] },
    sources: [],
    managed: true,
    updated_at: '2026-09-17T12:00:00Z',
  }
  await page.route('**/api/v1/sites/site-1/articles/article-1', async (route) => {
    if (route.request().method() === 'GET') return json(route, article)
    return json(route, article)
  })
  await page.route('**/api/v1/sites/site-1/articles/article-1/revisions', async (route) => json(route, { items: [], total: 0 }))

  await page.goto('/sites/site-1/content/articles/article-1')
  await expect(page.getByRole('heading', { name: 'Editorial check' })).toBeVisible()
  await expect(page.getByText('missing_author', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Planning guidance' })).toBeVisible()
  await expect(page.getByText('windshield repair Houston', { exact: true })).toBeVisible()
  await expect(page.getByText('These observations inform planning only; they are not ranking guarantees or publication authorization.')).toBeVisible()
  await expect(page.getByText('Not checked yet', { exact: true })).toHaveCount(0)
})

test('article editor saves and reloads allowlisted image provenance without provider payloads', async ({ page }) => {
  await installWorkspaceMocks(page)
  let article = {
    id: 'article-1',
    site_id: 'site-1',
    title: 'A useful repair guide',
    slug: 'a-useful-repair-guide',
    body: '<p>Bring your repair questions.</p>',
    status: 'drafting',
    brief: {
      generation: { kind: 'provider_draft', provider: 'isolated-test' },
      research: { complete: true, sources: [{ url: 'https://example.test/about', content_hash: 'verified-test-hash' }] },
      image_sources: [
        { url: 'https://cdn.example/owner.jpg', kind: 'owner_provided', owner_confirmed: true, alt: 'Technician inspecting a vehicle', provider_payload: { secret: 'must-not-render' } },
        { url: 'https://cdn.example/licensed.jpg', kind: 'licensed', attribution: 'Studio Photographer', license: 'CC BY 4.0', alt: 'Repair shop exterior' },
        { url: 'https://cdn.example/generated.jpg', kind: 'generated_illustration', disclosure: 'AI-generated illustration; not a real shop', not_real: true, alt: 'Illustrated repair scene', provider_name: 'hidden-provider' },
      ],
    },
    checks: null,
    sources: [],
    managed: true,
    updated_at: '2026-09-17T12:00:00Z',
  }
  let savedBrief: Record<string, unknown> | undefined
  let savedSources: unknown
  await page.route('**/api/v1/sites/site-1/articles/article-1', async (route) => {
    if (route.request().method() === 'GET') return json(route, article)
    if (route.request().method() === 'PATCH') {
      const requestBody = (route.request().postDataJSON() ?? {}) as { brief?: Record<string, unknown>; sources?: unknown }
      savedBrief = requestBody.brief
      savedSources = requestBody.sources
      article = { ...article, brief: requestBody.brief ?? article.brief }
      return json(route, article)
    }
    return json(route, article)
  })
  await page.route('**/api/v1/sites/site-1/articles/article-1/revisions', async (route) => json(route, { items: [], total: 0 }))

  await page.goto('/sites/site-1/content/articles/article-1')
  await expect(page.getByRole('heading', { name: 'Image provenance', exact: true })).toBeVisible()
  await expect(page.getByLabel('Image 1 owner confirmed')).toBeChecked()
  await expect(page.getByLabel('Image 2 attribution')).toHaveValue('Studio Photographer')
  await expect(page.getByLabel('Image 2 license')).toHaveValue('CC BY 4.0')
  await expect(page.getByLabel('Image 3 disclosure')).toHaveValue('AI-generated illustration; not a real shop')
  await expect(page.getByLabel('Image 3 not real confirmation')).toBeChecked()
  await expect(page.getByRole('heading', { name: 'Planning guidance · image provenance' })).toBeVisible()
  await expect(page.getByText(/Owner confirmed/)).toBeVisible()
  await expect(page.getByText(/Attribution: Studio Photographer/)).toBeVisible()
  await expect(page.getByText(/Disclosure: AI-generated illustration; not a real shop/)).toBeVisible()
  await expect(page.getByText('must-not-render', { exact: true })).toHaveCount(0)
  await expect(page.getByText('hidden-provider', { exact: true })).toHaveCount(0)

  await page.getByLabel('Image 1 owner confirmed').uncheck()
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Confirm that image 1 was provided by the site owner.', { exact: true })).toBeVisible()
  expect(savedBrief).toBeUndefined()
  await page.getByLabel('Image 1 owner confirmed').check()

  await page.getByLabel('Image 2 attribution').fill('')
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Add attribution for licensed image 2.', { exact: true })).toBeVisible()
  await page.getByLabel('Image 2 attribution').fill('Studio Photographer')
  await page.getByLabel('Image 2 license').fill('')
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Add the license for licensed image 2.', { exact: true })).toBeVisible()
  await page.getByLabel('Image 2 license').fill('CC BY 4.0')

  await page.getByLabel('Image 3 disclosure').fill('')
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Add a disclosure for generated image 3.', { exact: true })).toBeVisible()
  await page.getByLabel('Image 3 disclosure').fill('AI-generated illustration; not a real shop')
  await page.getByLabel('Image 3 not real confirmation').uncheck()
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Confirm that generated image 3 is not a real product, premises, or completed work.', { exact: true })).toBeVisible()
  await page.getByLabel('Image 3 not real confirmation').check()
  await page.getByLabel('Sources', { exact: false }).fill('{"url":"https://example.test/about","content_hash":"verified-test-hash"}')

  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Article saved. The API now has the latest editor state.')).toBeVisible()
  expect(savedBrief?.generation).toEqual({ kind: 'provider_draft', provider: 'isolated-test' })
  expect(savedBrief?.research).toEqual({ complete: true, sources: [{ url: 'https://example.test/about', content_hash: 'verified-test-hash' }] })
  expect(savedSources).toEqual([{ url: 'https://example.test/about', content_hash: 'verified-test-hash' }])
  expect(savedBrief?.image_sources).toEqual([
    { url: 'https://cdn.example/owner.jpg', kind: 'owner_provided', alt: 'Technician inspecting a vehicle', owner_confirmed: true },
    { url: 'https://cdn.example/licensed.jpg', kind: 'licensed', attribution: 'Studio Photographer', license: 'CC BY 4.0', alt: 'Repair shop exterior' },
    { url: 'https://cdn.example/generated.jpg', kind: 'generated_illustration', alt: 'Illustrated repair scene', disclosure: 'AI-generated illustration; not a real shop', not_real: true },
  ])

  await page.reload()
  await expect(page.getByLabel('Image 3 disclosure')).toHaveValue('AI-generated illustration; not a real shop')
  await expect(page.getByLabel('Image 3 not real confirmation')).toBeChecked()
  await expect(page.getByText('hidden-provider', { exact: true })).toHaveCount(0)
})

test('connections explain WordPress and WooCommerce capability coverage without exposing credentials', async ({ page }) => {
  await installWorkspaceMocks(page, {
    connectionItems: [
      {
        kind: 'wordpress',
        status: 'connected',
        credentials: { application_password: 'must-not-render' },
        capabilities: {
          authenticated: true,
          native: { read: true, create: true, update: true, publish: false },
          editorial: { read: true, write: true, conditionally_writable_fields: ['body'], builder_constraints: { detected: true } },
          resource_types: [
            { key: 'post', label: 'Posts', inventoryable: true, editorial_write: { supported: true, automatic: true } },
            { key: 'page', label: 'Pages', inventoryable: true, editorial_write: { supported: true, automatic: true } },
            { key: 'portfolio', label: 'Portfolio', inventoryable: true, editorial_write: { supported: false, automatic: false, policy: 'inventory_only' } },
          ],
          seo: { read: true, write: false, writable_fields: [] },
          plugins: { forgeseo: { detected: true, read: true, webhooks: true } },
        },
      },
      {
        kind: 'woocommerce',
        status: 'connected',
        credentials: { consumer_secret: 'must-not-render' },
        capabilities: {
          authenticated: true,
          woocommerce_api: true,
          products: { read: true, update: true },
          categories: { read: false, update: false },
          protected_commerce_fields: ['price', 'stock'],
        },
      },
    ],
  })
  await page.goto('/sites/site-1/settings/connections')

  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  const woocommerce = page.locator('form.connection-card').filter({ hasText: 'WooCommerce' }).first()
  await expect(wordpress.getByText('Capability summary', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('Editorial type: Posts', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('Portfolio is inventoryable for audit and planning only. Editorial writes require a specific connector contract.', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('Automatic', { exact: true }).first()).toBeVisible()
  await expect(wordpress.getByText('Read Only', { exact: true }).first()).toBeVisible()
  await expect(wordpress.getByText('Needs Review', { exact: true }).first()).toBeVisible()
  const notifications = wordpress.locator('li').filter({ hasText: 'Targeted change notifications' })
  await expect(notifications.getByText('Automatic', { exact: true })).toBeVisible()
  await expect(notifications.getByText('The verified ForgeSEO connector can send signed change notifications for targeted checks. Periodic polling remains active as a fallback.', { exact: true })).toBeVisible()
  await expect(wordpress.getByLabel('Webhook secret', { exact: true })).toHaveAttribute('type', 'password')
  await expect(wordpress.getByLabel('Webhook secret', { exact: true })).toHaveValue('')
  await expect(wordpress.getByText('Optional, site-scoped, and stored encrypted. Only needed when the optional ForgeSEO connector sends signed change notifications. Periodic polling continues without it.', { exact: true })).toBeVisible()
  await expect(woocommerce.getByText('Unsupported', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('must-not-render', { exact: true })).toHaveCount(0)
})

test('WordPress resource coverage stays review-only when a descriptor is malformed', async ({ page }) => {
  await installWorkspaceMocks(page, {
    connectionItems: [{
      kind: 'wordpress',
      status: 'connected',
      checked_at: '2026-09-22T12:00:00Z',
      capabilities: {
        authenticated: true,
        native: { read: true },
        resource_types: [
          { key: 'post', label: 'Posts', inventoryable: true, editorial_write: { supported: true, automatic: true } },
          { malformed: true },
        ],
      },
    }],
  })
  await page.goto('/sites/site-1/settings/connections')

  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  await expect(wordpress.getByText('Editorial type: Posts', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('Additional editorial resource coverage', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('Some resource descriptors were malformed or incomplete. Do not treat the listed types as the complete site inventory until the next check is clean.', { exact: true })).toBeVisible()
  await expect(wordpress.getByText('Needs Review', { exact: true }).first()).toBeVisible()
})

test('DataForSEO connection explains policy-driven competitor observations and location requirements', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/settings/connections')
  const dataForSeo = page.locator('form.connection-card').filter({ hasText: 'DataForSEO' }).first()
  await expect(dataForSeo.getByText('Optional keyword, SERP, and policy-driven competitor observations with provider pricing.', { exact: true })).toBeVisible()
  await expect(dataForSeo.getByText('Required for competitor observations. Use the DataForSEO location code for the site’s target market.', { exact: true })).toBeVisible()
})

test('WordPress notification capability separates connector review from unsupported coverage', async ({ page }) => {
  await installWorkspaceMocks(page, {
    connectionItems: [{
      kind: 'wordpress',
      status: 'connected',
      credentials: { webhook_secret: 'must-not-render-webhook' },
      capabilities: {
        native: { read: true },
        plugins: { forgeseo: { detected: true, read: true, webhooks: false } },
      },
    }],
  })
  await page.goto('/sites/site-1/settings/connections')
  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  const notifications = wordpress.locator('li').filter({ hasText: 'Targeted change notifications' })
  await expect(notifications.getByText('Needs Review', { exact: true })).toBeVisible()
  await expect(notifications.getByText('The ForgeSEO connector is present, but signed notifications are not advertised as available. Review its configuration; periodic polling continues.', { exact: true })).toBeVisible()
  await expect(page.getByText('must-not-render-webhook', { exact: true })).toHaveCount(0)
})

test('Google connection cards explain the bounded OAuth flow and keep the start link site-scoped', async ({ page }) => {
  await installWorkspaceMocks(page, {
    connectionItems: [
      { kind: 'gsc', status: 'needs_test', safe_fields: { site_url: 'sc-domain:acme.example' } },
      { kind: 'ga4', status: 'needs_test', safe_fields: { property_id: '123456789' } },
    ],
  })
  await page.goto('/sites/site-1/settings/connections')

  const gsc = page.locator('form.connection-card').filter({ hasText: 'Google Search Console' }).first()
  const ga4 = page.locator('form.connection-card').filter({ hasText: 'Google Analytics 4' }).first()
  await expect(gsc.getByText('Save the OAuth client ID and secret first, then authorize read-only Search Console access. Tokens stay encrypted on this site.')).toBeVisible()
  await expect(ga4.getByText('Save the OAuth client ID and secret first, then authorize read-only Analytics 4 access. Tokens stay encrypted on this site.')).toBeVisible()
  await expect(gsc.getByRole('link', { name: 'Connect with Google' })).toHaveAttribute('href', '/api/v1/sites/site-1/connections/gsc/oauth/start')
  await expect(ga4.getByRole('link', { name: 'Connect with Google' })).toHaveAttribute('href', '/api/v1/sites/site-1/connections/ga4/oauth/start')
})

test('Google connection cards distinguish verified access from provider errors without exposing diagnostics', async ({ page }) => {
  await installWorkspaceMocks(page, {
    connectionItems: [
      {
        kind: 'gsc',
        status: 'connected',
        capabilities: { last_connection_test: { status: 'verified', kind: 'gsc', read_only: true, tested_at: '2026-09-17T12:00:00Z' } },
      },
      {
        kind: 'ga4',
        status: 'error',
        capabilities: { last_connection_test: { status: 'error', kind: 'ga4', error_code: 'remote_error', tested_at: '2026-09-17T12:00:00Z' } },
      },
    ],
  })
  await page.goto('/sites/site-1/settings/connections')

  const gsc = page.locator('form.connection-card').filter({ hasText: 'Google Search Console' }).first()
  const ga4 = page.locator('form.connection-card').filter({ hasText: 'Google Analytics 4' }).first()
  await expect(gsc.getByText('Read-only Google Search Console access was verified. ForgeSEO can collect observations, but it cannot change provider data.', { exact: true })).toBeVisible()
  await expect(gsc.getByRole('status', { name: 'Google Search Console connection readiness: Automatic' })).toBeVisible()
  await expect(ga4.getByText('The last provider check failed. Review the property and authorization, then run the connection test again.', { exact: true })).toBeVisible()
  await expect(ga4.getByRole('status', { name: 'Google Analytics 4 connection readiness: Degraded' })).toBeVisible()
  await expect(ga4.getByText('remote_error', { exact: true })).toHaveCount(0)
})

test('GA4 settings save bounded conversion reporting lists and reload without echoing credentials', async ({ page }) => {
  const controls = await installWorkspaceMocks(page, {
    connectionItems: [{
      kind: 'ga4',
      status: 'needs_test',
      safe_fields: {
        property_id: '123456789',
        conversion_event_names: ['generate_lead'],
        dimensions: ['date'],
        metrics: ['sessions'],
      },
      credentials: { client_secret: 'must-not-render-client-secret', refresh_token: 'must-not-render-refresh-token' },
    }],
  })
  await page.goto('/sites/site-1/settings/connections')

  const ga4 = page.locator('form.connection-card').filter({ hasText: 'Google Analytics 4' }).first()
  await expect(ga4.getByText('Optional. Comma-separated GA4 event names to report as business conversions; up to 12 names. This does not create or change events in Google Analytics.', { exact: true })).toBeVisible()
  await expect(ga4.getByText('Optional. Comma-separated GA4 dimensions, up to 8. Include eventName when you need to compare conversion events; leave blank for the default date view.', { exact: true })).toBeVisible()
  await expect(ga4.getByText('Optional. Comma-separated GA4 metrics, up to 10. Include conversions for conversion-aware reporting; leave blank for the default sessions and users view.', { exact: true })).toBeVisible()
  await expect(ga4.getByLabel('Client secret', { exact: true })).toHaveValue('')
  await expect(page.getByText('must-not-render-client-secret', { exact: true })).toHaveCount(0)
  await expect(page.getByText('must-not-render-refresh-token', { exact: true })).toHaveCount(0)

  const eventNames = Array.from({ length: 13 }, (_, index) => `event_${index + 1}`).join(', ')
  await ga4.getByLabel('Conversion event names').fill(eventNames)
  await ga4.getByLabel('Reporting dimensions').fill('date, eventName')
  await ga4.getByLabel('Reporting metrics').fill('sessions, conversions')
  await ga4.getByRole('button', { name: 'Save', exact: true }).click()

  await expect(page.getByText('Google Analytics 4 settings saved.', { exact: true })).toBeVisible()
  expect(controls.connectionSaves).toEqual([{
    kind: 'ga4',
    body: {
      credentials: {},
      settings: {
        property_id: '123456789',
        conversion_event_names: Array.from({ length: 12 }, (_, index) => `event_${index + 1}`),
        dimensions: ['date', 'eventName'],
        metrics: ['sessions', 'conversions'],
      },
    },
  }])
  await expect(ga4.getByLabel('Conversion event names')).toHaveValue(eventNames.split(', ').slice(0, 12).join(', '))
  await expect(ga4.getByLabel('Reporting dimensions')).toHaveValue('date, eventName')
  await expect(ga4.getByLabel('Reporting metrics')).toHaveValue('sessions, conversions')
})

test('an unconfigured WordPress connection is clearly marked as needing connection', async ({ page }) => {
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/settings/connections')
  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  await expect(wordpress.getByText('Needs Connection', { exact: true }).last()).toBeVisible()
  await expect(wordpress.getByText('Connect this source before ForgeSEO can inspect its capabilities.', { exact: true })).toBeVisible()
  const notifications = wordpress.locator('li').filter({ hasText: 'Targeted change notifications' })
  await expect(notifications.getByText('Needs Connection', { exact: true })).toBeVisible()
  await expect(notifications.getByText('Connect WordPress to enable optional signed change notifications. Periodic polling continues without them.', { exact: true })).toBeVisible()
})

test('activity stream renders a new site-scoped event without manual refresh', async ({ page }) => {
  await page.addInitScript(() => {
    class FakeEventSource extends EventTarget {
      static instances: FakeEventSource[] = []
      onopen: (() => void) | null = null
      onerror: (() => void) | null = null
      url: string
      readyState = 1
      constructor(url: string) {
        super()
        this.url = url
        FakeEventSource.instances.push(this)
        queueMicrotask(() => this.onopen?.())
      }
      close() { this.readyState = 2 }
    }
    // The test-only constructor is intentionally local to the page; the
    // production hook still uses the browser's authenticated EventSource.
    Object.defineProperty(window, 'EventSource', { configurable: true, writable: true, value: FakeEventSource })
    Object.defineProperty(window, '__forgeEventSourceInstances', { configurable: true, value: FakeEventSource.instances })
  })
  await installWorkspaceMocks(page)
  await page.goto('/sites/site-1/activity')
  await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible()
  await page.evaluate(() => {
    const instances = (window as unknown as { __forgeEventSourceInstances: Array<EventTarget> }).__forgeEventSourceInstances
    const event = new MessageEvent('activity', { data: JSON.stringify({
      id: 99,
      site_id: 'site-1',
      kind: 'job_finished',
      message: 'Targeted audit finished',
      created_at: '2026-09-16T16:00:00Z',
      data: {
        job_id: 'internal-activity-job-id',
        idempotency_key: 'internal-activity-idempotency-key',
        credentials: { token: 'activity-secret' },
        provider_response: 'raw-provider-response',
      },
    }) })
    instances[0]?.dispatchEvent(event)
  })
  await expect(page.getByText('Targeted audit finished', { exact: true })).toBeVisible()
  await expect(page.getByText('Recorded by server', { exact: true })).toBeVisible()
  await expect(page.getByText('internal-activity-job-id', { exact: true })).toHaveCount(0)
  await expect(page.getByText('internal-activity-idempotency-key', { exact: true })).toHaveCount(0)
  await expect(page.getByText('activity-secret', { exact: true })).toHaveCount(0)
  await expect(page.getByText('raw-provider-response', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Live', { exact: true })).toBeVisible()
})

test('run history shows safe live job progress and reconnects without exposing internals', async ({ page }) => {
  await page.addInitScript(() => {
    class FakeEventSource extends EventTarget {
      static instances: FakeEventSource[] = []
      onopen: (() => void) | null = null
      onerror: (() => void) | null = null
      readyState = 1
      constructor() {
        super()
        FakeEventSource.instances.push(this)
        queueMicrotask(() => this.onopen?.())
      }
      close() { this.readyState = 2 }
    }
    Object.defineProperty(window, 'EventSource', { configurable: true, writable: true, value: FakeEventSource })
    Object.defineProperty(window, '__forgeEventSourceInstances', { configurable: true, value: FakeEventSource.instances })
  })
  await installWorkspaceMocks(page, {
    jobs: [{
      id: 'job-history-progress',
      site_id: 'site-1',
      kind: 'full_cycle',
      status: 'running',
      attempts: 1,
      created_at: '2026-09-17T14:00:00Z',
      updated_at: '2026-09-17T14:01:00Z',
      idempotency_key: 'secret-idempotency-key',
      payload: { provider_token: 'must-not-render' },
      result: { stage_job_id: 'secret-stage-job-id' },
    }],
  })
  await page.goto('/sites/site-1/jobs')
  await expect(page.getByRole('heading', { name: 'Run history', exact: true })).toBeVisible()
  await page.evaluate(() => {
    const instances = (window as unknown as { __forgeEventSourceInstances: Array<EventTarget> }).__forgeEventSourceInstances
    const event = new MessageEvent('progress', { data: JSON.stringify({
      id: 99,
      site_id: 'site-1',
      kind: 'job_progress',
      message: 'provider-token must-not-render raw-provider-response',
      data: {
        site_id: 'site-1',
        job_id: 'job-progress-internal-id',
        job_kind: 'full_cycle',
        status: 'running',
        phase: 'stage_start',
        stage: 'inventory',
        stage_index: 2,
        stage_count: 5,
        percent: 40,
        message: 'provider-token must-not-render raw-provider-response',
        stage_job_id: 'secret-stage-job-id',
        idempotency_key: 'secret-idempotency-key',
        credentials: { token: 'must-not-render' },
        payload: { provider_response: 'must-not-render' },
      },
    }) })
    instances[0]?.dispatchEvent(event)
  })
  await expect(page.getByText('Full cycle is running.', { exact: false })).toBeVisible()
  await expect(page.locator('.notice').filter({ hasText: 'Inventory is in progress.' })).toBeVisible()
  await expect(page.locator('.notice').filter({ hasText: '40% complete.' })).toBeVisible()
  await expect(page.getByText('secret-stage-job-id', { exact: true })).toHaveCount(0)
  await expect(page.getByText('secret-idempotency-key', { exact: true })).toHaveCount(0)
  await expect(page.getByText('must-not-render', { exact: true })).toHaveCount(0)
  await expect(page.getByText('provider-token must-not-render raw-provider-response', { exact: true })).toHaveCount(0)
  await page.evaluate(() => {
    const instances = (window as unknown as { __forgeEventSourceInstances: Array<{ onerror: (() => void) | null }> }).__forgeEventSourceInstances
    instances[0]?.onerror?.()
  })
  await expect(page.getByText('Live updates are reconnecting', { exact: true })).toBeVisible()
})
