// Setup wizard logic tests. Run: node --test dashboard-ui/tests/
import test from 'node:test'
import assert from 'node:assert/strict'
import * as sw from '../src/setupWizard.js'
import { STEPS, shouldOpen, nextStep, prevStep, stepView, wakePhrase, heardSince, tokenStep } from '../src/setupWizard.js'
import { TABS, TAB_IDS } from '../src/tabs.js'

// The probe's step ids, in the server's order (GET /api/dashboard/setup/probe).
const IDS = ['welcome', 'listening', 'voice', 'brain', 'wake', 'speakers', 'nodes', 'finish']
const probe = over => ({
  pending: true, token_required: false, restart: 'auto',
  host: { arch: 'x86_64', cpus: 8, ram_mb: 16000, disk_free_gb: 200, python: '3.12.3', slow: false },
  restart_pending: [], boot_time: 1000.5, last_wake: null,
  steps: [
    { id: 'welcome', done: true, detail: '' },
    { id: 'listening', done: true, detail: 'Whisper base', tier: 'embedded' },
    { id: 'voice', done: false, detail: 'ElevenLabs', tier: 'cloud' },
    { id: 'brain', done: false, detail: 'echo' },
    { id: 'wake', done: true, detail: 'stock model' },
    { id: 'speakers', done: false, detail: 'no zones' },
    { id: 'nodes', done: false, detail: '0 online' },
    { id: 'finish', done: false, detail: 'ready' },
  ],
  ...over,
})

test('contract: the API is exported', () => {
  for (const name of ['shouldOpen', 'nextStep', 'prevStep', 'stepView', 'wakePhrase', 'heardSince', 'tokenStep']) {
    assert.equal(typeof sw[name], 'function', name)
  }
  assert.ok(Array.isArray(STEPS))
})

test('STEPS: the eight probe ids in order, each titled; only welcome is not skippable', () => {
  assert.deepEqual(STEPS.map(s => s.id), IDS)
  for (const s of STEPS) assert.ok(typeof s.title === 'string' && s.title.length > 0, s.id)
  assert.deepEqual(STEPS.filter(s => !s.skippable).map(s => s.id), ['welcome'])
})

test('STEPS: no title says "brain" (the naming law: it is intelligence)', () => {
  for (const s of STEPS) assert.doesNotMatch(s.title, /\bbrains?\b/i, s.id)
  assert.equal(STEPS.find(s => s.id === 'brain').title, 'Intelligence')
})

test('TAB_IDS: the tab list App.jsx renders, not a copy', () => {
  assert.deepEqual(TAB_IDS, TABS.map(t => t.id))
  assert.deepEqual(TAB_IDS, ['rooms', 'voice', 'system'])
})

test('shouldOpen: a pending box opens on an empty hash or a tab hash', () => {
  assert.equal(shouldOpen(probe(), ''), true)
  for (const id of TAB_IDS) {
    assert.equal(shouldOpen(probe(), id), true, id)
    assert.equal(shouldOpen(probe(), `#${id}`), true, `#${id}`)
  }
  assert.equal(shouldOpen(probe(), '#'), true)
})

test('shouldOpen: never on an upgraded install (pending false), whatever the hash', () => {
  for (const hash of ['', 'rooms', 'voice', 'system', 'keys', 'setup', '#rooms']) {
    assert.equal(shouldOpen(probe({ pending: false }), hash), false, hash)
  }
})

test('shouldOpen: a card hash (#keys, #stt) is a deliberate link, so the wizard stays shut', () => {
  for (const hash of ['keys', '#keys', 'stt', 'intelligence', 'localai']) {
    assert.equal(shouldOpen(probe(), hash), false, hash)
  }
})

test('shouldOpen: no probe, a failed probe or a non-true pending never opens', () => {
  assert.equal(shouldOpen(null, ''), false)
  assert.equal(shouldOpen(undefined, ''), false)
  assert.equal(shouldOpen({}, ''), false)
  assert.equal(shouldOpen(probe({ pending: 'yes' }), ''), false)
})

test('nextStep / prevStep walk the eight steps and stop at the ends', () => {
  assert.equal(nextStep('welcome'), 'listening')
  assert.equal(nextStep('nodes'), 'finish')
  assert.equal(nextStep('finish'), null)
  assert.equal(prevStep('listening'), 'welcome')
  assert.equal(prevStep('finish'), 'nodes')
  assert.equal(prevStep('welcome'), null)
  assert.equal(nextStep('nope'), null)
  assert.equal(prevStep('nope'), null)
  const walked = [STEPS[0].id]
  while (nextStep(walked.at(-1))) walked.push(nextStep(walked.at(-1)))
  assert.deepEqual(walked, IDS)
})

