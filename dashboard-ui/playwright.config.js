import { defineConfig, devices } from '@playwright/test'

// Browser harness for the dashboard (dev-side only; nothing a user installs
// runs it). Mock mode serves the committed dist/ on its own loopback port and
// answers the API from e2e/fakeApi.js. Live mode (LVI_TEST_LIVE=1 with
// LVI_E2E_URL) runs only the live specs against a real bot.
const live = process.env.LVI_TEST_LIVE === '1'

export default defineConfig({
  testDir: './e2e',
  testMatch: live ? '**/live.spec.js' : '**/*.spec.js',
  testIgnore: live ? [] : ['**/live.spec.js'],
  fullyParallel: true,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: live ? process.env.LVI_E2E_URL : 'http://127.0.0.1:4173',
    ignoreHTTPSErrors: live,  // the bot's self-signed certificate
  },
  projects: [
    { name: 'desktop-chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } },
    // The narrowest phone the wizard supports, on Android's engine.
    { name: 'phone-android', use: { ...devices['Pixel 7'], viewport: { width: 360, height: 740 } } },
    { name: 'phone-ios', use: { ...devices['iPhone 13'] } },
  ],
  webServer: live ? undefined : {
    command: 'npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173/dashboard',
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
