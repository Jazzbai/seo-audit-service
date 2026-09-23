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
  name: 'Candidate Pilot',
  origin: 'https://candidate.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: {},
}

const finding = {
  id: 'finding-1',
  page_id: 'page-1',
  key: 'pages:1:missing_meta_description',
  code: 'missing_meta_description',
  severity: 'medium',
  title: 'Page is missing a meta description',
  details: {},
  status: 'open',
  last_seen_at: '2026-09-20T12:00:00Z',
}

function candidate(id: string, status: string, after: string) {
  return {
    id,
    page_id: 'page-1',
    field: 'meta_description',
    before_value: 'Existing description',
    after_value: after,
    source_hash: `source-${id}`,
    status,
    details: { url: `https://candidate.example/${id}` },
    created_at: '2026-09-20T12:00:00Z',
  }
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installIssueMocks(page: Page, options: { emptyCandidatePage?: boolean } = {}) {
  let candidates = [
    candidate('candidate-primary', 'pending', 'Primary suggested description'),
    candidate('candidate-sibling', 'pending', 'Sibling suggested description'),
    candidate('candidate-historical', 'superseded', 'Historical superseded description'),
  ]

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1' && method === 'GET') return json(route, site)
    if (path === '/sites/site-1/overview' && method === 'GET') return json(route, {
      site,
      counts: { pages: 1, open_findings: 1, pending_candidates: 2, published_articles: 0, open_incidents: 0 },
      monitoring: { status: 'healthy' },
      budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 },
      recent_events: [],
      coverage: { status: 'partial', error_count: 0, pending_url_count: 0 },
      connections: [],
    })
    if (path === '/sites/site-1/findings' && method === 'GET') {
      return json(route, { items: [finding], total: 100 })
    }
    if (path === '/sites/site-1/candidates' && method === 'GET') {
      const offset = Number(url.searchParams.get('offset') ?? '0')
      if (offset >= 50) {
        return json(route, { items: options.emptyCandidatePage ? [] : [candidate('candidate-page-two', 'pending', 'Candidate on page two')], total: 51 })
      }
      return json(route, { items: candidates, total: options.emptyCandidatePage ? 51 : candidates.length })
    }
    const decisionMatch = path.match(/^\/sites\/site-1\/candidates\/([^/]+)\/decision$/)
    if (decisionMatch && method === 'POST') {
      const id = decisionMatch[1]
      const decision = (request.postDataJSON() as { decision?: string }).decision
      candidates = candidates.map((item) => item.id === id ? { ...item, status: decision === 'approve' ? 'approved' : 'rejected' } : item)
      return json(route, candidates.find((item) => item.id === id))
    }
    return json(route, { items: [], total: 0 })
  })
}

test('candidate pagination is independent from findings and keeps later siblings reachable', async ({ page }) => {
  const candidateOffsets: number[] = []
  const findingOffsets: number[] = []
  await installIssueMocks(page)
  await page.unroute('**/api/v1/**')
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1' && method === 'GET') return json(route, site)
    if (path === '/sites/site-1/findings' && method === 'GET') {
      findingOffsets.push(Number(url.searchParams.get('offset') ?? '0'))
      return json(route, { items: [finding], total: 100 })
    }
    if (path === '/sites/site-1/candidates' && method === 'GET') {
      const offset = Number(url.searchParams.get('offset') ?? '0')
      candidateOffsets.push(offset)
      return json(route, { items: offset >= 50 ? [candidate('candidate-page-two', 'pending', 'Candidate on page two')] : [candidate('candidate-primary', 'pending', 'Primary suggested description')], total: 51 })
    }
    return json(route, { items: [], total: 0 })
  })

  await page.goto('/sites/site-1/issues')
  await expect(page.locator('.candidate-card').filter({ hasText: 'Primary suggested description' })).toBeVisible()
  await expect(page.getByText('Page is missing a meta description', { exact: true })).toBeVisible()

  await page.getByRole('navigation', { name: 'Candidate results' }).getByRole('button', { name: 'Next results' }).click()
  await expect(page.locator('.candidate-card').filter({ hasText: 'Candidate on page two' })).toBeVisible()
  await expect(page.getByText('Page is missing a meta description', { exact: true })).toBeVisible()
  expect(candidateOffsets).toContain(50)
  expect(findingOffsets.every((offset) => offset === 0)).toBe(true)
})

test('partial candidate decisions retain siblings and historical statuses', async ({ page }) => {
  await installIssueMocks(page)
  await page.goto('/sites/site-1/issues')

  const primary = page.locator('.candidate-card').filter({ hasText: 'Primary suggested description' })
  await expect(primary).toBeVisible()
  await expect(page.locator('.candidate-card').filter({ hasText: 'Sibling suggested description' })).toBeVisible()
  await expect(page.locator('.candidate-card').filter({ hasText: 'Superseded' })).toBeVisible()

  await primary.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(page.getByText('Candidate approved.', { exact: true })).toBeVisible()
  await expect(page.locator('.candidate-card').filter({ hasText: 'Sibling suggested description' })).toBeVisible()
  await expect(page.locator('.candidate-card').filter({ hasText: 'Superseded' })).toBeVisible()
  await expect(primary.getByText('Approved', { exact: true })).toBeVisible()
})

test('a nonempty candidate total is not presented as an empty queue on an empty result page', async ({ page }) => {
  await installIssueMocks(page, { emptyCandidatePage: true })
  await page.goto('/sites/site-1/issues')

  await page.getByRole('navigation', { name: 'Candidate results' }).getByRole('button', { name: 'Next results' }).click()
  await expect(page.getByRole('heading', { name: 'No candidates on this page', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No candidates waiting', exact: true })).toHaveCount(0)
  await expect(page.getByText('51 Total', { exact: true })).toBeVisible()
})
