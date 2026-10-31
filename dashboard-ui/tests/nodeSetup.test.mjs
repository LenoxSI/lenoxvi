// Node setup module tests. Run: node --test dashboard-ui/tests/
import test from 'node:test'
import assert from 'node:assert/strict'
import * as ns from '../src/nodeSetup.js'
import { parseReplyLine, validateSettings, hostOfferUrl, checkManifest, isSupported, NodeLink } from '../src/nodeSetup.js'

test('contract: the frozen API is exported', () => {
  for (const name of ['isSupported', 'hostOfferUrl', 'parseReplyLine', 'validateSettings', 'checkManifest', 'NodeLink']) {
    assert.equal(typeof ns[name], 'function', name)
  }
  assert.equal(ns.PROTO, 1)
  assert.deepEqual(ns.SETTINGS_REGION, [0x9000, 0x10000])
  assert.deepEqual(ns.TRACKS, ['array', 'flex', 'waveshare'])
})

test('parseReplyLine finds OH< inside a log line and ignores everything else', () => {
  assert.deepEqual(parseReplyLine('I (12) wifi: connOH<{"cmd":"info","ok":true}'), { cmd: 'info', ok: true })
  assert.equal(parseReplyLine('I (12) wifi: connected'), null)
  assert.equal(parseReplyLine('OH<{broken'), null)
})

test('validateSettings mirrors the firmware rules', () => {
  assert.equal(validateSettings({ name: 'Kitchen_2' }), null)
  assert.match(validateSettings({ name: 'Kit chen' }), /^name:/)
  assert.match(validateSettings({ wifi_ssid: 'x'.repeat(33), wifi_pass: 'p' }), /^wifi_ssid:/)
  assert.match(validateSettings({ wifi_ssid: 'Maple' }), /^wifi_pass:/)
  assert.equal(validateSettings({ wifi_ssid: 'Maple', wifi_pass: 'f'.repeat(64) }), null)
  assert.match(validateSettings({ wifi_ssid: 'Maple', wifi_pass: 'z'.repeat(64) }), /^wifi_pass:/)
  assert.equal(validateSettings({ bot_url: '' }), null)
  assert.match(validateSettings({ bot_url: 'http://192.0.2.10:7860/api/offer' }), /^bot_url:/)
  assert.match(validateSettings({ wifi_ssid: 'Maple', wifi_pass: 'x', bogus: 1 }), /^bogus:/)
})

test('validateSettings: more firmware rules', () => {
  assert.equal(validateSettings({ bot_url: 'https://192.0.2.10:7860/api/offer' }), null)
  assert.match(validateSettings({ bot_url: 'https://192.0.2.10 /api/offer' }), /^bot_url:/)
  assert.match(validateSettings({ bot_url: 'https://192.0.2.10:7860/api/offerx' }), /^bot_url:/)
  assert.match(validateSettings({ bot_url: `https://${'a'.repeat(120)}/api/offer` }), /^bot_url:/)
  assert.match(validateSettings({ wifi_pass: 'x' }), /^wifi_ssid:/)
  assert.match(validateSettings({ wifi_ssid: '', wifi_pass: '' }), /^wifi_ssid:/)
  assert.equal(validateSettings({ wifi_ssid: 'Maple', wifi_pass: '' }), null)
  assert.equal(validateSettings({ wifi_ssid: 'Maple', wifi_pass: 'p'.repeat(63) }), null)
  assert.match(validateSettings({ name: 'K'.repeat(32) }), /^name:/)
  assert.match(validateSettings({ name: 7 }), /^name:/)
})

test('hostOfferUrl', () => {
  assert.equal(hostOfferUrl('https://192.0.2.10:7860'), 'https://192.0.2.10:7860/api/offer')
})

test('isSupported needs Web Serial and a secure context', () => {
  assert.equal(isSupported({ serial: {} }, { isSecureContext: true }), true)
  assert.equal(isSupported({ serial: {} }, { isSecureContext: false }), false)
  assert.equal(isSupported({}, { isSecureContext: true }), false)
  assert.equal(isSupported(undefined, undefined), false)
})

