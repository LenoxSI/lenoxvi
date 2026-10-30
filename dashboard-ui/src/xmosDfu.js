// Audio chip (XMOS XVF3800) flashing over WebUSB: a small DFU 1.1 client, the pinned
// images, and the sound check that tells whether a node needs it. Writes go to the
// upgrade slot (alt 1) only; alt 0 is the chip's recovery copy and is never selected.

export const XMOS_VENDOR_ID = 0x2886
export const XMOS_IMAGES = {
  array: {
    repo: 'respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY',
    commit: '4b49bfd19977c63cf6e90dfe9e5827e74e20bf6d',
    path: 'xmos_firmwares/i2s/old_firmwares/application_xvf3800_i2s_master_v1.0.8_48k.bin',
    size: 888832,
    sha256: 'd60d0bc2c7f5a67ffa9c9206e066b2d8f1ccb49ebba673f3a7bcff1cf197dfb2',
    version: '1.0.8',
  },
  flex: {
    repo: 'formatBCE/Respeaker-XVF3800-ESPHome-integration',
    commit: '8c4aa8ad0098bcc8b076ef17660bab4e0cf0531d',
    path: 'application_xvf3800_inthost-lr48-sqr-i2c-v1.0.7-release.bin',
    size: 888832,
    sha256: 'd1132b7e779818923072396315304a5dd2324aa56969b4111b30678f968a7c6a',
    version: '1.0.7',
  },
}
export const DFU = { DNLOAD: 1, GETSTATUS: 3, CLRSTATUS: 4, ABORT: 6 }
export const DFU_STATE = { IDLE: 2, DNLOAD_SYNC: 3, DNBUSY: 4, DNLOAD_IDLE: 5, MANIFEST_SYNC: 6, MANIFEST: 7, MANIFEST_WAIT_RESET: 8, ERROR: 10 }
const UPGRADE_ALT = 1
const WAITING = [DFU_STATE.DNLOAD_SYNC, DFU_STATE.DNBUSY, DFU_STATE.MANIFEST_SYNC, DFU_STATE.MANIFEST]

export class XmosError extends Error {
  constructor(code, message) { super(message); this.name = 'XmosError'; this.code = code }
}
const noLine = () => new XmosError('no-line', 'Pick the board first.')

export function xmosImageUrl(line, images = XMOS_IMAGES) {
  const im = images[line]
  if (!im) throw noLine()
  return `https://raw.githubusercontent.com/${im.repo}/${im.commit}/${im.path}`
}

export function xmosImageName(line, images = XMOS_IMAGES) {
  const im = images[line]
  if (!im) throw noLine()
  return im.path.split('/').pop()
}
const fallbackHint = (line, images) => `Get ${xmosImageName(line, images)} and choose it under “Or a downloaded file”.`

export async function sha256Hex(bytes, subtle = globalThis.crypto.subtle) {
  const d = new Uint8Array(await subtle.digest('SHA-256', bytes))
  return Array.from(d, b => b.toString(16).padStart(2, '0')).join('')
}

export async function checkXmosImage(bytes, line, { images = XMOS_IMAGES, subtle } = {}) {
  const im = images[line]
  if (!im) throw noLine()
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  const L = line.toUpperCase()
  if (b.length !== im.size) throw new XmosError('size-mismatch', `The ${L} image is ${b.length} bytes, expected ${im.size}; not flashing it.`)
  const got = await sha256Hex(b, subtle)
  if (got !== im.sha256) {
    throw new XmosError('hash-mismatch', `The ${L} image doesn't match the pinned file (got ${got.slice(0, 12)}…, expected ${im.sha256.slice(0, 12)}…); not flashing it.`)
  }
  return b
}

export async function fetchXmosImage(line, { fetchFn = globalThis.fetch, images = XMOS_IMAGES, subtle } = {}) {
  const url = xmosImageUrl(line, images)
  const hint = fallbackHint(line, images)
  let buf
  try {
    const r = await fetchFn(url)
    if (!r.ok) throw new XmosError('fetch-failed', `Couldn’t download the audio chip firmware from GitHub (HTTP ${r.status}). ${hint}`)
    buf = await r.arrayBuffer()   // the body can drop mid-download too: still a download failure, nothing was written
  } catch (e) {
    if (e instanceof XmosError) throw e
    throw new XmosError('fetch-failed', `Couldn’t download the audio chip firmware from GitHub (network). ${hint}`)
  }
  try { return await checkXmosImage(new Uint8Array(buf), line, { images, subtle }) } catch (e) {
    if (e instanceof XmosError && e.code !== 'no-line') throw new XmosError(e.code, `${e.message} ${hint}`)
    throw e
  }
}

