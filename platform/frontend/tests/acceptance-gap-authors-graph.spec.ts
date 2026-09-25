import { expect, test, type Page, type Route } from '@playwright/test'

type FixtureSite = { id: string; name: string; origin: string; timezone: string; language: string; paused: boolean; facts: Record<string, unknown> }

const sites: FixtureSite[] = [
  { id: 'site-a', name: 'Author Site A', origin: 'https://a.example', timezone: 'America/Chicago', language: 'en', paused: true, facts: {} },
  { id: 'site-b', name: 'Author Site B', origin: 'https://b.example', timezone: 'America/Chicago', language: 'en', paused: true, facts: {} },
  { id: 'site-graph', name: 'Graph Test Site', origin: 'https://graph.example', timezone: 'America/Chicago', language: 'en', paused: true, facts: {} },
]

const completePolicy = (authorId: string | null = null) => ({
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
  author_id: authorId,
})

const wordpress = {
  kind: 'wordpress',
  status: 'connected',
  checked_at: '2026-09-24T10:00:00Z',
  capabilities: { authenticated_author: { id: 'cached-author', name: 'Stale cached author' } },
}

const ownerScopeReview = {
  kind: 'owner_attested_exchange_rbac',
  reviewed_at: '2026-09-24T10:00:00Z',
  reviewer_id: 'owner-1',
  configuration_sha256: 'safe-hash-value',
  evidence: 'Admin verified the configured Exchange mailbox scope.',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page, options: {
  role?: 'owner' | 'editor' | 'viewer'
  authors?: Record<string, unknown>
  articleAuthorId?: string | null
  graphConnection?: Record<string, unknown> | null
  notificationOutcome?: 'accepted' | 'failed' | 'blocked' | 'outcome_unknown'
} = {}) {
  const role = options.role ?? 'owner'
  let graphConnection = options.graphConnection ?? null
  let graphSave: Record<string, unknown> | null = null
  let scopeReviewBody: Record<string, unknown> | null = null
  let authTestCount = 0
  const authorCalls: string[] = []
  const articleCreates: Record<string, unknown>[] = []
  const articleUpdates: Record<string, unknown>[] = []
  const notificationTests: Record<string, unknown>[] = []
  const receiptPosts: Array<{ jobId: string; body: Record<string, unknown> }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, {
      user: { id: 'owner-1', email: `${role}@example.test`, name: role },
      team: { id: 'team-1', name: 'Fixture team' },
      role,
      csrf_token: 'test-csrf',
    })
    if (path === '/sites' && method === 'GET') return json(route, { items: sites, total: sites.length })
    if (path === '/settings') return json(route, { global_pause: false })

    const siteMatch = path.match(/^\/sites\/([^/]+)$/)
    if (siteMatch && method === 'GET') {
      const site = sites.find((item) => item.id === siteMatch[1])
      return json(route, site ?? { detail: 'Site not found' }, site ? 200 : 404)
    }

    const authorsMatch = path.match(/^\/sites\/([^/]+)\/authors$/)
    if (authorsMatch && method === 'GET') {
      authorCalls.push(authorsMatch[1])
      const data = options.authors?.[authorsMatch[1]] ?? {
        items: [{ id: 'fresh-author', name: 'Fresh Author' }],
        complete: true,
        checked_at: '2026-09-25T14:05:00Z',
        authenticated_user_id: 'wp-user-1',
        blockers: [],
      }
      if (typeof data === 'function') return json(route, await (data as () => Promise<unknown>)())
      return json(route, data)
    }

    const policyMatch = path.match(/^\/sites\/([^/]+)\/policy$/)
    if (policyMatch && method === 'GET') {
      const authorId = policyMatch[1] === 'site-a' ? 'author-a' : policyMatch[1] === 'site-b' ? 'author-a' : null
      return json(route, { id: `policy-${policyMatch[1]}`, version: 1, settings: completePolicy(authorId) })
    }
    if (policyMatch && method === 'PUT') return json(route, { id: 'policy-updated', version: 2, settings: (request.postDataJSON() as { settings: unknown }).settings })

    if (path.match(/^\/sites\/[^/]+\/connections$/) && method === 'GET') {
      return json(route, { items: [wordpress, ...(graphConnection ? [graphConnection] : [])], total: 1 + Number(Boolean(graphConnection)) })
    }
    const graphSaveMatch = path.match(/^\/sites\/([^/]+)\/connections\/microsoft_graph$/)
    if (graphSaveMatch && method === 'PUT') {
      graphSave = request.postDataJSON() as Record<string, unknown>
      const saveSettings = graphSave.settings as Record<string, unknown>
      graphConnection = {
        kind: 'microsoft_graph',
        status: 'connected',
        settings: saveSettings,
        safe_fields: { tenant_id: 'tenant-existing', client_id: 'client-existing' },
        capabilities: { scope_review: null },
      }
      return json(route, graphConnection)
    }
    const scopeMatch = path.match(/^\/sites\/([^/]+)\/connections\/microsoft_graph\/scope-review$/)
    if (scopeMatch && method === 'POST') {
      scopeReviewBody = request.postDataJSON() as Record<string, unknown>
      graphConnection = {
        ...graphConnection,
        capabilities: { scope_review: ownerScopeReview },
      }
      return json(route, graphConnection)
    }
    if (path.match(/^\/sites\/[^/]+\/connections\/microsoft_graph\/test$/) && method === 'POST') {
      authTestCount += 1
      return json(route, { id: 'auth-job', kind: 'microsoft_graph_connection_test', status: 'complete' }, 202)
    }
    if (path === '/sites/site-graph/jobs/auth-job' && method === 'GET') return json(route, { id: 'auth-job', kind: 'microsoft_graph_connection_test', status: 'complete' })
    if (path === '/sites/site-graph/jobs/notification-job-1' && method === 'GET') {
      const outcome = options.notificationOutcome ?? 'accepted'
      return json(route, { id: 'notification-job-1', site_id: 'site-graph', kind: 'microsoft_graph_test_email', status: outcome === 'accepted' ? 'complete' : 'partial', result: { status: outcome } })
    }

    if (path.match(/^\/sites\/[^/]+\/budgets$/)) return json(route, { reservations: { items: [], total: 0 } })

    const articleRoute = path.match(/^\/sites\/([^/]+)\/articles(?:\/([^/]+))?(?:\/(revisions))?$/)
    if (articleRoute) {
      const [, siteId, articleId, revisions] = articleRoute
      if (revisions) return json(route, { items: [], total: 0 })
      if (method === 'POST') {
        const body = request.postDataJSON() as Record<string, unknown>
        articleCreates.push(body)
        return json(route, { id: 'article-new', site_id: siteId, title: body.title, body: '', brief: {}, sources: [], status: 'planned', author_id: body.author_id ?? null, managed: true })
      }
      if (articleId && method === 'PATCH') {
        const body = request.postDataJSON() as Record<string, unknown>
        articleUpdates.push(body)
        return json(route, { id: articleId, site_id: siteId, title: body.title ?? 'Existing review draft', body: body.body ?? '', brief: body.brief ?? {}, sources: body.sources ?? [], status: 'review_needed', author_id: body.author_id ?? options.articleAuthorId ?? null, managed: true })
      }
      if (articleId) return json(route, { id: articleId, site_id: siteId, title: 'Existing review draft', body: '<p>Draft.</p>', brief: {}, sources: [], status: 'review_needed', author_id: options.articleAuthorId ?? null, managed: true, updated_at: '2026-09-24T10:00:00Z' })
      return json(route, { items: [], total: 0 })
    }

    if (path === '/sites/site-graph/notifications/test' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      notificationTests.push(body)
      return json(route, { id: 'notification-job-1', site_id: 'site-graph', kind: 'microsoft_graph_test_email', status: 'queued', result: { status: 'accepted' } }, 202)
    }
    const receiptMatch = path.match(/^\/sites\/site-graph\/notifications\/([^/]+)\/receipt$/)
    if (receiptMatch && method === 'POST') {
      receiptPosts.push({ jobId: receiptMatch[1], body: request.postDataJSON() as Record<string, unknown> })
      return json(route, { id: 'receipt-job', site_id: 'site-graph', kind: 'microsoft_graph_receipt', status: 'complete', result: { recipient_confirmation: 'confirmed', receipt: { kind: 'owner_confirmed_recipient_receipt' } } }, 201)
    }

    if (path.endsWith('/overview')) {
      const siteId = path.split('/')[2]
      const site = sites.find((item) => item.id === siteId) ?? sites[0]
      return json(route, { site, counts: { pages: 0, open_findings: 0, pending_candidates: 0, published_articles: 0, open_incidents: 0 }, monitoring: { status: 'not_running' }, budget: { limit_cents: 30000, spent_cents: 0, reserved_cents: 0 }, recent_events: [], coverage: { status: 'unknown' }, connections: [] })
    }

    return json(route, { items: [], total: 0 })
  })

  return {
    authorCalls,
    articleCreates,
    articleUpdates,
    getGraphSave: () => graphSave,
    getScopeReviewBody: () => scopeReviewBody,
    notificationTests,
    receiptPosts,
    getAuthTestCount: () => authTestCount,
  }
}

