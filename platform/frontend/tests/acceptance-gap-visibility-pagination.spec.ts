import { expect, test } from '@playwright/test'

test('visibility filters before paging and can reach older observations', async ({ page }) => {
  const requests: string[] = []
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace('/api/v1', '')
    const json = (value: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) })
    if (path === '/auth/status') return json({ initialized: true })
    if (path === '/auth/me') return json({ user: { id: 'u', name: 'Owner' }, team: { id: 't' }, role: 'owner', csrf_token: 'csrf' })
    if (path === '/sites') return json({ items: [{ id: 's', name: 'Pilot', origin: 'https://pilot.example', timezone: 'UTC', facts: {} }], total: 1 })
    if (path.endsWith('/policy')) return json({ settings: { tracked_questions: [] } })
    if (path.endsWith('/measurements')) {
      requests.push(url.search)
      const offset = Number(url.searchParams.get('offset'))
      const filtered = url.searchParams.get('source') === 'gsc'
      const ai = url.searchParams.get('source') === 'ai_sample'
      return json({ items: Array.from({ length: filtered || ai || offset ? 1 : 200 }, (_, i) => ({
        id: `${offset}-${i}`, kind: ai ? 'ai_sample' : 'gsc', source: ai ? 'ai_sample' : 'gsc', observed_at: new Date().toISOString(), data: { marker: ai ? 'ai-result' : filtered ? 'filtered-result' : offset ? 'older-result' : 'recent-result' },
      })), total: filtered || ai ? 1 : 201 })
    }
    return json({ items: [], total: 0 })
  })
  await page.goto('/sites/s/visibility')
  await expect(page.getByRole('button', { name: 'Next observations' })).toBeEnabled()
  expect(requests[0]).toContain('source=visibility')
  await page.getByRole('button', { name: 'Next observations' }).click()
  await expect(page.getByRole('table')).toContainText('older-result')
  expect(requests.at(-1)).toContain('offset=200')
  await page.getByLabel('Observation source', { exact: true }).selectOption('gsc')
  await expect(page.getByRole('table')).toContainText('filtered-result')
  expect(requests.at(-1)).toContain('source=gsc')
  expect(requests.at(-1)).toContain('offset=0')
  await expect(page.getByRole('button', { name: 'Next observations' })).toBeDisabled()
  await page.getByLabel('Observation source', { exact: true }).selectOption('ai_sample')
  await expect(page.getByRole('table')).toContainText('ai-result')
  await expect(page.getByRole('table')).toContainText('AI Response Sample')
  await expect(page.getByRole('table')).not.toContainText('Unknown / Imported')
})
