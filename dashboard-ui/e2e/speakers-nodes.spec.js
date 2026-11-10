// Setup steps 6 and 7, Speakers and Nodes: the zone list and the node cards
// as they are, saved through their own routes. The Nodes step reads the probe
// every 5 s while it is open, so a node that connects shows up by itself.
import { test, expect, FAKE_PLAYERS } from './fakeApi.js'
import { STEPS } from '../src/setupWizard.js'

const API = '/api/dashboard'
const PROBE = `${API}/setup/probe`
const SPEAKERS = STEPS.find(s => s.id === 'speakers')
const NODES = STEPS.find(s => s.id === 'nodes')
const AFTER_NODES = STEPS[STEPS.indexOf(NODES) + 1]
const WIIM = { name: 'Room 2', ip: '192.0.2.41' }
const DESKTOP_ONLY = 'Node setup needs Chrome or Edge on a desktop computer.'

const wizard = page => page.locator('[data-wizard]')
const title = (page, text) => page.getByRole('heading', { level: 1, name: text, exact: true })
const zonesHeadings = page => page.getByRole('heading', { level: 2, name: 'Zones', exact: true })
const writes = fake => fake.writes().map(w => [w.method, w.path.slice(API.length), w.body])
const sideScroll = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
const duplicateIds = page => page.evaluate(() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id)
  return ids.filter((id, i) => ids.indexOf(id) !== i)
})

async function openStep(page, name) {
  await page.goto('/dashboard')
  for (const s of STEPS) {
    await expect(title(page, s.title)).toBeVisible()
    if (s.title === name) return
    if (s.id === STEPS[0].id) await page.getByRole('button', { name: 'Continue', exact: true }).click()
    else await page.getByRole('button', { name: /^(Skip|Not yet)$/ }).click()
  }
}

async function addWiim(page) {
  await page.getByLabel('Zone name').fill(WIIM.name)
  await page.getByLabel('Zone backend').selectOption('wiim')
  await expect(page.getByLabel('Sonos player UID')).toHaveCount(0)
  await page.getByLabel('Zone IP address').fill(WIIM.ip)
  await page.getByRole('button', { name: 'Add zone', exact: true }).click()
  await expect(wizard(page).locator('tbody tr', { hasText: WIIM.ip })).toContainText('wiim')
}

test('Speakers: Sonos discovery fills the add form, Test plays through its route, a WiiM is added by address', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, SPEAKERS.title)
  await expect(zonesHeadings(page)).toHaveCount(1)
  await expect(wizard(page).getByText('add a WiiM by its IP address', { exact: false })).toBeVisible()

  await page.getByRole('button', { name: 'Discover Sonos on LAN', exact: true }).click()
  for (const p of FAKE_PLAYERS) await expect(wizard(page).getByText(p.ip, { exact: true })).toBeVisible()
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)
  const [first] = FAKE_PLAYERS
  await wizard(page).getByRole('button', { name: 'Fill into add form' }).first().click()
  await expect(page.getByLabel('Zone name')).toHaveValue(first.name)
  await expect(page.getByLabel('Zone IP address')).toHaveValue(first.ip)
  await page.getByRole('button', { name: 'Add zone', exact: true }).click()
  const row = wizard(page).locator('tbody tr', { hasText: first.ip })
  await expect(row).toContainText('sonos')
  await row.getByRole('button', { name: 'Play test clip' }).click()
  await expect(page.getByText(`Test clip playing on ${first.name}.`, { exact: false })).toBeVisible()

  await addWiim(page)
  expect(await duplicateIds(page)).toEqual([])
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)
  expect(writes(fake)).toEqual([
    ['POST', '/zones/discover', {}],
    ['POST', '/zones', { name: first.name, backend: 'sonos', ip: first.ip, player_uid: first.uid }],
    ['POST', `/zones/${encodeURIComponent(first.name)}/test`, {}],
    ['POST', '/zones', { name: WIIM.name, backend: 'wiim', ip: WIIM.ip }],
  ])
  // Zone actions apply at once: moving on has nothing left to save.
  await page.getByRole('button', { name: /^(Save and continue|Continue)$/ }).click()
  await expect(title(page, NODES.title)).toBeVisible()
  expect(fake.writes()).toHaveLength(4)
})