test('checkManifest refuses another line, the settings region, and incomplete sets', () => {
  const ok = { track: 'flex', parts: [0, 0x8000, 0x10000, 0x20000].map((offset, i) =>
    ({ name: ['bootloader', 'partition-table', 'ota-data', 'app'][i], file: `f${i}`, offset, size: 100, sha256: 'a'.repeat(64) })) }
  assert.equal(checkManifest(ok, 'flex'), null)
  assert.match(checkManifest(ok, 'array'), /line/)
  assert.match(checkManifest({ ...ok, parts: ok.parts.slice(0, 3) }, 'flex'), /4 parts|incomplete/)
  const bad = structuredClone(ok); bad.parts[1].offset = 0x9000
  assert.match(checkManifest(bad, 'flex'), /settings/)
  const spill = structuredClone(ok); spill.parts[1].size = 0x1001 // 0x8000 + 0x1001 reaches into 0x9000
  assert.match(checkManifest(spill, 'flex'), /settings/)
  const path = structuredClone(ok); path.parts[2].file = '../config.yaml'
  assert.match(checkManifest(path, 'flex'), /file/)
})

// ── fakes ────────────────────────────────────────────────────────────────

const enc = new TextEncoder()
const reply = (obj) => `\nOH<${JSON.stringify(obj)}\n`
const INFO = {
  cmd: 'info', ok: true, proto: 1, track: 'flex', fw: '1.2.47', mac: 'aa:bb:cc:dd:ee:ff', name: 'Kitchen',
  wifi_ssid: 'Maple', bot_url: 'https://192.0.2.10:7860/api/offer', bot_url_saved: true, link: 'bot', ip: '192.0.2.21', up_s: 5,
}
const tick = (ms = 1) => new Promise((r) => setTimeout(r, ms))

/** A Web Serial-like port with a scripted node: node(req, port) -> raw chunks to emit. */
class FakePort {
  constructor(node) {
    this.node = node
    this.written = [] // raw text the page wrote
    this.requests = [] // parsed OH> requests
    this.opens = 0
    this.isOpen = false
    this.offline = false
    this.readable = null
    this.writable = null
  }
  getInfo() { return { usbVendorId: 0x303a, usbProductId: 0x1001 } }
  async open() {
    if (this.offline) throw new Error('device not found')
    if (this.isOpen) throw new Error('already open')
    this.isOpen = true
    this.opens++
    this.readable = new ReadableStream({ start: (c) => { this._ctl = c } })
    const dec = new TextDecoder()
    let buf = ''
    this.writable = new WritableStream({
      write: (chunk) => {
        const s = dec.decode(chunk, { stream: true })
        this.written.push(s)
        buf += s
        for (let nl = buf.indexOf('\n'); nl >= 0; nl = buf.indexOf('\n')) {
          const line = buf.slice(0, nl)
          buf = buf.slice(nl + 1)
          if (!line.startsWith('OH>')) continue
          const req = JSON.parse(line.slice(3))
          this.requests.push(req)
          this.emit(this.node(req, this) ?? [])
        }
      },
    })
  }
  async close() {
    if (!this.isOpen) throw new Error('not open')
    if (this.readable?.locked || this.writable?.locked) throw new TypeError('streams locked')
    this.isOpen = false
    this.readable = null
    this.writable = null
  }
  /** Each chunk is its own read, so replies arrive split. */
  async emit(chunks) {
    const ctl = this._ctl
    for (const c of chunks) {
      await tick()
      try { ctl.enqueue(enc.encode(c)) } catch { return } // stream gone (reset)
    }
  }
  /** The board resets: the USB device drops, then comes back after `ms`. */
  reenumerate(ms = 150) {
    this.offline = true
    this._ctl.error(new Error('device lost'))
    setTimeout(() => { this.offline = false }, ms)
  }
}

const fakeSerial = (port) => ({
  requestPort: async () => port,
  getPorts: async () => (port.offline ? [] : [port]),
})

async function openLink(node, opts = {}) {
  const port = new FakePort(node)
  const link = new NodeLink(port, { serial: fakeSerial(port), ...opts })
  await link.open()
  return { port, link }
}

/** Answers info with INFO (overrides from `info`), set with ok + the saved list. */
function simpleNode({ info = {}, set } = {}) {
  return (req) => {
    if (req.cmd === 'info') return [reply({ ...INFO, ...info, id: req.id })]
    if (req.cmd === 'set') return [reply({ cmd: 'set', id: req.id, ok: true, saved: Object.keys(req).filter((k) => k !== 'id' && k !== 'cmd'), ...set })]
    return []
  }
}

// ── NodeLink over a FakePort ─────────────────────────────────────────────

test('request opens the picked port with the ESP32-S3 filter', async () => {
  const port = new FakePort(simpleNode())
  let filters
  const serial = { requestPort: async (o) => { filters = o.filters; return port }, getPorts: async () => [port] }
  const link = await NodeLink.request({ serial })
  assert.deepEqual(filters, ns.USB_FILTERS)
  assert.equal(port.isOpen, true)
  assert.equal((await link.info({ timeoutMs: 1000 })).track, 'flex')
  await link.close()
  assert.equal(port.isOpen, false)
})

