// Audio chip (XMOS) module tests. Run: node --test dashboard-ui/tests/
import test from 'node:test'
import assert from 'node:assert/strict'
import * as x from '../src/xmosDfu.js'
import {
  XMOS_IMAGES, XmosDfu, XmosError, DFU, DFU_STATE, checkXmosImage, fetchXmosImage, xmosImageUrl,
  findDfuInterface, parseDfuFunctional, audioSample, audioVerdict, watchSound, platformOf, isUsbSupported, sha256Hex,
} from '../src/xmosDfu.js'

const ST = DFU_STATE
const bytes = n => Uint8Array.from({ length: n }, (_, i) => (i * 7 + 3) & 0xff)
const quick = () => { const slept = []; return { slept, sleep: ms => { slept.push(ms); return Promise.resolve() } } }
const capture = fn => { try { fn() } catch (e) { return e } return null }

// The configuration descriptor a safe-mode chip returns: config header, one interface
// descriptor per alt setting, then the DFU functional descriptor.
function configBytes({ alts, cls, sub, protocol, transferSize, tolerant, functional }) {
  const out = [9, 2, 0, 0, 1, 1, 0, 0x80, 50]
  for (const a of alts) out.push(9, 4, 0, a, 0, cls, sub, protocol, 0)
  if (functional) out.push(9, 0x21, 0x01 | (tolerant ? 0x04 : 0), 0xff, 0x00, transferSize & 0xff, transferSize >> 8, 0x10, 0x01)
  out[2] = out.length & 0xff; out[3] = out.length >> 8
  return Uint8Array.from(out)
}

// A WebUSB device with a DFU 1.1 state machine that records every call.
class FakeDfu {
  constructor(o = {}) {
    this.o = { alts: [0, 1, 2], cls: 0xfe, sub: 0x01, protocol: 2, transferSize: 64, tolerant: false, functional: true,
               busy: 1, pollMs: 7, failAt: -1, statusOnly: false, manifest: 'wait-reset', start: ST.IDLE, stuck: false,
               openError: null, claimError: null, resetError: false, ...o }
    this.configuration = null
    this.configurations = [{ configurationValue: 1, interfaces: [{ interfaceNumber: 0, alternates: this.o.alts.map(a => ({
      alternateSetting: a, interfaceClass: this.o.cls, interfaceSubclass: this.o.sub, interfaceProtocol: this.o.protocol, interfaceName: null })) }] }]
    this.log = []; this.chunks = []; this.alt = null; this.state = this.o.start; this.busyLeft = 0; this.err = 0; this.gone = false
  }
  async open() { this.log.push(['open']); if (this.o.openError) throw this.o.openError }
  async close() { this.log.push(['close']) }
  async selectConfiguration(v) { this.log.push(['config', v]); this.configuration = this.configurations[0] }
  async claimInterface(n) { this.log.push(['claim', n]); if (this.o.claimError) throw this.o.claimError }
  async selectAlternateInterface(n, a) { this.log.push(['alt', n, a]); this.alt = a }
  async reset() { this.log.push(['reset']); if (this.o.resetError) throw new Error('NetworkError: device gone') }
  _st(status, poll, state) {
    const b = Uint8Array.from([status, poll & 0xff, (poll >> 8) & 0xff, poll >> 16, state, 0])
    return { status: 'ok', data: new DataView(b.buffer) }
  }
  async controlTransferIn(s, len) {
    if (this.gone) throw new Error('NotFoundError: device gone')
    if (s.requestType === 'standard') {
      this.log.push(['desc', len])
      const b = configBytes(this.o).slice(0, len)
      return { status: 'ok', data: new DataView(b.buffer) }
    }
    assert.equal(s.request, DFU.GETSTATUS, 'only GETSTATUS reads')
    this.log.push(['status'])
    let status = 0
    if (this.state === ST.DNLOAD_SYNC) {
      if (this.err) { status = this.err; this.state = this.o.statusOnly ? ST.DNLOAD_IDLE : ST.ERROR }
      else if (this.busyLeft > 0) { this.busyLeft--; return this._st(0, this.o.pollMs, ST.DNBUSY) }
      else this.state = ST.DNLOAD_IDLE
    } else if (this.state === ST.MANIFEST_SYNC) {
      if (this.o.manifest === 'drop') { this.gone = true; throw new Error('NetworkError: device left the bus') }
      if (this.o.manifest === 'stuck') return this._st(0, this.o.pollMs, ST.MANIFEST)
      this.state = this.o.manifest === 'idle' ? ST.IDLE : ST.MANIFEST_WAIT_RESET
      return this._st(0, this.o.pollMs, ST.MANIFEST)
    } else if (this.state === ST.ERROR) status = this.err || 0x0e
    return this._st(status, 0, this.state)
  }
  async controlTransferOut(s, data) {
    if (this.gone) throw new Error('NotFoundError: device gone')
    const len = data ? data.byteLength : 0
    this.log.push(['out', s.request, s.value, len, this.alt])
    if (s.request === DFU.DNLOAD) {
      assert.equal(this.alt, 1, 'DNLOAD only on the upgrade slot')
      if (len) {
        this.chunks.push(Uint8Array.from(data))
        this.err = s.value === this.o.failAt ? 0x0a : 0
        this.busyLeft = this.o.busy
        this.state = ST.DNLOAD_SYNC
      } else this.state = ST.MANIFEST_SYNC
    } else if (s.request === DFU.CLRSTATUS) { this.state = ST.IDLE; this.err = 0 }
    else if (s.request === DFU.ABORT) { if (!this.o.stuck) this.state = ST.IDLE }
    return { status: 'ok', bytesWritten: len }
  }
}
const withConfig = o => { const d = new FakeDfu(o); d.configuration = d.configurations[0]; return d }
const fakeImages = async () => {
  const b = bytes(100)
  return { b, images: { array: { repo: 'r/x', commit: 'c0ffee', path: 'a.bin', size: 100, sha256: await sha256Hex(b), version: '1.0.8' } } }
}

