import {expect, test} from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

test('Docker candidate serves healthy API and accessible first-login screen without creating an account', async ({page, request}) => {
  const health = await request.get('/health')
  expect(health.ok()).toBe(true)
  expect(await health.json()).toEqual({status: 'ok', service: 'forgeseo-platform'})
  await page.goto('/')
  await expect(page).toHaveURL(/\/(bootstrap|login)$/)
  await expect(page.getByRole('textbox', {name: /email/i})).toBeVisible()
  const result = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
  expect(result.violations.map(v => v.id)).toEqual([])
  await page.screenshot({path: 'test-results/local-candidate-desktop.png', fullPage: true})
  await page.setViewportSize({width: 390, height: 844})
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({path: 'test-results/local-candidate-mobile.png', fullPage: true})
})
