// The Keys card's row helpers: the save route for a row (keyRoute) and the
// state chip it wears (keyState). App.jsx owns the rows and hands in the
// lane list; this module is pure logic, so `node --test` covers all of it.

// The save route for a row: the existing kind-aware routes (one write path).
// A catalog key with no route here can't be saved from the card (null) —
// never sent through another kind's route.
export function keyRoute(row, lanes) {
  if (row.kind === 'elevenlabs') return ['/api/dashboard/tts/key/elevenlabs', {}]
  if (row.kind === 'deepgram') return ['/api/dashboard/tts/key/deepgram', {}]
  if (row.kind === 'localai') return ['/api/dashboard/localai/key', {}]
  const lane = lanes.find(l => l.kind === row.kind)
  if (!lane) return null
  const named = lane.per_agent_keys && row.agent && row.agent !== lane.default_agent
  return ['/api/dashboard/intelligence/key', { kind: row.kind, ...(named ? { agent: row.agent } : {}) }]
}

export function keyState(row) {
  // Chip words: lowercase, one or two words (brand Chip rule); the restart
  // note moves to the hover. Warning tone = needs a restart or a key.
  // A key nothing uses yet needs no restart: neutral tone, no restart note.
  if (!row.set && row.pending_restart) return row.in_use
    ? { text: 'removed', tone: 'warning', title: 'Removed. Restart the bot to apply.' }
    : { text: 'removed', tone: 'neutral', title: 'Removed. Nothing uses it, so no restart is needed.' }
  if (!row.set) return row.in_use ? { text: 'missing', tone: 'warning' } : { text: 'not set', tone: 'neutral' }
  if (row.pending_restart) return row.in_use
    ? { text: `saved · ${row.hint || ''}`, tone: 'warning', title: 'Saved. Restart the bot to apply.' }
    : { text: `saved · ${row.hint || ''}`, tone: 'neutral', title: 'Saved. Not in use yet: switching to it marks a restart pending.' }
  return { text: `set · ${row.hint || ''}`, tone: 'neutral' }
}
