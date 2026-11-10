// Setup step 8, Restart and test, and Finish: a restart applies what the
// steps saved (or a note asks for one by hand), the wake word heard after
// that boot proves the setup, and Finish clears the marker. Also: unsaved
// edits in a step ask before the page is left.
import { test, expect } from './fakeApi.js'
import { STEPS, wakePhrase } from '../src/setupWizard.js'

const API = '/api/dashboard'
const PROBE = `${API}/setup/probe`
const VOICE = STEPS.find(s => s.id === 'voice')
const FINISH = STEPS.at(-1)

const wizard = page => page.locator('[data-wizard]')
const title = (page, text) => page.getByRole('heading', { level: 1, name: text, exact: true })
const restarts = fake => fake.writes().filter(w => w.method === 'POST' && w.path === `${API}/restart`)
const summaryRow = (page, step) => wizard(page).getByRole('list', { name: 'Setup summary' }).locator(`li[data-step="${step.id}"]`)
const sideScroll = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
const duplicateIds = page => page.evaluate(() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id)
  return ids.filter((id, i) => ids.indexOf(id) !== i)
})

// Whether the page's leave warning is armed: a beforeunload listener cancels the event.
const armed = page => page.evaluate(() => {
  const e = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(e)
  return e.defaultPrevented
})

// Walks from Welcome to `name`, skipping every step on the way.
async function walkTo(page, name) {
  for (const s of STEPS) {
    await expect(title(page, s.title)).toBeVisible()
    if (s.title === name) return
    if (s.id === STEPS[0].id) await page.getByRole('button', { name: 'Continue', exact: true }).click()
    else await page.getByRole('button', { name: /^(Skip|Not yet)$/ }).click()
  }
}

// A Kokoro voice change saved on the Voice step: TTS then waits for a restart.
async function saveVoiceThenFinish(page, fake) {
  await page.goto('/dashboard')
  await walkTo(page, VOICE.title)
  const tts = fake.state[`${API}/tts`]
  await page.getByLabel('Kokoro voice', { exact: true }).selectOption(tts.catalog.kokoro.find(v => v !== tts.kokoro_voice))
  await page.getByRole('button', { name: 'Save and continue', exact: true }).click()
  for (let i = STEPS.indexOf(VOICE) + 1; i < STEPS.length - 1; i++) {
    await expect(title(page, STEPS[i].title)).toBeVisible()
    await page.getByRole('button', { name: /^(Skip|Not yet)$/ }).click()
  }
  await expect(title(page, FINISH.title)).toBeVisible()
}

