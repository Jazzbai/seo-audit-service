import { expect, test } from '@playwright/test'

test('Google connection lists properties and saves only the property explicitly selected for this site', async ({ page }) => {
  let saved: unknown
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const json = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/auth/status') return json({ initialized: true, account_creation_enabled: false })
    if (path === '/auth/me') return json({ user: { id: 'u', name: 'Owner' }, team: { id: 't' }, role: 'owner', csrf_token: 'csrf' })
    if (path === '/sites') return json({ items: [{ id: 'site-1', name: 'H-Town', origin: 'https://htown.example', facts: {} }], total: 1 })
    if (path.endsWith('/google-oauth/config')) return json({ configured: true, mode: 'platform' })
    if (path.endsWith('/google-properties/ga4')) return json({ items: [{ value: '419632796', label: 'Another site' }, { value: '468885883', label: 'H-Town' }], complete: true })
    if (path.endsWith('/connections/ga4') && request.method() === 'PUT') { saved = request.postDataJSON(); return json({ kind: 'ga4', status: 'needs_test' }) }
    if (path.endsWith('/connections')) return json({ items: [{ id: 'ga4', kind: 'ga4', status: 'needs_test', capabilities: { settings: {} } }], total: 1 })
    return json({ items: [], total: 0 })
  })
  await page.goto('/sites/site-1/settings/connections')
  const card = page.locator('.connection-card').filter({ has: page.getByText('Google Analytics 4', { exact: true }) })
  await expect(card.getByLabel('Property ID', { exact: true })).toHaveValue('')
  await card.getByLabel('Available Analytics properties', { exact: true }).selectOption('468885883')
  await expect(card.getByLabel('Property ID', { exact: true })).toHaveValue('468885883')
  await card.getByRole('button', { name: 'Save', exact: true }).click()
  await expect.poll(() => saved).toEqual({ settings: { property_id: '468885883' } })
})
