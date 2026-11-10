// The setup wizard's shell: when it opens by itself, that it replaces the
// tabs, the Welcome token check, Skip setup and the System tab's way back in.
// Runs on every project (desktop Chromium, Android Chromium, iPhone WebKit).
import { test, expect } from './fakeApi.js'
import { STEPS } from '../src/setupWizard.js'

const TOKEN_KEY = 'lenoxvi-dashboard-token'
const RIGHT = 'e2e-right-token-4d1c'
const PROBE = '/api/dashboard/setup/probe'

const wizard = page => page.locator('[data-wizard]')
// The wizard's own field (TokenDialog, the fallback prompt, shares the label).
const tokenField = page => wizard(page).getByLabel('Dashboard token')
const title = (page, text) => page.getByRole('heading', { level: 1, name: text, exact: true })
const tabsNav = page => page.getByRole('navigation', { name: 'Tabs' })
const stored = page => page.evaluate(k => localStorage.getItem(k), TOKEN_KEY)
const duplicateIds = page => page.evaluate(() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id)
  return ids.filter((id, i) => ids.indexOf(id) !== i)
})
const sideScroll = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)

test('upgraded install (no marker): the dashboard, never the wizard; System offers it again', async ({ page, fake }) => {
  await fake.load('upgraded')
  await page.goto('/dashboard')
  await expect(page.locator('[data-tab="rooms"]')).toBeVisible()
  await expect.poll(() => fake.gets(PROBE).length).toBeGreaterThan(0)
  await expect(wizard(page)).toHaveCount(0)
  await page.waitForTimeout(6000)  // through a poll cycle
  await expect(wizard(page)).toHaveCount(0)
  expect(new URL(page.url()).hash).toBe('')

  await tabsNav(page).getByRole('link', { name: 'System' }).click()
  const again = page.getByRole('link', { name: 'Run setup again' })
  await expect(again).toBeVisible()
  await expect(again).toHaveAttribute('href', '#setup')
  await again.click()
  await expect(wizard(page)).toBeVisible()
  await expect(page.locator('[data-tab]')).toHaveCount(0)
  expect(fake.writes().filter(w => w.path.startsWith('/api/dashboard/setup/'))).toEqual([])
})

