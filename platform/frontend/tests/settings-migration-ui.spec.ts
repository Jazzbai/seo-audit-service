import { expect, test, type Page, type Route } from '@playwright/test'

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

const policy = {
  enabled: false,
  allowed_actions: ['metadata'],
  protected_paths: ['/', '/contact*'],
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

async function installMocks(page: Page, measurementResponse: unknown, role: 'owner' | 'editor' | 'viewer' = 'owner') {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, { user: { id: 'user-1', email: 'viewer@example.com', name: 'Viewer' }, team: { id: 'team-1', name: 'Pilot team' }, role, csrf_token: 'csrf-test-token' })
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/policy' && request.method() === 'GET') return json(route, { id: 'policy-1', version: 4, settings: policy })
    if (path === '/settings' && request.method() === 'GET') return json(route, { global_pause: true })
    if (path === '/sites/site-1' && request.method() === 'GET') return json(route, site)
    if (path === '/sites/site-1/connections' && request.method() === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/pages' && request.method() === 'GET') return json(route, { items: [], total: 0 })
    if (path === '/sites/site-1/budgets' && request.method() === 'GET') return json(route, { reservations: { items: [], total: 0 } })
    if (path === '/sites/site-1/measurements' && request.method() === 'GET') return json(route, measurementResponse)
    return json(route, { items: [], total: 0 })
  })
}

function migrationPanel(page: Page) {
  return page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Migration history', exact: true }) })
}

test('settings distinguishes no imported history from an empty optimization result', async ({ page }) => {
  await installMocks(page, {
    items: [{ id: 'measurement-1', kind: 'gsc', source: 'search-console', observed_at: '2026-09-20T12:00:00Z', data: {} }],
    total: 1,
  })
  await page.goto('/sites/site-1/settings/policies')

  const panel = migrationPanel(page)
  await expect(panel.getByRole('heading', { name: 'No imported history', exact: true })).toBeVisible()
  await expect(panel).toContainText('fresh inventory is still required')
  await expect(panel).toContainText('not proof that the site is optimized')
  await expect(panel.getByRole('button', { name: /import|write|rollback/i })).toHaveCount(0)
})

test('settings shows imported historical evidence and its counts without granting authority', async ({ page }) => {
  await installMocks(page, {
    items: [{
      id: 'migration-1',
      kind: 'legacy_import',
      source: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      observed_at: '2026-09-20T12:00:00Z',
      data: {
        archive: 'site-1/legacy/checkpoint.json',
        sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        imported_pages: 52,
        history_counts: { observations: 1692, evidence_artifacts: 54, findings: 1123, proposals: 187, executions: 24 },
        approval_authority_imported: false,
        requires_fresh_inventory: true,
      },
    }],
    total: 1,
  }, 'viewer')
  await page.goto('/sites/site-1/settings/policies')

  const panel = migrationPanel(page)
  await expect(panel.getByText('Imported historical evidence', { exact: true }).first()).toBeVisible()
  await expect(panel.getByText('52', { exact: true })).toBeVisible()
  await expect(panel.getByText('3,080', { exact: true })).toBeVisible()
  await expect(panel.getByText('site-1/legacy/checkpoint.json', { exact: true })).toBeVisible()
  await expect(panel.getByText('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', { exact: true }).first()).toBeVisible()
  await expect(panel).toContainText('Imported approvals do not grant current permissions')
  await expect(panel).toContainText('Fresh inventory is required')
  await expect(panel.getByRole('button', { name: /import|write|rollback/i })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Save policy controls', exact: true })).toBeDisabled()
})

test('settings labels a rolled-back import and stale evidence while preserving its record', async ({ page }) => {
  await installMocks(page, {
    items: [{
      id: 'migration-rolled-back',
      kind: 'legacy_import',
      observed_at: '2026-01-01T12:00:00Z',
      data: {
        archive: 'site-1/legacy/rolled-back.json',
        checksum: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        imported_pages: 52,
        history_counts: { observations: 1692 },
      },
    }, {
      id: 'migration-rollback-record',
      kind: 'legacy_rollback',
      source: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      observed_at: '2026-01-02T12:00:00Z',
      data: {
        sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        rolled_back_pages: 52,
        archive_retained: true,
      },
    }],
    total: 2,
  })
  await page.goto('/sites/site-1/settings/policies')

  const panel = migrationPanel(page)
  await expect(panel.getByText('Import rolled back', { exact: true }).first()).toBeVisible()
  await expect(panel.getByText('Stale', { exact: true })).toBeVisible()
  await expect(panel).toContainText('remains visible for auditability')
  await expect(panel).toContainText('fresh inventory is required before relying on current coverage')
  await expect(panel.getByRole('button', { name: /import|write|rollback/i })).toHaveCount(0)
})
