import { expect, test } from '@playwright/test'

for (const succeeds of [true, false]) {
  test(`Google return automatically shows ${succeeds ? 'verified access' : 'property failure'} without another Test click`, async ({ page }) => {
    let finished = false
    let manualTests = 0
    let allowFinish = false
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
      const json = (data: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
      if (path === '/auth/status') return json({ initialized: true })
      if (path === '/auth/me') return json({ user: { id: 'u', email: 'owner@example.com', name: 'Owner' }, team: { id: 't', name: 'Team' }, role: 'owner', csrf_token: 'csrf' })
      if (path === '/sites') return json({ items: [{ id: 'site-1', name: 'Pilot', origin: 'https://pilot.example', timezone: 'America/Chicago', facts: {} }], total: 1 })
      if (path.endsWith('/google-oauth/config')) return json({ configured: true, mode: 'platform' })
      if (path.endsWith('/connections/gsc/test')) { manualTests++; return json({ id: 'unexpected' }) }
      if (path.endsWith('/jobs/oauth-check')) {
        finished = allowFinish
        return json({ id: 'oauth-check', kind: 'connection_test', payload: { kind: 'gsc' }, status: finished ? succeeds ? 'complete' : 'blocked' : 'running', result: finished ? { status: succeeds ? 'verified' : 'error' } : {} })
      }
      if (path.endsWith('/connections')) return json({ items: [{ kind: 'gsc', status: finished ? succeeds ? 'connected' : 'error' : 'needs_test', capabilities: { settings: { site_url: 'https://pilot.example/' }, last_connection_test: { status: finished ? succeeds ? 'verified' : 'error' : 'pending' } } }], total: 1 })
      return json({ items: [], total: 0 })
    })
    await page.goto('/sites/site-1/settings/connections?oauth=connected&kind=gsc&verification_job=oauth-check')
    await expect(page.getByText('Google Search Console: Google authorization saved. Verifying property access automatically…', { exact: true })).toBeVisible()
    allowFinish = true
    if (succeeds) {
      await expect(page.getByText('Google Search Console: access verified. Automatic data collection is ready; no further action is needed.', { exact: true })).toBeVisible()
      await expect(page.getByRole('status', { name: 'Google Search Console connection readiness: Automatic' })).toBeVisible()
    } else {
      await expect(page.getByRole('alert')).toContainText('Google authorization was saved, but property access could not be verified')
      await expect(page.getByRole('status', { name: 'Google Search Console connection readiness: Degraded' })).toBeVisible()
    }
    expect(manualTests).toBe(0)
  })
}