export function isUsbSupported(nav = globalThis.navigator) { return !!(nav && nav.usb) }

export function platformOf(nav = globalThis.navigator) {
  const p = String(nav?.userAgentData?.platform || '').toLowerCase()
  const ua = String(nav?.userAgent || '')
  if (p === 'windows' || /Windows/.test(ua)) return 'windows'
  if (p === 'macos' || /Macintosh|Mac OS X/.test(ua)) return 'mac'
  if (p === 'linux' || (/Linux/.test(ua) && !/Android|CrOS/.test(ua))) return 'linux'
  return 'other'
}

export function findDfuInterface(device) {
  const dfu = []
  for (const itf of device.configuration?.interfaces || []) {
    for (const alt of itf.alternates || []) {
      if (alt.interfaceClass === 0xfe && alt.interfaceSubclass === 0x01) dfu.push({ itf, alt })
    }
  }
  if (!dfu.length) throw new XmosError('not-dfu', 'That isn’t the audio chip in safe mode.')
  const mode = dfu.filter(d => d.alt.interfaceProtocol === 0x02)
  if (!mode.length) throw new XmosError('not-safe-mode', 'The audio chip isn’t in safe mode.')
  const up = mode.find(d => d.alt.alternateSetting === UPGRADE_ALT)
  if (!up) throw new XmosError('no-upgrade-slot', 'This chip has no upgrade slot; not flashing it.')
  return { interfaceNumber: up.itf.interfaceNumber }
}

export function parseDfuFunctional(bytes, interfaceNumber) {
  let cur = null
  for (let i = 0; i + 1 < bytes.length;) {
    const len = bytes[i], type = bytes[i + 1]
    if (len < 2) break
    if (type === 0x04 && i + 8 < bytes.length) {
      cur = { ifc: bytes[i + 2], cls: bytes[i + 5], sub: bytes[i + 6] }
    } else if (type === 0x21 && len >= 7 && cur && cur.ifc === interfaceNumber && cur.cls === 0xfe && cur.sub === 0x01) {
      const attributes = bytes[i + 2]
      return {
        attributes,
        manifestTolerant: (attributes & 0x04) !== 0,
        transferSize: bytes[i + 5] | (bytes[i + 6] << 8),
        dfuVersion: len >= 9 ? bytes[i + 7] | (bytes[i + 8] << 8) : 0,
      }
    }
    i += len
  }
  return null
}

export function audioSample(reply) {
  const a = reply && reply.ok && reply.data && reply.data.audio
  if (!a || typeof a.frames !== 'number') return null
  return { frames: a.frames, up: Number(reply.data.boot?.up_s ?? 0), ver: (a.dsp && a.dsp.ver) || '' }
}

// After an audio chip flash the node must read its own line's image version; another version
// means the other board's image went on.
export function expectVersion(result, line, images = XMOS_IMAGES) {
  const want = images[line]?.version
  if (!result || result.state !== 'ok' || !result.ver || !want || result.ver === want) return result
  return { state: 'wrong', ver: result.ver, expected: want }
}

export function audioVerdict(a, b) {
  if (!a || !b) return 'retry'
  if (b.up < a.up) return 'retry'   // it restarted between the reads: compare again
  return b.frames > a.frames ? 'ok' : 'silent'
}

// stop() ends the check at once (a newer check, a new board, a refused token): 'cancelled'.
export async function watchSound({ read, sleep, now = () => Date.now(), gapMs = 5000, deadlineMs = 60000, stop = () => false }) {
  const end = now() + deadlineMs
  const sample = async () => { try { return audioSample(await read()) } catch { return null } }
  const cancelled = { state: 'cancelled', ver: '' }
  await sleep(gapMs)
  while (now() < end) {
    if (stop()) return cancelled
    const a = await sample()
    if (stop()) return cancelled
    await sleep(gapMs)
    if (!a) continue
    if (stop()) return cancelled
    const b = await sample()
    if (stop()) return cancelled
    const v = audioVerdict(a, b)
    if (v !== 'retry') return { state: v, ver: b.ver }
  }
  return { state: 'unknown', ver: '' }
}

const ct = (request, value, index) => ({ requestType: 'class', recipient: 'interface', request, value, index })
const accessDenied = e => new XmosError('access-denied', `This computer blocked access to the chip (${(e && e.message) || e}).`)