test('info retries until the node answers (log noise, split chunks, CRLF)', async () => {
  let asked = 0
  const { port, link } = await openLink((req) => {
    if (req.cmd !== 'info') return []
    asked++
    if (asked < 3) return ['I (812) boot: still booting\r\n', 'W (813) wifi: no ']
    const full = `I (900) wifi: connOH<${JSON.stringify({ ...INFO, id: req.id })}\r\n`
    const a = Math.floor(full.length / 3)
    return ['\r\n', full.slice(0, a), full.slice(a, 2 * a), full.slice(2 * a)]
  })
  const info = await link.info({ timeoutMs: 5000 })
  assert.equal(asked, 3)
  assert.equal(info.track, 'flex')
  assert.equal(info.name, 'Kitchen')
  assert.equal(link.lastInfo, info)
  assert.ok(port.written.every((w) => w.startsWith('\nOH>') && w.endsWith('\n')), 'each request is \\nOH>{...}\\n')
  assert.ok(port.requests.every((r) => r.cmd === 'info' && Number.isInteger(r.id)))
  await link.close()
})

test('info rejects after timeoutMs when the node never answers', async () => {
  const { link } = await openLink(() => ['I (5) app: some other firmware\n'])
  const t0 = Date.now()
  await assert.rejects(link.info({ timeoutMs: 400 }), /answer/)
  assert.ok(Date.now() - t0 >= 350)
  await link.close()
})

test('replies are routed by id; stray and foreign replies are ignored', async () => {
  const { link } = await openLink((req) => [
    reply({ cmd: 'info', id: req.id + 1000, ok: true, track: 'array' }), // someone else's id
    'OH<{not json\n',
    reply({ ...INFO, id: req.id }),
  ])
  assert.equal((await link.info({ timeoutMs: 2000 })).track, 'flex')
  await link.close()
})

test('info refuses a board that speaks another setup protocol', async () => {
  const { link } = await openLink(simpleNode({ info: { proto: 2 } }))
  const err = await link.info({ timeoutMs: 1000 }).catch((e) => e)
  assert.equal(err.message, 'this board speaks setup protocol 2; this page speaks 1')
  assert.equal(link.lastInfo, null)
  await link.close()
})

test('info refuses a different board once one has answered, and keeps lastInfo', async () => {
  let mac = 'aa:bb:cc:dd:ee:ff'
  const { link } = await openLink((req) => simpleNode({ info: { mac } })(req))
  const first = await link.info({ timeoutMs: 1000 })
  assert.equal((await link.info({ timeoutMs: 1000 })).mac, mac, 'the same board again is fine')
  const kept = link.lastInfo
  mac = '11:22:33:44:55:66'
  await assert.rejects(link.info({ timeoutMs: 1000 }), /^Error: a different board answered: unplug the other boards and connect again$/)
  assert.equal(link.lastInfo, kept)
  assert.equal(link.lastInfo.mac, first.mac)
  await link.close()
})

test('save sends ONLY the given fields, resolves with saved, and log never sees the password', async () => {
  const PASS = 'Willow-pass-1234'
  const logged = []
  const { port, link } = await openLink(simpleNode(), { log: (...a) => logged.push(a) })
  const saved = await link.save({ wifi_ssid: 'Maple', wifi_pass: PASS, name: 'Kitchen' })
  assert.deepEqual(saved, ['wifi_ssid', 'wifi_pass', 'name'])
  const set = port.requests.find((r) => r.cmd === 'set')
  assert.deepEqual(Object.keys(set).sort(), ['cmd', 'id', 'name', 'wifi_pass', 'wifi_ssid'])
  assert.equal(set.wifi_pass, PASS)
  assert.ok(logged.length > 0, 'the spy saw progress text')
  assert.ok(logged.every((args) => !JSON.stringify(args).includes(PASS)), 'password reached log()')
  await link.close()
})

test('save rejects with the node error verbatim', async () => {
  const { link } = await openLink(simpleNode({ set: { ok: false, error: 'wifi_ssid: nvs write failed', saved: ['bot_url'] } }))
  const err = await link.save({ wifi_ssid: 'Maple', wifi_pass: 'pw', bot_url: '' }).catch((e) => e)
  assert.equal(err.message, 'wifi_ssid: nvs write failed')
  assert.deepEqual(err.saved, ['bot_url'])
  await link.close()
})

