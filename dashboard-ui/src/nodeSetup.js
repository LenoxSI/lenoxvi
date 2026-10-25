// Node setup over USB: the page side of the USB setup protocol v1.
// Pure helpers are unit-tested with `node --test`; NodeLink takes any
// Web Serial-like port, so tests pass a fake one.
export const PROTO = 1
export const USB_FILTERS = [{ usbVendorId: 0x303a, usbProductId: 0x1001 }]
export const SETTINGS_REGION = [0x9000, 0x10000] // nvs + phy_init: never written
export const TRACKS = ['array', 'flex', 'waveshare']

const FIELDS = ['wifi_ssid', 'wifi_pass', 'name', 'bot_url']
const MAX_LINE = 512 // bytes per protocol line, prefix included
const BAUD = 115200
const enc = new TextEncoder()
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** Resolve with `p`, or with `fallback` after `ms`. */
function within(p, ms, fallback) {
  let t
  return Promise.race([p, new Promise((r) => { t = setTimeout(r, ms, fallback) })]).finally(() => clearTimeout(t))
}

function isOurPort(port) {
  const i = port.getInfo()
  return USB_FILTERS.some((f) => f.usbVendorId === i.usbVendorId && f.usbProductId === i.usbProductId)
}

async function sha256Hex(bytes) {
  const d = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes))
  return Array.from(d, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** Web Serial available in a secure context. */
export function isSupported(nav = globalThis.navigator, win = globalThis.window) {
  return Boolean(win?.isSecureContext && nav?.serial)
}

/** The offer URL nodes should use for the host serving this page. */
export function hostOfferUrl(origin) {
  return origin.replace(/\/+$/, '') + '/api/offer'
}

/** Parse a reply out of one received line ('OH<' anywhere); null when there is none. */
export function parseReplyLine(line) {
  for (let i = line.indexOf('OH<'); i >= 0; i = line.indexOf('OH<', i + 1)) {
    try {
      const v = JSON.parse(line.slice(i + 3))
      if (v && typeof v === 'object' && !Array.isArray(v)) return v
    } catch { /* not a reply from here; try the next marker */ }
  }
  return null
}

function wifiPassOk(p) {
  return p.length <= 63 || /^[0-9a-fA-F]{64}$/.test(p)
}

function botUrlOk(u) {
  return enc.encode(u).length <= 127 && /^[\x21-\x7e]+$/.test(u) && !/["'\\]/.test(u)
    && /^https:\/\/[^/?#]+\/api\/offer$/.test(u)
}

/** Firmware rules for `set` fields; null = ok, else '<field>: <rule>'. */
export function validateSettings(fields) {
  // Messages name the field and the rule, never the value (it may be the password).
  for (const k of Object.keys(fields)) if (!FIELDS.includes(k)) return `${k}: unknown field`
  for (const [k, v] of Object.entries(fields)) if (typeof v !== 'string') return `${k}: must be text`
  const { wifi_ssid: ssid, wifi_pass: pass, name, bot_url: url } = fields
  if ((ssid === undefined) !== (pass === undefined)) {
    return ssid === undefined ? 'wifi_ssid: required with wifi_pass' : 'wifi_pass: required with wifi_ssid'
  }
  if (ssid !== undefined) {
    const n = enc.encode(ssid).length
    if (n < 1 || n > 32) return 'wifi_ssid: 1 to 32 bytes'
    if (!wifiPassOk(pass)) return 'wifi_pass: up to 63 characters, or exactly 64 hex digits'
  }
  if (name !== undefined && !/^[A-Za-z0-9_-]{1,31}$/.test(name)) return 'name: 1 to 31 letters, digits, _ or -'
  if (url !== undefined && url !== '' && !botUrlOk(url)) {
    return 'bot_url: an https:// address ending in /api/offer, at most 127 characters, no spaces or quotes'
  }
  return null
}

/** Manifest sanity for `track`; null = ok, else a reason. */
export function checkManifest(manifest, track) {
  if (!manifest || typeof manifest !== 'object') return 'no manifest'
  if (manifest.track !== track) return `manifest is for the ${manifest.track} line, not ${track}`
  const parts = manifest.parts
  if (!Array.isArray(parts) || parts.length !== 4) return `incomplete flash set: expected 4 parts, got ${parts?.length ?? 0}`
  const [lo, hi] = SETTINGS_REGION
  for (const p of parts) {
    if (typeof p?.name !== 'string' || !p.name) return 'a part has no name'
    if (typeof p.file !== 'string' || !/^[\w.-]+$/.test(p.file) || p.file.startsWith('.')) return `${p.name}: bad file name`
    if (!Number.isInteger(p.offset) || p.offset < 0) return `${p.name}: bad offset`
    if (!Number.isInteger(p.size) || p.size <= 0) return `${p.name}: bad size`
    if (typeof p.sha256 !== 'string' || !/^[0-9a-f]{64}$/i.test(p.sha256)) return `${p.name}: bad sha256`
    if (p.offset < hi && p.offset + p.size > lo) return `${p.name} would write the settings region (0x9000-0xFFFF)`
  }
  const sorted = [...parts].sort((a, b) => a.offset - b.offset)
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i - 1].offset + sorted[i - 1].size > sorted[i].offset) return `${sorted[i - 1].name} overlaps ${sorted[i].name}`
  }
  return null
}