for (const width of [1440, 390]) {
  test(`author selector uses fresh discovery, preserves a deleted selection as unverified, and refreshes at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    let authorChecks = 0
    const controls = await installMocks(page, {
      articleAuthorId: 'wp-author-7',
      authors: {
        'site-a': () => Promise.resolve(++authorChecks <= 2
          ? { items: [{ id: 'wp-author-7', name: 'Current WordPress Author' }], complete: true, checked_at: '2026-09-25T14:05:00Z', authenticated_user_id: 'wp-user-1', blockers: [] }
          : { items: [], complete: true, checked_at: '2026-09-25T14:10:00Z', authenticated_user_id: 'wp-user-1', blockers: [] }),
      },
    })

    await page.goto('/sites/site-a/content/articles/article-7')
    const authorSelect = page.getByLabel('Publishing author')
    await expect(authorSelect).toHaveValue('wp-author-7')
    await expect(authorSelect.locator('option[value="wp-author-7"]')).toHaveText('Current WordPress Author (wp-author-7)')
    await expect(page.getByText(/Last check:/)).toBeVisible()
    await expect(authorSelect.locator('option[value="cached-author"]')).toHaveCount(0)
    expect(controls.authorCalls).toContain('site-a')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)

    await page.getByRole('button', { name: 'Refresh authors' }).click()
    await expect(page.getByText('No authors returned', { exact: false })).toBeVisible()
    await expect(authorSelect).toHaveValue('wp-author-7')
    await expect(authorSelect.locator('option[value="wp-author-7"]')).toHaveText('Saved author wp-author-7 (unverified)')
    await expect(page.getByText('Selected author is unverified')).toBeVisible()
    await page.getByRole('button', { name: 'Save article' }).click()
    await expect(page.getByText(/not verified by the latest complete author check/)).toBeVisible()
    expect(controls.articleUpdates).toHaveLength(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}

test('author discovery errors and incomplete results are explicit; blank-author review drafts remain saveable', async ({ page }) => {
  let authorCalls = 0
  const controls = await installMocks(page, {
  })
  await page.route('**/api/v1/sites/site-a/authors', async (route) => {
    authorCalls += 1
    if (authorCalls <= 2) return json(route, { detail: 'WordPress author access was denied.' }, 403)
    return json(route, { items: [], complete: false, checked_at: '2026-09-25T14:15:00Z', authenticated_user_id: null, blockers: ['author_listing_denied'] })
  })

  await page.goto('/sites/site-a/content/new')
  await expect(page.getByRole('alert')).toContainText('Author discovery failed')
  await page.getByRole('button', { name: 'Refresh authors' }).click()
  await expect(page.getByText('Author discovery is incomplete')).toBeVisible()
  await expect(page.getByRole('list', { name: 'Author discovery blockers' })).toContainText('author_listing_denied')
  await expect(page.getByLabel('Publishing author')).toHaveValue('')

  await page.getByLabel('Title').fill('A review draft without a verified author')
  await page.getByRole('button', { name: 'Save article' }).click()
  await expect(page.getByText('Article created. You can now check, schedule, or publish it.')).toBeVisible()
  expect(controls.articleCreates).toHaveLength(1)
  expect(controls.articleCreates[0]).not.toHaveProperty('author_id')
})

test('a complete self-only listing is selectable and displays its warning without using cached authors', async ({ page }) => {
  await installMocks(page, {
    authors: {
      'site-a': {
        items: [{ id: 'wp-self', name: 'Authenticated WordPress User' }],
        complete: true,
        checked_at: '2026-09-25T14:20:00Z',
        authenticated_user_id: 'wp-self',
        blockers: [],
        warnings: ['connection_can_only_assign_self'],
      },
    },
  })
  await page.goto('/sites/site-a/settings/policies')
  const selector = page.getByLabel('Publishing author')
  await expect(selector.locator('option[value="wp-self"]')).toHaveText('Authenticated WordPress User (wp-self)')
  await expect(page.getByText('This WordPress connection can assign only its authenticated account as an author.')).toBeVisible()
  await expect(selector.locator('option[value="cached-author"]')).toHaveCount(0)
})

test('site changes load a new author response and keep the old policy selection visibly unverified', async ({ page }) => {
  const controls = await installMocks(page, {
    authors: {
      'site-a': { items: [{ id: 'author-a', name: 'Site A Author' }], complete: true, checked_at: '2026-09-25T10:00:00Z', authenticated_user_id: 'wp-a', blockers: [] },
      'site-b': { items: [{ id: 'author-b', name: 'Site B Author' }], complete: true, checked_at: '2026-09-25T11:00:00Z', authenticated_user_id: 'wp-b', blockers: [] },
    },
  })
  await page.goto('/sites/site-a/settings/policies')
  await expect(page.getByLabel('Publishing author').locator('option[value="author-a"]')).toHaveText('Site A Author (author-a)')
  await page.getByLabel('Choose a site').selectOption('site-b')
  await expect(page).toHaveURL(/\/sites\/site-b\/overview$/)
  await page.getByRole('link', { name: 'Policies & budget' }).click()
  await expect(page).toHaveURL(/\/sites\/site-b\/settings\/policies$/)
  const selector = page.getByLabel('Publishing author')
  await expect(selector.locator('option[value="author-b"]')).toHaveText('Site B Author (author-b)')
  await expect(selector.locator('option[value="author-a"]')).toHaveText('Saved author author-a (unverified)')
  await expect(page.getByText('Selected author is unverified')).toBeVisible()
  expect(controls.authorCalls).toContain('site-a')
  expect(controls.authorCalls).toContain('site-b')
})

test('Microsoft Graph save keeps its secret write-only and invalidates the mailbox attestation', async ({ page }) => {
  await installMocks(page, {
    graphConnection: {
      kind: 'microsoft_graph',
      status: 'connected',
      safe_fields: { tenant_id: 'tenant-existing', client_id: 'client-existing' },
      settings: { sender: 'reports@example.test', recipients: ['owner@example.test'], digest_enabled: false },
      capabilities: { scope_review: ownerScopeReview },
    },
  })
  await page.goto('/sites/site-graph/settings/connections')
  const graph = page.getByRole('region', { name: 'Microsoft 365 Graph connection' })
  await expect(graph.getByText('Microsoft 365 Graph', { exact: true })).toBeVisible()
  await expect(graph.getByLabel('Client secret')).toHaveValue('')
  await expect(graph).toContainText('OWNER ATTESTATION')
  await graph.getByLabel('Client secret').fill('write-only-graph-secret')
  await graph.getByRole('button', { name: 'Save Graph settings' }).click()
  await expect(graph.getByText('Saving Microsoft Graph settings invalidated the previous mailbox scope review.')).toBeVisible()
  await expect(graph.getByLabel('Client secret')).toHaveValue('')
  await expect(graph.getByText('write-only-graph-secret', { exact: true })).toHaveCount(0)
  await expect(graph.getByRole('button', { name: 'Send ONE test email now' })).toBeDisabled()
})

test('Microsoft Graph needs explicit owner scope review, sends one test only on request, and records human receipt', async ({ page }) => {
  const controls = await installMocks(page, {
    graphConnection: {
      kind: 'microsoft_graph',
      status: 'connected',
      safe_fields: { tenant_id: 'tenant-existing', client_id: 'client-existing' },
      settings: { sender: 'reports@example.test', recipients: ['owner@example.test'], digest_enabled: false },
      capabilities: { scope_review: null },
    },
  })
  await page.goto('/sites/site-graph/settings/connections')
  const graph = page.getByRole('region', { name: 'Microsoft 365 Graph connection' })
  const send = graph.getByRole('button', { name: 'Send ONE test email now' })
  await expect(send).toBeDisabled()
  expect(controls.notificationTests).toHaveLength(0)

  await graph.getByRole('button', { name: 'Test authentication (no email)' }).click()
  await expect(graph).toContainText('This checks Graph access only; no email was sent or delivery confirmed.')
  expect(controls.getAuthTestCount()).toBe(1)
  expect(controls.notificationTests).toHaveLength(0)

  await graph.getByLabel('Confirm mailbox scoped').check()
  await graph.getByLabel('Confirm no unscoped send').check()
  await graph.getByLabel('Admin evidence notes').fill('Reviewed Exchange Application Access Policy for the configured reports mailbox.')
  await graph.getByRole('button', { name: 'Record owner mailbox scope attestation' }).click()
  await expect(graph).toContainText('This is not machine verified.')
  expect(controls.getScopeReviewBody()).toEqual({
    confirms_mailbox_scoped: true,
    confirms_no_unscoped_send: true,
    evidence: 'Reviewed Exchange Application Access Policy for the configured reports mailbox.',
  })
  await expect(send).toBeEnabled()
  expect(controls.notificationTests).toHaveLength(0)

  await send.click()
  await expect(graph.getByRole('region', { name: 'Microsoft Graph test email job' })).toContainText('Delivery is still unconfirmed;')
  expect(controls.notificationTests).toHaveLength(1)
  expect(controls.notificationTests[0]).toMatchObject({ kind: 'microsoft_graph', confirm_send: true })
  expect(controls.notificationTests[0].idempotency_key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)

  const receiptButton = graph.getByRole('button', { name: 'Record owner receipt confirmation' })
  await expect(receiptButton).toBeDisabled()
  await graph.getByLabel('Confirm test email received').check()
  await graph.getByLabel('Receipt notes').fill('Received the test message in the configured mailbox.')
  await receiptButton.click()
  await expect(graph).toContainText('This is an explicit recipient confirmation; ForgeSEO did not read the mailbox.')
  expect(controls.receiptPosts).toEqual([{
    jobId: 'notification-job-1',
    body: { confirms_received: true, notes: 'Received the test message in the configured mailbox.' },
  }])
})

test('Microsoft Graph rejects more than ten recipients in its settings form', async ({ page }) => {
  const controls = await installMocks(page, {
    graphConnection: {
      kind: 'microsoft_graph',
      status: 'connected',
      safe_fields: { tenant_id: 'tenant-existing', client_id: 'client-existing' },
      settings: { sender: 'reports@example.test', recipients: ['owner@example.test'], digest_enabled: false },
      capabilities: { scope_review: null },
    },
  })
  await page.goto('/sites/site-graph/settings/connections')
  const graph = page.getByRole('region', { name: 'Microsoft 365 Graph connection' })
  await graph.getByLabel('Recipients').fill(Array.from({ length: 11 }, (_, index) => `person${index}@example.test`).join(', '))
  await expect(graph.getByText('Remove recipients until no more than 10 addresses remain.')).toBeVisible()
  await graph.getByRole('button', { name: 'Save Graph settings' }).click()
  await expect(graph.getByText('Microsoft Graph supports up to 10 configured recipients. Remove the extra addresses before saving.')).toBeVisible()
  expect(controls.getGraphSave()).toBeNull()
  expect(controls.notificationTests).toHaveLength(0)
})
