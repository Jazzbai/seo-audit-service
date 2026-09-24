import { expect, type Page } from '@playwright/test'

// Uses the supported audit endpoint, which itself queues the Chromium sample.
// A direct browser-job API request is intentionally not a public capability.
export async function verifyPlatformRendering(page: Page, siteId: string, articleId: string, url: string) {
  const api = `/api/v1/sites/${siteId}`
  const get = async (path: string) => (await page.request.get(api + path)).json()
  const publishedPage = (await get('/pages')).items.find((p: any) => p.url === url)
  expect(publishedPage?.id).toBeTruthy()
  const session = await (await page.request.get('/api/v1/auth/me')).json()
  const response = await page.request.post(api + '/jobs', {
    headers: { 'X-CSRF-Token': session.csrf_token, Origin: 'http://127.0.0.1:4173' },
    data: { kind: 'audit', payload: { seed_urls: [url], max_pages: 1, suppress_automation: true }, idempotency_key: `rehearsal-render:${articleId}` },
  })
  expect(response.status()).toBe(202)
  const auditId = (await response.json()).id
  await expect.poll(async () => (await get(`/jobs/${auditId}`)).status, { timeout: 60000, intervals: [2000] }).toMatch(/^(complete|partial)$/)
  const auditJob = await get(`/jobs/${auditId}`)
  expect(auditJob.result.browser_job_ids).toHaveLength(1)
  const renderId = auditJob.result.browser_job_ids[0]
  await expect.poll(async () => (await get(`/jobs/${renderId}`)).status, { timeout: 60000, intervals: [2000] }).toBe('complete')
  const renderJob = await get(`/jobs/${renderId}`)
  expect(renderJob.payload.page_id).toBe(publishedPage.id)
  expect(renderJob.result.status_code).toBe(200)
  expect(renderJob.result.screenshot_sha256).toMatch(/^[a-f0-9]{64}$/)
  expect(renderJob.result.resource_failures).toBe(0)
  const measurement = (await get('/measurements')).items.find((m: any) => m.kind === 'browser' && m.data?.page_id === publishedPage.id)
  expect(measurement?.source).toBe('chromium_lab')
  expect(measurement.data.field_data).toBe(false)
  return renderJob
}
