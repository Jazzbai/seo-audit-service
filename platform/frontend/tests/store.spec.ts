import { expect, test, type Page, type Route } from '@playwright/test'

const auth = {
  user: { id: 'user-1', email: 'owner@example.com', name: 'Alex Owner' },
  team: { id: 'team-1', name: 'Northstar team' },
  role: 'owner',
  csrf_token: 'csrf-test-token',
}

const site = {
  id: 'site-1',
  team_id: 'team-1',
  name: 'Acme Studio',
  origin: 'https://acme.example',
  timezone: 'America/Chicago',
  language: 'en',
  paused: true,
  facts: { business_name: 'Acme Studio', audience: 'Independent makers', locations: [], services: [], products: [], authors: [], confirmed_sources: [] },
}

const appUrl = 'http://127.0.0.1:4173'

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

const inventory = [
  { id: 'product-1', site_id: 'site-1', resource_key: 'products:1', url: 'https://acme.example/products/brake-pads', title: 'Brake Pads', resource_type: 'products', managed: false, enrolled: false, last_seen_at: '2026-09-16T14:00:00Z' },
  { id: 'category-1', site_id: 'site-1', resource_key: 'product_categories:1', url: 'https://acme.example/product-category/brakes', title: 'Brakes', resource_type: 'product_categories', managed: false, enrolled: false, last_seen_at: '2026-09-16T14:00:00Z' },
]

const freshnessInventory = [
  { id: 'product-fresh', site_id: 'site-1', resource_key: 'products:fresh', url: 'https://acme.example/products/recent', title: 'Recently observed product', resource_type: 'products', managed: false, enrolled: false, last_seen_at: new Date(Date.now() - 23 * 60 * 60 * 1000).toISOString() },
  { id: 'category-stale', site_id: 'site-1', resource_key: 'product_categories:stale', url: 'https://acme.example/product-category/old', title: 'Stale category', resource_type: 'product_categories', managed: false, enrolled: false, last_seen_at: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString() },
  { id: 'product-missing-time', site_id: 'site-1', resource_key: 'products:missing-time', url: 'https://acme.example/products/no-timestamp', title: 'Missing timestamp product', resource_type: 'products', managed: false, enrolled: false, last_seen_at: null },
  { id: 'category-invalid-time', site_id: 'site-1', resource_key: 'product_categories:invalid-time', url: 'https://acme.example/product-category/invalid-timestamp', title: 'Invalid timestamp category', resource_type: 'product_categories', managed: false, enrolled: false, last_seen_at: 'not-a-timestamp' },
]

const connection = {
  kind: 'woocommerce',
  status: 'connected',
  capabilities: {
    authenticated: true,
    woocommerce_api: true,
    products: { read: true, update: true },
    categories: { read: true, update: true },
    seo: { read: true, write: false, writable_fields: [], resource_types: ['product', 'category'] },
    protected_commerce_fields: ['price', 'stock', 'sku'],
  },
}

async function installStoreMocks(page: Page, options: { empty?: boolean; failProducts?: number; partial?: boolean; freshness?: boolean; connection?: Partial<typeof connection> } = {}) {
  let failProducts = options.failProducts ?? 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status' && method === 'GET') return json(route, { initialized: true })
    if (path === '/auth/me' && method === 'GET') return json(route, auth)
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/sites/site-1/products' && method === 'GET') {
      if (failProducts) {
        failProducts -= 1
        return json(route, { detail: 'The store inventory service is temporarily unavailable.' }, 503)
      }
      const items = options.empty ? [] : options.freshness ? freshnessInventory : inventory
      return json(route, { items, total: options.partial ? items.length + 3 : items.length })
    }
    if (path === '/sites/site-1/findings' && method === 'GET') {
      return json(route, options.empty ? { items: [], total: 0 } : { items: [{ id: 'finding-1', page_id: 'product-1', key: 'products:1:missing_meta_description', code: 'missing_meta_description', severity: 'medium', title: 'Product is missing a meta description', details: {}, status: 'open', last_seen_at: '2026-09-16T14:00:00Z' }], total: 1 })
    }
    if (path === '/sites/site-1/candidates' && method === 'GET') {
      return json(route, options.empty ? { items: [], total: 0 } : { items: [{ id: 'candidate-1', page_id: 'product-1', field: 'meta_description', before_value: '', after_value: 'A concise brake-pads overview for shoppers.', source_hash: 'hash-1', status: 'pending', details: { review_only_reasons: ['SEO writer is not verified for this store resource.'] }, created_at: '2026-09-16T14:00:00Z' }], total: 1 })
    }
    if (path === '/sites/site-1/connections' && method === 'GET') return json(route, options.empty ? { items: [], total: 0 } : { items: [{ ...connection, ...options.connection }], total: 1 })
    return json(route, { items: [], total: 0 })
  })
}

