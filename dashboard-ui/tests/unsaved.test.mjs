// Unsaved-changes helper tests. Run: node --test dashboard-ui/tests/
import test from 'node:test'
import assert from 'node:assert/strict'
import * as unsaved from '../src/unsaved.js'
import { norm, same, saveCards } from '../src/unsaved.js'

// A cards ref and its units in the shapes App.jsx passes: cards.current[k]
// holds the card, units[k].label names it in the bar, and each save(say)
// reports through say, returning true once saved. A null save: the card
// never registered, so it is missing.
const rig = saves => {
  const cards = { current: {} }, units = {}
  for (const [k, label, save] of saves) {
    if (save) cards.current[k] = { save }
    units[k] = { label }
  }
  return [cards, units]
}

test('contract: norm, same and saveCards are exported', () => {
  for (const name of ['norm', 'same', 'saveCards']) {
    assert.equal(typeof unsaved[name], 'function', name)
  }
})

test("norm: unset reads as '', strings trim, other values pass through", () => {
  assert.equal(norm(null), '')
  assert.equal(norm(undefined), '')
  assert.equal(norm('  hermes '), 'hermes')
  assert.equal(norm(0.5), 0.5)
})

test("same: unset reads as '' on either side", () => {
  assert.ok(same(null, ''))
  assert.ok(same(undefined, null))
  assert.ok(same('', '   '))
})

test('same: strings compare trimmed', () => {
  assert.ok(same('  hermes ', 'hermes'))
  assert.ok(!same(' hermes', 'openclaw'))
})

test('same: numbers and numeric strings compare as numbers', () => {
  assert.ok(same('0.5', 0.5))
  assert.ok(same(1, '1.00'))
  assert.ok(!same('1', '2'))
})

test('same: an empty value never equals a number', () => {
  assert.ok(!same('', 0))
  assert.ok(!same(null, 0))
  assert.ok(!same(0, ''))
})

test('same: non-numeric strings compare as strings', () => {
  assert.ok(same('hermes', 'hermes'))
  assert.ok(!same('hermes', 'openclaw'))
  assert.ok(!same('1x', '1 x'))
})

test("saveCards: the listed cards save one after another in the order of keys; all saved reads 'Saved.'", async () => {
  const order = []
  const [cards, units] = rig([
    ['rooms', 'Room mapping', () => { order.push('rooms'); return true }],
    ['voice', 'TTS', () => { order.push('voice'); return true }],
  ])
  const r = await saveCards(cards, units, ['voice', 'rooms'])
  assert.deepEqual(order, ['voice', 'rooms'])
  assert.deepEqual(r, { ok: true, notes: [], text: 'Saved.' })
})

test("saveCards: a 'Saved' message that mentions a restart adds 'Restart to apply.'", async () => {
  const [cards, units] = rig([['voice', 'TTS', say => { say('Saved — restart to apply.'); return true }]])
  const r = await saveCards(cards, units, ['voice'])
  assert.deepEqual(r, { ok: true, notes: [], text: 'Saved. Restart to apply.' })
})

test("saveCards: a 'Saved' message that says 'no restart' adds nothing", async () => {
  const [cards, units] = rig([
    ['stt', 'STT', say => { say('Saved — no restart needed.'); return true }],
    ['voice', 'TTS', say => { say('Saved.'); return true }],
  ])
  const r = await saveCards(cards, units, ['stt', 'voice'])
  assert.deepEqual(r, { ok: true, notes: [], text: 'Saved.' })
})

test("saveCards: any other message becomes a note '<label>: <text>.'", async () => {
  const [cards, units] = rig([
    ['stt', 'STT', say => { say('Pick a cloud key on System · Keys first'); return true }],
    ['voice', 'TTS', say => { say('Voice id set.'); return true }],
  ])
  const r = await saveCards(cards, units, ['stt', 'voice'])
  assert.deepEqual(r, { ok: true,
    notes: ['STT: Pick a cloud key on System · Keys first.', 'TTS: Voice id set.'],
    text: 'Saved. STT: Pick a cloud key on System · Keys first. TTS: Voice id set.' })
})

test('saveCards: a card that returns false, throws or is missing fails — the later cards still save, and ok is false', async () => {
  const order = []
  const [cards, units] = rig([
    ['stt', 'STT', say => { say('Save failed: no key saved'); return false }],
    ['voice', 'TTS', () => { throw new Error('socket hang up') }],
    ['wake', 'Wake model', null],
    ['tuning', 'Voice tuning', () => { order.push('tuning'); return true }],
    ['rooms', 'Room mapping', () => { order.push('rooms'); return true }],
  ])
  const r = await saveCards(cards, units, ['stt', 'voice', 'wake', 'tuning', 'rooms'])
  assert.deepEqual(order, ['tuning', 'rooms'])
  assert.deepEqual(r, { ok: false, notes: [],
    text: 'Saved 2 of 5. STT failed: no key saved. TTS failed: socket hang up. Wake model failed.' })
})
