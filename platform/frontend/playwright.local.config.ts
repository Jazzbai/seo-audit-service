import {defineConfig, devices} from '@playwright/test'

export default defineConfig({
  testDir: './tests', testMatch: 'local-candidate.spec.ts', workers: 1,
  reporter: [['list']],
  use: {baseURL: 'http://localhost:18080', ...devices['Desktop Chrome']},
})