export class NodeLink {
  /** @param port Web Serial-like port; opts.log(msg) never receives secrets */
  constructor(port, { log = () => {}, serial = globalThis.navigator?.serial } = {}) {
    this.port = port
    this.log = log
    this.serial = serial
    this.lastInfo = null
    this._nextId = 1
    this._pending = new Map() // request id -> reply handler
    this._reader = null
    this._writer = null
    this._loop = Promise.resolve()
    this._alive = false
  }

  /** Browser port picker (filtered), then open. */
  static async request({ serial = globalThis.navigator?.serial, log } = {}) {
    const port = await serial.requestPort({ filters: USB_FILTERS })
    const link = new NodeLink(port, { log, serial })
    await link.open()
    return link
  }

  async open() {
    await this.port.open({ baudRate: BAUD })
    this._attach()
  }

  _attach() {
    this._writer = this.port.writable.getWriter()
    this._reader = this.port.readable.getReader()
    this._alive = true
    this._loop = this._readLoop(this._reader)
  }

  async _readLoop(reader) {
    const dec = new TextDecoder()
    let buf = ''
    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        for (let nl = buf.indexOf('\n'); nl >= 0; nl = buf.indexOf('\n')) {
          this._onLine(buf.slice(0, nl).replaceAll('\r', ''))
          buf = buf.slice(nl + 1)
        }
        if (buf.length > 4 * MAX_LINE) buf = buf.slice(-MAX_LINE) // endless log text with no newline
      }
    } catch { /* port lost (the board reset or was unplugged) */ }
    this._alive = false
    try { reader.releaseLock() } catch { /* already released */ }
  }

  _onLine(line) {
    const reply = parseReplyLine(line)
    const handler = reply && this._pending.get(reply.id)
    if (handler) handler(reply)
  }

  async _write(req) {
    const line = `OH>${JSON.stringify(req)}`
    if (enc.encode(line).length > MAX_LINE) throw new Error(`${req.cmd}: request too long`)
    if (!this._writer) throw new Error('not connected')
    await this._writer.write(enc.encode(`\n${line}\n`))
  }

  /** Send once; resolve with the reply (ok or not), reject when none arrives in time. */
  _request(cmd, fields = {}, timeoutMs = 5000) {
    const id = this._nextId++
    return new Promise((resolve, reject) => {
      const done = (fn, v) => { clearTimeout(t); this._pending.delete(id); fn(v) }
      const t = setTimeout(() => done(reject, new Error(`no reply to ${cmd}`)), timeoutMs)
      this._pending.set(id, (reply) => done(resolve, reply))
      this._write({ id, cmd, ...fields }).catch((err) => done(reject, err))
    })
  }

  /** Release our reader and writer so the port can be closed. */
  async _release() {
    const r = this._reader
    const w = this._writer
    this._reader = null
    this._writer = null
    if (r) await r.cancel().catch(() => {})
    await this._loop
    if (w) { try { w.releaseLock() } catch { /* already released */ } }
  }

  /** Poll for our USB port to come back, then open it. */
  async _reopen(deadline) {
    for (;;) {
      await sleep(300)
      for (const p of await this.serial.getPorts()) {
        if (!isOurPort(p)) continue
        try {
          await p.open({ baudRate: BAUD })
        } catch { continue } // not back yet, or another program has it
        this.port = p
        this._attach()
        this.log('board is back on USB')
        return
      }
      if (Date.now() > deadline) throw new Error('the board did not come back on USB')
    }
  }

  /** Ask until the node answers (opening the port can reset it). Resolves with the info reply. */
  async info({ timeoutMs = 15000 } = {}) {
    const id = this._nextId++
    const reply = await new Promise((resolve, reject) => {
      const send = () => { this._write({ id, cmd: 'info' }).catch(() => {}) } // retried until the deadline
      const done = (fn, v) => { clearInterval(iv); clearTimeout(t); this._pending.delete(id); fn(v) }
      const iv = setInterval(send, 500)
      const noAnswer = Object.assign(new Error('no LenoxVI firmware answered'), { timeout: true })
      const t = setTimeout(() => done(reject, noAnswer), timeoutMs)
      this._pending.set(id, (r) => done(resolve, r))
      send()
    })
    if (!reply.ok) throw new Error(reply.error)
    if (reply.proto !== PROTO) throw new Error(`this board speaks setup protocol ${reply.proto}; this page speaks ${PROTO}`)
    // With two boards plugged in, a reopen can land on the other one: never talk to it as if it were this one.
    if (this.lastInfo && JSON.stringify(reply.mac) !== JSON.stringify(this.lastInfo.mac)) {
      throw new Error('a different board answered: unplug the other boards and connect again')
    }
    this.lastInfo = reply
    return reply
  }

  /** `set` -> resolves with reply.saved; throws Error(reply.error) when !ok. */
  async save(fields) {
    const bad = validateSettings(fields)
    if (bad) throw new Error(bad)
    this.log(`saving ${Object.keys(fields).join(', ')}`) // field names only, never values
    const reply = await this._request('set', fields, 10000)
    if (!reply.ok) throw Object.assign(new Error(reply.error), { saved: reply.saved ?? [] })
    return reply.saved
  }

  /** `restart`, then wait for the port to re-enumerate and reopen it. */
  async restart({ timeoutMs = 20000 } = {}) {
    const deadline = Date.now() + timeoutMs
    this.log('restarting the board')
    const gone = this._loop.then(() => null)
    const reply = await Promise.race([this._request('restart', {}, 3000).catch(() => null), gone])
    if (reply && !reply.ok) throw new Error(reply.error)
    if (!reply && this._alive) throw new Error('the board did not answer the restart')
    // Acked: the board resets right after; give its USB a moment to drop so we don't reopen the old connection.
    if (reply) await within(gone, 2000)
    await this._release()
    await this.port.close().catch(() => {})
    await this._reopen(deadline)
  }

  /** Poll info until link === 'bot'; onState(link) on every change; throws a stage-specific Error on timeout. */
  async watchLink({ timeoutMs = 90000, onState = () => {} } = {}) {
    const deadline = Date.now() + timeoutMs
    let link = null
    for (let left = timeoutMs; left > 0; left = deadline - Date.now()) {
      if (!this._alive) {
        // The board reset (a node that can't reach its host restarts in a loop): follow it back.
        await this._release()
        await this.port.close().catch(() => {})
        if (!(await this._reopen(deadline).then(() => true, () => false))) break
      }
      // Only silence keeps us polling; another board, protocol or !ok reply ends the watch as is.
      const reply = await this.info({ timeoutMs: Math.min(3000, left) }).catch((err) => {
        if (err.timeout) return null
        throw err
      })
      if (reply && reply.link !== link) {
        link = reply.link
        onState(link)
      }
      if (link === 'bot') return reply
      await sleep(Math.max(0, Math.min(500, deadline - Date.now())))
    }
    if (link === 'wifi_up') throw new Error('Wi-Fi is up but the host never answered: check the host address')
    if (link === null) throw new Error('the board stopped answering over USB')
    throw new Error('Wi-Fi never connected: check the network name and password')
  }

  /** Download + verify the line's set, write it with esptool-js (no erase), hard reset, reopen.
   *  onProgress({ part, pct }). Refuses when the board reported another line. */
  async flash(track, { fetchFn = globalThis.fetch, onProgress = () => {}, loadEsptool = () => import('esptool-js') } = {}) {
    if (!TRACKS.includes(track)) throw new Error(`unknown line: ${track}`)
    const board = this.lastInfo?.track
    if (board && board !== track) throw new Error(`this board runs the ${board} line, not ${track}: refusing to flash`)

    const res = await fetchFn(`/api/dashboard/firmware/${track}/flash`)
    const manifest = await res.json().catch(() => null)
    if (!res.ok) throw new Error(manifest?.detail || `flash set: HTTP ${res.status}`)
    const bad = checkManifest(manifest, track)
    if (bad) throw new Error(bad)
    const { mode, freq, size } = manifest.flash ?? {}
    if (![mode, freq, size].every((v) => typeof v === 'string' && v)) throw new Error('manifest has no flash mode, freq and size')

    const fileArray = []
    for (const p of manifest.parts) {
      this.log(`downloading ${p.name}`)
      const r = await fetchFn(`/firmware/${track}/${encodeURIComponent(p.file)}`)
      if (!r.ok) throw new Error(`${p.name}: download failed (HTTP ${r.status})`)
      const data = new Uint8Array(await r.arrayBuffer())
      if (data.length !== p.size) throw new Error(`${p.name}: size mismatch, download incomplete`)
      if (await sha256Hex(data) !== p.sha256.toLowerCase()) throw new Error(`${p.name}: sha256 mismatch, download corrupted`)
      fileArray.push({ data, address: p.offset })
    }

    await this._release()
    await this.port.close().catch(() => {}) // esptool-js opens the port itself
    let transport = null
    try {
      const { ESPLoader, Transport } = await loadEsptool()
      transport = new Transport(this.port)
      const quiet = { clean() {}, write() {}, writeLine() {} }
      const loader = new ESPLoader({ transport, baudrate: 460800, terminal: quiet })
      this.log('connecting to the bootloader')
      const chip = await loader.main()
      if (!String(chip).includes('ESP32-S3')) throw new Error(`not an ESP32-S3 (${chip}): refusing to flash`)
      this.log(`writing ${fileArray.length} parts`)
      await loader.writeFlash({
        fileArray, flashMode: mode, flashFreq: freq, flashSize: size, eraseAll: false, compress: true,
        reportProgress: (i, written, total) => onProgress({ part: manifest.parts[i].name, pct: Math.floor((100 * written) / total) }),
      })
      this.log('resetting the board')
      await loader.after('hard_reset')
    } catch (err) {
      // Leave the page a usable link if we can, but report what actually failed.
      await transport?.disconnect().catch(() => {})
      await this._reopen(Date.now() + 5000).catch(() => {})
      throw err
    }
    await transport.disconnect().catch(() => {})
    await this._reopen(Date.now() + 20000)
  }

  async close() {
    await this._release()
    await this.port.close().catch(() => {})
  }
}