test('save: a validateSettings failure rejects before anything is written', async () => {
  const { port, link } = await openLink(simpleNode())
  await assert.rejects(link.save({ name: 'Kit chen' }), /^Error: name:/)
  await assert.rejects(link.save({ wifi_ssid: 'Maple' }), /wifi_pass:/)
  await assert.rejects(link.save({ name: 'Kitchen', id: 9 }), /^Error: id:/)
  assert.deepEqual(port.written, [])
  await link.close()
})

test('the Wi-Fi password never reaches log, console, an Error or a fetch', async () => {
  const PASS = 'Maple-secret-5678'
  const seen = [] // everything that left the module by any route other than the port
  const spy = (...a) => { seen.push(a) }
  const saved = {}
  for (const k of ['log', 'info', 'warn', 'error', 'debug', 'trace']) { saved[k] = console[k]; console[k] = spy }
  const fetchBefore = globalThis.fetch
  globalThis.fetch = async (...a) => { seen.push(a); throw new Error('no network in tests') }
  try {
    let n = 0
    const { port, link } = await openLink((req) => {
      if (req.cmd !== 'set') return simpleNode()(req)
      n++
      return [reply(n === 1 ? { cmd: 'set', id: req.id, ok: true, saved: ['wifi'] }
        : { cmd: 'set', id: req.id, ok: false, error: 'wifi_pass: not acceptable', saved: [] })]
    }, { log: spy })
    await link.info({ timeoutMs: 1000 })
    await link.save({ wifi_ssid: 'Maple', wifi_pass: PASS })
    const errs = []
    errs.push(await link.save({ wifi_ssid: 'Maple', wifi_pass: PASS }).catch((e) => e)) // node refuses
    errs.push(await link.save({ wifi_ssid: 'M'.repeat(40), wifi_pass: PASS }).catch((e) => e)) // local rule
    errs.push(await link.save({ wifi_ssid: 'Maple', wifi_pass: PASS + 'z'.repeat(60) }).catch((e) => e)) // local rule
    await link.close()
    assert.equal(errs.length, 3)
    for (const e of errs) {
      assert.ok(e instanceof Error)
      assert.ok(!e.message.includes(PASS) && !String(e.stack).includes(PASS), `password in an Error: ${e.message}`)
    }
    assert.ok(!JSON.stringify(seen).includes(PASS), 'password reached log/console/fetch')
    // control: the password did go to the node, over the port only
    assert.equal(port.written.filter((w) => w.includes(PASS)).length, 2)
  } finally {
    Object.assign(console, saved)
    globalThis.fetch = fetchBefore
  }
})

test('restart survives re-enumeration and info works afterwards', async () => {
  const { port, link } = await openLink((req, p) => {
    if (req.cmd === 'restart') {
      setTimeout(() => p.reenumerate(200), 20)
      return [reply({ cmd: 'restart', id: req.id, ok: true })]
    }
    return simpleNode({ info: { up_s: p.opens } })(req)
  })
  await link.restart({ timeoutMs: 5000 })
  assert.equal(port.opens, 2)
  assert.equal(port.isOpen, true)
  const info = await link.info({ timeoutMs: 2000 })
  assert.equal(info.up_s, 2)
  await link.close()
})

test('restart: a refused restart rejects with the node error and keeps the port', async () => {
  const { port, link } = await openLink((req) =>
    req.cmd === 'restart' ? [reply({ cmd: 'restart', id: req.id, ok: false, error: 'ota in progress' })] : simpleNode()(req))
  await assert.rejects(link.restart({ timeoutMs: 2000 }), /^Error: ota in progress$/)
  assert.equal(port.opens, 1)
  assert.equal((await link.info({ timeoutMs: 1000 })).ok, true)
  await link.close()
})

function linkNode(states) {
  let i = 0
  return (req) => {
    if (req.cmd !== 'info') return []
    const link = states[Math.min(i++, states.length - 1)]
    return [reply({ ...INFO, id: req.id, link })]
  }
}

test('watchLink resolves on bot and calls onState for every change', async () => {
  const { link } = await openLink(linkNode(['boot', 'wifi_connecting', 'wifi_connecting', 'wifi_up', 'bot']))
  const states = []
  const r = await link.watchLink({ timeoutMs: 10000, onState: (s) => states.push(s) })
  assert.equal(r.link, 'bot')
  assert.deepEqual(states, ['boot', 'wifi_connecting', 'wifi_up', 'bot'])
  await link.close()
})

