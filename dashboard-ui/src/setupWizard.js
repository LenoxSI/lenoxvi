// The first-run setup wizard's pure logic: its steps, when it opens by itself,
// and the readouts it builds from GET /api/dashboard/setup/probe. No DOM, no
// React, no fetch, so `node --test` covers all of it.
import { TAB_IDS } from './tabs.js'

// The probe's step ids, in its order. The id 'brain' is the server's; what
// the user reads is the title, and that says Intelligence.
export const STEPS = [
  { id: 'welcome', title: 'Welcome', skippable: false },
  { id: 'listening', title: 'Listening', skippable: true },
  { id: 'voice', title: 'Voice', skippable: true },
  { id: 'brain', title: 'Intelligence', skippable: true },
  { id: 'wake', title: 'Wake word', skippable: true },
  { id: 'speakers', title: 'Speakers', skippable: true },
  { id: 'nodes', title: 'Nodes', skippable: true },
  { id: 'finish', title: 'Restart and test', skippable: true },
]

const IDS = STEPS.map(s => s.id)

// Opens by itself only on a marked box, and only over the default page or a
// tab: a card hash (#keys) is a link someone followed on purpose.
export function shouldOpen(probe, hash) {
  const id = String(hash ?? '').replace(/^#/, '')
  return probe?.pending === true && (id === '' || TAB_IDS.includes(id))
}

export function nextStep(id) {
  const i = IDS.indexOf(id)
  return i >= 0 && i < IDS.length - 1 ? IDS[i + 1] : null
}

export function prevStep(id) {
  const i = IDS.indexOf(id)
  return i > 0 ? IDS[i - 1] : null
}

// One step's state from the probe; a step an older bot doesn't report reads
// as not done.
export function stepView(probe, id) {
  const s = (Array.isArray(probe?.steps) ? probe.steps : []).find(x => x?.id === id)
  return { done: s?.done === true, detail: typeof s?.detail === 'string' ? s.detail : '', tier: s?.tier ?? null }
}

// What to tell the user to say: the box's wake name, else the selected stock
// model's key as words. Always computed from the API, never a literal.
export function wakePhrase(models, wakeName) {
  const name = String(wakeName ?? '').split(/\s+/).filter(Boolean).join(' ')
  if (name) return name
  const rows = Array.isArray(models) ? models : Array.isArray(models?.models) ? models.models : []
  const row = rows.find(m => m?.selected && m.source === 'stock')
  const words = String(row?.name ?? '').replace(/\.(onnx|tflite)$/, '').split(/[_\s-]+/).filter(Boolean)
  return words.length ? words.map(w => w[0].toUpperCase() + w.slice(1)).join(' ') : 'the wake word'
}

// The last wake, only if it came after this boot (a wake from before the
// restart proves nothing about the new setup).
export function heardSince(probe) {
  const wake = probe?.last_wake
  const boot = probe?.boot_time
  if (!wake || !Number.isFinite(wake.time) || !Number.isFinite(boot) || wake.time <= boot) return null
  return { room: wake.room ?? null, time: wake.time }
}

// Welcome's token field: none when the box has no token, else check a stored
// one or ask for it.
export function tokenStep(probe, stored) {
  if (!probe?.token_required) return 'none'
  return stored ? 'check' : 'ask'
}
