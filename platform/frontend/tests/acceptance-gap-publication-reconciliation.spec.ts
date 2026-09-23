import { expect, test, type Page, type Route } from '@playwright/test'

const site = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Pilot Workshop',
  origin: 'https://pilot.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: false,
  facts: { business_name: 'Pilot Workshop' },
}

const ambiguousPublication = {
  id: 'publication-1',
  article_id: 'article-1',
  operation_key: 'internal-operation-key',
  status: 'ambiguous',
  policy_version: 4,
  remote_id: null,
  result: { status: 'ambiguous', job_id: 'internal-job-id' },
  created_at: '2026-09-21T12:00:00Z',
  updated_at: '2026-09-21T12:01:00Z',
}

type MockOptions = {
  role?: 'owner' | 'editor' | 'viewer'
  publications?: unknown[]
  publicationAfterReconcile?: unknown[]
  reconcileResult?: Record<string, unknown>
  publicationsError?: boolean
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page, options: MockOptions = {}) {
  const role = options.role ?? 'owner'
  let publications = options.publications ?? [ambiguousPublication]
  let publicationListRequests = 0
  let reconcileRequests = 0
  let jobRequests = 0

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()

    if (path === '/auth/status' && method === 'GET') return json(route, { initialized: true })
    if (path === '/auth/me' && method === 'GET') return json(route, {
      user: { id: 'user-1', email: `${role}@example.com`, name: role },
      team: { id: 'team-1', name: 'Pilot team' },
      role,
      csrf_token: 'csrf-test-token',
    })
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1' && method === 'GET') return json(route, site)
    if (path === '/sites/site-1/publications' && method === 'GET') {
      publicationListRequests += 1
      if (options.publicationsError) return json(route, { detail: 'The publication ledger is temporarily unavailable.' }, 503)
      return json(route, { items: publications, total: publications.length })
    }
    if (path === '/sites/site-1/publications/publication-1/reconcile' && method === 'POST') {
      reconcileRequests += 1
      publications = options.publicationAfterReconcile ?? publications
      return json(route, { id: 'job-1', site_id: 'site-1', kind: 'reconcile_publication', status: 'queued' }, 202)
    }
    const jobMatch = path.match(/^\/sites\/site-1\/jobs\/([^/]+)$/)
    if (jobMatch && method === 'GET') {
      jobRequests += 1
      return json(route, {
        id: jobMatch[1],
        site_id: 'site-1',
        kind: 'reconcile_publication',
        status: 'complete',
        result: options.reconcileResult ?? { workflow: 'reconcile_publication', status: 'published', complete: true, job_id: 'internal-job-id' },
      })
    }
    return json(route, { items: [], total: 0 })
  })

  return {
    get publicationListRequests() { return publicationListRequests },
    get reconcileRequests() { return reconcileRequests },
    get jobRequests() { return jobRequests },
  }
}

test('owner can check an ambiguous remote outcome without attempting a new publication', async ({ page }) => {
  const controls = await installMocks(page, {
    publicationAfterReconcile: [{ ...ambiguousPublication, status: 'published', remote_id: 'remote-1', result: { status: 'published' } }],
    reconcileResult: { workflow: 'reconcile_publication', status: 'published', complete: true, job_id: 'internal-job-id' },
  })
  await page.goto('/sites/site-1/publications')

  await page.getByRole('button', { name: 'Check remote outcome' }).click()

  await expect(page.getByRole('status')).toContainText('The remote publication was confirmed. No new publication was attempted.')
  await expect(page.getByRole('button', { name: 'Check remote outcome' })).toHaveCount(0)
  expect(controls.reconcileRequests).toBe(1)
  expect(controls.jobRequests).toBe(1)
  expect(controls.publicationListRequests).toBeGreaterThanOrEqual(2)
  await expect(page.getByText('internal-operation-key', { exact: true })).toHaveCount(0)
  await expect(page.getByText('internal-job-id', { exact: true })).toHaveCount(0)
})

test('a held reconciliation result explains that review is still required', async ({ page }) => {
  const controls = await installMocks(page, {
    reconcileResult: { workflow: 'reconcile_publication', status: 'held', reason: 'snapshot_mismatch', next_action: 'review_publication_reconciliation' },
  })
  await page.goto('/sites/site-1/publications')

  await page.getByRole('button', { name: 'Check remote outcome' }).click()

  await expect(page.getByRole('status')).toContainText('No new publication was attempted; this publication remains held for review.')
  await expect(page.getByRole('status')).toContainText('The remote record did not match the saved publication snapshot.')
  expect(controls.reconcileRequests).toBe(1)
  expect(controls.jobRequests).toBe(1)
})

for (const role of ['editor', 'viewer'] as const) {
  test(`${role} sees reconciliation as read-only and cannot submit it`, async ({ page }) => {
    const controls = await installMocks(page, { role })
    await page.goto('/sites/site-1/publications')

    await expect(page.getByRole('button', { name: 'Check remote outcome' })).toHaveCount(0)
    await expect(page.getByText('Owner access required to check the remote outcome. This view is read-only.', { exact: true })).toBeVisible()
    expect(controls.reconcileRequests).toBe(0)
    expect(controls.jobRequests).toBe(0)
  })
}

test('an empty publication ledger does not imply work was optimized', async ({ page }) => {
  const controls = await installMocks(page, { publications: [] })
  await page.goto('/sites/site-1/publications')

  await expect(page.getByRole('heading', { name: 'No publication attempts' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Check remote outcome' })).toHaveCount(0)
  expect(controls.reconcileRequests).toBe(0)
})

test('a publication ledger error remains explicit and exposes no reconciliation action', async ({ page }) => {
  const controls = await installMocks(page, { publicationsError: true })
  await page.goto('/sites/site-1/publications')

  await expect(page.getByRole('alert')).toContainText('The publication ledger is temporarily unavailable.')
  await expect(page.getByRole('button', { name: 'Check remote outcome' })).toHaveCount(0)
  expect(controls.reconcileRequests).toBe(0)
})
