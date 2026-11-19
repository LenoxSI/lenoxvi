// Overview status chip tests. Run: node --test dashboard-ui/tests/
import test from 'node:test'
import assert from 'node:assert/strict'
import * as chips from '../src/chips.js'
import {
  BUILTIN, ECHO_KIND, TTS_NAMES, ttsName, TTS_KEY_HINT, STT_CLOUD,
  intelligenceChip, nodesChip, ttsChip, sttChip,
} from '../src/chips.js'

// The lane list App.jsx passes in (BUILTIN_LANES' shape), slimmed to the
// fields the chips read: kind, label, per_agent_keys, default_agent, brand.
const LANES = [
  { kind: 'hermes', label: 'Hermes', per_agent_keys: true, default_agent: 'default',
    brand: { backgroundColor: 'rgba(255, 255, 255, 0.92)', color: '#2563eb' } },
  { kind: 'openclaw', label: 'OpenClaw', per_agent_keys: false, default_agent: null,
    brand: { backgroundColor: 'rgba(23, 23, 23, 0.9)', color: '#ef4444' } },
  { kind: 'nanobot', label: 'Nanobot', per_agent_keys: false, default_agent: null, brand: null },
]

test('contract: the four chips and the tables they share are exported', () => {
  for (const name of ['intelligenceChip', 'nodesChip', 'ttsChip', 'sttChip', 'ttsName']) {
    assert.equal(typeof chips[name], 'function', name)
  }
  assert.equal(BUILTIN, 'builtin')
  assert.equal(ECHO_KIND, 'stub')
  assert.deepEqual(TTS_NAMES, { elevenlabs: 'ElevenLabs', deepgram: 'Deepgram', 'deepgram-flux': 'Deepgram Flux', kokoro: 'Kokoro', localai: 'LocalAI' })
  assert.deepEqual(TTS_KEY_HINT, { elevenlabs: 'elevenlabs_key_hint', deepgram: 'deepgram_key_hint', 'deepgram-flux': 'deepgram_key_hint', localai: 'localai_key_hint' })
  assert.deepEqual(Object.keys(STT_CLOUD), ['deepgram', 'elevenlabs'])
  assert.equal(STT_CLOUD.deepgram.name, 'Deepgram')
  assert.equal(STT_CLOUD.elevenlabs.name, 'ElevenLabs')
})

test('ttsName: known providers read their brand name, an unknown one passes through', () => {
  assert.equal(ttsName('kokoro'), 'Kokoro')
  assert.equal(ttsName('deepgram-flux'), 'Deepgram Flux')
  assert.equal(ttsName('madeup'), 'madeup')
  assert.equal(ttsName(undefined), undefined)
})

test("intelligenceChip: 'echo' (neutral, not a link) while the kind is the echo stub or the status line says echo", () => {
  const echo = { tone: 'neutral', text: 'echo', title: 'No intelligence picked yet: the bot repeats what it hears. Pick one on the Intelligence card.' }
  assert.deepEqual(intelligenceChip({}, { kind: 'stub' }, LANES), echo)
  assert.deepEqual(intelligenceChip({ intelligence: 'echo' }, null, LANES), echo)
})

test("intelligenceChip: a picked kind with no URL warns 'no url' and links to the Intelligence card", () => {
  assert.deepEqual(intelligenceChip({}, { kind: 'openclaw', url: '' }, LANES),
    { tone: 'warning', text: 'no url', href: '#intelligence', title: 'No OpenClaw URL saved. Set it on the Intelligence card.' })
  // An unknown kind falls back to the first lane's label.
  assert.deepEqual(intelligenceChip({}, { kind: 'madeup' }, LANES),
    { tone: 'warning', text: 'no url', href: '#intelligence', title: 'No Hermes URL saved. Set it on the Intelligence card.' })
})

