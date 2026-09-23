import { defineConfig, devices } from '@playwright/test'

const smokeBaseUrl = process.env.SMOKE_BASE_URL

export default defineConfig({
  testDir: './tests',
  // Real-backend and smoke checks have their own server harnesses. Keep the
  // default local UI suite deterministic and runnable with only Vite.
  testMatch: /(?:ui|business-facts|store|settings(?:-.*)?|acceptance-gap.*)\.spec\.ts/,
  timeout: 30_000,
  expect: { timeout: 6_000 },
  fullyParallel: true,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: smokeBaseUrl ?? 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: smokeBaseUrl ? undefined : {
    command: 'npm run dev -- --host 127.0.0.1',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: true,
    timeout: 30_000,
  },
})