test("stepView: a step's done, detail and tier from the probe", () => {
  assert.deepEqual(stepView(probe(), 'listening'), { done: true, detail: 'Whisper base', tier: 'embedded' })
  assert.deepEqual(stepView(probe(), 'voice'), { done: false, detail: 'ElevenLabs', tier: 'cloud' })
  assert.deepEqual(stepView(probe(), 'wake'), { done: true, detail: 'stock model', tier: null })
})

test('stepView: a step an older bot does not report, or no probe at all, is not done', () => {
  const older = probe({ steps: probe().steps.filter(s => s.id !== 'nodes') })
  assert.equal(stepView(older, 'nodes').done, false)
  assert.equal(stepView(probe({ steps: undefined }), 'voice').done, false)
  assert.equal(stepView(null, 'voice').done, false)
  assert.equal(stepView(probe({ steps: [{ id: 'voice', done: 'yes' }] }), 'voice').done, false)
})

// Made-up model keys and file names: a phrase is always computed from the API's rows.
const stock = (name, selected) => ({ name, source: 'stock', selected, size: 1 })
const custom = (name, selected, source = 'models') => ({ name, source, selected, size: 1 })

test("wakePhrase: the selected stock row's key, humanised", () => {
  assert.equal(wakePhrase([stock('hey_suite_bot', false), stock('ok_other_one', true)]), 'Ok Other One')
  assert.equal(wakePhrase([stock('hey_suite_bot', true)]), 'Hey Suite Bot')
  assert.equal(wakePhrase({ models: [stock('hey_suite_bot', true)] }), 'Hey Suite Bot')
})

test("wakePhrase: a selected custom model's file name, without extension or version suffix", () => {
  assert.equal(wakePhrase([custom('ok_made_up_v0.1.onnx', true), stock('hey_suite_bot', false)]), 'Ok Made Up')
  assert.equal(wakePhrase([custom('hello_test_box.onnx', true, 'root')]), 'Hello Test Box')
  assert.equal(wakePhrase([custom('yo_fake_word_v2.tflite', true)]), 'Yo Fake Word')
  assert.equal(wakePhrase([custom('hi_there_v1.0.3.onnx', true)]), 'Hi There')
})

test("wakePhrase: the bot's wake name is not the phrase (it alone does not trigger the model)", () => {
  assert.equal(wakePhrase([stock('hey_suite_bot', true)], 'Suite Name'), 'Hey Suite Bot')
  assert.equal(wakePhrase([stock('hey_suite_bot', false)], 'Suite Name'), 'the wake word')
})

test('wakePhrase: nothing selected or no list — "the wake word"', () => {
  assert.equal(wakePhrase([stock('hey_suite_bot', false), custom('ok_made_up.onnx', false)]), 'the wake word')
  assert.equal(wakePhrase([]), 'the wake word')
  assert.equal(wakePhrase(null), 'the wake word')
  assert.equal(wakePhrase({}), 'the wake word')
})

test('heardSince: a wake after this boot gives its room and time', () => {
  assert.deepEqual(heardSince(probe({ last_wake: { time: 1001.25, room: 'Room 1' } })), { room: 'Room 1', time: 1001.25 })
  assert.deepEqual(heardSince(probe({ last_wake: { time: 1001.25, room: null } })), { room: null, time: 1001.25 })
})

test('heardSince: a wake from before the restart, or none yet, is ignored', () => {
  assert.equal(heardSince(probe({ last_wake: { time: 999, room: 'Room 1' } })), null)
  assert.equal(heardSince(probe({ last_wake: { time: 1000.5, room: 'Room 1' } })), null)
  assert.equal(heardSince(probe({ last_wake: null })), null)
  assert.equal(heardSince(null), null)
})

test('heardSince: an unknown boot time proves nothing', () => {
  assert.equal(heardSince(probe({ boot_time: null, last_wake: { time: 1001, room: 'Room 1' } })), null)
})

test('tokenStep: no token on the box -> none; a token -> ask, or check a stored one', () => {
  assert.equal(tokenStep(probe({ token_required: false }), 'stored-value'), 'none')
  assert.equal(tokenStep(probe({ token_required: true }), ''), 'ask')
  assert.equal(tokenStep(probe({ token_required: true }), null), 'ask')
  assert.equal(tokenStep(probe({ token_required: true }), 'stored-value'), 'check')
  assert.equal(tokenStep(null, 'stored-value'), 'none')
})