test('Nodes: the probe is read every 5 s while the step is open, a new node shows up without a reload, the reads stop on leaving', async ({ page, fake }) => {
  test.setTimeout(60_000)
  await fake.load('fresh')
  const loads = []
  page.on('load', () => loads.push(page.url()))
  await openStep(page, NODES.title)
  const openedAt = Date.now()
  await expect(wizard(page).getByText('No node has connected yet.', { exact: false })).toBeVisible()
  await expect(zonesHeadings(page)).toHaveCount(0)  // the Speakers step owns the zone list
  // Reads after the step's own read on arrival: one every 5 s.
  const reads = () => fake.gets(PROBE).filter(r => r.at > openedAt).map(r => r.at)
  await expect.poll(() => reads().length, { timeout: 13_000 }).toBeGreaterThanOrEqual(2)
  const [a, b] = reads()
  expect(b - a, 'gap between probe reads').toBeGreaterThan(4000)
  expect(b - a, 'gap between probe reads').toBeLessThan(7000)
  expect(a - openedAt, 'first poll read after opening').toBeLessThan(7000)

  const before = loads.length
  fake.nodeOnline('Room 1')
  await expect(wizard(page).getByText('1 node online', { exact: false })).toBeVisible({ timeout: 8000 })
  await expect(wizard(page).locator('#rooms tbody tr', { hasText: 'Room 1' })).toBeVisible({ timeout: 8000 })
  await expect(wizard(page).locator('[data-role="node-state"]')).toHaveText('online')
  expect(loads.length).toBe(before)
  expect(await duplicateIds(page)).toEqual([])
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  await page.getByRole('button', { name: 'Back', exact: true }).click()
  await expect(title(page, SPEAKERS.title)).toBeVisible()
  await page.waitForTimeout(1000)  // the Speakers step's own read on arrival
  const settled = fake.gets(PROBE).length
  await page.waitForTimeout(6500)
  expect(fake.gets(PROBE).length, 'probe reads after leaving the Nodes step').toBe(settled)
  expect(fake.saves()).toEqual([])
})

test('Nodes: picking a zone for a room saves PUT /mapping with room_zones wrapped', async ({ page, fake }) => {
  await fake.load('fresh')
  await openStep(page, SPEAKERS.title)
  await addWiim(page)
  await page.getByRole('button', { name: /^(Save and continue|Continue)$/ }).click()
  await expect(title(page, NODES.title)).toBeVisible()
  fake.nodeOnline('Room 1')
  const pick = page.getByLabel('Zone for Room 1')
  await expect(pick).toBeVisible({ timeout: 8000 })
  await pick.selectOption(WIIM.name)
  await page.getByRole('button', { name: 'Save and continue', exact: true }).click()
  await expect(title(page, AFTER_NODES.title)).toBeVisible()
  const put = writes(fake).filter(([m, p]) => m === 'PUT' && p === '/mapping')
  expect(put).toEqual([['PUT', '/mapping', { room_zones: { 'Room 1': WIIM.name } }]])
  expect(fake.state[`${API}/mapping`].room_zones).toEqual({ 'Room 1': WIIM.name })
})

test('Nodes: USB setup on a desktop; a phone gets the desktop-only message and Skip moves on', async ({ page, fake }, info) => {
  await fake.load('fresh')
  await openStep(page, NODES.title)
  if (info.project.name.startsWith('phone-')) {
    await expect(wizard(page).getByText(DESKTOP_ONLY, { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Connect', exact: true })).toHaveCount(0)
    expect(await sideScroll(page)).toBeLessThanOrEqual(0)
    await page.getByRole('button', { name: 'Skip', exact: true }).click()
    await expect(title(page, AFTER_NODES.title)).toBeVisible()
  } else {
    await expect(page.getByRole('button', { name: 'Connect', exact: true })).toBeVisible()
    await expect(wizard(page).getByText(DESKTOP_ONLY, { exact: true })).toHaveCount(0)
  }
  expect(fake.writes()).toEqual([])
})