test('watchLink names the stuck stage on timeout', async () => {
  let { link } = await openLink(linkNode(['boot', 'wifi_connecting']))
  const states = []
  await assert.rejects(link.watchLink({ timeoutMs: 1200, onState: (s) => states.push(s) }), /Wi-Fi never connected/)
  assert.deepEqual(states, ['boot', 'wifi_connecting'])
  await link.close();
  ({ link } = await openLink(linkNode(['wifi_connecting', 'wifi_up'])))
  await assert.rejects(link.watchLink({ timeoutMs: 1200 }), /host address/)
  await link.close()
})

test('watchLink follows the board through a reset mid-watch and still reaches bot', async () => {
  let n = 0
  const { port, link } = await openLink((req, p) => {
    if (req.cmd !== 'info') return []
    const state = ['boot', 'wifi_connecting', 'wifi_up', 'bot'][Math.min(n++, 3)]
    if (state === 'wifi_connecting') setTimeout(() => p.reenumerate(200), 20) // resets right after answering
    return [reply({ ...INFO, id: req.id, link: state })]
  })
  const states = []
  const r = await link.watchLink({ timeoutMs: 10000, onState: (s) => states.push(s) })
  assert.equal(r.link, 'bot')
  assert.deepEqual(states, ['boot', 'wifi_connecting', 'wifi_up', 'bot'])
  assert.equal(port.opens, 2, 'reopened after the reset')
  assert.equal(port.isOpen, true)
  await link.close()
})

test('watchLink: a board that never comes back ends in the stage error', async () => {
  const { link } = await openLink((req, p) => {
    if (req.cmd !== 'info') return []
    setTimeout(() => p.reenumerate(3000), 20) // back only after the watch has given up
    return [reply({ ...INFO, id: req.id, link: 'wifi_connecting' })]
  })
  await assert.rejects(link.watchLink({ timeoutMs: 1500 }), /Wi-Fi never connected/)
})

test('info marks its own no-answer timeout, and only that one', async () => {
  const { link } = await openLink(() => [])
  const err = await link.info({ timeoutMs: 300 }).catch((e) => e)
  assert.equal(err.message, 'no LenoxVI firmware answered')
  assert.equal(err.timeout, true)
  await link.close()
  const { link: other } = await openLink(simpleNode({ info: { proto: 2 } }))
  assert.equal((await other.info({ timeoutMs: 1000 }).catch((e) => e)).timeout, undefined)
  await other.close()
})

test('watchLink: a different board answering mid-watch ends the watch with that error', async () => {
  let n = 0
  const { link } = await openLink((req) => {
    if (req.cmd !== 'info') return []
    const mac = n++ < 2 ? 'aa:bb:cc:dd:ee:ff' : '11:22:33:44:55:66'
    return [reply({ ...INFO, id: req.id, mac, link: 'wifi_connecting' })]
  })
  const t0 = Date.now()
  await assert.rejects(link.watchLink({ timeoutMs: 10000 }),
    /^Error: a different board answered: unplug the other boards and connect again$/)
  assert.ok(Date.now() - t0 < 5000, 'ended at once, not at the watch timeout')
  await link.close()
})

test('watchLink: a protocol error mid-watch ends the watch with that error', async () => {
  let n = 0
  const { link } = await openLink((req) => {
    if (req.cmd !== 'info') return []
    return [reply({ ...INFO, id: req.id, proto: n++ < 1 ? 1 : 2, link: 'boot' })]
  })
  await assert.rejects(link.watchLink({ timeoutMs: 10000 }), /^Error: this board speaks setup protocol 2; this page speaks 1$/)
  await link.close()
})

// ── flash ────────────────────────────────────────────────────────────────

