import { expect, test, type Page, type Route } from '@playwright/test'

const auth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Owner' },
  team: { id: 'team-1', name: 'Pilot team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-1', team_id: 'team-1', name: 'Pilot Workshop', origin: 'https://pilot.example',
  timezone: 'America/Chicago', language: 'en', paused: true, facts: {},
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installMocks(page: Page) {
  let smtp = {
    kind: 'smtp',
    status: 'connected',
    capabilities: { settings: { digest_enabled: false, host: 'smtp.example.com', sender: 'reports@example.com', recipients: ['owner@example.com'] } },
  }
  let saved: Record<string, unknown> | null = null
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, auth)
    if (path === '/sites' && request.method() === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/connections' && request.method() === 'GET') return json(route, { items: [smtp], total: 1 })
    if (path === '/sites/site-1/connections/smtp' && request.method() === 'PUT') {
      const body = request.postDataJSON() as { settings?: Record<string, unknown> }
      saved = body.settings ?? null
      smtp = { ...smtp, capabilities: { settings: { ...smtp.capabilities.settings, ...(body.settings ?? {}) } } }
      return json(route, smtp)
    }
    return json(route, { items: [], total: 0 })
  })
  return { getSaved: () => saved }
}

test('SMTP settings expose a visible weekly digest toggle and persist it', async ({ page }) => {
  const controls = await installMocks(page)
  await page.goto('/sites/site-1/settings/connections')

  const smtp = page.locator('form.connection-card').filter({ hasText: 'SMTP' }).first()
  const digest = smtp.getByLabel('Send weekly email digests')
  await expect(digest).toBeVisible()
  await expect(digest).not.toBeChecked()
  await digest.check()
  await smtp.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('SMTP settings saved.')).toBeVisible()
  expect(controls.getSaved()).toEqual(expect.objectContaining({ digest_enabled: true }))
})