test('xmos: contract — frozen API and pinned images', () => {
  for (const f of ['xmosImageUrl', 'sha256Hex', 'checkXmosImage', 'fetchXmosImage', 'isUsbSupported', 'platformOf',
    'findDfuInterface', 'parseDfuFunctional', 'audioSample', 'audioVerdict', 'watchSound']) assert.equal(typeof x[f], 'function', f)
  assert.equal(typeof XmosDfu, 'function'); assert.equal(typeof XmosDfu.request, 'function')
  assert.ok(new XmosError('no-line', 'm') instanceof Error)
  assert.equal(x.XMOS_VENDOR_ID, 0x2886)
  assert.deepEqual(Object.keys(XMOS_IMAGES).sort(), ['array', 'flex'])
  assert.equal(XMOS_IMAGES.array.sha256, 'd60d0bc2c7f5a67ffa9c9206e066b2d8f1ccb49ebba673f3a7bcff1cf197dfb2')
  assert.equal(XMOS_IMAGES.flex.sha256, 'd1132b7e779818923072396315304a5dd2324aa56969b4111b30678f968a7c6a')
  assert.equal(XMOS_IMAGES.array.size, 888832); assert.equal(XMOS_IMAGES.flex.size, 888832)
  assert.equal(XMOS_IMAGES.array.version, '1.0.8'); assert.equal(XMOS_IMAGES.flex.version, '1.0.7')
})

