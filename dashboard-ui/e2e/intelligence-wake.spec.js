// Setup steps 4 and 5, Intelligence and Wake word: the cards as they are,
// saved through their own routes; "Not yet" writes nothing and the Finish
// summary says the step was skipped.
import { test, expect, FAKE_AGENT, FAKE_REPLY } from './fakeApi.js'
import { STEPS } from '../src/setupWizard.js'

const KEY = 'test-key-not-real-0000'
const API = '/api/dashboard'
const HERMES_URL = '192.0.2.20:8642'
const OPENCLAW_URL = '192.0.2.21:18789'
const UPLOAD = { name: 'e2e_wake.onnx', mimeType: 'application/octet-stream', buffer: Buffer.alloc(2048) }
const [, LISTENING, , INTEL, WAKE, AFTER_WAKE] = STEPS
const FINISH = STEPS.at(-1)

const wizard = page => page.locator('[data-wizard]')
const title = (page, text) => page.getByRole('heading', { level: 1, name: text, exact: true })
const saveAndContinue = page => page.getByRole('button', { name: 'Save and continue', exact: true }).click()
const skip = page => page.getByRole('button', { name: /^(Skip|Not yet)$/ }).click()
const keyVars = page => wizard(page).locator('#keys-table tr[data-var]').evaluateAll(rs => rs.map(r => r.dataset.var).sort())
const sideScroll = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
const duplicateIds = page => page.evaluate(() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id)
  return ids.filter((id, i) => ids.indexOf(id) !== i)
})
const saves = fake => fake.saves().map(w => [w.method, w.path.slice(API.length), w.body])
const summaryRow = (page, step) => wizard(page).getByRole('list', { name: 'Setup summary' }).locator(`li[data-step="${step.id}"]`)

async function openStep(page, name) {
  await page.goto('/dashboard')
  for (const s of STEPS) {
    await expect(title(page, s.title)).toBeVisible()
    if (s.title === name) return
    if (s.id === STEPS[0].id) await page.getByRole('button', { name: 'Continue', exact: true }).click()
    else await skip(page)
  }
}

async function saveKeyRow(page, v) {
  const row = wizard(page).locator(`tr[data-var="${v}"]`)
  await row.getByRole('button', { name: 'Set', exact: true }).click()
  const box = wizard(page).locator('tr[data-role="replace"]')
  await box.getByRole('textbox').fill(KEY)
  await box.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(row.getByRole('button', { name: 'Replace', exact: true })).toBeVisible()
}

test('Intelligence on Hermes: key and URL save through their routes; Test answers from the saved config', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, INTEL.title)
  await expect(wizard(page).getByText('No intelligence picked yet', { exact: false })).toBeVisible()
  await expect(wizard(page).locator('#keys')).toHaveCount(0)

  await page.getByLabel('Intelligence provider').selectOption('hermes')
  await expect.poll(() => keyVars(page)).toEqual(['LVI_HERMES_KEY'])
  await expect(wizard(page).locator('#key-add')).toHaveCount(0)
  await page.getByLabel('Intelligence URL').fill(HERMES_URL)
  const noKey = wizard(page).getByRole('button', { name: /^No Hermes key/ })
  await expect(noKey).toBeVisible()
  await noKey.click()
  await expect(wizard(page).locator('#keys')).toBeInViewport()
  await expect(page).toHaveURL(/#setup$/)
  expect(await duplicateIds(page)).toEqual([])
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  await saveKeyRow(page, 'LVI_HERMES_KEY')
  await expect(noKey).toHaveCount(0)
  const agent = fake.state[`${API}/intelligence/lanes`].lanes.find(l => l.kind === 'hermes').default_agent
  await expect(page.getByLabel('Intelligence agent')).toHaveValue(agent)
  await saveAndContinue(page)
  await expect(title(page, WAKE.title)).toBeVisible()
  expect(saves(fake)).toEqual([
    ['PUT', '/intelligence/key', { kind: 'hermes', api_key: KEY }],
    ['PUT', '/intelligence', { kind: 'hermes', url: HERMES_URL, agent }],
  ])

  // Test reads what was saved: back on the step, it answers.
  await page.getByRole('button', { name: 'Back', exact: true }).click()
  await expect(title(page, INTEL.title)).toBeVisible()
  await expect(page.getByLabel('Intelligence URL')).toHaveValue(HERMES_URL)
  await wizard(page).getByRole('button', { name: 'Test saved config' }).click()
  await expect(page.getByText(FAKE_REPLY, { exact: false })).toBeVisible()
  expect(fake.writes().filter(w => w.method === 'POST' && w.path === `${API}/intelligence/test`)).toHaveLength(1)
  expect(await page.evaluate(() => document.body.innerText)).not.toContain(KEY)
  for (const r of fake.requests) expect(r.url).not.toContain(KEY)
})