async function hex(bytes) {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
  return Array.from(d, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** A full flash set for `track` served by a fake fetch; `calls` records every URL. */
async function flashSet(track = 'flex') {
  const names = ['bootloader', 'partition-table', 'ota-data', 'app']
  const files = ['bootloader.bin', 'partition-table.bin', 'ota_data_initial.bin', `${track}_esp32.bin`]
  const offsets = [0, 0x8000, 0x10000, 0x20000]
  const blobs = names.map((_, i) => new Uint8Array(200 + i * 10).fill(0x41 + i))
  const parts = await Promise.all(names.map(async (name, i) =>
    ({ name, file: files[i], offset: offsets[i], size: blobs[i].length, sha256: await hex(blobs[i]) })))
  const manifest = { track, version: '1.2.47', flash: { mode: 'dio', freq: '80m', size: '8MB' }, parts }
  const routes = { [`/api/dashboard/firmware/${track}/flash`]: manifest }
  files.forEach((f, i) => { routes[`/firmware/${track}/${f}`] = blobs[i] })
  const calls = []
  const fetchFn = async (url) => {
    calls.push(url)
    const body = routes[url]
    if (!body) return { ok: false, status: 404, json: async () => ({ detail: `nothing at ${url}` }) }
    return {
      ok: true, status: 200,
      json: async () => structuredClone(body),
      arrayBuffer: async () => body.slice().buffer,
    }
  }
  return { manifest, blobs, routes, calls, fetchFn }
}

/** esptool-js stand-in recording every call; the hard reset re-enumerates the port. */
function fakeEsptool(port, calls, chip = 'ESP32-S3 (QFN56) (revision v0.2)') {
  class Transport {
    constructor(device, ...rest) { calls.push(['Transport', device, rest]) }
    async disconnect() { calls.push(['disconnect']) }
  }
  class ESPLoader {
    constructor(opts) { calls.push(['ESPLoader', opts]) }
    async main() { calls.push(['main', port.isOpen]); return chip }
    async writeFlash(o) {
      calls.push(['writeFlash', o])
      o.fileArray.forEach((f, i) => { o.reportProgress(i, 0, f.data.length); o.reportProgress(i, f.data.length, f.data.length) })
    }
    async eraseFlash() { calls.push(['eraseFlash']) }
    async after(mode) {
      calls.push(['after', mode])
      port.offline = true
      setTimeout(() => { port.offline = false }, 150)
    }
  }
  return { ESPLoader, Transport }
}

test('flash refuses another line before any download', async () => {
  const { link } = await openLink(simpleNode({ info: { track: 'array' } }))
  await link.info({ timeoutMs: 1000 })
  const set = await flashSet('flex')
  let loaded = false
  await assert.rejects(link.flash('flex', { fetchFn: set.fetchFn, loadEsptool: async () => { loaded = true } }), /array line/)
  assert.deepEqual(set.calls, [])
  assert.equal(loaded, false)
  await link.close()
})

test('flash refuses a sha256 mismatch before loading esptool-js', async () => {
  const { port, link } = await openLink(simpleNode())
  await link.info({ timeoutMs: 1000 })
  const set = await flashSet('flex')
  set.routes['/firmware/flex/partition-table.bin'] = new Uint8Array(set.blobs[1].length).fill(0x7a) // same size, other bytes
  let loaded = false
  await assert.rejects(link.flash('flex', { fetchFn: set.fetchFn, loadEsptool: async () => { loaded = true } }), /partition-table: sha256/)
  assert.equal(loaded, false)
  assert.equal(port.isOpen, true, 'the link is untouched')
  await link.close()
})

test('flash refuses a manifest that writes the settings region, naming the route error', async () => {
  const { link } = await openLink(simpleNode())
  const set = await flashSet('flex')
  set.routes['/api/dashboard/firmware/flex/flash'].parts[2].offset = 0x9000
  let loaded = false
  const opts = { fetchFn: set.fetchFn, loadEsptool: async () => { loaded = true } }
  await assert.rejects(link.flash('flex', opts), /settings/)
  delete set.routes['/api/dashboard/firmware/flex/flash']
  await assert.rejects(link.flash('flex', opts), /nothing at/)
  assert.deepEqual(set.calls.filter((u) => u.startsWith('/firmware/')), [])
  assert.equal(loaded, false)
  await link.close()
})

test('flash writes the 4 parts at the manifest offsets, no erase, hard reset, reopens', async () => {
  const { port, link } = await openLink(simpleNode())
  await link.info({ timeoutMs: 1000 })
  const set = await flashSet('flex')
  const calls = []
  const progress = []
  await link.flash('flex', {
    fetchFn: set.fetchFn,
    onProgress: (p) => progress.push(p),
    loadEsptool: async () => fakeEsptool(port, calls),
  })
  assert.deepEqual(calls.map((c) => c[0]), ['Transport', 'ESPLoader', 'main', 'writeFlash', 'after', 'disconnect'])
  const [, device] = calls[0]
  assert.equal(device, port)
  assert.equal(calls[2][1], false, 'our side closed the port before esptool-js opened it')
  const opts = calls[1][1]
  assert.equal(opts.baudrate, 460800)
  assert.equal(typeof opts.terminal.writeLine, 'function')
  const w = calls.find((c) => c[0] === 'writeFlash')[1]
  assert.equal(w.fileArray.length, 4)
  assert.deepEqual(w.fileArray.map((f) => f.address), set.manifest.parts.map((p) => p.offset))
  w.fileArray.forEach((f, i) => { assert.ok(f.data instanceof Uint8Array); assert.deepEqual(f.data, set.blobs[i]) })
  assert.equal(w.eraseAll, false)
  assert.equal(w.compress, true)
  assert.deepEqual([w.flashMode, w.flashFreq, w.flashSize], ['dio', '80m', '8MB'])
  assert.deepEqual(calls.find((c) => c[0] === 'after'), ['after', 'hard_reset'])
  assert.ok(!calls.some((c) => c[0] === 'eraseFlash'))
  assert.ok(progress.length > 0 && progress.every((p) => Object.keys(p).sort().join() === 'part,pct'))
  assert.deepEqual(progress.filter((p) => p.pct === 100).map((p) => p.part), ['bootloader', 'partition-table', 'ota-data', 'app'])
  assert.equal(port.opens, 2, 'reopened after the reset')
  assert.equal((await link.info({ timeoutMs: 1000 })).fw, '1.2.47')
  await link.close()
})

test('flash refuses a chip that is not an ESP32-S3', async () => {
  const { port, link } = await openLink(simpleNode())
  const set = await flashSet('flex')
  const calls = []
  await assert.rejects(link.flash('flex', { fetchFn: set.fetchFn, loadEsptool: async () => fakeEsptool(port, calls, 'ESP32-C3 (QFN32)') }), /ESP32-S3/)
  assert.ok(!calls.some((c) => c[0] === 'writeFlash'))
  assert.ok(calls.some((c) => c[0] === 'disconnect'))
  await link.close()
})

test('flash: a failed write disconnects, reopens the link and rethrows the original error', async () => {
  const { port, link } = await openLink(simpleNode())
  await link.info({ timeoutMs: 1000 })
  const set = await flashSet('flex')
  const calls = []
  const boom = new Error('write failed at 0x20000')
  const fake = fakeEsptool(port, calls)
  class ESPLoader extends fake.ESPLoader {
    async writeFlash(o) { calls.push(['writeFlash', o]); throw boom }
  }
  await assert.rejects(link.flash('flex', { fetchFn: set.fetchFn, loadEsptool: async () => ({ ...fake, ESPLoader }) }), (e) => e === boom)
  assert.deepEqual(calls.map((c) => c[0]), ['Transport', 'ESPLoader', 'main', 'writeFlash', 'disconnect'])
  assert.equal(port.opens, 2, 'reopened after the failure')
  assert.equal(port.isOpen, true)
  assert.equal((await link.info({ timeoutMs: 1000 })).track, 'flex')
  await link.close()
})

test('the post-flash hard reset really pulses RTS with DTR low (real esptool-js)', async () => {
  // What flash() hands esptool-js ...
  const { port, link } = await openLink(simpleNode())
  await link.info({ timeoutMs: 1000 })
  const set = await flashSet('flex')
  const calls = []
  await link.flash('flex', { fetchFn: set.fetchFn, loadEsptool: async () => fakeEsptool(port, calls) })
  await link.close()
  const { resetConstructors } = calls.find((c) => c[0] === 'ESPLoader')[1]
  assert.equal(resetConstructors, ns.RESET_CONSTRUCTORS)
  // ... run through the REAL esptool-js after('hard_reset'), on a transport that records the lines.
  const { ESPLoader } = await import('esptool-js')
  const run = async (opts) => {
    const sig = []
    const transport = { setRTS: async (v) => { sig.push(`RTS=${+v}`) }, setDTR: async (v) => { sig.push(`DTR=${+v}`) }, getInfo: () => 'test', tracing: false }
    const quiet = { clean() {}, write() {}, writeLine() {} }
    await new ESPLoader({ transport, baudrate: 460800, terminal: quiet, ...opts }).after('hard_reset', false)
    return sig
  }
  assert.deepEqual(await run({ resetConstructors }), ['DTR=0', 'RTS=1', 'RTS=0'], 'chip held in reset, then a normal boot')
  // Canary: esptool-js 0.7.0's own hard reset never raises RTS, the bug the override fixes.
  // If an upgrade changes this, check whether the override is still needed.
  assert.deepEqual(await run({}), ['RTS=0'])
})

test('the reset still finishes when the board drops off USB mid-reset', async () => {
  const sig = []
  const transport = {
    setDTR: async (v) => { sig.push(`DTR=${+v}`) },
    setRTS: async (v) => { sig.push(`RTS=${+v}`); if (!v) throw new Error('device lost') },
  }
  await new ns.PulseReset(transport).reset()
  assert.deepEqual(sig, ['DTR=0', 'RTS=1', 'RTS=0'])
})

// ── A node that can't reach its host restarts in a loop ──────────────────

test('save follows a node that resets mid-request and sends set again', async () => {
  let sets = 0
  const { port, link } = await openLink((req, p) => {
    if (req.cmd === 'info') return [reply({ ...INFO, id: req.id })]
    if (req.cmd !== 'set') return []
    if (++sets === 1) { setTimeout(() => p.reenumerate(150), 0); return [] } // the first set lands in a reboot
    return [reply({ cmd: 'set', id: req.id, ok: true, saved: ['bot_url'] })]
  })
  assert.deepEqual(await link.save({ bot_url: 'https://192.0.2.10:7860/api/offer' }), ['bot_url'])
  assert.equal(sets, 2, 'sent again after the reset')
  assert.equal(port.opens, 2, 'followed the board back')
  await link.close()
})

test('save reopens a board that reset while the page sat idle', async () => {
  const { port, link } = await openLink(simpleNode())
  port.reenumerate(100) // the node restarted on its own between its last answer and Save
  await tick(250)
  assert.deepEqual(await link.save({ name: 'Den' }), ['name'])
  assert.equal(port.requests.filter((r) => r.cmd === 'set').length, 1, 'nothing sent into the dead port')
  assert.equal(port.opens, 2)
  await link.close()
})

test('save still gives up when the node never answers', async () => {
  const { link } = await openLink((req) => (req.cmd === 'info' ? [reply({ ...INFO, id: req.id })] : []))
  await assert.rejects(link.save({ name: 'Den' }, { timeoutMs: 1500 }), /no reply to set/)
  await link.close()
})

test('watchLink blames the host when the node reached Wi-Fi, then keeps restarting', async () => {
  let up = false
  const { link } = await openLink((req, p) => {
    if (req.cmd !== 'info') return []
    if (!up) {
      up = true
      setTimeout(() => p.reenumerate(100), 20) // Wi-Fi up, the offer fails, the node restarts ...
      return [reply({ ...INFO, id: req.id, link: 'wifi_up' })]
    }
    return [reply({ ...INFO, id: req.id, link: 'wifi_connecting' })] // ... and the watch ends while it rejoins
  })
  const states = []
  await assert.rejects(link.watchLink({ timeoutMs: 2000, onState: (s) => states.push(s) }), /host address/)
  assert.deepEqual(states.slice(0, 2), ['wifi_up', 'wifi_connecting'])
  await link.close()
})

// Node setup's Save: the node is saved, restarted and watched; each step is a fake here.
const saveSteps = ({ saveErr, restartErr, watchErr } = {}) => {
  const calls = []
  return {
    calls,
    save: async () => { calls.push('save'); if (saveErr) throw new Error(saveErr); return ['name', 'bot_url'] },
    onSaved: (saved) => calls.push(['onSaved', saved]),
    restart: async () => { calls.push('restart'); if (restartErr) throw new Error(restartErr) },
    watch: async () => { calls.push('watch'); if (watchErr) throw new Error(watchErr) },
  }
}

test('save then restart: a restart failure after a good save says it saved, not "Save failed"', async () => {
  const s = saveSteps({ restartErr: 'the board did not answer the restart' })
  const r = await ns.saveThenRestart(s)
  assert.equal(r.ok, false)
  assert.equal(r.text, "Saved name, bot_url. The node didn't restart (the board did not answer the restart) — unplug it and plug it back in to apply.")
  assert.doesNotMatch(r.text, /Save failed/)
  assert.deepEqual(r.saved, ['name', 'bot_url'])
  assert.deepEqual(s.calls, ['save', ['onSaved', ['name', 'bot_url']], 'restart'], 'values applied before the restart; no watch')
})

test('save then restart: a watch that throws after a good save is the same outcome', async () => {
  const s = saveSteps({ watchErr: 'the board stopped answering over USB' })
  const r = await ns.saveThenRestart(s)
  assert.equal(r.ok, false); assert.match(r.text, /^Saved name, bot_url\. The node didn't restart \(the board stopped answering over USB\)/)
  assert.deepEqual(s.calls, ['save', ['onSaved', ['name', 'bot_url']], 'restart', 'watch'])
})

test('save then restart: a failed save is still "Save failed" and nothing is applied or restarted', async () => {
  const s = saveSteps({ saveErr: 'no reply to set' })
  const r = await ns.saveThenRestart(s)
  assert.deepEqual(r, { ok: false, text: 'Save failed: no reply to set' })
  assert.deepEqual(s.calls, ['save'])
})

test('save then restart: save, restart and watch all fine is the plain success path', async () => {
  const s = saveSteps()
  const r = await ns.saveThenRestart(s)
  assert.deepEqual(r, { ok: true, saved: ['name', 'bot_url'] })
  assert.deepEqual(s.calls, ['save', ['onSaved', ['name', 'bot_url']], 'restart', 'watch'])
})
