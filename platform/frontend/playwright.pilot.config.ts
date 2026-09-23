import {defineConfig, devices} from '@playwright/test'

export default defineConfig({
  testDir: './tests', testMatch: 'pilot.spec.ts', workers: 1, timeout: 180000,
  expect: {timeout: 15000}, reporter: [['list']],
  // Fixture passwords are entered during onboarding. Never record them in traces.
  use: {baseURL: 'http://127.0.0.1:4173', actionTimeout: 20000, trace: 'off', screenshot: 'only-on-failure', ...devices['Desktop Chrome']},
  webServer: [
    {command: process.platform === 'win32' ? '..\\.venv\\Scripts\\python.exe ..\\scripts\\pilot_browser_server.py' : '../.venv/bin/python ../scripts/pilot_browser_server.py', url: 'http://127.0.0.1:18082/health', reuseExistingServer: false, timeout: 300000},
    {command: 'npm run dev -- --host 127.0.0.1', url: 'http://127.0.0.1:4173', reuseExistingServer: false, timeout: 30000},
  ],
})
