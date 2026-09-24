import { expect, test } from '@playwright/test'
import { verifyPlatformRendering } from './wordpress-render-check'

const source = 'https://consumer.ftc.gov/articles/0211-auto-repair-basics'
// Adapted from the retained 2026-09-24 source-backed review trial. No model call,
// invented business claim or new fact-verification claim is made by this test.
const body = `<h2>Preparing questions for a written repair estimate</h2>
<p>This article is an isolated publishing rehearsal, not a live business article. Before contacting a repair shop, prepare a short description of the problem and a list of questions. You do not need to diagnose the repair yourself.</p>
<h2>Ask what the estimate includes</h2>
<p>The <a href="${source}">FTC's Auto Repair Basics guide</a> recommends requesting a written estimate before agreeing to repairs. Its guidance identifies the condition to be repaired, necessary parts and anticipated labor charge as useful information to include. It also recommends clarifying how approval for additional work will be handled.</p>
<p>Keep your questions and a copy of the estimate together. Ask what still needs inspection and how any proposed changes will be explained. These are general consumer-preparation points, not a statement about insurance coverage or local legal requirements.</p>`

test('real WordPress UI: check, schedule, publish once, render, retry and restore to draft', async ({ page, request }, testInfo) => {
  const fixture = (await (await request.get('http://127.0.0.1:18082/__fixture')).json()).wordpress
  const metadata = await (await request.get('http://127.0.0.1:18082/__rehearsal')).json()
  expect(metadata.wordpress_only).toBe(true)
  expect(fixture.origin).toBe('https://wordpress.fixture.test')
  const crashes: string[] = []
  page.on('pageerror', error => crashes.push(error.name))
  await page.goto('/')
  await page.getByLabel('Your name').fill('Isolated rehearsal owner')
  await page.getByLabel('Work email').fill('rehearsal@example.test')
  await page.getByLabel('Workspace name').fill('Publishing rehearsal — no live sites')
  await page.getByLabel('Password', { exact: false }).fill('Disposable-browser-password!57')
  await page.getByLabel('Deployment setup token').fill('test-only-bootstrap-token')
  await page.getByRole('button', { name: 'Create workspace' }).click()
  await expect(page).toHaveURL(/sites\/new$/)
  await page.getByLabel('Site name').fill('Isolated WordPress publishing rehearsal')
  await page.getByLabel('Site origin').fill(fixture.origin)
  await page.getByLabel('Business name').fill('Isolated WordPress fixture')
  await page.getByLabel('Primary audience').fill('People preparing questions for a repair visit')
  await page.getByRole('textbox', { name: 'Services', exact: true }).fill('Repairs')
  await page.getByRole('textbox', { name: 'Services', exact: true }).press('Enter')
  await page.getByRole('textbox', { name: 'Confirmed sources', exact: true }).fill(source)
  await page.getByRole('textbox', { name: 'Confirmed sources', exact: true }).press('Enter')
  await page.getByLabel('WordPress username').fill(fixture.username)
  await page.getByLabel('Application password', { exact: false }).fill(fixture.application_password)
  await page.getByRole('button', { name: 'Create site' }).click()
  await expect(page).toHaveURL(/sites\/[a-f0-9]+\/overview$/)
  const siteId = page.url().split('/sites/')[1].split('/')[0]
  const api = `/api/v1/sites/${siteId}`
  const get = async (path: string) => {
    const response = await page.request.get(api + path)
    expect(response.ok()).toBe(true)
    return response.json()
  }
  await page.goto(`/sites/${siteId}/settings/connections`)
  const wordpress = page.locator('form.connection-card').filter({ hasText: 'WordPress' }).first()
  await wordpress.getByRole('button', { name: 'Test', exact: true }).click()
  await expect.poll(async () => (await get('/connections')).items.find((c: any) => c.kind === 'wordpress')?.status, { timeout: 45000 }).toBe('connected')

  await page.goto(`/sites/${siteId}/content/new`)
  await page.getByLabel('Title', { exact: false }).fill(`Isolated rehearsal: preparing questions for a repair estimate (${Date.now()})`)
  await page.getByLabel('Sources', { exact: false }).fill(source)
  await page.getByLabel('Body', { exact: false }).fill(body)
  await page.getByRole('button', { name: 'Save article' }).click()
  await expect(page).toHaveURL(/content\/articles\/[a-f0-9]+$/)
  const articleId = page.url().split('/articles/')[1]
  const editor = `/sites/${siteId}/content/articles/${articleId}`
  await page.getByRole('button', { name: 'Check', exact: true }).click()
  await expect.poll(async () => (await get(`/articles/${articleId}`)).checks.blockers).toContain('missing_author')
  await page.getByLabel('Publishing author', { exact: true }).selectOption(String(fixture.author_id))
  await page.getByRole('button', { name: 'Save article' }).click()
  await expect(page.getByText('Article saved. The API now has the latest editor state.')).toBeVisible()
  await page.getByRole('button', { name: 'Check', exact: true }).click()
  await expect(page.getByText('The editorial check passed.')).toBeVisible()

  await page.goto(`/sites/${siteId}/settings/policies`)
  await page.getByLabel('Publishing author', { exact: true }).selectOption(String(fixture.author_id))
  await page.getByRole('button', { name: 'Toggle site pause', exact: true }).click()
  await page.getByRole('button', { name: 'Toggle global pause', exact: true }).click()
  await page.getByRole('button', { name: 'Toggle automated workflows', exact: true }).click()
  await page.getByLabel('Publish', { exact: true }).check()
  await page.getByLabel('Metadata', { exact: true }).uncheck()
  // Only the explicit schedule should trigger publishing; remove default days.
  while (await page.locator('.day-picker label:has(input:checked)').count()) {
    await page.locator('.day-picker label:has(input:checked)').first().click()
  }
  await page.getByRole('button', { name: 'Save policy controls' }).click()
  await expect(page.getByText('Policy and pause controls saved as new server state.')).toBeVisible()
  await page.goto(editor)
  const future = new Date(Date.now() + 65000)
  const local = new Date(future.getTime() - future.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
  await page.getByLabel('Schedule time', { exact: false }).fill(local)
  await page.getByRole('button', { name: 'Schedule', exact: true }).click()
  await expect(page.getByText('Schedule request accepted by the API.')).toBeVisible()
  const scheduled = await get(`/articles/${articleId}`)
  expect(scheduled.status).toBe('scheduled')
  await page.screenshot({ path: testInfo.outputPath('scheduled.png'), fullPage: true })
  await expect.poll(async () => (await get(`/articles/${articleId}`)).status, { timeout: 140000, intervals: [2000] }).toBe('published')
  await page.reload()
  await expect(page.getByRole('button', { name: 'Roll back publication' })).toBeVisible()
  const publications = (await get('/publications')).items.filter((p: any) => p.article_id === articleId)
  expect(publications).toHaveLength(1)
  const publication = publications[0]
  expect(publication.result.public_status).toBe(200)
  expect(publication.result.evidence.sha256).toBeTruthy()
  const renderJob = await verifyPlatformRendering(page, siteId, articleId, publication.result.url)
  const renderJobId = renderJob.id
  const publicUrl = new URL(publication.result.url)
  expect(publicUrl.hostname).toBe('wordpress.fixture.test')

  // This is a real browser rendering of the real fixture's HTTP response, not
  // a mocked page or screenshot supplied as verification.
  const rendered = await page.context().newPage()
  await rendered.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.hostname !== 'wordpress.fixture.test') return route.abort()
    const response = await route.fetch({ url: `http://127.0.0.1:18090${url.pathname}${url.search}`, headers: { ...route.request().headers(), Host: url.host, 'X-Forwarded-Proto': 'https' } })
    await route.fulfill({ response })
  })
  await rendered.goto(publicUrl.href, { waitUntil: 'domcontentloaded' })
  await expect(rendered.getByRole('heading', { name: 'Ask what the estimate includes', exact: true })).toBeVisible()
  await expect(rendered.getByRole('link', { name: /FTC.s Auto Repair Basics guide/ })).toHaveAttribute('href', source)
  await rendered.screenshot({ path: testInfo.outputPath('wordpress-rendered.png'), fullPage: true })
  await rendered.close()

  const firstJob = (await get('/jobs')).items.find((j: any) => j.kind === 'publish' && j.payload?.article_id === articleId)
  expect(firstJob?.status).toBe('complete')
  const retryResponse = page.waitForResponse(r => r.url().endsWith(`/articles/${articleId}/publish`) && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Publish', exact: true }).click()
  expect((await (await retryResponse).json()).id).toBe(firstJob.id)
  await expect(page.getByText('Publication completed and verification is recorded.')).toBeVisible()
  const authorization = 'Basic ' + Buffer.from(`${fixture.username}:${fixture.application_password}`).toString('base64')
  const remoteHeaders = { Host: 'wordpress.fixture.test', 'X-Forwarded-Proto': 'https', Authorization: authorization }
  const article = await get(`/articles/${articleId}`)
  const matches = await request.get(`http://127.0.0.1:18090/wp-json/wp/v2/posts?context=edit&status=any&slug=${encodeURIComponent(article.slug)}`, { headers: remoteHeaders })
  expect(matches.ok()).toBe(true)
  const posts = await matches.json()
  expect(posts).toHaveLength(1)
  expect(String(posts[0].id)).toBe(publication.remote_id)

  page.once('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: 'Roll back publication' }).click()
  await expect(page.getByText('Rollback completed and the remote record was verified.')).toBeVisible({ timeout: 45000 })
  const restored = await request.get(`http://127.0.0.1:18090/wp-json/wp/v2/posts/${publication.remote_id}?context=edit`, { headers: remoteHeaders })
  expect((await restored.json()).status).toBe('draft')
  expect((await get(`/articles/${articleId}`)).status).toBe('rolled_back')
  const history = (await get('/publications')).items.filter((p: any) => p.article_id === articleId)
  expect(history).toHaveLength(1)
  expect(history[0].id).toBe(publication.id)
  expect(history[0].status).toBe('rolled_back')
  await page.screenshot({ path: testInfo.outputPath('rolled-back.png'), fullPage: true })
  const finalMetadata = await (await request.get('http://127.0.0.1:18082/__rehearsal')).json()
  expect(finalMetadata.scheduler_errors).toEqual([])
  expect(crashes).toEqual([])
  await testInfo.attach('rehearsal-evidence', { body: JSON.stringify({ ...finalMetadata, site_id: siteId, article_id: articleId, publication_id: publication.id, remote_id: publication.remote_id, publish_job_id: firstJob.id, render_job_id: renderJobId, browser_screenshot_sha256: renderJob.result.screenshot_sha256, remote_count_after_retry: posts.length, remote_final_status: 'draft', published_evidence_sha256: publication.result.evidence.sha256 }, null, 2), contentType: 'application/json' })
})
