import { defineConfig, devices } from '@playwright/test'

// Operator-selected Python executable; never read a production environment file.
const python = process.env.FORGE_TEST_PYTHON ?? (process.platform === 'win32' ? '..\\.venv\\Scripts\\python.exe' : '../.venv/bin/python')
const resume = process.env.FORGE_REHEARSAL_RESUME
if (resume && /["\r\n]/.test(resume)) throw new Error('Invalid rehearsal resume directory')
const runId = process.env.FORGE_REHEARSAL_RUN_ID ?? String(Date.now())
if (!/^[a-zA-Z0-9_-]+$/.test(runId)) throw new Error('Invalid rehearsal run identifier')
export default defineConfig({
  testDir: './tests', testMatch: resume ? 'wordpress-rehearsal-resume.spec.ts' : 'wordpress-rehearsal.spec.ts', workers: 1,
  timeout: 240000, expect: { timeout: 15000 },
  reporter: [['list'], ['json', { outputFile: `test-results/rehearsal-reports/${runId}.json` }]],
  outputDir: `test-results/wordpress-rehearsal-${runId}`,
  // A real fixture password passes through onboarding. Do not record any trace,
  // video or automatic screenshot; explicit captures happen after secrets clear.
  use: { baseURL: 'http://127.0.0.1:4173', actionTimeout: 20000, trace: 'off', video: 'off', screenshot: 'off', ...devices['Desktop Chrome'] },
  webServer: [
    { command: `"${python}" ../scripts/pilot_browser_server.py --wordpress-only --scheduled-publication --retain-evidence${resume ? ` --resume-evidence "${resume}"` : ''}`, url: 'http://127.0.0.1:18082/health', reuseExistingServer: false, timeout: 60000 },
    { command: 'npm run dev -- --host 127.0.0.1', url: 'http://127.0.0.1:4173', reuseExistingServer: false, timeout: 30000 },
  ],
})
