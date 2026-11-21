// Keys-row helper tests. Run: node --test dashboard-ui/tests/
import test from 'node:test'
import assert from 'node:assert/strict'
import * as keys from '../src/keys.js'
import { keyRoute, keyState } from '../src/keys.js'

// The lane list App.jsx passes in (BUILTIN_LANES' shape), slimmed to the
// fields keyRoute reads: kind, per_agent_keys, default_agent.
const LANES = [
  { kind: 'hermes', per_agent_keys: true, default_agent: 'default' },
  { kind: 'openclaw', per_agent_keys: false, default_agent: null },
  { kind: 'nanobot', per_agent_keys: false, default_agent: null },
]

test('contract: keyRoute and keyState are exported', () => {
  for (const name of ['keyRoute', 'keyState']) {
    assert.equal(typeof keys[name], 'function', name)
  }
})

test("keyRoute: the ElevenLabs, Deepgram and LocalAI rows each go to their own route, with an empty body", () => {
  assert.deepEqual(keyRoute({ kind: 'elevenlabs' }, LANES), ['/api/dashboard/tts/key/elevenlabs', {}])
  assert.deepEqual(keyRoute({ kind: 'deepgram' }, LANES), ['/api/dashboard/tts/key/deepgram', {}])
  assert.deepEqual(keyRoute({ kind: 'localai' }, LANES), ['/api/dashboard/localai/key', {}])
  // The speech rows answer before the lane lookup: an empty lane list is fine.
  assert.deepEqual(keyRoute({ kind: 'localai' }, []), ['/api/dashboard/localai/key', {}])
})

test('keyRoute: a lane row goes to the intelligence key route with its kind', () => {
  assert.deepEqual(keyRoute({ kind: 'openclaw' }, LANES), ['/api/dashboard/intelligence/key', { kind: 'openclaw' }])
  assert.deepEqual(keyRoute({ kind: 'nanobot' }, LANES), ['/api/dashboard/intelligence/key', { kind: 'nanobot' }])
})

test("keyRoute: a named agent on a per-agent-key lane adds agent to the body", () => {
  assert.deepEqual(keyRoute({ kind: 'hermes', agent: 'butler' }, LANES),
    ['/api/dashboard/intelligence/key', { kind: 'hermes', agent: 'butler' }])
})

test("keyRoute: the lane's default agent, no agent, or a lane without per-agent keys adds no agent", () => {
  assert.deepEqual(keyRoute({ kind: 'hermes', agent: 'default' }, LANES),
    ['/api/dashboard/intelligence/key', { kind: 'hermes' }])
  assert.deepEqual(keyRoute({ kind: 'hermes' }, LANES),
    ['/api/dashboard/intelligence/key', { kind: 'hermes' }])
  // OpenClaw has no per-agent keys: a named agent there still saves the base key.
  assert.deepEqual(keyRoute({ kind: 'openclaw', agent: 'butler' }, LANES),
    ['/api/dashboard/intelligence/key', { kind: 'openclaw' }])
})

test("keyRoute: a kind with no lane has no route (null), never another kind's route", () => {
  assert.equal(keyRoute({ kind: 'madeup' }, LANES), null)
  assert.equal(keyRoute({ kind: 'madeup', agent: 'butler' }, []), null)
})

test("keyState: removed and in use warns 'removed' with the restart note", () => {
  assert.deepEqual(keyState({ set: false, pending_restart: true, in_use: true }),
    { text: 'removed', tone: 'warning', title: 'Removed. Restart the bot to apply.' })
})

test("keyState: removed and unused stays neutral — nothing uses it, so no restart note", () => {
  assert.deepEqual(keyState({ set: false, pending_restart: true, in_use: false }),
    { text: 'removed', tone: 'neutral', title: 'Removed. Nothing uses it, so no restart is needed.' })
})

test("keyState: a missing key in use warns 'missing'; one nothing uses reads 'not set'", () => {
  assert.deepEqual(keyState({ set: false, in_use: true }), { text: 'missing', tone: 'warning' })
  assert.deepEqual(keyState({ set: false, in_use: false }), { text: 'not set', tone: 'neutral' })
})

test("keyState: saved and in use warns 'saved · <hint>' with the restart note", () => {
  assert.deepEqual(keyState({ set: true, pending_restart: true, in_use: true, hint: 'ab12' }),
    { text: 'saved · ab12', tone: 'warning', title: 'Saved. Restart the bot to apply.' })
})

test("keyState: saved and unused stays neutral — switching to it marks the restart pending", () => {
  assert.deepEqual(keyState({ set: true, pending_restart: true, in_use: false, hint: 'ab12' }),
    { text: 'saved · ab12', tone: 'neutral', title: 'Saved. Not in use yet: switching to it marks a restart pending.' })
})

test("keyState: a key the running setup uses reads 'set · <hint>', neutral, no hover note", () => {
  assert.deepEqual(keyState({ set: true, in_use: true, hint: 'ab12' }), { text: 'set · ab12', tone: 'neutral' })
  assert.deepEqual(keyState({ set: true, hint: 'ab12' }), { text: 'set · ab12', tone: 'neutral' })
})

test('keyState: a missing hint renders as an empty string', () => {
  assert.deepEqual(keyState({ set: true }), { text: 'set · ', tone: 'neutral' })
  assert.deepEqual(keyState({ set: true, pending_restart: true, in_use: true, hint: null }),
    { text: 'saved · ', tone: 'warning', title: 'Saved. Restart the bot to apply.' })
})
