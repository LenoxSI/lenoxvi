// Setup steps 2 and 3, Listening and Voice: the tier pick shows the helper
// cards, "Save and continue" runs the cards' own saves (nothing changed,
// nothing written), and keys go only through the existing key routes.
import { test, expect, FAKE_LOCALAI } from './fakeApi.js'
import { STEPS } from '../src/setupWizard.js'

const KEY = 'test-key-not-real-0000'
const LOCALAI = '192.0.2.50:8080'
const API = '/api/dashboard'
const LISTENING = STEPS[1].title, VOICE = STEPS[2].title, NEXT_AFTER_VOICE = STEPS[3].title

const wizard = page => page.locator('[data-wizard]')
const title = (page, text) => page.getByRole('heading', { level: 1, name: text, exact: true })
const tier = (page, name) => wizard(page).getByRole('radio', { name: new RegExp(`^${name}\\b`) })
const saveAndContinue = page => page.getByRole('button', { name: 'Save and continue', exact: true }).click()
const keyVars = page => wizard(page).locator('#keys-table tr[data-var]').evaluateAll(rs => rs.map(r => r.dataset.var).sort())
const sideScroll = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
const duplicateIds = page => page.evaluate(() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id)
  return ids.filter((id, i) => ids.indexOf(id) !== i)
})
const saves = fake => fake.saves().map(w => [w.method, w.path.slice(API.length), w.body])

async function openStep(page, name) {
  await page.goto('/dashboard')
  for (const s of STEPS) {
    await expect(title(page, s.title)).toBeVisible()
    if (s.title === name) return
    await page.getByRole('button', { name: s.id === STEPS[0].id ? 'Continue' : 'Skip', exact: true }).click()
  }
}

// A key typed into its row on the step's own key box, saved through its route.
async function saveKeyRow(page, v) {
  const row = wizard(page).locator(`tr[data-var="${v}"]`)
  await row.getByRole('button', { name: 'Set', exact: true }).click()
  const box = wizard(page).locator('tr[data-role="replace"]')
  await box.getByRole('textbox').fill(KEY)
  await box.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(row.getByRole('button', { name: 'Replace', exact: true })).toBeVisible()
}

test('Embedded on both steps: Save and continue with nothing changed writes nothing and moves on', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, LISTENING)
  await expect(tier(page, 'Embedded')).toBeChecked()
  await expect(wizard(page).locator('#stt')).toBeVisible()
  await expect(wizard(page).locator('#keys')).toHaveCount(0)
  await expect(wizard(page).locator('#localai')).toHaveCount(0)
  await expect(wizard(page).getByText('Local or Cloud answer faster on this machine.')).toHaveCount(0)
  await saveAndContinue(page)
  await expect(title(page, VOICE)).toBeVisible()
  await expect(page.getByText('Nothing changed.', { exact: true })).toBeVisible()

  await expect(tier(page, 'Embedded')).toBeChecked()
  await expect(wizard(page).locator('#voice')).toBeVisible()
  await saveAndContinue(page)
  await expect(title(page, NEXT_AFTER_VOICE)).toBeVisible()
  expect(fake.writes()).toEqual([])
  await expect(page).toHaveURL(/#setup$/)
})

test('Listening on Cloud: only the cloud key rows; Deepgram opens once its key is saved; Save picks it', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, LISTENING)
  await tier(page, 'Cloud').check()
  await expect(wizard(page).locator('#keys')).toBeVisible()
  await expect.poll(() => keyVars(page)).toEqual(['DEEPGRAM_API_KEY', 'ELEVENLABS_API_KEY'])
  await expect(wizard(page).locator('#key-add')).toHaveCount(0)
  const deepgram = page.getByLabel('STT provider').locator('option[value="deepgram"]')
  await expect(deepgram).toBeDisabled()
  expect(await duplicateIds(page)).toEqual([])

  await saveKeyRow(page, 'DEEPGRAM_API_KEY')
  await expect(deepgram).toBeEnabled()
  await page.getByLabel('STT provider').selectOption('deepgram')
  await saveAndContinue(page)
  await expect(title(page, VOICE)).toBeVisible()
  expect(saves(fake)).toEqual([
    ['PUT', '/tts/key/deepgram', { api_key: KEY }],
    ['PUT', '/stt', { provider: 'deepgram' }],
  ])
  expect(await page.evaluate(() => document.body.innerText)).not.toContain(KEY)
  for (const r of fake.requests) expect(r.url).not.toContain(KEY)
})

