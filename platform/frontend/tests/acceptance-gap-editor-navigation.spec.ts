import { expect, test } from '@playwright/test'

test('new-article navigation hydrates the saved record before allowing another edit', async ({ page }) => {
  const site = { id: 'site-editor', name: 'Editor fixture', origin: 'https://editor.example', paused: true, facts: {} }
  let article: Record<string, unknown> = { id: 'article-created', site_id: site.id, title: '', body: '', brief: {}, sources: [], status: 'planned', author_id: null }
  let saves = 0
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    const method = route.request().method()
    let body: unknown = { items: [], total: 0 }
    if (path === '/auth/status') body = { initialized: true }
    if (path === '/auth/me') body = { user: { id: 'u', name: 'Editor', email: 'editor@example.test' }, team: { id: 't', name: 'Fixture' }, role: 'editor', csrf_token: 'fixture-csrf' }
    if (path === '/sites') body = { items: [site], total: 1 }
    if (path === '/sites/site-editor') body = site
    if (path === '/sites/site-editor/articles' && method === 'POST') {
      article = { ...article, ...route.request().postDataJSON() }
      body = article
    }
    if (path === '/sites/site-editor/articles/article-created') {
      if (method === 'PATCH') {
        saves++
        article = { ...article, ...route.request().postDataJSON() }
      } else {
        // useResource still holds the /new response while this read is in flight.
        await new Promise(resolve => setTimeout(resolve, 150))
      }
      body = article
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.goto('/sites/site-editor/content/new')
  await page.getByLabel('Title', { exact: false }).fill('A complete repair preparation title')
  await page.getByLabel('Sources', { exact: false }).fill('https://consumer.ftc.gov/articles/0211-auto-repair-basics')
  await page.getByLabel('Body', { exact: false }).fill('<p>Keep your questions and estimate together.</p>')
  const savedRecordLoaded = page.waitForResponse(response => response.url().endsWith('/articles/article-created') && response.request().method() === 'GET')
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page).toHaveURL(/articles\/article-created$/)
  await savedRecordLoaded
  await expect(page.getByLabel('Title', { exact: false })).toHaveValue('A complete repair preparation title')
  await expect(page.getByLabel('Body', { exact: false })).toHaveValue('<p>Keep your questions and estimate together.</p>')
  await expect(page.getByLabel('Sources', { exact: false })).toHaveValue('https://consumer.ftc.gov/articles/0211-auto-repair-basics')
  await page.getByLabel('Title', { exact: false }).fill('An edited complete title')
  await page.getByRole('button', { name: 'Save article', exact: true }).click()
  await expect(page.getByText('Article saved. The API now has the latest editor state.')).toBeVisible()
  expect(saves).toBe(2)
  expect(article.body).toBe('<p>Keep your questions and estimate together.</p>')
})
