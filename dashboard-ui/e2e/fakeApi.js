// The dashboard API, faked for the browser harness. GETs answer from a fixture
// that tests/setup_wizard_test.py --dump-fixtures generated from the real
// routes; allowed writes update that state so the probe follows them. Every
// request is recorded, and a test fails on any write outside the wizard's
// allowlist or any GET the fixture doesn't cover.
import { test as base, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { STEPS } from '../src/setupWizard.js'

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

// POSTs that change nothing on the bot: they only look (or test).
const LOOKS = ['POST /intelligence/agents', 'POST /intelligence/test', 'POST /localai/test', 'POST /keys/test',
  'POST /setup/check']

// Made-up answers for the fake bot's outbound looks (LocalAI's models, an agent, a reply).
export const FAKE_LOCALAI = { stt: 'e2e-stt-model', tts: { id: 'e2e-voice-model', backend: 'kokoro' } }
export const FAKE_AGENT = 'e2e-agent'
export const FAKE_REPLY = 'e2e reply text'
// The Intelligence step's id is the server's; specs and the fake reach it by title.
const STEPS_INTEL_ID = STEPS.find(s => s.title === 'Intelligence').id

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
  // Writes that change something: every write but the POSTs that only look
  // (agent discovery, tests, the token check).
  saves() { return this.writes().filter(r => !LOOKS.includes(`${r.method} ${r.path.slice(API.length)}`)) }

  async handle(route) {
    const req = route.request()
    const url = new URL(req.url())
    const method = req.method()
    const path = url.pathname
    let body = null
    if (/^multipart\//.test(req.headers()['content-type'] || '')) {
      // An upload: keep the file's name, not its bytes.
      const name = /filename="([^"]*)"/.exec(req.postDataBuffer()?.toString('latin1') || '')?.[1] ?? null
      body = { multipart: true, filename: name }
    } else {
      try { body = req.postDataJSON() } catch { body = req.postData() }
    }
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
    const handler = this.routes()[`${method} ${path.replace(/^\/oww\/models\/[^/]+$/, '/oww/models/*')}`]
    if (!handler) return reply(200, { ok: true })
    const [status, json] = handler(body || {})
    this.sync()
    return reply(status, json)
  }

  get(p) { return this.state[`${API}${p}`] }

  keyRow(v) { return (this.get('/keys')?.keys || []).find(r => r.var === v) }
  keySet(v) { return !!this.keyRow(v)?.set }
  saveKey(v, value) {
    const row = this.keyRow(v)
    if (row) Object.assign(row, { set: true, hint: value.slice(0, 4), pending_restart: !!row.in_use })
    return row
  }

  // What each allowed write does to the fake bot, after the real route: a
  // key fills its row; settings land where the GETs read them. [status, body].
  routes() {
    const key = (v, extra = {}) => b => {
      if (typeof b.api_key !== 'string' || b.api_key.length < 8) return [422, { detail: 'api_key: too short' }]
      this.saveKey(v, b.api_key)
      return [200, { ok: true, api_key_set: true, ...extra }]
    }
    const withScheme = u => (/^https?:\/\//.test(u) ? u : `http://${u}`)
    const intel = () => this.get('/intelligence')
    const laneKeyVar = (kind, agent) => (this.get('/keys')?.keys || [])
      .find(r => r.kind === kind && (!agent || !r.agent || r.agent === agent))?.var
    return {
      'PUT /tts/key/elevenlabs': key('ELEVENLABS_API_KEY'),
      'PUT /tts/key/deepgram': key('DEEPGRAM_API_KEY'),
      'PUT /localai/key': key('LVI_LOCALAI_KEY', { var: 'LVI_LOCALAI_KEY' }),
      'PUT /localai': b => {
        const la = this.get('/localai')
        for (const f of ['url', 'model_stt', 'model_tts', 'voice']) {
          if (b[f] === undefined || b[f] === null) continue
          const v = f === 'url' && b[f] ? withScheme(b[f]) : b[f]
          if (f in la) la[f] = v || (f === 'url' ? null : '')
          if (f === 'model_tts') this.get('/tts').localai_model_tts = v
          if (f === 'voice') this.get('/tts').localai_voice = v
        }
        return [200, { ok: true, url: la.url, model_stt: la.model_stt }]
      },
      'POST /localai/test': () => {
        const la = this.get('/localai')
        if (!la.url) return [200, { ok: false, ms: null, reason: 'Set the LocalAI URL first', stt: [], tts: [] }]
        if (!this.keySet('LVI_LOCALAI_KEY')) return [200, { ok: false, ms: null, reason: 'Add the LocalAI key first', stt: [], tts: [] }]
        return [200, { ok: true, ms: 12.5, reason: null, stt: [FAKE_LOCALAI.stt], tts: [FAKE_LOCALAI.tts] }]
      },
      'PUT /stt': b => {
        const stt = this.get('/stt')
        const p = stt.providers.find(x => x.name === b.provider)
        if (b.provider !== undefined && (!p || !p.available)) return [422, { detail: `${b.provider}: ${p?.reason || 'unknown provider'}` }]
        if (b.provider !== undefined) stt.current.provider = b.provider
        if (b.language !== undefined) stt.current.language = b.language
        return [200, { ok: true, restart_needed: b.provider !== undefined, key_needed: false, current: stt.current }]
      },
      'PUT /tts': b => {
        const tts = this.get('/tts')
        if (b.provider === 'elevenlabs' && !b.voice_id) return [422, { detail: 'voice_id required for elevenlabs' }]
        for (const [k, v] of Object.entries(b)) if (k in tts || k === 'provider') tts[k] = v
        return [200, { ok: true }]
      },
      'PUT /intelligence/key': b => {
        const v = laneKeyVar(b.kind, b.agent)
        if (!v) return [422, { detail: `kind: unknown lane ${b.kind}` }]
        return key(v, { key_set: true, var: v, kind: b.kind })(b)
      },
      'PUT /intelligence': b => {
        const cur = intel()
        const lane = (this.get('/intelligence/lanes')?.lanes || []).find(l => l.kind === b.kind)
        if (!lane) return [422, { detail: `kind: unknown lane ${b.kind}` }]
        const url = b.url ? withScheme(b.url).replace(/\/?$/, '').replace(/(\/v1)?$/, '/v1') : null
        // A lane that lists its agents only once its URL is saved (no default
        // agent): its first save keeps just the URL, as the route does.
        if (lane.agent_required && lane.default_agent === null && b.agent === undefined && url && !cur.agents[b.kind]) {
          cur.urls = { ...cur.urls, [b.kind]: url }
          return [200, { ok: true, kind: cur.kind, url_only: true }]
        }
        if (!url && !cur.urls[b.kind]) return [422, { detail: `set a URL for ${b.kind} first` }]
        cur.kind = b.kind
        cur.url = url || cur.urls[b.kind]
        cur.urls = { ...cur.urls, [b.kind]: cur.url }
        if (b.agent !== undefined) cur.agents = { ...cur.agents, [b.kind]: b.agent || null }
        return [200, { ok: true, kind: b.kind }]
      },
      'POST /intelligence/agents': b => {
        const cur = intel()
        const lane = (this.get('/intelligence/lanes')?.lanes || []).find(l => l.kind === b.kind)
        const saved = cur.urls[b.kind] || (cur.kind === b.kind ? cur.url : null)
        const keyed = !!cur.keys_set[b.kind]
        const agents = lane?.default_agent ? [{ id: lane.default_agent, key_set: keyed }]
          : saved ? [{ id: FAKE_AGENT, key_set: keyed }] : []
        return [200, { agents, note: agents.length ? null : 'Save the URL first: agents are listed from the saved address.',
                       selected: cur.agents[b.kind] || lane?.default_agent || null }]
      },
      'POST /intelligence/test': () => {
        const cur = intel()
        if (!cur.url) return [200, { ok: false, error: 'no url configured' }]
        if (!cur.keys_set[cur.kind]) return [200, { ok: false, error: 'no key set (save the key first)' }]
        return [200, { ok: true, latency_ms: 42, model: cur.agents[cur.kind] || cur.kind, reply: FAKE_REPLY }]
      },
      'POST /oww/models': b => {
        const name = b.filename || ''
        if (!/^[A-Za-z0-9_.-]+\.onnx$/.test(name)) return [400, { detail: 'filename must be letters/digits/_-. and end .onnx' }]
        const models = this.get('/oww/models').models
        if (!models.some(m => m.name === name && m.source === 'models')) {
          models.push({ name, path: `models/${name}`, source: 'models', selected: false, size: 2048 })
        }
        return [200, { ok: true, name, size: 2048 }]
      },
      'PUT /oww/select': b => {
        const models = this.get('/oww/models').models
        const row = models.find(m => m.name === b.model && (!b.source || m.source === b.source))
        if (!row) return [404, { detail: `model '${b.model}' not found` }]
        for (const m of models) m.selected = m === row
        return [200, { ok: true, model: row.source === 'stock' ? row.name : row.path, note: 'applies on next bot restart' }]
      },
      'PUT /wake': b => {
        if (typeof b.name === 'string') this.get('/wake').name = b.name.trim().split(/\s+/).filter(Boolean).join(' ')
        return [200, { ok: true, name: this.get('/wake').name }]
      },
    }
  }

  // The GETs' derived fields after a write, by the routes' own rules: a
  // provider is pickable once its setup and key are saved; the probe's steps
  // follow the saved picks.
  sync() {
    const stt = this.get('/stt'), tts = this.get('/tts'), la = this.get('/localai'), cur = this.get('/intelligence')
    const laOk = !!la?.url && this.keySet('LVI_LOCALAI_KEY')
    if (la) la.key_set = this.keySet('LVI_LOCALAI_KEY')
    const open = (p, ok) => {
      if (p.reason === 'not installed' || p.reason === 'model files missing') return
      p.available = ok
      if (ok) delete p.reason
    }
    const KEY = { deepgram: 'DEEPGRAM_API_KEY', 'deepgram-flux': 'DEEPGRAM_API_KEY', elevenlabs: 'ELEVENLABS_API_KEY' }
    for (const p of [...(stt?.providers || []), ...(tts?.providers || [])]) {
      if (p.name === 'localai') open(p, laOk)
      else if (KEY[p.name]) open(p, this.keySet(KEY[p.name]))
    }
    if (stt) {
      stt.deepgram_key_set = this.keySet('DEEPGRAM_API_KEY')
      stt.elevenlabs_key_set = this.keySet('ELEVENLABS_API_KEY')
    }
    if (tts) {
      tts.deepgram_key_hint = this.keyRow('DEEPGRAM_API_KEY')?.hint ?? null
      tts.elevenlabs_key_hint = this.keyRow('ELEVENLABS_API_KEY')?.hint ?? null
      tts.localai_key_hint = this.keyRow('LVI_LOCALAI_KEY')?.hint ?? null
    }
    if (cur) {
      for (const k of Object.keys(cur.keys_set)) {
        const row = (this.get('/keys')?.keys || []).find(r => r.kind === k)
        cur.keys_set[k] = !!row?.set
      }
      cur.key_set = !!cur.keys_set[cur.kind]
    }
    const step = id => this.probe?.steps.find(s => s.id === id)
    const speech = (id, list, name) => {
      const s = step(id), p = (list || []).find(x => x.name === name)
      if (s && p) Object.assign(s, { done: !!p.available, detail: p.label, tier: p.tier })
    }
    speech('listening', stt?.providers, stt?.current.provider)
    speech('voice', tts?.providers, tts?.provider)
    const intelStep = this.probe?.steps.find(s => s.id === STEPS_INTEL_ID)
    if (intelStep && cur) {
      const lane = (this.get('/intelligence/lanes')?.lanes || []).find(l => l.kind === cur.kind)
      Object.assign(intelStep, { done: !!lane && !!cur.url && !!cur.keys_set[cur.kind], detail: lane ? lane.label : 'echo' })
    }
    const wake = step('wake'), sel = (this.get('/oww/models')?.models || []).find(m => m.selected)
    if (wake && sel) Object.assign(wake, { done: true, detail: sel.source === 'stock' ? 'stock model' : sel.name })
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