test('Restart to apply, then the wake word heard after the new boot; Finish clears the marker and lands on Rooms', async ({ page, fake }) => {
  test.setTimeout(60_000)
  await fake.load('fresh')
  await saveVoiceThenFinish(page, fake)
  const phrase = wakePhrase(fake.state[`${API}/oww/models`])
  const restart = page.getByRole('button', { name: 'Restart to apply (1)', exact: true })
  await expect(restart).toBeVisible()
  await expect(wizard(page).getByText(`Say “${phrase}”`, { exact: false })).toHaveCount(0)
  expect(await duplicateIds(page)).toEqual([])
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  fake.wake('Room 2')  // heard by the old process: proves nothing about the new setup
  await restart.click()
  await expect.poll(() => restarts(fake).length).toBe(1)
  await expect(wizard(page).getByText(`Say “${phrase}”`, { exact: false })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('button', { name: /^Restart/ })).toHaveCount(0)
  await page.waitForTimeout(5500)  // a poll after the new boot
  await expect(wizard(page).getByText('Heard you in', { exact: false })).toHaveCount(0)

  fake.wake('Room 1')
  await expect(wizard(page).getByText('Heard you in Room 1', { exact: false })).toBeVisible({ timeout: 8000 })
  const client = wizard(page).getByRole('link', { name: /talk from this browser/i })
  await expect(client).toHaveAttribute('href', '/client/')
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  await page.getByRole('button', { name: 'Finish', exact: true }).click()
  await expect(page).toHaveURL(/\/dashboard#rooms$/)
  await expect(page.locator('[data-tab="rooms"]')).toBeVisible()
  expect(fake.writes().filter(w => w.path === `${API}/setup/done`).map(w => w.body)).toEqual([{ skipped: false }])
  expect(restarts(fake)).toHaveLength(1)

  // The Restart step's reads end with the wizard; a reload stays on the dashboard.
  const reads = fake.gets(PROBE).length
  await page.waitForTimeout(6000)
  expect(fake.gets(PROBE).length, 'probe reads after Finish').toBe(reads)
  await page.reload()
  await expect(page.locator('[data-tab="rooms"]')).toBeVisible()
  await expect.poll(() => fake.gets(PROBE).length).toBeGreaterThan(reads)
  await page.waitForTimeout(500)
  await expect(wizard(page)).toHaveCount(0)
  await expect(page).toHaveURL(/#rooms$/)
})

test('a bot that cannot restart itself: the step asks for a restart by hand and never posts one', async ({ page, fake }) => {
  test.setTimeout(60_000)
  await fake.load('manual')
  await saveVoiceThenFinish(page, fake)
  await expect(wizard(page).getByText('Restart the bot yourself', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: /^Restart/ })).toHaveCount(0)
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  fake.reboot()  // restarted by hand: the step's next read sees the new boot
  const phrase = wakePhrase(fake.state[`${API}/oww/models`])
  await expect(wizard(page).getByText(`Say “${phrase}”`, { exact: false })).toBeVisible({ timeout: 8000 })
  await expect(wizard(page).getByText('Restart the bot yourself', { exact: false })).toHaveCount(0)
  await page.getByRole('button', { name: 'Finish', exact: true }).click()
  await expect(page).toHaveURL(/#rooms$/)
  expect(restarts(fake)).toEqual([])
})

test('the summary lists every step done or skipped, each with a way back', async ({ page, fake }) => {
  await fake.load('fresh')
  await page.goto('/dashboard')
  await walkTo(page, FINISH.title)
  const probe = fake.state[PROBE]
  for (const s of STEPS.slice(1, -1)) {
    const done = probe.steps.find(x => x.id === s.id).done
    await expect(summaryRow(page, s)).toContainText(s.title)
    await expect(summaryRow(page, s)).toContainText(done ? 'done' : 'skipped')
    await expect(summaryRow(page, s).getByRole('button', { name: `${done ? 'Change' : 'Set up'} ${s.title}` })).toBeVisible()
  }
  const speakers = STEPS.find(s => s.id === 'speakers')
  await summaryRow(page, speakers).getByRole('button').click()
  await expect(title(page, speakers.title)).toBeVisible()
  expect(fake.writes()).toEqual([])
})

test('leaving the page with an unsaved edit in a step asks first; a clean step leaves quietly', async ({ page, fake }) => {
  await fake.load('fresh')
  await page.goto('/dashboard')
  await walkTo(page, VOICE.title)
  await title(page, VOICE.title).click()  // a real click: browsers only ask a page someone used
  const tts = fake.state[`${API}/tts`]
  await page.getByLabel('Kokoro voice', { exact: true }).selectOption(tts.catalog.kokoro.find(v => v !== tts.kokoro_voice))
  await expect.poll(() => armed(page), { message: 'leave warning armed with a dirty step' }).toBe(true)
  const asked = []
  page.on('dialog', d => { asked.push(d.type()); d.dismiss() })
  await page.close({ runBeforeUnload: true })
  await expect.poll(() => asked).toEqual(['beforeunload'])
  expect(page.isClosed()).toBe(false)

  await page.getByRole('button', { name: 'Skip', exact: true }).click()  // drops the step's drafts
  await expect(title(page, STEPS[STEPS.indexOf(VOICE) + 1].title)).toBeVisible()
  await expect.poll(() => armed(page), { message: 'leave warning disarmed with a clean step' }).toBe(false)
  // Leaving again: a reload (WebKit never finishes a second close once a
  // leave prompt was dismissed) goes through without asking.
  await page.reload()
  await expect(title(page, STEPS[0].title)).toBeVisible()
  expect(asked).toEqual(['beforeunload'])
  expect(fake.writes()).toEqual([])
})
