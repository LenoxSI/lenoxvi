// The Overview status chips' pure logic: what each chip says, its tone, its
// hover title and its setup link, from the shapes /api/dashboard/* returns.
// App.jsx owns the data and hands in the lane list; this module decides the
// chips. No DOM, no React, no fetch, so `node --test` covers all of it.

const esc = s => String(s ?? '')

// A node's own speaker as a reply zone (server: node_speaker.BUILTIN).
export const BUILTIN = 'builtin'

// intelligence.kind for "no intelligence picked yet": the bot answers from its echo
// stub. Not a lane (no URL, key or agents); a fresh install starts here.
export const ECHO_KIND = 'stub'

// Intelligence: the brand the bot runs, unless the configured kind has no
// URL or no key saved (the next restart can't reach it).
export function intelligenceChip(status, intel, lanes) {
  const laneOf = kind => lanes.find(l => l.kind === kind)
  // Unknown kind -> the first lane's label (today's 'Hermes' fallback).
  const laneLabel = kind => laneOf(kind)?.label || lanes[0].label
  // No intelligence picked yet (a fresh install): the bot echoes. Not a fault, so a
  // plain chip (only setup warnings are links); the hover says what to do.
  if ((intel?.kind || '') === ECHO_KIND || (!intel && status?.intelligence === 'echo'))
    return { tone: 'neutral', text: 'echo', title: 'No intelligence picked yet: the bot repeats what it hears. Pick one on the Intelligence card.' }
  if (intel) {
    const label = laneLabel(intel.kind)
    if (!intel.url)
      return { tone: 'warning', text: 'no url', href: '#intelligence', title: `No ${label} URL saved. Set it on the Intelligence card.` }
    // A per-agent-key lane's profile reads its own key (e.g.
    // LVI_HERMES_KEY_<AGENT>), which /intelligence doesn't report: only the
    // kind's base key is judged here.
    const profile = laneOf(intel.kind)?.per_agent_keys && intel.agents?.[intel.kind] && intel.agents[intel.kind] !== laneOf(intel.kind).default_agent
    if (!profile && !intel.key_set)
      return { tone: 'warning', text: 'no key', href: '#keys', title: `No ${label} key saved. Add it on the Keys card.` }
  }
  // Brand chips: the colours come from the lane list. Inline
  // styles — these are foreign brands, they don't earn design tokens.
  // Chip shows the brand only — the agent id lives in the Intelligence
  // section: strip " · <agent>" from the label.
  const brand = (esc(status?.intelligence) || '—').split(' · ')[0]
  const lane = lanes.find(l => l.label === brand)
  // Only a real brand wears brand colors — the loading
  // placeholder ('—') must not wear brand colors.
  const s = lane?.brand || null
  // A lane with no brand of its own: a plain chip, not the muted placeholder.
  return { tone: s ? 'brand' : 'neutral', muted: !s && !lane, style: s, text: brand.toLowerCase() }
}

// Nodes online: green = every node up, yellow = some down, red = all down.
// An online room with no zone is a warning too: unmapped rooms are silent
// by design (a speaker node's unmapped room plays on its own speaker).
// Judged only once /mapping has loaded.
export function nodesChip(rooms, mapping) {
  if (!rooms) return { text: '—', muted: true }
  if (!rooms.length) return { text: 'no nodes', muted: true }
  const down = rooms.filter(r => !r.online).map(r => esc(r.room))
  const text = `${rooms.length - down.length}/${rooms.length}`
  const heard = room => mapping.room_zones[room] || mapping.speakers?.[room]?.zone === BUILTIN
  const silent = mapping?.room_zones ? rooms.filter(r => r.online && !heard(r.room)).map(r => esc(r.room)) : []
  if (silent.length)
    return { tone: 'warning', text: `${silent.length} unmapped`, href: '#rooms',
      title: `No speaker zone for ${silent.join(', ')}: replies there play nowhere. ${down.length ? `${down.join(', ')} offline.` : 'All nodes online.'}` }
  if (!down.length) return { tone: 'success', text, title: 'All nodes online' }
  if (down.length === rooms.length) return { tone: 'danger', text, title: 'All nodes offline' }
  return { tone: 'warning', text, title: `${down.join(', ')} offline` }
}

