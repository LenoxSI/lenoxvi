// The dashboard API, faked for the browser harness. GETs answer from a fixture
// that tests/setup_wizard_test.py --dump-fixtures generated from the real
// routes; allowed writes update that state so the probe follows them. Every
// request is recorded, and a test fails on any write outside the wizard's
// allowlist or any GET the fixture doesn't cover.
import { test as base, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

const API = '/api/dashboard'

// Every route the setup wizard may write through (existing card routes plus
// the two /setup/* writes). Anything else that mutates fails the test.
export const ALLOWED_WRITES = [
  ['PUT', /^\/(stt|tts|localai|intelligence|wake|mapping)$/],
  ['PUT', /^\/(tts\/key\/(elevenlabs|deepgram)|localai\/key|intelligence\/key)$/],
  ['POST', /^\/(intelligence\/(agents|test)|localai\/test|keys\/test)$/],
  ['POST', /^\/oww\/models$/], ['PUT', /^\/oww\/select$/], ['DELETE', /^\/oww\/models\/[^/]+$/],
  ['POST', /^\/zones(\/discover|\/[^/]+\/test)?$/], ['DELETE', /^\/zones\/[^/]+$/], ['PUT', /^\/zones\/[^/]+\/volume$/],
  ['POST', /^\/nodes\/[^/]+\/[\w-]+$/], ['POST', /^\/node-speaker\/test$/], ['PUT', /^\/node-speaker\/volume$/],
  ['POST', /^\/restart$/],
  ['POST', /^\/setup\/(check|done)$/],
]

const allowed = (method, path) =>
  path.startsWith(`${API}/`) && ALLOWED_WRITES.some(([m, re]) => m === method && re.test(path.slice(API.length)))

const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), 'utf8'))

export class FakeApi {
  constructor(page) {
    this.page = page
    this.requests = []  // { method, path, url, body, status }
    this.violations = []
    this.unknownGets = []
  }

  // Seed from a fixture; `token` puts the fake bot in token mode (writes
  // without the right X-Dashboard-Token answer 401, as the guard does).
  async load(name, { token = null } = {}) {
    this.state = structuredClone(fixture(name).responses)
    this.token = token
    await this.page.route('**/api/**', route => this.handle(route))
  }

  get probe() { return this.state[`${API}/setup/probe`] }

  writes() { return this.requests.filter(r => r.method !== 'GET' && r.method !== 'HEAD') }
  gets(path) { return this.requests.filter(r => r.method === 'GET' && r.path === path) }

  async handle(route) {
    const req = route.request()
    const url = new URL(req.url())
    const method = req.method()
    const path = url.pathname
    let body = null
    try { body = req.postDataJSON() } catch { body = req.postData() }
    const rec = { method, path, url: req.url(), body, status: 0 }
    this.requests.push(rec)
    const reply = (status, json) => { rec.status = status; return route.fulfill({ status, json }) }

    if (method === 'GET' || method === 'HEAD') {
      if (path in this.state) return reply(200, this.state[path])
      this.unknownGets.push(path)
      return reply(404, { detail: 'not in the e2e fixture' })
    }
    if (!allowed(method, path)) {
      this.violations.push(`${method} ${path}`)
      return reply(403, { detail: 'not on the setup wizard write allowlist' })
    }
    if (this.token && req.headers()['x-dashboard-token'] !== this.token) {
      return reply(401, { detail: 'dashboard: invalid or missing token' })
    }
    return this.write(method, path.slice(API.length), body, reply)
  }

  write(method, path, body, reply) {
    if (path === '/setup/check') return reply(200, { ok: true })
    if (path === '/setup/done') {
      const extra = Object.keys(body || {}).filter(k => k !== 'skipped')
      if (extra.length || (body?.skipped !== undefined && typeof body.skipped !== 'boolean')) {
        return reply(422, { detail: extra.map(k => ({ loc: ['body', k], msg: 'Extra inputs are not permitted' })) })
      }
      this.probe.pending = false
      const finish = this.probe.steps.find(s => s.id === 'finish')
      if (finish) finish.done = true
      return reply(200, { ok: true, pending: false })
    }
    if (path === '/intelligence/agents') return reply(200, { agents: [] })
    return reply(200, { ok: true })
  }

  expectOnlyAllowedWrites() {
    expect(this.violations, 'writes outside the setup wizard allowlist').toEqual([])
  }

  expectFixtureCoversGets() {
    expect([...new Set(this.unknownGets)], 'GETs the e2e fixture does not answer').toEqual([])
  }
}

// `fake` in a test: the page's API, checked against the allowlist after
// every test. (`provide` is Playwright's fixture callback, usually named `use`.)
export const test = base.extend({
  fake: async ({ page }, provide) => {
    const fake = new FakeApi(page)
    await provide(fake)
    fake.expectOnlyAllowedWrites()
    fake.expectFixtureCoversGets()
  },
})

export { expect }
