// The setup wizard against a real bot, end to end. Opt-in only: it runs with
// LVI_TEST_LIVE=1 and LVI_E2E_URL (the bot's https://<ip>:7860), and skips
// otherwise. The bot needs a fresh state dir that holds setup.pending; a bot
// with a dashboard token also needs LVI_E2E_TOKEN, which is sent as the
// header and never printed. It restarts the bot, so one browser runs it.
import { test, expect } from '@playwright/test'
import { STEPS } from '../src/setupWizard.js'

const LIVE = process.env.LVI_TEST_LIVE === '1' && !!process.env.LVI_E2E_URL
const TOKEN = process.env.LVI_E2E_TOKEN || ''
const API = '/api/dashboard'
const FINISH = STEPS.at(-1)

test.describe.configure({ mode: 'serial' })

const title = (page, text) => page.getByRole('heading', { level: 1, name: text, exact: true })

// What the settings GETs say, minus what changes on its own (load state,
// availability, uptime). Skipping every step must leave all of it as it was.
async function settings(request) {
  const get = async p => (await request.get(`${API}/${p}`)).json()
  const [stt, tts, intel, wake, oww, tuning, mapping] = await Promise.all(
    ['stt', 'tts', 'intelligence', 'wake', 'oww/models', 'voice-tuning', 'mapping'].map(get))
  const ttsSaved = { ...tts }
  delete ttsSaved.providers
  return {
    stt: stt.current, tts: ttsSaved, wake, tuning,
    intelligence: { kind: intel.kind, url: intel.url, urls: intel.urls, agents: intel.agents },
    wakeModel: (oww.models || []).find(m => m.selected)?.path ?? null,
    mapping: { room_zones: mapping.room_zones, zones: mapping.zones },
  }
}

test('live: a marked bot opens the wizard; skip 2–7, restart, Finish: the marker is gone, no setting changed', async ({ page, request }, info) => {
  test.skip(!LIVE, 'live only: set LVI_TEST_LIVE=1 and LVI_E2E_URL')
  test.skip(info.project.name !== 'desktop-chromium', 'it restarts the bot: one browser runs it')
  test.setTimeout(240_000)
  const first = await (await request.get(`${API}/setup/probe`)).json()
  test.skip(first.pending !== true, 'needs a bot whose fresh state dir holds setup.pending')
  test.skip(first.token_required && !TOKEN, 'this bot has a dashboard token: set LVI_E2E_TOKEN')
  const before = await settings(request)
  const headers = TOKEN ? { 'X-Dashboard-Token': TOKEN } : {}

  await page.goto('/dashboard')
  await expect(page).toHaveURL(/\/dashboard#setup$/)
  await expect(title(page, STEPS[0].title)).toBeVisible()
  if (first.token_required) await page.locator('[data-wizard]').getByLabel('Dashboard token').fill(TOKEN)
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  for (const s of STEPS.slice(1, -1)) {
    await expect(title(page, s.title)).toBeVisible()
    await page.getByRole('button', { name: /^(Skip|Not yet)$/ }).click()
  }
  await expect(title(page, FINISH.title)).toBeVisible()

  // Restart: through the step's button when it offers one, else the same
  // route the button uses. Back = /status from a process that booted after.
  const askedAt = Date.now() / 1000
  const button = page.getByRole('button', { name: /^Restart to apply/ })
  if (first.restart === 'auto' && await button.isVisible()) await button.click()
  else if (first.restart === 'auto') expect((await request.post(`${API}/restart`, { headers })).ok()).toBe(true)
  else test.skip(true, 'this bot restarts by hand (restart: manual)')
  await expect.poll(async () => {
    try {
      const s = await (await request.get(`${API}/status`)).json()
      return s.uptime_secs != null && Date.now() / 1000 - s.uptime_secs > askedAt + 1
    } catch { return false }
  }, { timeout: 120_000, intervals: [2000] }).toBe(true)
  await expect(page.getByText(/^Say /)).toBeVisible({ timeout: 30_000 })

  await page.getByRole('button', { name: 'Finish', exact: true }).click()
  await expect(page).toHaveURL(/\/dashboard#rooms$/)
  const after = await (await request.get(`${API}/setup/probe`)).json()
  expect(after.pending).toBe(false)
  expect(await settings(request)).toEqual(before)
  await page.reload()
  await expect(page.locator('[data-tab="rooms"]')).toBeVisible()
  await page.waitForTimeout(1000)
  await expect(page.locator('[data-wizard]')).toHaveCount(0)
})