test('Intelligence on OpenClaw, first setup: the URL-only save keeps the step open; the next save switches', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, INTEL.title)
  await page.getByLabel('Intelligence provider').selectOption('openclaw')
  await expect.poll(() => keyVars(page)).toEqual(['LVI_OPENCLAW_KEY'])
  await page.getByLabel('Intelligence URL').fill(OPENCLAW_URL)
  await saveKeyRow(page, 'LVI_OPENCLAW_KEY')
  await saveAndContinue(page)
  await expect(page.getByText('pick an agent, then Save again', { exact: false })).toBeVisible()
  await expect(title(page, INTEL.title)).toBeVisible()

  await expect(page.getByLabel('Intelligence agent')).toHaveValue(FAKE_AGENT)
  await saveAndContinue(page)
  await expect(title(page, WAKE.title)).toBeVisible()
  expect(saves(fake)).toEqual([
    ['PUT', '/intelligence/key', { kind: 'openclaw', api_key: KEY }],
    ['PUT', '/intelligence', { kind: 'openclaw', url: OPENCLAW_URL }],
    ['PUT', '/intelligence', { kind: 'openclaw', url: OPENCLAW_URL, agent: FAKE_AGENT }],
  ])
})

test('Intelligence "Not yet": no write, the echo note shows, and Finish lists the step as skipped', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, INTEL.title)
  await expect(wizard(page).getByText('No intelligence picked yet', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'Not yet', exact: true }).click()
  await expect(title(page, WAKE.title)).toBeVisible()
  while (!(await title(page, FINISH.title).isVisible())) await skip(page)

  await expect(summaryRow(page, INTEL)).toContainText(INTEL.title)
  await expect(summaryRow(page, INTEL)).toContainText('skipped')
  await expect(summaryRow(page, LISTENING)).toContainText('done')
  await summaryRow(page, INTEL).getByRole('button').click()
  await expect(title(page, INTEL.title)).toBeVisible()
  expect(fake.saves()).toEqual([])
  expect(fake.writes()).toEqual([])
})

test('Wake word: the stock model is in use with its licence; an upload and a wake name save through their routes', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, WAKE.title)
  const stock = fake.state[`${API}/oww/models`].models.find(m => m.selected)
  const model = page.getByLabel('Wake word model')
  await expect(model).toHaveValue(stock.path)
  const row = wizard(page).locator('tbody tr', { hasText: stock.name })
  await expect(row).toContainText(stock.license)
  await expect(row).toContainText('in use')
  expect(await duplicateIds(page)).toEqual([])
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  await page.getByLabel('Wake name').fill('Test Den')
  await wizard(page).locator('input[type="file"]').setInputFiles(UPLOAD)
  await wizard(page).getByRole('button', { name: 'Upload', exact: true }).click()
  await expect(model.locator(`option[value="models/${UPLOAD.name}"]`)).toHaveCount(1)
  await model.selectOption(`models/${UPLOAD.name}`)
  await saveAndContinue(page)
  await expect(title(page, AFTER_WAKE.title)).toBeVisible()
  expect(saves(fake)).toEqual([
    ['POST', '/oww/models', { multipart: true, filename: UPLOAD.name }],
    ['PUT', '/wake', { name: 'Test Den' }],
    ['PUT', '/oww/select', { model: UPLOAD.name, source: 'models' }],
  ])
})
