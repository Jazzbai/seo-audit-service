import { expect, test } from '@playwright/test'

test('real backend exposes the public auth status contract', async ({ request }) => {
  const response = await request.get('/api/v1/auth/status')
  expect(response.ok()).toBeTruthy()
  const payload = await response.json()
  expect(typeof payload.initialized).toBe('boolean')
})
