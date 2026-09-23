import { expect, test, type Route } from '@playwright/test'

const auth = {
  user: { id: 'user-2', email: 'viewer@example.com', name: 'Viewer' },
  team: { id: 'team-1', name: 'Pilot team' },
  role: 'viewer',
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

const article = {
  id: 'article-1',
  site_id: 'site-1',
  title: 'A review-only article',
  slug: 'a-review-only-article',
  body: '<p>Grounded draft content.</p>',
  status: 'review_needed',
  brief: {},
  checks: { passed: false, blockers: ['missing_author'], warnings: [] },
  sources: [],
  author_id: null,
  scheduled_at: null,
  managed: true,
  updated_at: '2026-09-18T12:00:00Z',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

test('viewer sees article publishing controls as read-only instead of submitting a forbidden request', async ({ page }) => {
  let publishRequests = 0

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')

    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/articles/article-1' && request.method() === 'GET') return json(route, article)
    if (path === '/sites/site-1/articles/article-1/revisions') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/connections') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/pages') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/articles/article-1' && request.method() === 'POST') {
      publishRequests += 1
      return json(route, { detail: 'Viewer cannot publish' }, 403)
    }
    return json(route, { items: [], total: 0 })
  })

  await page.goto('/sites/site-1/content/articles/article-1')

  await expect(page.getByRole('heading', { name: 'A review-only article', exact: true })).toBeVisible()
  await expect(page.getByText('Viewer access can review article state, but editor access is required to save, check, schedule, publish, or roll back content.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save article', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Check', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Schedule', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Publish', exact: true })).toBeDisabled()
  expect(publishRequests).toBe(0)
})
