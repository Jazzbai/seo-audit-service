import {expect, test} from '@playwright/test'

test('isolated pilot: UI onboarding, policy, audit, plan, check, publish, verify and rollback', async ({page, request}) => {
  const fixtures = await (await request.get('http://127.0.0.1:18082/__fixture')).json()
  await page.goto('/')
  await page.getByLabel('Your name').fill('Local Pilot Owner')
  await page.getByLabel('Work email').fill('local-pilot@example.test')
  await page.getByLabel('Workspace name').fill('Isolated release acceptance')
  await page.getByLabel('Password', {exact: false}).fill('Disposable-browser-password!57')
  await page.getByLabel('Deployment setup token').fill('test-only-bootstrap-token')
  await page.getByRole('button', {name: 'Create workspace'}).click()
  await expect(page).toHaveURL(/sites\/new$/)

  for (const kind of ['wordpress', 'woocommerce']) {
    const fixture = fixtures[kind]
    const name = kind === 'wordpress' ? 'Isolated Workshop' : 'Isolated Store'
    await page.goto('/sites/new')
    await page.getByLabel('Site name').fill(name)
    await page.getByLabel('Site origin').fill(fixture.origin)
    await page.getByLabel('Business name').fill(name)
    await page.getByLabel('Primary audience').fill('Local customers')
    await page.getByRole('textbox', {name: 'Services', exact: true}).fill('Repairs')
    await page.getByRole('textbox', {name: 'Services', exact: true}).press('Enter')
    await page.getByRole('textbox', {name: 'Confirmed sources', exact: true}).fill(fixture.origin)
    await page.getByRole('textbox', {name: 'Confirmed sources', exact: true}).press('Enter')
    await page.getByLabel('WordPress username').fill(fixture.username)
    await page.getByLabel('Application password', {exact: false}).fill(fixture.application_password)
    await page.getByRole('button', {name: 'Create site'}).click()
    await expect(page).toHaveURL(/sites\/[a-f0-9]+\/overview$/)
    const id = page.url().split('/sites/')[1].split('/')[0]
    await page.goto(`/sites/${id}/settings/connections`)
    const wordpress = page.locator('form.connection-card').filter({hasText: 'WordPress'}).first()
    await wordpress.getByRole('button', {name: 'Test', exact: true}).click()
    await expect.poll(async () => (await (await page.request.get(`/api/v1/sites/${id}/connections`)).json()).items.find((c: any) => c.kind === 'wordpress')?.status, {timeout: 45000}).toBe('connected')
    if (kind === 'woocommerce') {
      const woo = page.locator('form.connection-card').filter({hasText: 'WooCommerce'}).first()
      await woo.getByLabel('Consumer key', {exact: true}).fill(fixture.consumer_key)
      await woo.getByLabel('Consumer secret', {exact: true}).fill(fixture.consumer_secret)
      await woo.getByRole('button', {name: 'Save', exact: true}).click()
      await expect(page.getByText('WooCommerce settings saved.')).toBeVisible()
      await woo.getByRole('button', {name: 'Test', exact: true}).click()
      await expect.poll(async () => (await (await page.request.get(`/api/v1/sites/${id}/connections`)).json()).items.find((c: any) => c.kind === 'woocommerce')?.status, {timeout: 45000}).toBe('connected')
    }
    await page.goto(`/sites/${id}/overview`)
    await page.getByRole('button', {name: 'Run full cycle', exact: true}).click()
    await expect.poll(async () => ['complete', 'partial'].includes((await (await page.request.get(`/api/v1/sites/${id}/jobs`)).json()).items.find((j: any) => j.kind === 'full_cycle')?.status), {timeout: 90000}).toBe(true)
    const inventory = await (await page.request.get(`/api/v1/sites/${id}/pages?limit=200`)).json()
    expect(inventory.items.length).toBeGreaterThan(0)
    const jobs = await (await page.request.get(`/api/v1/sites/${id}/jobs`)).json()
    const cycle = jobs.items.find((j: any) => j.kind === 'full_cycle')
    expect(cycle.result.stages.find((s: any) => s.name === 'inventory').result.complete).toBe(true)
    expect(cycle.result.stages.find((s: any) => s.name === 'public_audit').result.checked_pages).toBeGreaterThan(0)
    // Larger stores finish in bounded continuations. A partial parent is not
    // relabeled as complete just because its child audit will continue.
    if (cycle.status === 'partial') expect(cycle.result.complete).toBe(false)
    const planned = await (await page.request.get(`/api/v1/sites/${id}/articles`)).json()
    expect(planned.items.length).toBeGreaterThan(0)
    if (kind === 'woocommerce') {
      expect(inventory.items.some((p: any) => p.resource_type === 'products')).toBe(true)
      await page.goto(`/sites/${id}/store`)
      await expect(page.locator('main h1')).toBeVisible()
    }
    await page.goto(`/sites/${id}/settings/policies`)
    await page.getByLabel('Publishing author', {exact: true}).selectOption(String(fixture.author_id))
    await page.getByRole('button', {name: 'Toggle site pause', exact: true}).click()
    if (await page.getByRole('button', {name: 'Toggle global pause', exact: true}).getAttribute('aria-pressed') === 'true') {
      await page.getByRole('button', {name: 'Toggle global pause', exact: true}).click()
    }
    await page.getByRole('button', {name: 'Toggle automated workflows', exact: true}).click()
    await page.getByLabel('Publish', {exact: true}).check()
    await page.getByRole('button', {name: 'Save policy controls'}).click()
    await expect(page.getByText('Policy and pause controls saved as new server state.')).toBeVisible()
    const policy = await (await page.request.get(`/api/v1/sites/${id}/policy`)).json()
    expect(policy.settings.enabled).toBe(true)
    expect(policy.settings.allowed_actions).toContain('publish')

    // Continue a brief created by the planner rather than publishing a separate
    // manually created article that bypasses the planning-to-editor handoff.
    await page.goto(`/sites/${id}/content/articles/${planned.items[0].id}`)
    const title = `Preparing questions for your ${kind} repair visit`
    const body = `<h2>Before your visit</h2><p>${name} provides Repairs. Write down the questions you want to discuss during your visit.</p>`
    await page.getByLabel('Title', {exact: false}).fill(title)
    await page.getByLabel('Publishing author', {exact: true}).selectOption(String(fixture.author_id))
    await page.getByLabel('Sources', {exact: false}).fill(fixture.origin)
    await page.getByLabel('Body', {exact: false}).fill(body)
    await page.getByRole('button', {name: 'Save article'}).click()
    await expect(page.getByText('Article saved. The API now has the latest editor state.')).toBeVisible()
    await expect(page).toHaveURL(/content\/articles\/[a-f0-9]+$/)
    const articleId = page.url().split('/articles/')[1]
    await page.getByRole('button', {name: 'Check', exact: true}).click()
    await expect(page.getByText('The editorial check passed.')).toBeVisible()
    await page.getByRole('button', {name: 'Publish', exact: true}).click()
    await expect(page.getByText('Publication completed and verification is recorded.')).toBeVisible({timeout: 45000})
    const article = await (await page.request.get(`/api/v1/sites/${id}/articles/${articleId}`)).json()
    expect(article.status).toBe('published')
    const pubs = await (await page.request.get(`/api/v1/sites/${id}/publications`)).json()
    const publication = pubs.items.find((p: any) => p.article_id === articleId)
    expect(publication.result.public_status).toBe(200)
    expect(publication.result.evidence.sha256).toBeTruthy()
    const publicUrl = new URL(publication.result.url)
    const localUrl = `http://127.0.0.1:${kind === 'wordpress' ? 18090 : 18091}${publicUrl.pathname}`
    const publicPage = await request.get(localUrl, {headers: {Host: publicUrl.host, 'X-Forwarded-Proto': 'https'}})
    expect(publicPage.status()).toBe(200)
    expect(await publicPage.text()).toContain('Write down the questions')
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', {name: 'Roll back publication'}).click()
    await expect(page.getByText('Rollback completed and the remote record was verified.')).toBeVisible({timeout: 45000})
    expect((await (await page.request.get(`/api/v1/sites/${id}/articles/${articleId}`)).json()).status).toBe('rolled_back')
    expect((await request.get(localUrl, {headers: {Host: publicUrl.host, 'X-Forwarded-Proto': 'https'}})).status()).toBe(404)
  }
})