test('xmos: image URLs are pinned to a commit, never a branch', () => {
  assert.equal(xmosImageUrl('array'), 'https://raw.githubusercontent.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/4b49bfd19977c63cf6e90dfe9e5827e74e20bf6d/xmos_firmwares/i2s/old_firmwares/application_xvf3800_i2s_master_v1.0.8_48k.bin')
  assert.equal(xmosImageUrl('flex'), 'https://raw.githubusercontent.com/formatBCE/Respeaker-XVF3800-ESPHome-integration/8c4aa8ad0098bcc8b076ef17660bab4e0cf0531d/application_xvf3800_inthost-lr48-sqr-i2c-v1.0.7-release.bin')
  for (const l of ['array', 'flex']) assert.doesNotMatch(xmosImageUrl(l), /\/(master|main)\//)
  assert.equal(capture(() => xmosImageUrl('waveshare')).code, 'no-line')
})

test('xmos: the image check passes only the pinned bytes', async () => {
  const { b, images } = await fakeImages()
  assert.equal((await checkXmosImage(b, 'array', { images })).length, 100)
  const short = await checkXmosImage(b.subarray(0, 99), 'array', { images }).catch(e => e)
  assert.equal(short.code, 'size-mismatch'); assert.match(short.message, /99 bytes, expected 100/)
  const bad = Uint8Array.from(b); bad[50] ^= 1
  const hash = await checkXmosImage(bad, 'array', { images }).catch(e => e)
  assert.equal(hash.code, 'hash-mismatch'); assert.match(hash.message, new RegExp(images.array.sha256.slice(0, 12)))
  assert.equal((await checkXmosImage(b, 'flex', { images }).catch(e => e)).code, 'no-line')
})

test('xmos: fetch uses the pinned URL and checks before returning', async () => {
  const { b, images } = await fakeImages()
  const seen = []
  const ok = await fetchXmosImage('array', { images, fetchFn: async u => { seen.push(u); return { ok: true, status: 200, arrayBuffer: async () => b.buffer.slice(0) } } })
  assert.equal(ok.length, 100)
  assert.deepEqual(seen, ['https://raw.githubusercontent.com/r/x/c0ffee/a.bin'])
  const e404 = await fetchXmosImage('array', { images, fetchFn: async () => ({ ok: false, status: 404 }) }).catch(e => e)
  assert.equal(e404.code, 'fetch-failed'); assert.match(e404.message, /HTTP 404/)
  const eNet = await fetchXmosImage('array', { images, fetchFn: async () => { throw new TypeError('Failed to fetch') } }).catch(e => e)
  assert.equal(eNet.code, 'fetch-failed'); assert.match(eNet.message, /network/)
  const bad = Uint8Array.from(b); bad[0] ^= 1
  const eHash = await fetchXmosImage('array', { images, fetchFn: async () => ({ ok: true, status: 200, arrayBuffer: async () => bad.buffer }) }).catch(e => e)
  assert.equal(eHash.code, 'hash-mismatch')
})

test('xmos: only a safe-mode DFU interface with an upgrade slot is accepted', () => {
  assert.deepEqual(findDfuInterface(withConfig()), { interfaceNumber: 0 })
  assert.equal(capture(() => findDfuInterface(withConfig({ cls: 0xff }))).code, 'not-dfu')
  assert.equal(capture(() => findDfuInterface(withConfig({ protocol: 1 }))).code, 'not-safe-mode')
  assert.equal(capture(() => findDfuInterface(withConfig({ alts: [0] }))).code, 'no-upgrade-slot')
})

test('xmos: the DFU functional descriptor gives the block size and manifest mode', () => {
  const d = new FakeDfu({ transferSize: 4096, tolerant: true })
  const fd = parseDfuFunctional(configBytes(d.o), 0)
  assert.equal(fd.transferSize, 4096); assert.equal(fd.manifestTolerant, true); assert.equal(fd.dfuVersion, 0x0110)
  assert.equal(parseDfuFunctional(configBytes(new FakeDfu({ functional: false }).o), 0), null)
  assert.equal(parseDfuFunctional(configBytes(d.o), 3), null)
})

test('xmos: open claims the DFU interface and selects alt 1, never alt 0', async () => {
  const d = new FakeDfu()
  const dfu = await new XmosDfu(d, quick()).open()
  assert.deepEqual(d.log.filter(l => l[0] === 'alt'), [['alt', 0, 1]])
  assert.ok(d.log.some(l => l[0] === 'config'))
  assert.equal(dfu.transferSize, 64)
})

test('xmos: open returns the chip to idle first', async () => {
  const e = new FakeDfu({ start: ST.ERROR }); await new XmosDfu(e, quick()).open()
  assert.ok(e.log.some(l => l[0] === 'out' && l[1] === DFU.CLRSTATUS))
  const b = new FakeDfu({ start: ST.DNLOAD_IDLE }); await new XmosDfu(b, quick()).open()
  assert.ok(b.log.some(l => l[0] === 'out' && l[1] === DFU.ABORT))
  const s = new FakeDfu({ start: ST.DNLOAD_IDLE, stuck: true })
  assert.equal((await new XmosDfu(s, quick()).open().catch(err => err)).code, 'bad-state')
})

test('xmos: open refuses before claiming anything that is not the chip in safe mode', async () => {
  for (const [o, code] of [[{ cls: 0xff }, 'not-dfu'], [{ protocol: 1 }, 'not-safe-mode'], [{ alts: [0] }, 'no-upgrade-slot']]) {
    const d = new FakeDfu(o)
    assert.equal((await new XmosDfu(d, quick()).open().catch(e => e)).code, code)
    assert.ok(!d.log.some(l => l[0] === 'claim'), code + ': nothing claimed')
  }
  assert.equal((await new XmosDfu(new FakeDfu({ functional: false }), quick()).open().catch(e => e)).code, 'no-descriptor')
})

test('xmos: blocked USB access is reported as access-denied', async () => {
  const se = Object.assign(new Error('Access denied.'), { name: 'SecurityError' })
  assert.equal((await new XmosDfu(new FakeDfu({ openError: se }), quick()).open().catch(e => e)).code, 'access-denied')
  const ne = Object.assign(new Error('Unable to claim interface.'), { name: 'NetworkError' })
  assert.equal((await new XmosDfu(new FakeDfu({ claimError: ne }), quick()).open().catch(e => e)).code, 'access-denied')
})

test('xmos: flash writes every byte in descriptor-sized blocks, numbered from 0, then a zero-length end', async () => {
  const d = new FakeDfu({ transferSize: 64, busy: 2, pollMs: 7 }); const q = quick()
  const dfu = await new XmosDfu(d, q).open()
  const img = bytes(64 * 5 + 10)
  const pct = []
  const r = await dfu.flash(img, { onProgress: p => pct.push(p) })
  const outs = d.log.filter(l => l[0] === 'out' && l[1] === DFU.DNLOAD)
  assert.deepEqual(outs.map(l => l[2]), [0, 1, 2, 3, 4, 5, 6])
  assert.deepEqual(outs.map(l => l[3]), [64, 64, 64, 64, 64, 10, 0])
  assert.ok(outs.every(l => l[4] === 1), 'every DNLOAD on alt 1')
  assert.deepEqual(Buffer.concat(d.chunks), Buffer.from(img))
  assert.ok(q.slept.filter(ms => ms === 7).length >= 12, 'DNBUSY waits honour bwPollTimeout')
  assert.equal(pct.at(-1), 100); assert.deepEqual(pct, [...pct].sort((a, b) => a - b))
  assert.equal(r.manifested, 'wait-reset')
  assert.ok(d.log.some(l => l[0] === 'reset'))
})

test('xmos: both manifest behaviours count as done, and a vanished chip too', async () => {
  for (const [manifest, want] of [['idle', 'idle'], ['wait-reset', 'wait-reset'], ['drop', 'disconnected']]) {
    const d = new FakeDfu({ manifest })
    const r = await (await new XmosDfu(d, quick()).open()).flash(bytes(100))
    assert.equal(r.manifested, want, manifest)
  }
  const d = new FakeDfu({ resetError: true })
  assert.equal((await (await new XmosDfu(d, quick()).open()).flash(bytes(10))).manifested, 'wait-reset')
})

test('xmos: an error status mid-write clears it and fails naming the block', async () => {
  const d = new FakeDfu({ failAt: 3 })
  const dfu = await new XmosDfu(d, quick()).open()
  const e = await dfu.flash(bytes(64 * 6)).catch(err => err)
  assert.equal(e.code, 'dfu-status'); assert.match(e.message, /block 3/)
  const i = d.log.findIndex(l => l[0] === 'out' && l[1] === DFU.DNLOAD && l[2] === 3)
  assert.ok(d.log.slice(i).some(l => l[0] === 'out' && l[1] === DFU.CLRSTATUS), 'CLRSTATUS after the error')
  assert.ok(!d.log.slice(i).some(l => l[0] === 'out' && l[1] === DFU.DNLOAD && l[2] === 4), 'stops writing')
})

test('xmos: a nonzero status fails even when the chip claims a normal state', async () => {
  const d = new FakeDfu({ failAt: 2, statusOnly: true })
  const e = await (await new XmosDfu(d, quick()).open()).flash(bytes(64 * 4)).catch(err => err)
  assert.equal(e && e.code, 'dfu-status'); assert.match(e.message, /block 2/)
})

// Fake time for the status waits. Past an hour of fake time it throws a plain Error, so a
// wait with no deadline fails the test instead of hanging it.
const dfuClock = () => {
  let t = 0; const slept = []
  return { slept, now: () => t, sleep: ms => {
    slept.push(ms); t += ms
    return t > 3600000 ? Promise.reject(new Error('still polling after an hour')) : Promise.resolve()
  } }
}

test('xmos: a chip stuck busy on a block fails after the wait deadline, naming the state', async () => {
  const d = new FakeDfu({ busy: Infinity, pollMs: 1000 }); const c = dfuClock()
  const e = await (await new XmosDfu(d, c).open()).flash(bytes(64 * 3)).catch(err => err)
  assert.ok(e instanceof XmosError, `an XmosError, got ${e}`)
  assert.equal(e.code, 'dfu-timeout')
  assert.match(e.message, new RegExp(`state ${ST.DNBUSY}\\b`)); assert.match(e.message, /block 0/)
  assert.ok(c.now() >= 60000 && c.now() <= 61000, `gave up after 60 s of chip time, not ${c.now()} ms`)
  assert.ok(!d.log.some(l => l[0] === 'out' && l[1] === DFU.DNLOAD && l[2] === 1), 'stops writing')
})

test('xmos: a chip stuck manifesting fails after the wait deadline instead of reading as done', async () => {
  const d = new FakeDfu({ manifest: 'stuck', pollMs: 1000 }); const c = dfuClock()
  const e = await (await new XmosDfu(d, c).open()).flash(bytes(100)).catch(err => err)
  assert.ok(e instanceof XmosError, `an XmosError, got ${JSON.stringify(e)}`)
  assert.equal(e.code, 'dfu-timeout'); assert.match(e.message, new RegExp(`state ${ST.MANIFEST}\\b`))
})

test('xmos: a huge bwPollTimeout sleeps the cap, a zero one the floor', async () => {
  const huge = quick()
  await (await new XmosDfu(new FakeDfu({ busy: 1, pollMs: 0xffffff }), huge).open()).flash(bytes(10))
  assert.ok(huge.slept.length > 0 && huge.slept.every(ms => ms === 5000), `slept ${huge.slept}`)
  const zero = quick()
  await (await new XmosDfu(new FakeDfu({ busy: 1, pollMs: 0 }), zero).open()).flash(bytes(10))
  assert.ok(zero.slept.length > 0 && zero.slept.every(ms => ms === 5), `slept ${zero.slept}`)
})

test('xmos: the wait deadline is per block — a slow chip within it still flashes', async () => {
  // 50 s busy on every block: past one deadline in total, inside it per block
  const d = new FakeDfu({ busy: 50, pollMs: 1000 }); const c = dfuClock()
  const r = await (await new XmosDfu(d, c).open()).flash(bytes(64 * 3))
  assert.equal(r.manifested, 'wait-reset'); assert.ok(c.now() >= 150000, `took ${c.now()} ms of chip time`)
  assert.ok(c.slept.every(ms => ms === 1000), 'an in-range bwPollTimeout is slept as given')
})

test('xmos: flash refuses before open', async () => {
  assert.equal((await new XmosDfu(new FakeDfu(), quick()).flash(bytes(10)).catch(e => e)).code, 'bad-state')
})

test('xmos: the USB picker is limited to Seeed devices', async () => {
  let filters = null
  const d = new FakeDfu()
  const got = await XmosDfu.request({ usb: { requestDevice: async o => { filters = o.filters; return d } } })
  assert.deepEqual(filters, [{ vendorId: 0x2886 }]); assert.equal(got.device, d)
})

const reply = (frames, up, ver = '1.0.8') => ({ ok: true, data: { boot: { up_s: up }, audio: { frames, dsp: { ver } } } })
test('xmos: audio samples and verdicts', () => {
  assert.deepEqual(audioSample(reply(10, 5)), { frames: 10, up: 5, ver: '1.0.8' })
  assert.equal(audioSample({ ok: false }), null); assert.equal(audioSample(null), null)
  assert.equal(audioSample({ ok: true, data: { audio: {} } }), null)
  assert.equal(audioVerdict(audioSample(reply(10, 5)), audioSample(reply(260, 10))), 'ok')
  assert.equal(audioVerdict(audioSample(reply(0, 5)), audioSample(reply(0, 10))), 'silent')
  assert.equal(audioVerdict(audioSample(reply(500, 40)), audioSample(reply(0, 2))), 'retry')
  assert.equal(audioVerdict(null, audioSample(reply(1, 1))), 'retry')
})

const clock = () => { let t = 0; return { now: () => t, sleep: ms => { t += ms; return Promise.resolve() } } }
test('xmos: the sound check — rising, silent, reboot in between, unreachable', async () => {
  const run = async seq => {
    const c = clock(); let i = 0
    return watchSound({ ...c, read: async () => { const v = seq[Math.min(i++, seq.length - 1)]; if (v instanceof Error) throw v; return v } })
  }
  assert.deepEqual(await run([reply(10, 5), reply(260, 10)]), { state: 'ok', ver: '1.0.8' })
  assert.deepEqual(await run([reply(0, 5, '1.0.4'), reply(0, 10, '1.0.4')]), { state: 'silent', ver: '1.0.4' })
  assert.deepEqual(await run([reply(500, 38), reply(0, 3), reply(0, 3), reply(250, 8)]), { state: 'ok', ver: '1.0.8' })
  assert.deepEqual(await run([new Error('504')]), { state: 'unknown', ver: '' })
})

test('xmos: platform detection for the driver hints', () => {
  assert.equal(platformOf({ userAgentData: { platform: 'Windows' }, userAgent: '' }), 'windows')
  assert.equal(platformOf({ userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }), 'windows')
  assert.equal(platformOf({ userAgentData: { platform: 'macOS' }, userAgent: '' }), 'mac')
  assert.equal(platformOf({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' }), 'linux')
  assert.equal(platformOf({ userAgent: 'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0)' }), 'other')
  assert.equal(isUsbSupported({ usb: {} }), true); assert.equal(isUsbSupported({}), false)
})

test('xmos: a download failure names the file to use and where to choose it', async () => {
  const { b, images } = await fakeImages()
  const hint = /a\.bin.*Or a downloaded file/
  const e404 = await fetchXmosImage('array', { images, fetchFn: async () => ({ ok: false, status: 404 }) }).catch(e => e)
  assert.equal(e404.code, 'fetch-failed'); assert.match(e404.message, /HTTP 404/); assert.match(e404.message, hint)
  const eNet = await fetchXmosImage('array', { images, fetchFn: async () => { throw new TypeError('Failed to fetch') } }).catch(e => e)
  assert.equal(eNet.code, 'fetch-failed'); assert.match(eNet.message, hint)
  // the body dropping mid-download is a download failure too, not a write failure
  const eBody = await fetchXmosImage('array', { images, fetchFn: async () => ({ ok: true, status: 200, arrayBuffer: async () => { throw new TypeError('network error') } }) }).catch(e => e)
  assert.equal(eBody.code, 'fetch-failed'); assert.match(eBody.message, hint)
  // a wrong file from GitHub (a captive portal page) points at the fallback as well
  const bad = Uint8Array.from(b); bad[0] ^= 1
  const eHash = await fetchXmosImage('array', { images, fetchFn: async () => ({ ok: true, status: 200, arrayBuffer: async () => bad.buffer }) }).catch(e => e)
  assert.equal(eHash.code, 'hash-mismatch'); assert.match(eHash.message, hint)
  // the user's own file is already the fallback: no hint
  const eOwn = await checkXmosImage(bad, 'array', { images }).catch(e => e)
  assert.equal(eOwn.code, 'hash-mismatch'); assert.doesNotMatch(eOwn.message, /Or a downloaded file/)
  assert.equal(x.xmosImageName('array', images), 'a.bin')
  assert.equal(x.xmosImageName('flex'), 'application_xvf3800_inthost-lr48-sqr-i2c-v1.0.7-release.bin')
})

test('xmos: a stopped sound check ends at once and reads no more', async () => {
  let reads = 0, stopped = false
  const r = await watchSound({ ...clock(), read: async () => { reads++; stopped = true; return reply(10, 5) }, stop: () => stopped })
  assert.deepEqual(r, { state: 'cancelled', ver: '' }); assert.equal(reads, 1)
  let reads2 = 0
  assert.deepEqual(await watchSound({ ...clock(), read: async () => { reads2++; return reply(10, 5) }, stop: () => true }), { state: 'cancelled', ver: '' })
  assert.equal(reads2, 0)
})

test("xmos: after a flash, a node reading another line's version is flagged", () => {
  assert.deepEqual(x.expectVersion({ state: 'ok', ver: '1.0.7' }, 'array'), { state: 'wrong', ver: '1.0.7', expected: '1.0.8' })
  assert.deepEqual(x.expectVersion({ state: 'ok', ver: '1.0.8' }, 'flex'), { state: 'wrong', ver: '1.0.8', expected: '1.0.7' })
  assert.deepEqual(x.expectVersion({ state: 'ok', ver: '1.0.8' }, 'array'), { state: 'ok', ver: '1.0.8' })
  assert.deepEqual(x.expectVersion({ state: 'ok', ver: '1.0.7' }, 'flex'), { state: 'ok', ver: '1.0.7' })
  assert.deepEqual(x.expectVersion({ state: 'silent', ver: '1.0.4' }, 'array'), { state: 'silent', ver: '1.0.4' })
  assert.deepEqual(x.expectVersion({ state: 'ok', ver: '' }, 'array'), { state: 'ok', ver: '' })
  assert.deepEqual(x.expectVersion({ state: 'ok', ver: '1.0.8' }, 'waveshare'), { state: 'ok', ver: '1.0.8' })
})