test('Listening on Local: LocalAI and its key row; the No key link scrolls to the key box; Test, then Save', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, LISTENING)
  await tier(page, 'Local').check()
  await expect(wizard(page).locator('#localai')).toBeVisible()
  await expect.poll(() => keyVars(page)).toEqual(['LVI_LOCALAI_KEY'])
  expect(await duplicateIds(page)).toEqual([])

  // The address saves on its own: the step stays, so LocalAI can be picked next.
  await page.getByLabel('LocalAI address').fill(LOCALAI)
  await saveAndContinue(page)
  await expect(title(page, LISTENING)).toBeVisible()
  await expect(page.getByLabel('LocalAI address')).toHaveValue(LOCALAI)

  const noKey = wizard(page).getByRole('button', { name: /^No LocalAI key/ })
  await expect(noKey).toBeVisible()
  await expect(wizard(page).getByRole('link', { name: /System · Keys/ })).toHaveCount(0)
  await noKey.click()
  await expect(wizard(page).locator('#keys')).toBeInViewport()
  await expect(page).toHaveURL(/#setup$/)
  await expect(title(page, LISTENING)).toBeVisible()

  await saveKeyRow(page, 'LVI_LOCALAI_KEY')
  await wizard(page).locator('#localai').getByRole('button', { name: 'Test', exact: true }).click()
  await expect(wizard(page).locator('#localai-test')).toContainText(FAKE_LOCALAI.stt)

  await page.getByLabel('STT provider').selectOption('localai')
  await page.getByLabel('LocalAI speech-to-text model').selectOption(FAKE_LOCALAI.stt)
  await saveAndContinue(page)
  await expect(title(page, VOICE)).toBeVisible()
  expect(saves(fake)).toEqual([
    ['PUT', '/localai', { url: LOCALAI }],
    ['PUT', '/localai/key', { api_key: KEY }],
    ['PUT', '/localai', { model_stt: FAKE_LOCALAI.stt }],
    ['PUT', '/stt', { provider: 'localai' }],
  ])
  expect(fake.writes().some(w => w.method === 'POST' && w.path === `${API}/localai/test`)).toBe(true)
})

test('Voice: a Kokoro voice change saves through PUT /tts', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, VOICE)
  const tts = fake.state[`${API}/tts`]
  const pick = tts.catalog.kokoro.find(v => v !== tts.kokoro_voice)
  await page.getByLabel('Kokoro voice', { exact: true }).selectOption(pick)
  await saveAndContinue(page)
  await expect(title(page, NEXT_AFTER_VOICE)).toBeVisible()
  expect(saves(fake)).toEqual([['PUT', '/tts', { provider: 'kokoro', kokoro_voice: pick }]])
})

test('Voice: ElevenLabs with no voice ID shows the card\'s own error and the step stays put', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, VOICE)
  await tier(page, 'Cloud').check()
  await expect.poll(() => keyVars(page)).toEqual(['DEEPGRAM_API_KEY', 'ELEVENLABS_API_KEY'])
  const el = page.getByLabel('TTS provider').locator('option[value="elevenlabs"]')
  await expect(el).toBeDisabled()
  await saveKeyRow(page, 'ELEVENLABS_API_KEY')
  await expect(el).toBeEnabled()
  await page.getByLabel('TTS provider').selectOption('elevenlabs')
  await saveAndContinue(page)
  await expect(page.getByText("Voice ID can't be empty", { exact: false })).toBeVisible()
  await expect(title(page, VOICE)).toBeVisible()
  expect(saves(fake)).toEqual([['PUT', '/tts/key/elevenlabs', { api_key: KEY }]])
  expect(await page.evaluate(() => document.body.innerText)).not.toContain(KEY)
})

test('a slow machine: both speech steps say Local or Cloud answer faster', async ({ page, fake }) => {
  await fake.load('slow')
  await openStep(page, LISTENING)
  await expect(wizard(page).getByText('Local or Cloud answer faster on this machine.')).toBeVisible()
  await page.getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(title(page, VOICE)).toBeVisible()
  await expect(wizard(page).getByText('Local or Cloud answer faster on this machine.')).toBeVisible()
})

test('no sideways scroll on either step, on every tier, with a key box open', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, LISTENING)
  for (const step of [LISTENING, VOICE]) {
    await expect(title(page, step)).toBeVisible()
    for (const t of ['Embedded', 'Local', 'Cloud']) {
      await tier(page, t).check()
      await expect(tier(page, t)).toBeChecked()
      expect(await sideScroll(page), `${step} · ${t}`).toBeLessThanOrEqual(0)
    }
    await wizard(page).locator('tr[data-var="DEEPGRAM_API_KEY"]').getByRole('button', { name: 'Set', exact: true }).click()
    await expect(wizard(page).locator('tr[data-role="replace"]')).toBeVisible()
    expect(await sideScroll(page), `${step} · key box`).toBeLessThanOrEqual(0)
    expect(await duplicateIds(page)).toEqual([])
    if (step === LISTENING) await page.getByRole('button', { name: 'Skip', exact: true }).click()
  }
  expect(fake.saves()).toEqual([])
})