test("intelligenceChip: a URL but no key warns 'no key' and links to the Keys card", () => {
  assert.deepEqual(intelligenceChip({}, { kind: 'openclaw', url: 'http://host:18789', key_set: false }, LANES),
    { tone: 'warning', text: 'no key', href: '#keys', title: 'No OpenClaw key saved. Add it on the Keys card.' })
  // A per-agent-key lane on its default profile is judged by the base key.
  assert.deepEqual(intelligenceChip({}, { kind: 'hermes', url: 'http://host:8642', key_set: false, agents: { hermes: 'default' } }, LANES),
    { tone: 'warning', text: 'no key', href: '#keys', title: 'No Hermes key saved. Add it on the Keys card.' })
})

test("intelligenceChip: a Hermes profile with its own key never gets a false 'no key'", () => {
  assert.deepEqual(intelligenceChip({ intelligence: 'Hermes · butler' },
    { kind: 'hermes', url: 'http://host:8642', key_set: false, agents: { hermes: 'butler' } }, LANES),
    { tone: 'brand', muted: false, style: { backgroundColor: 'rgba(255, 255, 255, 0.92)', color: '#2563eb' }, text: 'hermes' })
})

test('intelligenceChip: only a known lane wears brand colours, and the agent is stripped from the label', () => {
  // A known lane with brand colours: the chip shows the brand, wearing them.
  assert.deepEqual(intelligenceChip({ intelligence: 'OpenClaw · butler' },
    { kind: 'openclaw', url: 'http://host:18789', key_set: true }, LANES),
    { tone: 'brand', muted: false, style: { backgroundColor: 'rgba(23, 23, 23, 0.9)', color: '#ef4444' }, text: 'openclaw' })
  // A known lane with no brand of its own: a plain chip, not the muted placeholder.
  assert.deepEqual(intelligenceChip({ intelligence: 'Nanobot' },
    { kind: 'nanobot', url: 'http://host:8900', key_set: true }, LANES),
    { tone: 'neutral', muted: false, style: null, text: 'nanobot' })
  // The loading placeholder and an unknown brand never wear brand colours.
  assert.deepEqual(intelligenceChip({}, null, LANES), { tone: 'neutral', muted: true, style: null, text: '—' })
  assert.deepEqual(intelligenceChip(null, null, LANES), { tone: 'neutral', muted: true, style: null, text: '—' })
  assert.deepEqual(intelligenceChip({ intelligence: 'Madeup Cloud' },
    { kind: 'madeup', url: 'http://host:1', key_set: true }, LANES),
    { tone: 'neutral', muted: true, style: null, text: 'madeup cloud' })
})

test("nodesChip: '—' while /status loads, 'no nodes' when the list is empty", () => {
  assert.deepEqual(nodesChip(null, null), { text: '—', muted: true })
  assert.deepEqual(nodesChip([], {}), { text: 'no nodes', muted: true })
})

test("nodesChip: an online room with no zone warns 'N unmapped' and links to Rooms", () => {
  const rooms = [{ room: 'Kitchen', online: true }, { room: 'Garage', online: true }]
  assert.deepEqual(nodesChip(rooms, { room_zones: { Kitchen: 'den' } }),
    { tone: 'warning', text: '1 unmapped', href: '#rooms',
      title: 'No speaker zone for Garage: replies there play nowhere. All nodes online.' })
  // The title names the offline rooms too, when there are any.
  assert.deepEqual(nodesChip([{ room: 'Kitchen', online: true }, { room: 'Garage', online: false }], { room_zones: {} }),
    { tone: 'warning', text: '1 unmapped', href: '#rooms',
      title: 'No speaker zone for Kitchen: replies there play nowhere. Garage offline.' })
})