test('Run setup again with unsaved edits asks first: Cancel keeps them on the tab, OK opens setup', async ({ page, fake }) => {
  await fake.load('upgraded')
  await page.goto('/dashboard#system')
  const address = page.getByLabel('LocalAI address')
  await expect(address).toBeEnabled()
  await address.fill('192.0.2.51:8080')
  const bar = page.getByRole('region', { name: 'Unsaved changes' })
  await expect(bar).toContainText('1 unsaved')

  const asked = []
  page.once('dialog', d => { asked.push(d.message()); d.dismiss() })
  await page.getByRole('link', { name: 'Run setup again' }).click()
  await expect.poll(() => asked.length).toBe(1)
  expect(asked[0]).toBe('Discard 1 unsaved change and open setup?')
  await expect(page).toHaveURL(/#system$/)
  await expect(wizard(page)).toHaveCount(0)
  await expect(address).toHaveValue('192.0.2.51:8080')
  await expect(bar).toContainText('1 unsaved')

  page.once('dialog', d => { asked.push(d.message()); d.accept() })
  await page.getByRole('link', { name: 'Run setup again' }).click()
  await expect(wizard(page)).toBeVisible()
  await expect(page).toHaveURL(/#setup$/)
  expect(asked).toHaveLength(2)
  await expect(bar).toHaveCount(0)
  expect(fake.saves()).toEqual([])  // the System tab's agent discovery only looks
})

test('fresh install: lands on #setup, Welcome shows the host, the tabs are not in the DOM', async ({ page, fake }) => {
  await fake.load('fresh')
  await page.goto('/dashboard')
  await expect(page).toHaveURL(/\/dashboard#setup$/)
  await expect(wizard(page)).toBeVisible()
  await expect(title(page, 'Welcome')).toBeVisible()
  await expect(page.locator('[data-tab]')).toHaveCount(0)
  await expect(tabsNav(page)).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^Restart/ })).toHaveCount(0)
  await expect(wizard(page).getByText('8 cores')).toBeVisible()
  await expect(tokenField(page)).toHaveCount(0)
  expect(await duplicateIds(page)).toEqual([])
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  await page.reload()  // the hash keeps a reload in the wizard
  await expect(title(page, 'Welcome')).toBeVisible()
  await expect(page).toHaveURL(/#setup$/)
})

test('the frame walks all eight steps; Back, Skip and Continue write nothing', async ({ page, fake }) => {
  await fake.load('fresh')
  await page.goto('/dashboard')
  for (const [i, s] of STEPS.entries()) {
    await expect(title(page, s.title)).toBeVisible()
    await expect(wizard(page).locator('h1 + p')).toHaveText(/\w{3,}/)  // the step's plain-words line
    await expect(page.getByText(`Step ${i + 1} of ${STEPS.length}`, { exact: true })).toBeVisible()
    // Intelligence says "Not yet" for its Skip.
    await expect(page.getByRole('button', { name: /^(Skip|Not yet)$/ })).toHaveCount(s.skippable && i < STEPS.length - 1 ? 1 : 0)
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(i ? 1 : 0)
    expect(await duplicateIds(page)).toEqual([])
    expect(await sideScroll(page)).toBeLessThanOrEqual(0)
    // Steps that host cards save them on the way ("Save and continue").
    if (i < STEPS.length - 1) await page.getByRole('button', { name: /^(Save and continue|Continue)$/ }).click()
  }
  await page.getByRole('button', { name: 'Back', exact: true }).click()
  await expect(title(page, STEPS.at(-2).title)).toBeVisible()
  await page.getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(title(page, STEPS.at(-1).title)).toBeVisible()
  await expect(page).toHaveURL(/#setup$/)
  expect(fake.writes()).toEqual([])
})

test('token box: a masked field; a wrong token stores nothing; the right one is stored, never in a URL', async ({ page, fake }) => {
  await fake.load('token', { token: RIGHT })
  const urls = []
  page.on('framenavigated', f => { if (f === page.mainFrame()) urls.push(f.url()) })
  await page.goto('/dashboard')
  const field = tokenField(page)
  await expect(field).toHaveAttribute('type', 'password')
  await expect(field).toHaveAttribute('autocomplete', 'off')
  expect(await sideScroll(page)).toBeLessThanOrEqual(0)

  await field.fill('not-the-token')
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(page.getByText("That token didn't work.")).toBeVisible()
  await expect(title(page, 'Welcome')).toBeVisible()
  expect(await stored(page)).toBeNull()
  urls.push(page.url())

  await field.fill(RIGHT)
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(title(page, STEPS[1].title)).toBeVisible()
  expect(await stored(page)).toBe(RIGHT)
  urls.push(page.url())
  await page.reload()
  urls.push(page.url())

  for (const u of urls) expect(u).not.toContain(RIGHT)
  for (const r of fake.requests) expect(r.url).not.toContain(RIGHT)
  const checks = fake.writes().filter(w => w.path === '/api/dashboard/setup/check')
  expect(checks.slice(0, 2).map(c => c.status)).toEqual([401, 200])
  await expect(page.getByRole('dialog')).toHaveCount(0)  // the wizard asked, not the fallback prompt
})

test('token box with the token already stored: checked quietly, no field', async ({ page, fake }) => {
  await fake.load('token', { token: RIGHT })
  await page.addInitScript(([k, v]) => localStorage.setItem(k, v), [TOKEN_KEY, RIGHT])
  await page.goto('/dashboard')
  await expect(title(page, 'Welcome')).toBeVisible()
  await expect.poll(() => fake.writes().filter(w => w.path === '/api/dashboard/setup/check').map(c => c.status))
    .toEqual([200])
  await expect(tokenField(page)).toHaveCount(0)
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(title(page, STEPS[1].title)).toBeVisible()
})

test('token box with a stale stored token: the field asks for the new one', async ({ page, fake }) => {
  await fake.load('token', { token: RIGHT })
  await page.addInitScript(([k]) => { if (!sessionStorage.getItem('seeded')) { localStorage.setItem(k, 'stale-token'); sessionStorage.setItem('seeded', '1') } }, [TOKEN_KEY])
  await page.goto('/dashboard')
  const field = tokenField(page)
  await expect(field).toBeVisible()
  await field.fill(RIGHT)
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(title(page, STEPS[1].title)).toBeVisible()
  expect(await stored(page)).toBe(RIGHT)
})

test('Skip setup clears the marker and lands on the dashboard; a reload stays there', async ({ page, fake }) => {
  await fake.load('fresh')
  await page.goto('/dashboard')
  await expect(title(page, 'Welcome')).toBeVisible()
  await page.getByRole('button', { name: 'Skip setup' }).click()
  await expect(page).toHaveURL(/\/dashboard#rooms$/)
  await expect(page.locator('[data-tab="rooms"]')).toBeVisible()
  await expect(wizard(page)).toHaveCount(0)
  expect(fake.writes().map(w => [w.method, w.path, w.body]))
    .toContainEqual(['POST', '/api/dashboard/setup/done', { skipped: true }])

  const probes = fake.gets(PROBE).length
  await page.reload()
  await expect(page.locator('[data-tab="rooms"]')).toBeVisible()
  await expect.poll(() => fake.gets(PROBE).length).toBeGreaterThan(probes)
  await page.waitForTimeout(500)
  await expect(wizard(page)).toHaveCount(0)
  await expect(page).toHaveURL(/#rooms$/)
})
