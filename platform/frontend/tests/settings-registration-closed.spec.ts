import { expect, test } from '@playwright/test'

for (const initialized of [true, false]) {
  for (const creationFlag of [false, undefined]) {
    test(`registration stays closed with initialized=${initialized}, flag=${creationFlag}`, async ({ page }) => {
      const writes: string[] = []
      await page.route('**/api/v1/**', async route => {
        const request = route.request()
        const path = new URL(request.url()).pathname
        if (request.method() !== 'GET') writes.push(path)
        const body = path.endsWith('/auth/status')
          ? { initialized, account_creation_enabled: creationFlag }
          : { detail: 'Not authenticated' }
        await route.fulfill({ status: path.endsWith('/auth/status') ? 200 : 401, contentType: 'application/json', body: JSON.stringify(body) })
      })
      await page.goto('/bootstrap')
      await expect(page).toHaveURL(/\/login$/)
      await expect(page.getByRole('heading', { name: 'Sign in to Jazzb SEO' })).toBeVisible()
      await expect(page.getByRole('link', { name: 'Create the first owner account' })).toHaveCount(0)
      await expect(page.getByLabel('Deployment setup token')).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Create workspace' })).toHaveCount(0)
      expect(writes).toEqual([])
    })
  }
}