test("nodesChip: a room counts as mapped through its own speaker (a builtin zone)", () => {
  const rooms = [{ room: 'Garage', online: true }]
  assert.deepEqual(nodesChip(rooms, { room_zones: {}, speakers: { Garage: { zone: BUILTIN } } }),
    { tone: 'success', text: '1/1', title: 'All nodes online' })
  // Without its own speaker it is the unmapped warning.
  assert.deepEqual(nodesChip(rooms, { room_zones: {} }),
    { tone: 'warning', text: '1 unmapped', href: '#rooms',
      title: 'No speaker zone for Garage: replies there play nowhere. All nodes online.' })
})

test('nodesChip: green when every node is up, red when all are down, yellow naming the rooms offline', () => {
  const rooms = [{ room: 'Kitchen', online: true }, { room: 'Garage', online: false }]
  assert.deepEqual(nodesChip([{ room: 'Kitchen', online: true }], { room_zones: { Kitchen: 'den' } }),
    { tone: 'success', text: '1/1', title: 'All nodes online' })
  assert.deepEqual(nodesChip(rooms.map(r => ({ ...r, online: false })), { room_zones: { Kitchen: 'den', Garage: 'den' } }),
    { tone: 'danger', text: '0/2', title: 'All nodes offline' })
  assert.deepEqual(nodesChip(rooms, { room_zones: { Kitchen: 'den', Garage: 'den' } }),
    { tone: 'warning', text: '1/2', title: 'Garage offline' })
})

test('nodesChip: unmapped rooms are judged only once /mapping has loaded', () => {
  const rooms = [{ room: 'Kitchen', online: true }]
  for (const mapping of [null, {}, { speakers: { Kitchen: { zone: 'den' } } }]) {
    assert.deepEqual(nodesChip(rooms, mapping), { tone: 'success', text: '1/1', title: 'All nodes online' }, String(mapping))
  }
})

test("ttsChip: '—' while /status has not answered", () => {
  assert.deepEqual(ttsChip(null, null), { text: '—', muted: true })
  assert.deepEqual(ttsChip(null, { provider: 'kokoro' }), { text: '—', muted: true })
})

test("ttsChip: a room that can't speak is the danger chip 'tts down'; an offline room's stale report doesn't count", () => {
  assert.deepEqual(ttsChip({ rooms: [{ room: 'Kitchen', online: true, tts_usable: false }] }, null),
    { tone: 'danger', text: 'tts down', title: "Kitchen can't speak. Reboot the node (or the bot) to rebuild its session." })
  assert.deepEqual(ttsChip({ rooms: [{ room: 'Garage', online: false, tts_usable: false }] }, null),
    { text: '—', muted: true })
})

test("ttsChip: a configured cloud voice with no saved key warns 'no key' and links to Keys; Kokoro and a saved key need none", () => {
  assert.deepEqual(ttsChip({ rooms: [] }, { provider: 'elevenlabs' }),
    { tone: 'warning', text: 'no key', href: '#keys', title: 'No ElevenLabs key saved. Add it on the Keys card.' })
  assert.deepEqual(ttsChip({ rooms: [], tts_provider: 'kokoro' }, { provider: 'kokoro' }),
    { text: 'kokoro', muted: false })
  assert.deepEqual(ttsChip({ rooms: [], tts_provider: 'deepgram-flux' },
    { provider: 'deepgram-flux', deepgram_key_hint: 'ab12' }),
    { text: 'deepgram flux', muted: false })
})

test("ttsChip: rooms on the local fallback voice warn '<name> fallback', naming the unreachable provider", () => {
  const rooms = [{ room: 'Kitchen', online: true, tts_provider: 'Kokoro (fallback)' }]
  assert.deepEqual(ttsChip({ rooms, tts_provider: 'elevenlabs' }, { provider: 'elevenlabs', elevenlabs_key_hint: 'ab12' }),
    { tone: 'warning', text: 'kokoro fallback', title: 'Kitchen on the local Kokoro voice: ElevenLabs is unreachable' })
})