test('Store shows product and category inventory with scoped findings and review-only opportunities', async ({ page }) => {
  await installStoreMocks(page)
  await page.goto(`${appUrl}/sites/site-1/store`)

  await expect(page.getByRole('heading', { name: 'Woo store', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Products', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Categories', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: /Brake Pads/ }).first()).toBeVisible()
  await expect(page.getByText('Brakes', { exact: true })).toBeVisible()
  await expect(page.getByText('Product is missing a meta description', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'SEO opportunities', exact: true })).toBeVisible()
  await expect(page.getByText('Recommendations are review-only here; use Issues for the site-wide approval workflow.')).toBeVisible()
  await expect(page.getByText('Review only:', { exact: true })).toBeVisible()
  await expect(page.getByText('Prices, inventory, SKUs, checkout, and other commercial fields are not part of this editorial surface and are never written by these controls.')).toBeVisible()
  await expect(page.getByText('Commerce fields', { exact: true }).locator('..').getByText('Protected', { exact: true })).toBeVisible()
})

test('Store preserves empty inventory while clearly asking for a WooCommerce connection', async ({ page }) => {
  await installStoreMocks(page, { empty: true })
  await page.goto(`${appUrl}/sites/site-1/store`)

  await expect(page.getByText('WooCommerce: Needs Connection', { exact: true })).toBeVisible()
  await expect(page.getByText('Connect WooCommerce before ForgeSEO can verify live catalog access.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No store records yet', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No store findings recorded', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'No store opportunities yet', exact: true })).toBeVisible()
  await expect(page.getByText('An empty queue does not mean the store is fully optimized.')).toBeVisible()
})

test('Store presents a failed WooCommerce check as an error with a retry direction', async ({ page }) => {
  await installStoreMocks(page, { connection: { status: 'error', error: 'The provider rejected the last connection test.' } })
  await page.goto(`${appUrl}/sites/site-1/store`)

  const connectionError = page.getByRole('alert').filter({ hasText: 'WooCommerce: Error' })
  await expect(connectionError).toBeVisible()
  await expect(connectionError).toContainText('The latest WooCommerce connection check failed.')
  await expect(connectionError).toContainText('Review the connection and retry')
  await expect(connectionError).toContainText('The provider rejected the last connection test.')
  await expect(page.getByText('WooCommerce: Needs Review', { exact: true })).toHaveCount(0)
  const capabilityHeader = page.getByRole('heading', { name: 'Capability coverage', exact: true }).locator('../..')
  await expect(capabilityHeader.getByText('Error', { exact: true })).toBeVisible()
})

test('Store labels truncated inventory as partial coverage and scopes the visible queue', async ({ page }) => {
  await installStoreMocks(page, { partial: true })
  await page.goto(`${appUrl}/sites/site-1/store`)

  const coverage = page.getByRole('status').filter({ hasText: 'Inventory coverage is partial' })
  await expect(coverage).toContainText('Showing 2 of 5 catalog records')
  await expect(coverage).toContainText('only cover the records returned here')
  await expect(coverage).toContainText('an empty queue does not mean the store is fully optimized')
})

test('Store classifies inventory freshness conservatively at the daily cadence', async ({ page }) => {
  await installStoreMocks(page, { freshness: true })
  await page.goto(`${appUrl}/sites/site-1/store`)

  const freshRow = page.getByRole('row').filter({ hasText: 'Recently observed product' })
  const staleRow = page.getByRole('row').filter({ hasText: 'Stale category' })
  const missingRow = page.getByRole('row').filter({ hasText: 'Missing timestamp product' })
  const invalidRow = page.getByRole('row').filter({ hasText: 'Invalid timestamp category' })

  await expect(freshRow.getByText('Within Daily Cadence', { exact: true })).toBeVisible()
  await expect(staleRow.getByText('Stale', { exact: true })).toBeVisible()
  await expect(missingRow.getByText('Freshness Unknown', { exact: true })).toBeVisible()
  await expect(invalidRow.getByText('Freshness Unknown', { exact: true })).toBeVisible()
  await expect(staleRow).toContainText('Observed more than 24 hours ago')
  await expect(missingRow).toContainText('freshness is unknown, not current')
  await expect(page.getByText('Freshness uses the daily inventory cadence: records observed more than 24 hours ago are stale; missing or invalid timestamps remain unknown.', { exact: true })).toBeVisible()
})

test('Store keeps its recoverable error and retry path', async ({ page }) => {
  await installStoreMocks(page, { failProducts: 2 })
  await page.goto(`${appUrl}/sites/site-1/store`)

  await expect(page.getByRole('alert')).toContainText('store inventory service is temporarily unavailable')
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('heading', { name: 'Woo store', exact: true })).toBeVisible()
})
