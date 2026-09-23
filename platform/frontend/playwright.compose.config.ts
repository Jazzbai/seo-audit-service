import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.FORGE_COMPOSE_URL ?? 'http://127.0.0.1:18080'
const allowInsecureTls = process.env.FORGE_COMPOSE_ALLOW_INSECURE_TLS === 'true'

export default defineConfig({
  testDir: './tests',
  testMatch: 'integration.spec.ts',
  workers: 1,
  timeout: 60000,
  reporter: [['list']],
  use: {
    baseURL,
    // Disposable local Compose gates use Caddy's generated certificate. Keep
    // this opt-in so a real deployment still fails browser tests on untrusted
    // TLS instead of masking a certificate or hostname configuration error.
    ignoreHTTPSErrors: allowInsecureTls,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
})