test('ttsChip: the healthy chip names the provider the rooms use; the configured one until a room reports', () => {
  // A room reporting a provider outside the configured one's family wins.
  assert.deepEqual(ttsChip({ rooms: [{ room: 'Kitchen', online: true, tts_provider: 'kokoro' }], tts_provider: 'elevenlabs' },
    { provider: 'elevenlabs', elevenlabs_key_hint: 'ab12' }), { text: 'kokoro', muted: false })
  // Rooms report Deepgram Flux as plain "deepgram": the configured name is the more specific.
  assert.deepEqual(ttsChip({ rooms: [{ room: 'Kitchen', online: true, tts_provider: 'deepgram' }], tts_provider: 'deepgram-flux' },
    { provider: 'deepgram-flux', deepgram_key_hint: 'ab12' }), { text: 'deepgram flux', muted: false })
  // Offline rooms don't count: their last report is stale.
  assert.deepEqual(ttsChip({ rooms: [{ room: 'Garage', online: false, tts_provider: 'kokoro' }], tts_provider: 'elevenlabs' },
    { provider: 'elevenlabs', elevenlabs_key_hint: 'ab12' }), { text: 'elevenlabs', muted: false })
})

test("sttChip: '—' before /stt answers, 'loading' while Whisper loads, then 'whisper <model>' when it's up", () => {
  assert.deepEqual(sttChip(null), { text: '—', muted: true })
  assert.deepEqual(sttChip({ provider: 'whisper', state: 'loading', model: 'base' }),
    { text: 'loading', muted: true, title: 'Whisper base is loading' })
  assert.deepEqual(sttChip({ provider: 'whisper', state: 'ready', model: 'base' }), { text: 'whisper base' })
})

test("sttChip: Whisper failing to load is the danger chip 'whisper failed', on Embedded or on standby", () => {
  assert.deepEqual(sttChip({ provider: 'whisper', state: 'error', error: 'no model found' }),
    { tone: 'danger', text: 'whisper failed', title: 'no model found' })
  // A cloud provider on standby reports Whisper's own failure.
  assert.deepEqual(sttChip({ provider: 'deepgram', active: 'whisper', state: 'error' }),
    { tone: 'danger', text: 'whisper failed', title: 'Whisper failed to load' })
})

test("sttChip: a cloud provider fallen back to Whisper warns 'embedded fallback' naming it and its rooms", () => {
  assert.deepEqual(sttChip({ provider: 'deepgram', active: 'whisper', fallback_reason: 'Deepgram down', fallback_rooms: ['Kitchen', 'Garage'] }),
    { tone: 'warning', text: 'embedded fallback', title: 'Deepgram down (rooms: Kitchen, Garage)' })
  assert.deepEqual(sttChip({ provider: 'elevenlabs', active: 'whisper' }),
    { tone: 'warning', text: 'embedded fallback', title: 'ElevenLabs unreachable' })
})

test("sttChip: the cloud chips read 'deepgram <model>' and 'elevenlabs scribe'", () => {
  assert.deepEqual(sttChip({ provider: 'deepgram', active: 'deepgram', cloud_model: 'nova-3' }), { text: 'deepgram nova-3' })
  assert.deepEqual(sttChip({ provider: 'deepgram', active: 'deepgram' }), { text: 'deepgram' })
  assert.deepEqual(sttChip({ provider: 'elevenlabs', active: 'elevenlabs' }), { text: 'elevenlabs scribe' })
})

test("sttChip: LocalAI down warns 'embedded fallback' linking to the LocalAI card; up, the chip reads 'localai <model>'", () => {
  assert.deepEqual(sttChip({ provider: 'localai', active: 'whisper' }),
    { tone: 'warning', text: 'embedded fallback', href: '#localai',
      title: 'LocalAI unreachable: Whisper base on this appliance is listening' })
  assert.deepEqual(sttChip({ provider: 'localai', active: 'localai', localai_model: 'whisper-large-v3' }),
    { text: 'localai whisper-large-v3' })
  assert.deepEqual(sttChip({ provider: 'localai', active: 'localai' }), { text: 'localai' })
})