export class XmosDfu {
  static async request(nav = globalThis.navigator) {
    return new XmosDfu(await nav.usb.requestDevice({ filters: [{ vendorId: XMOS_VENDOR_ID }] }))
  }

  constructor(device, { sleep = ms => new Promise(r => setTimeout(r, ms)) } = {}) {
    this.device = device; this.sleep = sleep
    this.ifc = null; this.transferSize = 0; this.manifestTolerant = false
  }

  async open() {
    const d = this.device
    try { await d.open() } catch (e) { throw accessDenied(e) }
    if (!d.configuration) await d.selectConfiguration(1)
    const { interfaceNumber } = findDfuInterface(d)
    try { await d.claimInterface(interfaceNumber) } catch (e) { throw accessDenied(e) }
    await d.selectAlternateInterface(interfaceNumber, UPGRADE_ALT)
    this.ifc = interfaceNumber
    const fd = parseDfuFunctional(await this._configDescriptor(), interfaceNumber)
    if (!fd || !fd.transferSize) throw new XmosError('no-descriptor', 'The chip didn’t describe its DFU settings; not flashing it.')
    this.transferSize = fd.transferSize
    this.manifestTolerant = fd.manifestTolerant
    await this._toIdle()
    return this
  }

  async _configDescriptor() {
    const setup = { requestType: 'standard', recipient: 'device', request: 0x06, value: 0x0200, index: 0 }
    const head = await this.device.controlTransferIn(setup, 9)
    const full = await this.device.controlTransferIn(setup, head.data.getUint16(2, true))
    return new Uint8Array(full.data.buffer, full.data.byteOffset, full.data.byteLength)
  }

  async _out(request, value, data) {
    const r = await this.device.controlTransferOut(ct(request, value, this.ifc), data || new Uint8Array(0))
    if (r && r.status && r.status !== 'ok') throw new XmosError('transfer', `USB transfer failed (${r.status}).`)
  }

  async _status() {
    const v = (await this.device.controlTransferIn(ct(DFU.GETSTATUS, 0, this.ifc), 6)).data
    return { status: v.getUint8(0), poll: v.getUint8(1) | (v.getUint8(2) << 8) | (v.getUint8(3) << 16), state: v.getUint8(4) }
  }

  async _toIdle() {
    let s = await this._status()
    if (s.state === DFU_STATE.ERROR) { await this._out(DFU.CLRSTATUS, 0); s = await this._status() }
    if (s.state !== DFU_STATE.IDLE) { await this._out(DFU.ABORT, 0); s = await this._status() }
    if (s.state !== DFU_STATE.IDLE) throw new XmosError('bad-state', `The chip didn’t return to idle (state ${s.state}).`)
  }

  async _waitFor(block, wanted) {
    for (;;) {
      const s = await this._status()
      if (s.status !== 0 || s.state === DFU_STATE.ERROR) {
        try { await this._out(DFU.CLRSTATUS, 0) } catch { /* the error below is the news */ }
        throw new XmosError('dfu-status', `The chip reported an error (status ${s.status}) at block ${block}.`)
      }
      if (wanted.includes(s.state)) return s
      if (!WAITING.includes(s.state)) throw new XmosError('bad-state', `Unexpected chip state ${s.state} at block ${block}.`)
      await this.sleep(Math.max(s.poll, 1))
    }
  }

  async flash(bytes, { onProgress } = {}) {
    if (this.ifc === null) throw new XmosError('bad-state', 'Connect the audio chip first.')
    const n = this.transferSize
    let block = 0
    for (let off = 0; off < bytes.length; off += n, block++) {
      const chunk = bytes.subarray(off, Math.min(off + n, bytes.length))
      await this._out(DFU.DNLOAD, block & 0xffff, chunk)
      await this._waitFor(block, [DFU_STATE.DNLOAD_IDLE])
      if (onProgress) onProgress(Math.round(((off + chunk.length) / bytes.length) * 100))
    }
    let manifested = 'disconnected'
    try {
      await this._out(DFU.DNLOAD, block & 0xffff)   // zero-length block: end of image
      const s = await this._waitFor(block, [DFU_STATE.IDLE, DFU_STATE.MANIFEST_WAIT_RESET])
      manifested = s.state === DFU_STATE.IDLE ? 'idle' : 'wait-reset'
    } catch (e) {
      if (e instanceof XmosError) throw e
      // A USB error after the last block: the chip left the bus to install the image.
      // The sound check after the replug is the verdict.
    }
    try { await this.device.reset() } catch { /* it may already be gone */ }
    return { manifested }
  }

  async close() { try { await this.device.close() } catch { /* already gone */ } }
}