export const TTS_NAMES = { elevenlabs: 'ElevenLabs', deepgram: 'Deepgram', 'deepgram-flux': 'Deepgram Flux', kokoro: 'Kokoro', localai: 'LocalAI' }
export const ttsName = p => TTS_NAMES[p] || p

// TTS: the voice service the rooms are actually using (each online room
// reports its live provider); the configured one only until a room has
// reported — config edits apply on the next restart. Offline rooms don't
// count: their last report is stale. A configured cloud voice with no saved
// key (GET /tts) is a warning: it can't speak after the next restart.
export const TTS_KEY_HINT = { elevenlabs: 'elevenlabs_key_hint', deepgram: 'deepgram_key_hint', 'deepgram-flux': 'deepgram_key_hint', localai: 'localai_key_hint' }
export function ttsChip(status, tts) {
  if (!status) return { text: '—', muted: true }
  const live = (status.rooms || []).filter(r => r.online)
  const down = live.filter(r => r.tts_usable === false).map(r => esc(r.room))
  if (down.length)
    return { tone: 'danger', text: 'tts down',
      title: `${down.join(', ')} can't speak. Reboot the node (or the bot) to rebuild its session.` }
  const hint = TTS_KEY_HINT[tts?.provider]
  if (hint && !tts[hint])
    return { tone: 'warning', text: 'no key', href: '#keys',
      title: `No ${ttsName(tts.provider)} key saved. Add it on the Keys card.` }
  const isFb = r => String(r.tts_provider || '').endsWith('(fallback)')
  // Rooms report Deepgram Flux as plain "deepgram": when the room's service
  // is the configured one's family, the configured name is the more
  // specific (a switch between the two Deepgram voices shows early).
  const roomP = live.find(r => r.tts_provider && !isFb(r))?.tts_provider
  const cfgP = status.tts_provider
  const primary = roomP && !String(cfgP || '').startsWith(roomP) ? roomP : (cfgP || roomP)
  const fb = live.filter(isFb)
  if (fb.length) {
    const name = ttsName(String(fb[0].tts_provider).replace(/\s*\(fallback\)$/, ''))
    return { tone: 'warning', text: `${name.toLowerCase()} fallback`,
      title: `${fb.map(r => esc(r.room)).join(', ')} on the local ${name} voice: ${ttsName(primary)} is unreachable` }
  }
  return { text: ttsName(primary)?.toLowerCase() || '—', muted: !primary }
}

// Cloud STT providers (Whisper is the local one): the name in messages, the
// GET /stt flag saying its key is saved, and its chip words.
export const STT_CLOUD = {
  deepgram: { name: 'Deepgram', key: 'deepgram_key_set', chip: st => `deepgram ${st.cloud_model || ''}`.trim() },
  elevenlabs: { name: 'ElevenLabs', key: 'elevenlabs_key_set', chip: () => 'elevenlabs scribe' },
}

// STT: Whisper <model> on Embedded, or a LocalAI or cloud provider with
// Embedded Whisper as its fallback.
export function sttChip(st) {
  if (!st) return { text: '—', muted: true }
  const whisper = `Whisper ${st.model || ''}`.trim()
  const cloud = STT_CLOUD[st.provider]
  const onWhisper = !cloud || st.active === 'whisper'
  if (onWhisper && st.state === 'error') return { tone: 'danger', text: 'whisper failed', title: st.error || 'Whisper failed to load' }
  if (cloud && st.active === 'whisper') {
    const rooms = (st.fallback_rooms || []).map(esc)
    return { tone: 'warning', text: 'embedded fallback',
      title: `${st.fallback_reason || `${cloud.name} unreachable`}${rooms.length ? ` (rooms: ${rooms.join(', ')})` : ''}` }
  }
  if (cloud) return { text: cloud.chip(st) }
  if (st.provider === 'localai') {
    if (st.active === 'whisper')
      return { tone: 'warning', text: 'embedded fallback', href: '#localai',
        title: `${st.fallback_reason || 'LocalAI unreachable'}: Whisper base on this appliance is listening` }
    return { text: `localai ${st.localai_model || ''}`.trim() }
  }
  if (st.state === 'loading') return { text: 'loading', muted: true, title: `${whisper} is loading` }
  return { text: whisper.toLowerCase() }
}
