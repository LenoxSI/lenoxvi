// LenoxVI dashboard — app. Wireframe-C structure, in tabs:
// pinned header (tabs + Restart), status chips, then Rooms, Voice or System.
// Brand: dark theme default, one lime element leads, sentence case,
// no emoji. Live data via the same /api/dashboard/* routes the old
// page.html used — routes are the contract, every shape verified
// against the live rig.

import { Fragment, createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { isSupported, hostOfferUrl, validateSettings, NodeLink } from './nodeSetup.js'
import { XmosDfu, XMOS_IMAGES, isUsbSupported, platformOf, fetchXmosImage, checkXmosImage, watchSound, expectVersion,
         xmosImageName, xmosImageUrl } from './xmosDfu.js'

/* ── API layer ─────────────────────────────────────────────── */
const TOKEN_KEY = 'lenoxvi-dashboard-token'
// LVI_DASHBOARD_TOKEN on the bot makes every
// mutation 401 without X-Dashboard-Token (guard.py). The React port sent
// a stored token but never ASKED for one — setting the token bricked
// every save. On a mutation 401: prompt (once, shared across concurrent
// callers), remember in localStorage, retry the request once. A token
// that 401s again is dropped so the next save asks fresh. Reads stay
// open server-side, so GETs never prompt — except a guarded read that
// passes promptForToken (node /info talks to the node, so it's guarded).
// The ask is TokenDialog's masked field: window.prompt showed the token
// in plain text on screen.
let tokenPrompt = null
let tokenAsker = null  // set while TokenDialog is mounted
function askToken(wasStored) {
  if (!tokenPrompt) {
    tokenPrompt = Promise.resolve()
      .then(() => (tokenAsker ? tokenAsker(wasStored) : null))
      .then(t => (t || '').trim() || null)
      .finally(() => { setTimeout(() => { tokenPrompt = null }, 0) })
  }
  return tokenPrompt
}

async function api(path, opts = {}, _retried = false) {
  const headers = { ...(opts.headers || {}) }
  const tok = localStorage.getItem(TOKEN_KEY)
  if (tok) headers['X-Dashboard-Token'] = tok
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(opts.json)
  }
  let r
  try {
    r = await fetch(path, { ...opts, headers })
  } catch {
    const e = new Error('network')
    e.network = true
    throw e
  }
  const method = (opts.method || 'GET').toUpperCase()
  if (r.status === 401 && (method !== 'GET' || opts.promptForToken) && !_retried) {
    const t = await askToken(!!tok)
    if (t) {
      localStorage.setItem(TOKEN_KEY, t)
      return api(path, opts, true)
    }
    const e = new Error('dashboard token required — save cancelled')
    e.status = 401
    throw e
  }
  if (r.status === 401 && _retried) localStorage.removeItem(TOKEN_KEY)  // wrong token: ask fresh next time
  let body = {}
  try { body = await r.json() } catch { /* empty body */ }
  if (!r.ok) {
    let msg = body.detail || r.statusText
    if (Array.isArray(msg)) {
      msg = msg.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ')
    }
    const e = new Error(msg)
    e.status = r.status
    throw e
  }
  return body
}

/* ── common message strip ──────────────────────────────────── */
function useMsg() {
  const [state, setState] = useState({ text: '', cls: 'muted', owner: '' })
  const set = (text, cls = 'muted', owner = '') => setState({ text, cls, owner })
  return [state, set]
}

const MSG_CLASSES = {
  muted: 'text-[var(--muted)]',
  ok: 'text-[var(--success)]',
  err: 'text-[var(--danger)]',
  pulse: 'text-[var(--muted)] animate-pulse',
}

function AppMsg({ msg }) {
  const { text, cls, owner } = msg
  return (
    <p aria-live="polite" className={`min-h-[22px] text-[13.5px] m-0 ${MSG_CLASSES[cls] || MSG_CLASSES.muted}`}>
      {text ? (owner ? `${owner} — ${text}` : text) : ''}
    </p>
  )
}

// The dashboard-token ask (askToken above). A password field, so the token
// never shows on screen; Cancel or Escape answers null (the save is cancelled).
function TokenDialog() {
  const [ask, setAsk] = useState(null)  // { wasStored, resolve } while open
  const [val, setVal] = useState('')
  useEffect(() => {
    tokenAsker = wasStored => new Promise(resolve => { setVal(''); setAsk({ wasStored, resolve }) })
    return () => { tokenAsker = null }
  }, [])
  if (!ask) return null
  const done = t => { ask.resolve(t); setAsk(null); setVal('') }
  return (
    <div role="dialog" aria-modal="true" aria-label="Dashboard token"
      style={{ position: 'fixed', inset: 0, zIndex: 50, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0, 0, 0, 0.6)' }}>
      <form className="rounded-2xl bg-[var(--surface)] p-4 grid gap-3" style={{ width: 'min(420px, 92vw)' }}
        onSubmit={e => { e.preventDefault(); done(val) }}
        onKeyDown={e => { if (e.key === 'Escape') done(null) }}>
        <p className="text-[13.5px] m-0">{ask.wasStored
          ? 'Saved dashboard token was rejected. Enter the token (LVI_DASHBOARD_TOKEN on the bot):'
          : 'This dashboard requires a token (LVI_DASHBOARD_TOKEN on the bot):'}</p>
        <input type="password" autoFocus autoComplete="off" spellCheck={false} aria-label="Dashboard token"
          value={val} onChange={e => setVal(e.target.value)} className={INPUT_CLS} />
        <div className="flex gap-2 justify-end">
          <button type="button" className={BTN_DEFAULT} onClick={() => done(null)}>Cancel</button>
          <button type="submit" className={BTN_PRIMARY}>OK</button>
        </div>
      </form>
    </div>
  )
}

/* ── restart lane ──────────────────────────────────────────── */
// restartBot's one caller is the header's Restart button (saves never restart; they
// add to /status restart_pending), and the React `rebooting` state can lag
// a second click — two concurrent restarts meant two transient restart
// units and two 90s wait loops fighting over one message chip. A
// module-level latch is the only airtight double-fire guard.
let _botRestarting = false
async function restartBot(setMsg, setRebooting) {
  if (_botRestarting) return false
  _botRestarting = true
  setRebooting(true)
  setMsg('Restarting…', 'pulse', 'Bot control')
  // "Back" = a process that BOOTED after this request (browser clock minus
  // the bot's uptime). A small uptime alone isn't enough: the old process
  // answers for a few seconds after the request, and if it had itself
  // booted under 30 s ago it looked like the new one.
  const askedAt = Date.now() / 1000
  let postErr = null
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await api('/api/dashboard/restart', { method: 'POST' })
      postErr = null
      break
    } catch (e) {
      postErr = e
      await new Promise(r => setTimeout(r, 1500))
    }
  }
  if (postErr && !postErr.network) {
    // The bot answered with a refusal (token cancelled, scheduling failed):
    // nothing was scheduled, and waiting 90 s for it only kept the Restart
    // button disabled. A network error is different — the request may have
    // landed before the bot went down — so that one still waits.
    setMsg(`restart request failed (${postErr.message}) — the bot was not restarted.`, 'err', 'Bot control')
    setRebooting(false)
    _botRestarting = false
    return false
  }
  let up = 0
  for (let i = 0; i < 45; i++) {
    await new Promise(r => setTimeout(r, 2000))
    try {
      const s = await api('/api/dashboard/status')
      // The new process boots 2 s+ after the request (the server's delay);
      // the 1 s margin absorbs reply latency on the old one's answer.
      if (s.uptime_secs != null && Date.now() / 1000 - s.uptime_secs > askedAt + 1) { up = 1; break }
    } catch { /* still down */ }
    setMsg(`Restarting… ${Math.min(90, (i + 1) * 2)}s`, 'pulse', 'Bot control')
  }
  if (up) {
    setMsg('Bot is back — reloading panels…', 'ok', 'Bot control')
    window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))
  } else {
    setMsg((postErr ? `restart request failed (${postErr.message}) — ` : '') +
      'uptime never reset within 90s; the old bot process is still running.', 'err', 'Bot control')
  }
  setRebooting(false)
  _botRestarting = false
  return !!up
}

/* ── shared bits ───────────────────────────────────────────── */
// Uptime as people say it: 4m, 3h 12m, 2d 4h.
const sec = s => {
  if (s == null) return '—'
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`
}

// last_seen / last_wake times arrive as epoch SECONDS (registry
// time.time()) — never ms, never ISO. Never treat a missing value as
// epoch zero (the "20,713d ago" bug: = new Date(seconds-as-ms) → 1970).
const fmtAgo = t => {
  if (!t) return '—'
  const ms = t > 1e12 ? t : t * 1000  // accept seconds or ms safely
  const diff = Date.now() - ms
  if (!isFinite(diff) || diff < 0) return '—'
  const s = diff / 1000
  if (s < 90) return 'just now'
  if (s < 3600) return Math.round(s / 60) + 'm ago'
  if (s < 86400) return Math.round(s / 3600) + 'h ago'
  return Math.round(s / 86400) + 'd ago'
}
const esc = s => String(s ?? '')
// A room with no override uses the house agent: "Default agent", naming it
// when it has a name of its own ('default' is the main agent itself).
// Room lists sort by name, case-insensitive, in every table.
const byRoom = (a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: 'base' })
const defaultAgentLabel = a => (!a || a === 'default' ? 'Default agent' : `Default agent (${a})`)

const INPUT_CLS = 'rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2'
// Line badge per node: the hardware behind each firmware line (fw_push `line`).
// A node's own speaker as a reply zone (server: node_speaker.BUILTIN).
const BUILTIN = 'builtin'
const LINE_TITLE = {
  array: 'reSpeaker XVF3800 mic array (LED ring, physical mute button)',
  flex: 'reSpeaker Flex (XVF3800, no LED ring or button)',
  waveshare: 'Waveshare esp32-s3-audio board (built-in speaker option)',
}
const BTN = 'rounded-full px-3 py-1.5 text-[14px] font-medium cursor-pointer disabled:opacity-50'
const BTN_DEFAULT = `${BTN} bg-[var(--default)] text-[var(--default-foreground)]`
const BTN_PRIMARY = `${BTN} bg-[var(--accent)] text-[var(--accent-foreground)]`
const BTN_DANGER = `${BTN} bg-transparent border border-[var(--danger)] text-[var(--danger)]`

// One logo — dark is the only theme (index.css, no light variant
// exists in this app), so one img, sized to the header.
function Wordmark() {
  return (
    <img src="/lenoxvi-horizontal-on-dark.svg" alt="LenoxVI"
      className="h-[34px] w-auto" />
  )
}

const H2 = ({ children }) => (
  <h2 className="font-display text-[18px] leading-[28px] font-semibold m-0">{children}</h2>
)

function Section({ id, title, note, action, children }) {
  return (
    <section id={id} className="grid grid-cols-1 gap-3 scroll-mt-[110px]">
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div className="grid gap-1">
          <H2>{title}</H2>
          {note ? <p className="text-[14px] leading-5 text-[var(--muted)] m-0 max-w-[680px]">{note}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

function Field({ label, children, right, between }) {
  return (
    <div className="grid gap-1">
      <span className="text-[12px] font-medium text-[var(--muted)]">{label}</span>
      <div className={`flex items-center gap-2 flex-wrap${right ? ' justify-end' : ''}${between ? ' justify-between' : ''}`}>
        {children}
      </div>
    </div>
  )
}

// Switch-style toggle (session mode). Controlled: `checked` comes from
// pending state (optimistic) or server status; parent owns the POST.
function Switch({ checked, onChange, title }) {
  return (
    <button type="button" role="switch" aria-checked={checked} title={title}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-[20px] w-[36px] shrink-0 rounded-full border cursor-pointer transition-colors
        ${checked ? 'bg-[var(--accent)] border-[var(--accent)]' : 'bg-[var(--default)] border-[var(--separator)]'}`}>
      <span className={`absolute top-[2px] h-[14px] w-[14px] rounded-full bg-white transition-all
        ${checked ? 'left-[19px]' : 'left-[3px]'}`} />
    </button>
  )
}

// API key box: a saved key shows its first 4 characters (the server's
// masked hint), so users can see WHICH key is set when troubleshooting.
// Focus selects the hint; typing replaces it (hidden). The hint is never
// sent back as a key — readKey() returns '' until the box is edited.
// Remount it (React key) after a save to show the new hint.
// File picker: a button that opens the browser's chooser, then the chosen
// file's name. The raw file input stays out of sight; Enter or Space on the
// focused button opens the chooser too.
function FilePick({ accept, file, onPick }) {
  const ref = useRef(null)
  return (
    <>
      <label className={BTN_DEFAULT} tabIndex={0} role="button"
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ref.current?.click() } }}>
        <input ref={ref} type="file" accept={accept} className="hidden"
          onClick={e => { e.target.value = '' }}
          onChange={e => onPick(e.target.files?.[0] || null)} />
        Choose file
      </label>
      <span className="text-[13px] font-mono text-[var(--muted)]">{file ? file.name : `no ${accept} file chosen`}</span>
    </>
  )
}

function KeyInput({ id, hint, placeholder, label }) {
  const [dirty, setDirty] = useState(false)
  const [val, setVal] = useState('')
  return (
    <input id={id} data-dirty={dirty ? '1' : '0'} type={dirty ? 'password' : 'text'}
      value={dirty ? val : (hint || '')} placeholder={placeholder} aria-label={label || placeholder} autoComplete="off" spellCheck={false}
      title={hint ? 'Saved key (first 4 characters shown). Paste a new key to replace it.' : undefined}
      onFocus={e => { if (!dirty) e.target.select() }}
      onChange={e => { setDirty(true); setVal(e.target.value) }}
      className={`${INPUT_CLS} flex-1 min-w-[280px] font-mono`} />
  )
}
const readKey = (id) => {
  const el = document.getElementById(id)
  return el && el.dataset.dirty === '1' ? el.value.trim() : ''
}

// Intelligence lanes (kind -> UI facts) from GET /api/dashboard/intelligence/lanes,
// in display order. The built-in copy is today's lanes: it renders until the bot
// answers, and stays if it can't (an older bot has no /lanes route).
const BUILTIN_LANES = [
  { kind: 'hermes', label: 'Hermes', agent_required: true, per_agent_keys: true, default_agent: 'default',
    url_placeholder: '<host>:8642',
    agent_help: 'Agents (profiles) on this Hermes gateway. Each one needs its own API key.',
    no_agent_label: null, brand: { backgroundColor: 'rgba(255, 255, 255, 0.92)', color: '#2563eb' } },
  { kind: 'openclaw', label: 'OpenClaw', agent_required: true, per_agent_keys: false, default_agent: null,
    url_placeholder: '<host>:18789',
    agent_help: 'Agents on this OpenClaw Gateway (its /v1/models).',
    no_agent_label: null, brand: { backgroundColor: 'rgba(23, 23, 23, 0.9)', color: '#ef4444' } },
  { kind: 'nanobot', label: 'Nanobot', agent_required: false, per_agent_keys: false, default_agent: null,
    url_placeholder: '<host>:8900',
    agent_help: 'The model this nanobot server runs (its /v1/models). Optional: with none picked, nanobot uses its own.',
    no_agent_label: "nanobot's own model", brand: null },
]
let LANES = BUILTIN_LANES
const laneOf = kind => LANES.find(l => l.kind === kind)
// Unknown kind -> the first lane's label (today's 'Hermes' fallback).
const laneLabel = kind => laneOf(kind)?.label || LANES[0].label
// The lane whose agents each carry their own key (Hermes profiles).
const perAgentLane = () => LANES.find(l => l.per_agent_keys)
// A saved URL the bot would rebuild from host:port alone (http://, no path or
// just /v1) shows as host:port; the bot fills those parts back in on Save.
const shortUrl = u => (u || '').match(/^http:\/\/([^/]+)(\/v1)?\/?$/)?.[1] || u || ''

// A picked provider/agent with no key: point at the Keys card (keys are
// entered there only).
function MissingKey({ label }) {
  return (
    <a href="#keys" className="text-[13px] text-[var(--warning-soft-foreground)] hover:underline">
      No {label} key → System · Keys
    </a>
  )
}

// LocalAI's speech models, asked of the server (POST /localai/test reaches
// the SAVED LocalAI with the SAVED key; keys never reach the browser).
// `on` = only ask while a card has LocalAI picked. Refreshes after any save.
function useLocalAIModels(on) {
  const [found, setFound] = useState(null)   // { ok, ms, reason, stt, tts } | null
  useEffect(() => {
    if (!on) return
    let alive = true
    const get = () => api('/api/dashboard/localai/test', { method: 'POST' })
      .then(r => { if (alive) setFound(r) })
      .catch(e => { if (alive) setFound({ ok: false, reason: e.message, stt: [], tts: [] }) })
    get()
    window.addEventListener('lenoxvi:reload-all', get)
    return () => { alive = false; window.removeEventListener('lenoxvi:reload-all', get) }
  }, [on])
  return found
}

// The save route for a row: the existing kind-aware routes (one write path).
// A catalog key with no route here can't be saved from the card (null) —
// never sent through another kind's route.
function keyRoute(row) {
  if (row.kind === 'elevenlabs') return ['/api/dashboard/tts/key/elevenlabs', {}]
  if (row.kind === 'deepgram') return ['/api/dashboard/tts/key/deepgram', {}]
  if (row.kind === 'localai') return ['/api/dashboard/localai/key', {}]
  const lane = laneOf(row.kind)
  if (!lane) return null
  const named = lane.per_agent_keys && row.agent && row.agent !== lane.default_agent
  return ['/api/dashboard/intelligence/key', { kind: row.kind, ...(named ? { agent: row.agent } : {}) }]
}

function keyState(row) {
  // Chip words: lowercase, one or two words (brand Chip rule); the restart
  // note moves to the hover. Warning tone = needs a restart or a key.
  // A key nothing uses yet needs no restart: neutral tone, no restart note.
  if (!row.set && row.pending_restart) return row.in_use
    ? { text: 'removed', cls: WARN_CHIP_CLS, title: 'Removed. Restart the bot to apply.' }
    : { text: 'removed', cls: CHIP_CLS, title: 'Removed. Nothing uses it, so no restart is needed.' }
  if (!row.set) return row.in_use ? { text: 'missing', cls: WARN_CHIP_CLS } : { text: 'not set', cls: CHIP_CLS }
  if (row.pending_restart) return row.in_use
    ? { text: `saved · ${row.hint || ''}`, cls: WARN_CHIP_CLS, title: 'Saved. Restart the bot to apply.' }
    : { text: `saved · ${row.hint || ''}`, cls: CHIP_CLS, title: 'Saved. Not in use yet: switching to it marks a restart pending.' }
  return { text: `set · ${row.hint || ''}`, cls: CHIP_CLS }
}

function KeysCard({ setMsg }) {
  const [keys, setKeys] = useState(null)
  const [open, setOpen] = useState(null)        // var being replaced
  const [epoch, setEpoch] = useState(0)         // remounts key boxes after a save
  const [tests, setTests] = useState({})        // var -> {status, detail}
  const [agents, setAgents] = useState([])      // per-agent-key agents with no key yet
  const [agentNote, setAgentNote] = useState('')
  const [addPick, setAddPick] = useState('')     // catalog var, or 'agent:<id>'; '' = nothing picked yet
  const [addEpoch, setAddEpoch] = useState(0)    // remounts (clears) the Add box when its pick goes away
  const [busy, setBusy] = useState(false)        // an Add in flight (no double submit)
  const [loadErr, setLoadErr] = useState('')
  const msgRef = useRef(setMsg)
  const discover = useRef(null)
  const discSeq = useRef(0)                      // only the newest discovery reply counts
  useEffect(() => {
    discover.current = async () => {
      const seq = ++discSeq.current
      const lane = perAgentLane()
      if (!lane) { setAgents([]); return }
      try {
        const d = await api('/api/dashboard/intelligence/agents', { method: 'POST', json: { kind: lane.kind } })
        if (seq !== discSeq.current) return
        setAgents((d.agents || []).filter(a => a.id !== lane.default_agent && !a.key_set).map(a => a.id))
        setAgentNote(d.note || '')
      } catch (e) {
        if (seq !== discSeq.current) return
        setAgents([]); setAgentNote('Agent list failed: ' + e.message)
      }
    }
  })
  useEffect(() => {
    const h = () => discover.current?.()
    h()
    window.addEventListener('lenoxvi:reload-all', h)  // an added agent key leaves the list
    return () => window.removeEventListener('lenoxvi:reload-all', h)
  }, [])
  useEffect(() => { msgRef.current = setMsg })
  const load = () => api('/api/dashboard/keys').then(d => { setKeys(d.keys || []); return d })
  useEffect(() => {
    const get = () => api('/api/dashboard/keys').then(d => { setKeys(d.keys || []); setLoadErr('') })
      .catch(e => { setLoadErr(e.message); msgRef.current('Keys load failed: ' + e.message, 'err', 'Keys') })
    get()
    // After a restart or any card's save: pending/in-use follow the truth.
    window.addEventListener('lenoxvi:reload-all', get)
    return () => window.removeEventListener('lenoxvi:reload-all', get)
  }, [])

  const save = async (row, inputId) => {
    const key = readKey(inputId)
    if (!key) { setMsg('Nothing to save — paste a key first.', 'muted', 'Keys'); return null }
    const target = keyRoute(row)
    if (!target) { setMsg(`${row.label} keys can't be saved from the card yet.`, 'err', 'Keys'); return null }
    const [route, extra] = target
    try {
      const r = await api(route, { method: 'PUT', json: { ...extra, api_key: key } })
      setOpen(null); setEpoch(n => n + 1)
      setTests(t => ({ ...t, [row.var]: undefined }))
      const d = await load()
      window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))  // other cards drop their "No key" links
      // Only a key the running setup uses needs a restart (the Restart
      // button counts it). A key for a provider not picked yet is read when
      // that provider is switched on, and that save marks the restart.
      const var_ = (r && r.var) || row.var
      const inUse = !!(d?.keys || []).find(k => k.var === var_)?.in_use
      if (inUse) {
        setMsg('Saved — restart to apply.', 'ok', 'Keys')
      } else {
        setMsg(`${row.label} key saved. Not in use yet, so no restart is needed.`, 'ok', 'Keys')
      }
      return { ...(r || {}), in_use: inUse }
    } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'Keys'); return null }
  }

  const remove = async row => {
    const what = row.in_use
      ? `It is IN USE: ${row.used_by.join(' + ')} will stop working after the next restart.`
      : 'Nothing uses it right now.'
    if (!window.confirm(`Remove the ${row.label} key (${row.var})?\n\n${what}\n\n` +
      'A private backup of .env is kept on the appliance (newest 3). For a leaked key, also revoke it with the provider.')) return
    try {
      await api(`/api/dashboard/keys/${encodeURIComponent(row.var)}`, { method: 'DELETE' })
      setOpen(null); setTests(t => ({ ...t, [row.var]: undefined }))
      await load()
      window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))
      if (row.in_use) {
        setMsg('Removed — restart to apply.', 'ok', 'Keys')
      } else {
        setMsg(`${row.label} key removed. Nothing used it, so no restart is needed.`, 'ok', 'Keys')
      }
    } catch (e) { setMsg('Remove failed: ' + e.message, 'err', 'Keys') }
  }

  const test = async row => {
    setTests(t => ({ ...t, [row.var]: { status: 'testing' } }))
    try {
      const r = await api('/api/dashboard/keys/test', { method: 'POST', json: { var: row.var } })
      setTests(t => ({ ...t, [row.var]: r }))
    } catch (e) { setTests(t => ({ ...t, [row.var]: { status: 'unreachable', detail: e.message } })) }
  }

  // Rows = keys you have, a removed one until restart, and any key in use
  // but missing (a broken setup never hides). Everything else is offered
  // in the one Add picker.
  const shown = r => r.set || r.pending_restart || r.in_use
  const services = (keys || []).filter(r => !shown(r))
  const offered = v => (v.startsWith('agent:') ? agents.includes(v.slice(6))
    : services.some(r => r.var === v && keyRoute(r)))
  // Only an explicit pick is ever saved to. A default first entry moved
  // under a pasted key whenever a refresh changed the list, so the key went
  // to a service nobody chose. A pick that a refresh removed stays visible
  // as "pick again", and the pasted key is cleared with it.
  const pickValue = addPick
  const stale = !!pickValue && !offered(pickValue)
  useEffect(() => {
    if (!stale || !readKey('keyin-add')) return
    setAddEpoch(n => n + 1)
    msgRef.current('The service you picked is no longer offered — the pasted key was cleared. Pick again.', 'err', 'Keys')
  }, [stale])
  const add = async () => {
    if (busy) return
    if (!pickValue || stale) {
      setMsg('That key is no longer offered — pick again.', 'err', 'Keys'); return  // stays on 'pick again' until the user chooses
    }
    const row = pickValue.startsWith('agent:')
      ? { var: '', label: `${perAgentLane()?.label} · ${pickValue.slice(6)}`, kind: perAgentLane()?.kind, agent: pickValue.slice(6) }
      : services.find(r => r.var === pickValue)
    setBusy(true)
    try {
      const r = await save(row, 'keyin-add')
      const v = r && (r.var || row.var)
      if (!v) return
      setAddPick('')
      await test({ var: v })  // the saved key, checked BEFORE any restart
    } finally { setBusy(false) }
  }
  const shownRows = (keys || []).filter(shown)
  const openVar = shownRows.some(r => r.var === open) ? open : null  // a row that left takes its box with it

  return (
    <Section id="system" title="Keys" note="">
      <div id="keys" className="rounded-2xl bg-[var(--surface)] p-4 grid gap-3">
        {!keys ? <span className="text-[13px] text-[var(--muted)]">{loadErr ? `Keys load failed: ${loadErr}` : 'Loading…'}</span> : (
          <div style={{ overflowX: 'auto' }}>
            <table id="keys-table" className="w-full text-[14px]">
              <thead>
                <tr className="text-[12px] text-[var(--muted)]">
                  <th className="text-left px-4 py-2">Key</th>
                  <th className="hidden sm:table-cell text-left px-4 py-2">For</th>
                  <th className="hidden sm:table-cell text-left px-4 py-2">In use</th>
                  <th className="hidden sm:table-cell text-left px-4 py-2">Status</th>
                  <th className="text-left px-4 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {shownRows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })).map(row => {
                  const st = keyState(row)
                  const t = tests[row.var]
                  const close = () => setOpen(null)
                  return (
                    <Fragment key={row.var}>
                      <tr data-var={row.var} className="border-t border-[var(--separator)]">
                        <td className="px-4 py-2">
                          <div className="text-[14px] font-medium">{row.label}</div>
                          <div className="text-[12px] font-mono text-[var(--muted)] break-all">{row.var}</div>
                          {/* Phone: the For / In use / Status columns fold in here. */}
                          <div className="sm:hidden flex flex-wrap items-center gap-2 mt-1">
                            {row.used_by.map(u => <span key={u} className={CHIP_CLS}>{u.toLowerCase()}</span>)}
                            {row.in_use && <span className={OK_CHIP_CLS}>in use</span>}
                            <span className={st.cls} title={st.title}>{st.text}</span>
                          </div>
                        </td>
                        <td className="hidden sm:table-cell px-4 py-2">
                          <div className="flex flex-wrap items-center gap-2">
                            {row.used_by.map(u => <span key={u} className={CHIP_CLS}>{u.toLowerCase()}</span>)}
                          </div>
                        </td>
                        <td className="hidden sm:table-cell px-4 py-2">
                          {row.in_use && <span className={OK_CHIP_CLS}>in use</span>}
                        </td>
                        <td className="hidden sm:table-cell px-4 py-2"><span className={st.cls} title={st.title} data-role="state">{st.text}</span></td>
                        <td className="px-4 py-2 text-right">
                          <div className="flex flex-wrap items-center justify-end gap-2">
                            <button className={BTN_DEFAULT} disabled={!row.set || t?.status === 'testing'} onClick={() => test(row)}>
                              {t?.status === 'testing' ? 'Testing…' : 'Test'}</button>
                            <button className={BTN_DEFAULT} onClick={() => setOpen(open === row.var ? null : row.var)}>
                              {row.set ? 'Replace' : 'Set'}</button>
                            {row.set && <button className={BTN_DANGER} onClick={() => remove(row)}>Remove</button>}
                          </div>
                        </td>
                      </tr>
                      {openVar === row.var && (
                        <tr data-role="replace">
                          <td colSpan={5} className="px-4 py-2">
                            <div className="flex items-center gap-2 flex-wrap"
                              onKeyDown={e => { if (e.key === 'Escape') close() }}>
                              <KeyInput id={`keyin-${row.var}`} key={`${row.var}-${epoch}`} hint=""
                                placeholder={`paste the new ${row.label} key…`} />
                              <button className={BTN_PRIMARY} onClick={() => save(row, `keyin-${row.var}`)}>Save</button>
                              <button className={BTN_DEFAULT} onClick={close}>Cancel</button>
                            </div>
                          </td>
                        </tr>
                      )}
                      {t && t.status !== 'testing' && (
                        <tr>
                          <td colSpan={5} data-role="test"
                            className={`px-4 py-2 text-[13px] ${t.status === 'ok' ? 'text-[var(--success)]'
                              : t.status === 'needs_setup' ? 'text-[var(--muted)]' : 'text-[var(--danger)]'}`}>
                            {t.status === 'ok' ? '✓ ' : t.status === 'needs_setup' ? 'ⓘ ' : '✗ '}{t.detail || t.status}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center gap-2 flex-wrap">
          {!keys ? null : (services.length === 0 && agents.length === 0) ? (
            <span className="text-[13px] text-[var(--muted)]">Every key is set.{agentNote ? ` ${agentNote}` : ''}</span>
          ) : (
            <>
              <select id="key-add" aria-label="Key to add" className={`${INPUT_CLS} w-auto`} value={pickValue}
                onChange={e => setAddPick(e.target.value)}>
                {!pickValue && <option value="" disabled>pick a key…</option>}
                {stale && <option value={pickValue} disabled>(no longer offered — pick again)</option>}
                {services.length > 0 && (
                  <optgroup label="Services">
                    {services.map(r => (
                      <option key={r.var} value={r.var} disabled={!keyRoute(r)}>
                        {`${r.label} — ${r.used_by.join(' · ')}${keyRoute(r) ? '' : ' (not supported yet)'}`}</option>
                    ))}
                  </optgroup>
                )}
                {agents.length > 0 && (
                  <optgroup label={`${perAgentLane()?.label} agents`}>
                    {agents.map(a => <option key={a} value={`agent:${a}`}>{`${perAgentLane()?.label} · ${a}`}</option>)}
                  </optgroup>
                )}
              </select>
              <KeyInput id="keyin-add" key={`add-${epoch}-${addEpoch}`} hint="" placeholder="paste the key…" label="New key" />
              <button className={BTN_PRIMARY} disabled={busy || !pickValue || stale} onClick={add}>Add</button>
              {agentNote && agents.length === 0 && <span className="text-[13px] text-[var(--muted)]">{agentNote}</span>}
            </>
          )}
        </div>
      </div>
    </Section>
  )
}

/* ── unsaved changes ───────────────────────────────────────── */
// "Changed" means the draft differs from the saved value: numbers compare as
// numbers, strings trimmed, unset as ''.
const norm = v => (v == null ? '' : typeof v === 'string' ? v.trim() : v)
const same = (a, b) => {
  const x = norm(a), y = norm(b)
  return x === y || (x !== '' && y !== '' && !isNaN(x) && !isNaN(y) && Number(x) === Number(y))
}

// A settings card's draft: only the fields that differ from `saved`.
// Untouched fields follow the server on every poll and a changed one is never
// overwritten; a revert, or a poll that brings the saved value level with it,
// drops the draft. blank = what the card shows for an unset field (a slider's
// default), so moving it back is no change. scope: drafts made under another
// scope read as none (room agent picks belong to one system).
function useDraft(saved, { scope = '', blank = {} } = {}) {
  const [state, setState] = useState({ scope, d: {}, seen: '' })
  const d = state.scope === scope ? state.d : {}
  const eq = (k, v) => same(v ?? blank[k], saved[k] ?? blank[k])
  const without = (src, gone) => Object.fromEntries(Object.entries(src).filter(([k, v]) => !gone(k, v)))
  // New saved values: drop the drafts they now match (during render, so no
  // frame shows a stale count).
  const seen = JSON.stringify([saved, blank])
  if (state.seen !== seen) {
    setState({ ...state, seen, d: state.scope === scope ? without(state.d, (k, v) => k in saved && eq(k, v)) : state.d })
  }
  const edit = f => setState(s => ({ ...s, scope, d: f(s.scope === scope ? s.d : {}) }))
  return {
    draft: d,
    val: k => (k in d ? d[k] : saved[k]),
    changed: k => k in d && !eq(k, d[k]),
    // { field: value }; undefined puts the field back to saved. A typed
    // space is kept (it would vanish under the cursor), it just isn't a change.
    set: patch => edit(cur => {
      const n = { ...cur }
      for (const [k, v] of Object.entries(patch)) {
        if (v === undefined || (eq(k, v) && norm(v) === v)) delete n[k]
        else n[k] = v
      }
      return n
    }),
    discard: () => edit(() => ({})),
    // After a save (and its reload): drop the fields it sent, unless edited
    // again meanwhile; a value the server stored differently can't linger.
    snap: keys => Object.fromEntries(keys.filter(k => k in d).map(k => [k, d[k]])),
    settle: sent => edit(cur => without(cur, (k, v) => k in sent && v === sent[k])),
  }
}

// Settings cards register their unsaved changes; the bar at the page bottom
// saves or discards them all. Actions (keys, volumes, node and zone buttons,
// uploads) stay instant and never register.
const UnsavedCtx = createContext(() => {})
// The bar's order = page order, tab by tab. Keys are the cards' section ids,
// so each label links to its card (and opens its tab).
const UNSAVED_ORDER = ['rooms', 'voice', 'stt', 'tuning', 'wake-name-card', 'wake-model', 'localai', 'intelligence']

// card: { label, count, save, discard }. save(say) is the card's own save,
// reporting through say (the bar sums them up) and returning true once saved.
function useUnsaved(key, card) {
  const put = useContext(UnsavedCtx)
  useEffect(() => put(key, card))  // every render: save and discard see the current draft
  useEffect(() => () => put(key, null), [key, put])
}

function UnsavedBar({ cards, units, setMsg }) {
  const [saving, setSaving] = useState(false)
  const keys = UNSAVED_ORDER.filter(k => units[k]?.count > 0)
  const total = keys.reduce((n, k) => n + units[k].count, 0)
  if (!total) return null
  const end = s => (/[.!?]$/.test(s) ? s : s + '.')
  // One card after another; one summary at the end instead of each card's
  // message flashing over the last. A failed card keeps its draft.
  const saveAll = async () => {
    setSaving(true)
    let done = 0, restart = false
    const failed = [], notes = []
    for (const k of keys) {
      const said = []
      let ok = false
      try { ok = !!(await cards.current[k]?.save(text => { said.push(text) })) }
      catch (e) { said.push(e.message) }
      const label = units[k].label
      if (!ok) {
        const why = (said[said.length - 1] || '').replace(/^[^:]*failed: /, '')
        failed.push(end(`${label} failed${why ? `: ${why}` : ''}`))
        continue
      }
      done++
      for (const text of said) {
        // "Saved — …" folds into the summary; anything else is a next step
        // the card asked for, kept in full.
        if (!text.startsWith('Saved')) notes.push(end(`${label}: ${text}`))
        else if (/restart/i.test(text) && !/no restart/i.test(text)) restart = true
      }
    }
    setSaving(false)
    const head = failed.length ? `Saved ${done} of ${keys.length}. ${failed.join(' ')}`
      : `Saved.${restart ? ' Restart to apply.' : ''}`
    setMsg([head, ...notes].join(' '), failed.length ? 'err' : 'ok')
  }
  const discard = () => {
    if (!window.confirm(`Discard ${total} unsaved change${total === 1 ? '' : 's'}?`)) return
    for (const k of keys) cards.current[k]?.discard()
  }
  return (
    <div role="region" aria-label="Unsaved changes" className="border-t border-[var(--separator)] bg-[var(--background)]"
      style={{ position: 'fixed', left: 0, right: 0, bottom: 0, zIndex: 10, paddingBottom: 'env(safe-area-inset-bottom)' }}>
      <div className="max-w-[1120px] mx-auto px-6 py-2 flex items-center justify-between gap-4 flex-wrap">
        <p className="text-[13.5px] m-0">
          <span className="text-[var(--muted)]">{total} unsaved: </span>
          {keys.map((k, i) => (
            <Fragment key={k}>
              {i > 0 && ' · '}
              {/* Already at this hash: no hashchange fires, so scroll to the card here. */}
              <a href={`#${k}`} className="text-[var(--foreground)] hover:underline"
                onClick={() => { if (window.location.hash === `#${k}`) scrollToCard(k) }}>
                {units[k].label}</a>
            </Fragment>
          ))}
        </p>
        <div className="flex gap-2">
          <button className={BTN_DEFAULT} disabled={saving} onClick={discard}>Discard</button>
          <button className={BTN_PRIMARY} disabled={saving} onClick={saveAll}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </div>
    </div>
  )
}

// Scroll a card's section to just under the pinned header. The header wraps
// taller on a phone than the sections' fixed scroll margin allows for.
function scrollToCard(id) {
  const el = document.getElementById(id)
  const card = el?.closest('section') || el
  if (!card) return
  const head = document.querySelector('header')?.offsetHeight || 0
  window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - head - 12)
}

/* ── tabs ──────────────────────────────────────────────────── */
const TABS = [
  { id: 'rooms', label: 'Rooms' },
  { id: 'voice', label: 'Voice' },
  { id: 'system', label: 'System' },
]

// The tab comes from the hash, so tabs are plain links (keyboard, middle-click,
// back/forward). Any other id (#keys, #stt) opens the tab that holds it, then
// scrolls to its card. The element decides first: some section ids are tab
// names too (the TTS section is #voice), and it sits on that tab.
function useHashTab() {
  const [nav, setNav] = useState({ tab: 'rooms', id: '' })
  useEffect(() => {
    const go = () => {
      const id = window.location.hash.slice(1)
      const el = id ? document.getElementById(id) : null
      const tab = el?.closest('[data-tab]')?.dataset.tab || (TABS.some(t => t.id === id) ? id : 'rooms')
      setNav({ tab, id })
    }
    go()
    window.addEventListener('hashchange', go)
    return () => window.removeEventListener('hashchange', go)
  }, [])
  // After the tab is shown (a card on a tab not shown can't be scrolled to).
  // A tab opens at the page top, status chips in view; a card scrolls to its
  // section, whose scroll margin clears the header.
  useEffect(() => {
    if (!nav.id) return
    if (TABS.some(t => t.id === nav.id)) { window.scrollTo(0, 0); return }
    scrollToCard(nav.id)
  }, [nav])
  return nav.tab
}

/* ── Overview ──────────────────────────────────────────────── */
// In the header row on every tab, so it never scrolls away.
function RestartButton({ status, setMsg, setRebooting, rebooting, unsaved }) {
  // Saved settings the running bot hasn't loaded yet. Saves never restart
  // (a restart reboots every node), so one restart applies them all.
  // One lime per view: while the unsaved bar shows, its Save leads.
  const pending = status?.restart_pending || []
  return (
    <button className={pending.length && !unsaved ? BTN_PRIMARY : BTN_DEFAULT} disabled={rebooting}
      title={pending.length ? `Pending: ${pending.join(', ')}` : undefined}
      onClick={() => {
        if (window.confirm(unsaved
          ? "You have unsaved changes. They won't apply. Restart anyway? Every node reboots and reconnects."
          : 'Restart the bot? Every node reboots and reconnects.')) {
          restartBot(setMsg, setRebooting)
        }
      }}>
      {pending.length ? `Restart to apply (${pending.length})` : 'Restart bot'}
    </button>
  )
}

// The status chips, under the header on every tab. Not pinned: a wrapped
// chip row would fill a phone screen.
function Overview({ status, tts, intelligence, mapping }) {
  return (
    <div className="flex flex-wrap gap-8 rounded-2xl bg-[var(--surface)] p-4">
      <Stat label="Uptime">
        <StatChip stat="uptime" muted={status?.uptime_secs == null}>{sec(status?.uptime_secs)}</StatChip>
      </Stat>
      <Stat label="Intelligence"><StatChip stat="intelligence" {...intelligenceChip(status, intelligence)} /></Stat>
      <Stat label="Nodes online"><StatChip stat="nodes" {...nodesChip(status?.rooms, mapping)} /></Stat>
      <Stat label="Last wake">
        <StatChip stat="last-wake" muted={!status?.last_wake}>
          {status?.last_wake ? `${esc(status.last_wake.room)}, ${fmtAgo(status.last_wake.time)}` : 'none since boot'}
        </StatChip>
      </Stat>
      <Stat label="Version">
        <StatChip stat="version" muted={!status?.version} className="font-mono">{esc(status?.version) || '…'}</StatChip>
      </Stat>
      <Stat label="STT"><StatChip stat="stt" {...sttChip(status?.stt, status?.stt_model)} /></Stat>
      <Stat label="TTS"><StatChip stat="tts" {...ttsChip(status, tts)} /></Stat>
    </div>
  )
}

function Stat({ label, children }) {
  return (
    <div className="grid gap-1">
      <span data-stat-label className="text-[12px] leading-4 font-medium text-[var(--muted)]">{label}</span>
      <span className="text-[14px] leading-5 font-medium">{children}</span>
    </div>
  )
}

// Status-bar chips (brand guide, Chip): soft status colours from the theme
// tokens — success = all good, warning = partly degraded, danger = down.
// Neutral otherwise: colour only shows up when it means something.
// Chip words are lowercase, one or two words (brand README: status chips
// are lowercase, even brand names — "sonos"); hover text is a sentence.
// A setup gap (no key, no URL, an unmapped room) is a warning chip that links
// to the card that fixes it; healthy chips are not links.
const STAT_TONE = {
  success: 'bg-[var(--success-soft)] text-[var(--success-soft-foreground)]',
  warning: 'bg-[var(--warning-soft)] text-[var(--warning-soft-foreground)]',
  danger: 'bg-[var(--danger-soft)] text-[var(--danger-soft-foreground)]',
  neutral: 'bg-[var(--default)]',
  brand: '',  // a foreign brand's own colours, set inline
}

function StatChip({ stat, tone = 'neutral', text, title, muted, href, className, style, children }) {
  const Tag = href ? 'a' : 'span'
  return (
    <Tag data-stat={stat} data-tone={tone} href={href} title={title || undefined} style={style || undefined}
      className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium${STAT_TONE[tone] ? ` ${STAT_TONE[tone]}` : ''}${muted ? ' text-[var(--muted)]' : ''}${href ? ' hover:underline' : ''}${className ? ` ${className}` : ''}`}>
      {children ?? text}
    </Tag>
  )
}

// Intelligence: the brand the bot runs, unless the configured kind has no
// URL or no key saved (the next restart can't reach it).
function intelligenceChip(status, intel) {
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
  // Brand chips (2026-10-08): the colours come from the lane list. Inline
  // styles — these are foreign brands, they don't earn design tokens.
  // Chip shows the brand only — the agent id lives in the Intelligence
  // section: strip " · <agent>" from the label.
  const brand = (esc(status?.intelligence) || '—').split(' · ')[0]
  const lane = LANES.find(l => l.label === brand)
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
function nodesChip(rooms, mapping) {
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

const TTS_NAMES = { elevenlabs: 'ElevenLabs', deepgram: 'Deepgram', 'deepgram-flux': 'Deepgram Flux', pocket: 'Pocket', kokoro: 'Kokoro', localai: 'LocalAI' }
const ttsName = p => TTS_NAMES[p] || p

// TTS: the voice service the rooms are actually using (each online room
// reports its live provider); the configured one only until a room has
// reported — config edits apply on the next restart. Offline rooms don't
// count: their last report is stale. A configured cloud voice with no saved
// key (GET /tts) is a warning: it can't speak after the next restart.
const TTS_KEY_HINT = { elevenlabs: 'elevenlabs_key_hint', deepgram: 'deepgram_key_hint', 'deepgram-flux': 'deepgram_key_hint', localai: 'localai_key_hint' }
function ttsChip(status, tts) {
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
const STT_CLOUD = {
  deepgram: { name: 'Deepgram', key: 'deepgram_key_set', chip: st => `deepgram ${st.cloud_model || ''}`.trim() },
  elevenlabs: { name: 'ElevenLabs', key: 'elevenlabs_key_set', chip: () => 'elevenlabs scribe' },
}

// STT: Whisper <model>, or a cloud provider with Whisper as its fallback.
// model falls back to the configured one (status.stt_model) before the
// first load reports it.
function sttChip(st, configuredModel) {
  if (!st) return { text: '—', muted: true }
  const whisper = `Whisper ${st.model || configuredModel || ''}`.trim()
  const cloud = STT_CLOUD[st.provider]
  const onWhisper = !cloud || st.active === 'whisper'
  if (onWhisper && st.state === 'error') return { tone: 'danger', text: 'whisper failed', title: st.error || 'Whisper failed to load' }
  if (cloud && st.active === 'whisper') {
    const rooms = (st.fallback_rooms || []).map(esc)
    return { tone: 'warning', text: 'whisper fallback',
      title: `${st.fallback_reason || `${cloud.name} unreachable`}${rooms.length ? ` (rooms: ${rooms.join(', ')})` : ''}` }
  }
  if (cloud) return { text: cloud.chip(st) }
  if (st.provider === 'localai') {
    if (st.active === 'whisper')
      return { tone: 'warning', text: 'whisper fallback', href: '#localai',
        title: `${st.fallback_reason || 'LocalAI unreachable'}: Whisper base on this appliance is listening` }
    return { text: `localai ${st.localai_model || ''}`.trim() }
  }
  if (st.state === 'loading') return { text: 'loading', muted: true, title: `${whisper} is loading` }
  return { text: whisper.toLowerCase() }
}

// A list's state row: one row across every column, so loading, a failed
// load and a truly empty list never look alike. Error beats loading beats
// empty; nothing to say = no row.
function TableState({ cols, loading, error, empty }) {
  const text = error ? `Couldn't load: ${error}` : loading ? 'Loading…' : empty
  if (!text) return null
  return <tr><td className={`px-4 py-2 ${error ? 'text-[var(--danger)]' : 'text-[var(--muted)]'}`} colSpan={cols}>{text}</td></tr>
}

/* ── node row actions ─────────────────────────────────────── */
function NodeActions({ room, online, lastSeen, reportedMuted, setMsg, reloadAll, children: renderRow }) {
  const [busy, setBusy] = useState(null)  // 'restart' | 'rename' | 'wifi' | 'mute'
  const [renameVal, setRenameVal] = useState('')
  const [wifiStage, setWifiStage] = useState(null)  // {nets} | {ssid}
  const [wifiPass, setWifiPass] = useState('')
  const [muted, setMuted] = useState(false)  // confirmed from the node's ack, never optimistic
  const [muteUnknown, setMuteUnknown] = useState(false)  // every /info try failed this connection
  const [muteBusy, setMuteBusy] = useState(false)
  // A Wi-Fi or name POST in flight. A second click reached a node already
  // rebooting (or already renamed) and its failure overwrote the success.
  const [sending, setSending] = useState(false)

  // Refresh the button from the NODE's truth on every mount (2026-10-07):
  // component state dies on page refresh, but the node remembers its mute
  // state — the button must re-derive it, or "Muted" un-highlights and the
  // UI lies about a muted mic. One GET per mount, NOT per 5s poll (that
  // would spam the data channel). Older firmware omits 'muted' = false.
  //
  // Ask once per CONNECTION, not once per mount.
  // The old latch never reset, so a node that rebooted (Restart button in
  // this same component) came back unmuted while the button still said
  // Muted — a privacy lie. Keyed on last_seen, NOT just online: registry
  // .mark_disconnected skips the offline flip while a newer session owns
  // the room, and on a fast reboot the new session connects BEFORE the
  // zombie is reaped — online never goes false, but mark_connected always
  // stamps a fresh last_seen. Up to 3 tries 4s apart: the first /info can
  // land before the new data channel is open.
  const askedAtRef = useRef(null)   // last_seen stamp of the connection we asked
  const triesRef = useRef(0)        // /info attempts for that connection
  // Bumped by every toggle — a slow /info reply that was sent
  // BEFORE the toggle must never stomp the fresh ack state (the stamp
  // guard only covers connection changes, not in-room races).
  const muteSeqRef = useRef(0)
  const [infoTick, setInfoTick] = useState(0)  // bumped only by the retry timer
  // Firmware 1.2.11+: the node REPORTS its mute state (fw_push at
  // connect, mute_push when an array node's physical button is pressed);
  // /status carries it as rooms[].muted. Follow it whenever it changes —
  // that's how a button press reaches this button within one 5s poll.
  // null = older firmware that never reports: the /info ask below covers it.
  useEffect(() => {
    if (typeof reportedMuted !== 'boolean' || muteBusy) return
    muteSeqRef.current++  // a newer truth than any /info in flight
    setMuted(reportedMuted)
    setMuteUnknown(false)
  }, [reportedMuted])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!online) { askedAtRef.current = null; return }
    // A node that REPORTS its mute state (1.2.11+) needs no ask — the
    // reported value is the truth and a racing /info reply could stomp it.
    if (typeof reportedMuted === 'boolean') return
    const stamp = String(lastSeen ?? 'online')
    if (askedAtRef.current === stamp) return
    if (askedAtRef.current !== `${stamp}#retry`) triesRef.current = 0  // new connection
    askedAtRef.current = stamp
    triesRef.current += 1
    // Staleness = "a newer connection took over" (stamp moved), NOT effect
    // cleanup — StrictMode's dev double-invoke would cancel the only ask.
    const seq = muteSeqRef.current  // staleness vs a toggle fired mid-flight
    // /info is guarded server-side (it talks to the node), so a token-less
    // browser must be asked for the token here — a GET never asks by default.
    api(`/api/dashboard/nodes/${encodeURIComponent(room)}/info`, { promptForToken: true })
      .then(r => {
        if (askedAtRef.current !== stamp || muteSeqRef.current !== seq) return
        if (r?.ok) { setMuted(!!r?.data?.muted); setMuteUnknown(false) }
        else throw new Error('info not ok')
      })
      .catch(e => {
        if (askedAtRef.current !== stamp || muteSeqRef.current !== seq) return
        // Out of tries: say so — never keep showing the LAST connection's state.
        // A cancelled or rejected token ask isn't retried (no repeat asks).
        if (triesRef.current >= 3 || e?.status === 401) { setMuteUnknown(true); return }
        setTimeout(() => {
          if (askedAtRef.current !== stamp) return
          askedAtRef.current = `${stamp}#retry`  // same connection, ask again
          setInfoTick(n => n + 1)
        }, 4000)
      })
  }, [room, online, lastSeen, infoTick, reportedMuted])

  // Raw POST — callers own the busy lifecycle (the wifi flow is
  // two-stage: busy must STAY 'wifi' after a successful scan; a
  // helper that auto-cleared busy killed the SSID form — caught
  // live 2026-10-07, "scan complete but no list").
  const post = (verb, json) =>
    api(`/api/dashboard/nodes/${encodeURIComponent(room)}/${verb}`, { method: 'POST', json })

  const toggleMute = async () => {
    if (muteBusy) return
    muteSeqRef.current++  // any /info in flight is now stale
    setMuteBusy(true)
    setMsg(`Toggling mute on ${room}…`, 'pulse', 'Nodes')
    try {
      // Unknown state → MUTE (privacy-safe), never a blind toggle.
      const r = await post('mute', { mute: muteUnknown ? true : !muted })
      const now = !!(r?.data?.muted)
      setMuted(now)
      setMuteUnknown(false)
      setMsg(`${room}: mic ${now ? 'MUTED — the node hears nothing' : 'live'}.`, 'ok', 'Nodes')
    } catch (e) { setMsg('Mute failed: ' + e.message, 'err', 'Nodes') }
    finally { setMuteBusy(false) }
  }

  const restart = async () => {
    if (!window.confirm(`Restart node '${room}'? It reboots and reconnects (~15s).`)) return
    setBusy('restart')
    setMsg(`Restarting ${room}…`, 'pulse', 'Nodes')
    try {
      await post('restart')
      setMsg(`Restart sent — ${room} reboots and reconnects (~15s).`, 'ok', 'Nodes')
    } catch (e) { setMsg('Node restart failed: ' + e.message, 'err', 'Nodes') }
    finally { setBusy(null) }
  }

  const rename = async () => {
    if (sending) return
    if (!renameVal.trim()) { setMsg('New name required.', 'err', 'Nodes'); return }
    if (!/^[A-Za-z0-9_-]+$/.test(renameVal)) {
      setMsg('Name: letters, numbers, _ or - only (max 31).', 'err', 'Nodes'); return }
    setBusy('rename')
    setSending(true)
    setMsg(`Renaming ${room} to ${renameVal}…`, 'pulse', 'Nodes')
    try {
      await post('name', { name: renameVal })
      setMsg(`Renamed to '${renameVal}' — hostname follows on the router.`, 'ok', 'Nodes')
      setRenameVal('')
      setBusy(null)
      await reloadAll()
    } catch (e) { setMsg('Rename failed: ' + e.message, 'err', 'Nodes') }  // form stays open to retry
    finally { setSending(false) }
  }

  const scanForWifi = async () => {
    setBusy('wifi')
    setWifiStage(null)
    setWifiPass('')
    setMsg(`Scanning networks from ${room}…`, 'pulse', 'Nodes')
    try {
      const r = await post('scan')
      const nets = Object.entries(r.data || {}).sort((a, c) => c[1] - a[1])
      if (!nets.length) {
        setBusy(null)
        setMsg(`Scan complete, but ${room} saw no networks.`, 'err', 'Nodes')
        return
      }
      // busy stays 'wifi' — the SSID form is gated on it.
      setWifiStage({ nets })
      setMsg(`Scan complete — pick a network for ${room}.`, 'ok', 'Nodes')
    } catch (e) { setBusy(null); setMsg('Scan failed: ' + e.message, 'err', 'Nodes') }
  }

  const applyWifi = async () => {
    if (sending) return
    if (!wifiStage?.ssid) { setMsg('Pick a network first.', 'err', 'Nodes'); return }
    if (!wifiPass) { setMsg('Wi-Fi password required.', 'err', 'Nodes'); return }
    setSending(true)
    setMsg(`Applying Wi-Fi to ${room}…`, 'pulse', 'Nodes')
    try {
      await post('wifi', { ssid: wifiStage.ssid, password: wifiPass })
      setMsg(`Wi-Fi saved — ${room} reboots onto the new network. If it can't reach the bot there, it switches back to the old network on its own within about 5 minutes.`, 'ok', 'Nodes')
      setWifiStage(null); setWifiPass('')
      setBusy(null)
      await reloadAll()
    } catch (e) { setMsg('Wi-Fi set failed: ' + e.message, 'err', 'Nodes') }  // form stays open to retry
    finally { setSending(false) }
  }

  // The Mute button renders in the row's STATUS cell, right after the
  // online/offline chip — on a phone the list scrolls sideways and the
  // far-right Actions cell was off-screen, so muting a room needed that
  // scroll. The row is this component's only caller and hands us a render
  // function as JSX children, so the ONE mute state here feeds both cells.
  // The state is a chip (like online/offline); the button names the action.
  const mute = (
    <>
      {muted && !muteUnknown &&
        <span className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE.warning}`}>muted</span>}
      <button className={BTN_DEFAULT} disabled={!online || busy || muteBusy}
        onClick={toggleMute}
        title={muteUnknown ? "Couldn't read this node's mute state since it reconnected — click to mute it."
          : muted ? 'Mic is muted — click to unmute.' : "Mute this node's mic. Array nodes: same as the physical mute button (red LED) — pressing either one updates both."}>
        {muteBusy ? '…' : muteUnknown ? 'Mute?' : muted ? 'Unmute' : 'Mute'}
      </button>
    </>
  )
  const actions = (
    <div className="grid gap-2 justify-items-end">
      <div className="flex gap-2 flex-wrap justify-end">
        <button className={BTN_DEFAULT} disabled={!online || busy}
          onClick={restart} title="Reboot the node (~15s reconnect).">
          {busy === 'restart' ? 'Restarting…' : 'Restart'}
        </button>
        {online && (
          <>
            <button className={BTN_DEFAULT} disabled={busy}
              onClick={() => { setRenameVal(room); setBusy('rename') }}
              title="Rename the node (letters, numbers, _ or -).">
              Rename
            </button>
            <button className={BTN_DEFAULT} disabled={busy}
              onClick={scanForWifi}
              title="Scan networks from the node, then set new Wi-Fi creds.">
              {busy === 'wifi' && !wifiStage ? 'Scanning…' : 'Wi-Fi…'}
            </button>
          </>
        )}
      </div>
      {busy === 'rename' && (
        <div className="flex gap-2 flex-wrap justify-end">
          <input className={`${INPUT_CLS} w-[160px] font-mono`} value={renameVal}
            onChange={e => setRenameVal(e.target.value)}
            placeholder="new-name" aria-label={`New name for ${room}`} />
          <button className={BTN_PRIMARY} disabled={sending} onClick={rename}>Save name</button>
          <button className={BTN_DEFAULT} onClick={() => { setBusy(null); setRenameVal('') }}>Cancel</button>
        </div>
      )}
      {busy === 'wifi' && wifiStage?.nets && (
        <div className="grid gap-2 justify-items-end">
          <select className={`${INPUT_CLS} w-[240px]`}
            onChange={e => setWifiStage({ ...wifiStage, ssid: e.target.value })}
            aria-label={`Network for ${room}`}>
            <option value="">pick a network…</option>
            {wifiStage.nets.map(([ssid, rssi]) => (
              <option key={ssid} value={ssid}>{ssid} ({rssi})</option>
            ))}
          </select>
          <input type="password" className={`${INPUT_CLS} w-[240px]`} value={wifiPass}
            onChange={e => setWifiPass(e.target.value)}
            placeholder="Wi-Fi password" aria-label={`Password for ${room}`} />
          <div className="flex gap-2 justify-end">
            <button className={BTN_PRIMARY} disabled={sending} onClick={applyWifi}>Apply</button>
            <button className={BTN_DEFAULT} onClick={() => { setBusy(null); setWifiStage(null) }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
  return renderRow({ mute, actions })
}

/* ── node details ─────────────────────────────────────────── */
// get_info's health keys (firmware 1.2.41+) in plain words. Older firmware
// omits them, so every line renders only from the keys the node sent; an
// unknown tag shows as the node sent it.
const BOOT_WHY = {
  'bot-closed': 'the bot closed the session',
  'peer-dropped': 'the connection to the bot dropped',
  'connect-timeout': "couldn't connect to the bot",
  'dc-never-opened': 'the data channel never opened',
  'liveness-no-pong': 'the bot stopped answering its liveness check',
  'cmd-restart': 'restarted from the dashboard',
  'check-ota': 'restarted to check for an update',
  'ota-success': 'restarted onto new firmware',
  'ota-health-timeout': 'new firmware failed its health check',
  'set-wifi': 'restarted onto new Wi-Fi',
  'wifi-trial-timeout': 'the new Wi-Fi never reached the bot',
  'wifi-trial-authfail': 'the new Wi-Fi password was refused',
  'offer-http-error': 'the bot refused its connection offer',
  'offer-bad-json': "the bot's answer to its offer was unreadable",
  'offer-no-sdp': "the bot's answer to its offer was empty",
  'offer-too-big': "the bot's answer to its offer was too big",
  'offer-oom': 'out of memory while connecting',
  'audio-oom': 'out of memory starting the mic',
  'rtvi-init': 'the message channel failed to start',
}
const OTA_LAST = {
  none: 'no update check yet',
  'up-to-date': 'up to date',
  'check-failed': 'update check failed',
  'bad-http': 'update server error',
  'wrong-board': 'refused an image built for another board',
  'refused-rolled-back': 'refused a version it rolled back from',
  'too-big': 'image too big',
  'download-failed': 'download failed',
  'flash-failed': 'flash write failed',
  'version-mismatch': "image version didn't match the manifest",
  updated: 'updated',
  deferred: 'update deferred',
}
// ESP-IDF Wi-Fi disconnect reasons worth naming; the rest show as a number.
const WIFI_REASON = {
  2: 'auth expired', 4: 'association expired', 8: 'left the network', 15: '4-way handshake timeout',
  201: 'no access point found', 202: 'auth failed', 203: 'association failed', 204: 'handshake timeout',
}
const upFor = s => (s < 60 ? `${s}s` : sec(s))

// One node's health from get_info (boot, Wi-Fi, updates, audio). One /info
// per open, never polled: each ask is a round-trip to the node. The ref (not
// effect cleanup) keeps it to one ask, as in NodeActions.
function NodeDetails({ room }) {
  const [info, setInfo] = useState(null)  // {data} | {error}
  const askedRef = useRef(false)
  useEffect(() => {
    if (askedRef.current) return
    askedRef.current = true
    api(`/api/dashboard/nodes/${encodeURIComponent(room)}/info`, { promptForToken: true })
      .then(r => setInfo(r?.ok ? { data: r.data || {} } : { error: 'no answer' }))
      .catch(e => setInfo({ error: e?.message || 'no answer' }))
  }, [room])

  const note = text => <span className="text-[13px] text-[var(--muted)]">{text}</span>
  if (!info) return note('Reading details from the node…')
  if (info.error) return note(`Details unavailable (${info.error}). Older firmware doesn't report them.`)
  const part = v => (v && typeof v === 'object' ? v : {})
  const num = v => typeof v === 'number'
  const boot = part(info.data.boot), wifi = part(info.data.wifi), ka = part(wifi.ka)
  const ota = part(info.data.ota), audio = part(info.data.audio)
  const warnings = []
  const rows = [
    ['Boot', [
      boot.why && (BOOT_WHY[boot.why] || String(boot.why)),
      boot.reason != null && `reset: ${boot.reason}`,
      num(boot.prev_up_s) && `previous run ${upFor(boot.prev_up_s)}`,
      num(boot.up_s) && `up ${upFor(boot.up_s)}`,
    ]],
    ['Wi-Fi', [
      num(wifi.rssi) && `${wifi.rssi} dBm`,
      num(wifi.last_reason) && wifi.last_reason !== 0
        && `last disconnect: ${WIFI_REASON[wifi.last_reason] || `reason ${wifi.last_reason}`}`,
      num(wifi.disconnects) && `${wifi.disconnects} disconnect${wifi.disconnects === 1 ? '' : 's'}`,
      (num(ka.resent) || num(ka.rescued)) && `keepalive: ${ka.resent ?? 0} resent, ${ka.rescued ?? 0} rescued`,
    ]],
    ['Updates', [
      ota.last && (OTA_LAST[ota.last] || String(ota.last)),
      ota.http != null && ota.http !== 0 && `HTTP ${ota.http}`,
      num(ota.at_s) && `${ota.at_s}s after boot`,
    ]],
    ['Audio', [
      audio.init === 'ok' && 'mic started',
      num(audio.max_gap_ms) && `max gap ${audio.max_gap_ms} ms`,
    ]],
  ].map(([label, parts]) => [label, parts.filter(p => typeof p === 'string')]).filter(([, parts]) => parts.length)
  if (audio.init && audio.init !== 'ok') warnings.push(`mic failed to start: ${audio.init}`)
  if (num(audio.zero_s) && audio.zero_s > 0) warnings.push(`mic sent silence for ${audio.zero_s} s while unmuted`)
  if (!rows.length && !warnings.length) return note("This node's firmware doesn't report details.")
  return (
    <div className="grid gap-1 text-[13px]">
      {rows.map(([label, parts]) => (
        <div key={label}>
          <span className="text-[12px] font-medium text-[var(--muted)]">{label}</span> {parts.join(' · ')}
        </div>
      ))}
      {warnings.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {warnings.map(w => (
            <span key={w} className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE.warning}`}>{w}</span>
          ))}
        </div>
      )}
    </div>
  )
}

function Rooms({ status, mapping, setMsg, reloadAll }) {
  // Per-room agent override. The Intelligence card's agent is the house
  // default; a room only differs when picked here ('' = back to Default).
  const [found, setFound] = useState(null)
  const [openDetails, setOpenDetails] = useState({})  // room -> Details shown under its Nodes row
  const agentKind = mapping?.agent_kind || ''
  const defaultAgent = mapping?.default_agent || ''
  const savedAgents = mapping?.room_agents?.[agentKind] || {}
  const discoverAgents = useRef(null)
  // found holds one kind's list, so one counter: a late reply for an older
  // request (or the kind before a switch) never replaces the newest one.
  const discSeq = useRef(0)
  useEffect(() => {
    discoverAgents.current = async () => {
      const seq = ++discSeq.current
      if (!agentKind) return
      try {
        const r = await api('/api/dashboard/intelligence/agents', { method: 'POST', json: { kind: agentKind } })
        if (seq === discSeq.current) setFound(r)
      } catch (e) {
        if (seq === discSeq.current) setFound({ agents: [], note: 'Agent list failed: ' + e.message })
      }
    }
  })
  useEffect(() => { discoverAgents.current?.() }, [agentKind])
  useEffect(() => {
    const h = () => discoverAgents.current?.()
    window.addEventListener('lenoxvi:reload-all', h)
    return () => window.removeEventListener('lenoxvi:reload-all', h)
  }, [])
  // Session-mode switches: optimistic pending until status confirms.
  const [pendingMode, setPendingMode] = useState({})

  const toggleMode = async (room, sticky) => {
    setPendingMode(p => ({ ...p, [room]: sticky ? 'sticky' : 'fresh' }))
    try {
      const r = await api(`/api/dashboard/nodes/${encodeURIComponent(room)}/session-mode`, {
        method: 'POST', json: { mode: sticky ? 'sticky' : 'fresh' } })
      setMsg(`${room}: session ${r.mode}` +
        (r.mode === 'sticky' ? ' — memory kept across reconnects (applies on next reconnect).'
                             : ' — memory clears each reconnect (applies on next reconnect).'), 'ok', 'Nodes')
      await reloadAll()
    } catch (e) {
      setMsg('Session mode save failed: ' + e.message, 'err', 'Nodes')
    } finally {
      setPendingMode(p => { const n = { ...p }; delete n[room]; return n })
    }
  }

  const zoneNames = Object.keys(mapping?.zones || {})
  // Rows = mapped rooms ∪ connected nodes. Rows
  // used to come from room_zones alone, so a node that had connected but
  // was never mapped had NO row — a fresh install could not map its first
  // node from the React UI (regression vs page.html; also why a third
  // node couldn't onboard). Sorted by name, the same order the Nodes list uses.
  const mappedRooms = Object.keys(mapping?.room_zones || {})
  const unmappedRooms = (status?.rooms || []).map(r => r.room)
    .filter(r => r && !mappedRooms.includes(r))
  const roomRows = [...mappedRooms, ...unmappedRooms].sort(byRoom)
  // Speaker nodes (server-side: board line) get their own built-in speaker
  // as a private zone. Unmapped = that speaker, by default.
  const speakers = mapping?.speakers || {}
  const zoneLabel = z => (z === BUILTIN ? 'Built-in speaker' : z)
  // An unmapped speaker room defaults to its own speaker only once the
  // server resolves it there (the node proved its speaker this session).
  const defaultZone = room => (speakers[room]?.zone === BUILTIN ? BUILTIN : undefined)
  // A zone deleted elsewhere while a draft held it: that draft entry is
  // dropped from every Save (the server refuses an unknown zone, so one
  // dead entry failed every row's Save until a page reload).
  const liveZone = z => z === BUILTIN || zoneNames.includes(z)
  // Drafts hold only the picks that differ from the saved map (useDraft), so
  // the 5 s poll never clobbers one and untouched rows keep following it.
  const zones = useDraft(Object.fromEntries(roomRows.map(r => [r, mapping?.room_zones?.[r] ?? defaultZone(r) ?? ''])))
  const agents = useDraft(Object.fromEntries(roomRows.map(r => [r, savedAgents[r] ?? ''])), { scope: agentKind })
  const mapDraft = zones.draft
  const agentDraft = agents.draft
  const changedRooms = roomRows.filter(r => zones.changed(r) || agents.changed(r))
  // One save for every changed row.
  const saveMapping = async say => {
    const picked = changedRooms.map(r => [r, zones.val(r)])
    if (picked.some(([, z]) => !z || !liveZone(z))) { say('Pick a zone first.', 'muted', 'Room → zone mapping'); return false }
    // PUT /mapping takes the FULL room_zones dict WRAPPED in a
    // room_zones key (MappingPayload contract, routes.py) — a bare
    // merged dict 422s (or worse: pre-forbid, silently wiped the
    // mapping via Pydantic's default_factory). Never unwrapped.
    // The server's map plus every row's draft, so one Save persists the
    // whole map on screen (and the full-dict wipe-guard contract stays honest).
    // Rebased on the server's CURRENT map: a room renamed (or gone) while
    // this draft sat unsaved is dropped from the draft, not PUT back under
    // its old name — that undid the rename's carried mapping.
    const liveMap = mapping?.room_zones || {}
    const liveRooms = new Set([...Object.keys(liveMap), ...(status?.rooms || []).map(r => r.room)])
    const stillLive = draft => Object.fromEntries(Object.entries(draft || {}).filter(([r]) => liveRooms.has(r)))
    const liveZones = draft => Object.fromEntries(Object.entries(draft).filter(([, z]) => liveZone(z)))
    const body = { room_zones: { ...liveMap, ...liveZones(stillLive(mapDraft)), ...Object.fromEntries(picked) } }
    // Only this system's overrides, and only when one was edited — an
    // untouched page leaves room_agents alone server-side.
    const liveAgentDraft = stillLive(agentDraft)
    const changed = Object.entries(liveAgentDraft).filter(([r, a]) => a !== (savedAgents[r] ?? ''))
    const agentEdits = changed.length > 0
    if (agentEdits) body.room_agents = { [agentKind]: { ...savedAgents, ...liveAgentDraft } }
    const sentZones = { ...mapDraft }, sentAgents = { ...agentDraft }
    try {
      await api('/api/dashboard/mapping', { method: 'PUT', json: body })
      // Keep the draft until the saved map is back: clearing it first showed
      // the OLD mapping (the row flicked back). An edit made meanwhile keeps
      // its draft.
      await reloadAll()
      zones.settle(sentZones)
      agents.settle(sentAgents)
      if (agentEdits) {
        const picks = changed.map(([r, a]) => `${r} → ${a || defaultAgentLabel(defaultAgent)}`).join(', ')
        say(`Saved — restart to apply (agent ${picks}).`, 'ok', 'Room → zone mapping')
      } else {
        say(`Saved — ${picked.map(([r, z]) => `${r} → ${zoneLabel(z)}`).join(', ')}: replies now route there (live, no restart).`, 'ok', 'Room → zone mapping')
      }
      return true
    } catch (e) { say('Mapping save failed: ' + e.message, 'err', 'Room → zone mapping'); return false }
  }
  useUnsaved('rooms', {
    label: 'room mapping', count: changedRooms.length, save: saveMapping,
    discard: () => { zones.discard(); agents.discard() },
  })

  return (
    <>
      <Section id="rooms" title="Room → zone mapping">
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden" style={{ overflowX: 'auto' }}>
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] text-[var(--muted)]">
                <th className="text-left px-4 py-2">Room (mic node)</th>
                <th className="text-left px-4 py-2">Zone</th>
                <th className="text-left px-4 py-2">Agent</th>
              </tr>
            </thead>
            <tbody>
              {roomRows.map(room => {
                const hasSpeaker = !!speakers[room]
                const unmapped = !mapping?.room_zones?.[room]
                const zone = mapDraft?.[room] ?? mapping?.room_zones?.[room] ?? defaultZone(room) ?? ''
                const agent = agentDraft[room] ?? savedAgents[room] ?? ''
                const agentList = found?.agents || []
                const known = agentList.some(a => a.id === agent)
                const effective = agentList.find(a => a.id === (agent || defaultAgent))
                return (
                <tr key={room} className="border-t border-[var(--separator)]">
                  <td className="px-4 py-2 font-mono text-[13px]">
                    {esc(room)}
                    {unmapped && !defaultZone(room) && <span className="ml-2 inline-block rounded-full px-2 py-0.5 text-[11px] font-sans font-medium text-[var(--muted)] border border-[var(--separator)]">unmapped</span>}
                  </td>
                  <td className="px-4 py-2">
                    {/* Empty placeholder: without it an unmapped row
                        DISPLAYS the first zone while its value is '' — a
                        fake selection that Save would reject as "pick a zone". */}
                    <select value={zone}
                      onChange={e => zones.set({ [room]: e.target.value })}
                      className={`${INPUT_CLS} w-[200px]`}>
                      {(zone === '' || (!zoneNames.includes(zone) && !(hasSpeaker && zone === BUILTIN))) &&
                        <option value="" disabled>{zoneNames.length ? 'pick a zone…' : 'add a zone below first'}</option>}
                      {hasSpeaker && <option value={BUILTIN}>{unmapped && defaultZone(room) ? 'Built-in speaker (default)' : 'Built-in speaker'}</option>}
                      {hasSpeaker && zoneNames.length > 0 && <option disabled value="__sep">──────────</option>}
                      {zoneNames.map(z => <option key={z} value={z}>{z}</option>)}
                    </select>
                  </td>
                  <td className="px-4 py-2">
                    <div className="grid gap-1">
                      <select value={agent} data-agent-room={room}
                        onChange={e => agents.set({ [room]: e.target.value })}
                        className={`${INPUT_CLS} w-[200px]`}>
                        <option value="">{defaultAgentLabel(defaultAgent)}</option>
                        {agent && !known && <option value={agent}>
                          {!found ? agent : (agentList.length ? `${agent} (not found)` : `${agent} (unverified)`)}</option>}
                        {agentList.map(a => <option key={a.id} value={a.id}>
                          {(a.id === laneOf(agentKind)?.default_agent && laneOf(agentKind)?.per_agent_keys ? 'default (pinned)' : a.id) + (a.key_set ? '' : ' — no key')}</option>)}
                      </select>
                      {effective && !effective.key_set &&
                        <MissingKey label={`${laneLabel(agentKind)} · ${effective.id}`} />}
                    </div>
                  </td>
                </tr>
                )
              })}
              {/* Rows come from /mapping and /status: empty only once both have loaded. */}
              <TableState cols={3} error={mapping?.loadError}
                loading={!roomRows.length && !(mapping?.room_zones && status)}
                empty={!roomRows.length && 'No mic nodes yet. A flashed node appears here when it connects.'} />
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Nodes"
        note="">
        <div className="rounded-2xl bg-[var(--surface)] overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] text-[var(--muted)]">
                <th className="text-left px-4 py-2">Node</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Since</th>
                <th className="text-left px-4 py-2">Firmware</th>
                <th className="text-left px-4 py-2">Session</th>
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {[...(status?.rooms || [])].sort((a, b) => byRoom(a.room, b.room)).map(r => (
                <NodeActions key={r.room} room={r.room} online={r.online} lastSeen={r.last_seen}
                  reportedMuted={r.muted} setMsg={setMsg} reloadAll={reloadAll}>
                  {({ mute, actions }) => (
                    <>
                    <tr className="border-t border-[var(--separator)]">
                      <td className="px-4 py-2 font-mono text-[13px]">
                        {esc(r.room)}
                        {r.online && (
                          <button aria-expanded={!!openDetails[r.room]}
                            className="ml-2 font-sans text-[12px] text-[var(--muted)] hover:text-[var(--foreground)] cursor-pointer"
                            onClick={() => setOpenDetails(o => ({ ...o, [r.room]: !o[r.room] }))}
                            title="Boot, Wi-Fi, update and audio health, read from the node.">
                            {openDetails[r.room] ? 'Hide details' : 'Details'}
                          </button>
                        )}
                      </td>
                      <td className="px-4 py-2">
                        {/* Brand Chip rule: soft success "online", soft danger "offline".
                            The Mute button rides with the chip (flex-wrap keeps them on
                            one line until the screen is too narrow) — in the far-right
                            Actions cell a phone had to scroll the list sideways to mute. */}
                        <div className="flex items-center gap-2 flex-wrap">
                          <span data-role="node-state"
                            className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE[r.online ? 'success' : 'danger']}`}>
                            {r.online ? 'online' : 'offline'}
                          </span>
                          {mute}
                          {r.online && r.tts_usable === false && (
                            <span
                              title={`TTS (${r.tts_provider || 'provider unknown'}) is down — this node hears you but can't speak. Reboot the node (or the bot) to rebuild its session.`}
                              className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE.danger}`}>
                              tts down
                            </span>
                          )}
                          {r.online && r.tts_usable !== false && String(r.tts_provider || '').endsWith('(fallback)') && (
                            <span
                              title={`The TTS provider is down — this room is speaking with the local ${ttsName(String(r.tts_provider).replace(/\s*\(fallback\)$/, ''))} voice until it comes back.`}
                              className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE.warning}`}>
                              tts fallback
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2">{fmtAgo(r.last_seen)}</td>
                      <td className="px-4 py-2 font-mono text-[13px]">
                        {esc(r.fw) || '—'}
                        {LINE_TITLE[r.line] && (
                          <span
                            title={LINE_TITLE[r.line]}
                            className="ml-2 inline-block rounded-full px-2 py-0.5 text-[11px] font-sans font-medium text-[var(--muted)] border border-[var(--separator)]">
                            {r.line}
                          </span>
                        )}
                        {/* fw_push (1.2.41+): what this boot recovered from. */}
                        {r.fw_rejected && (
                          <span
                            title={`This node refused ${esc(r.fw_rejected)} after it failed its health check and rolled back. Upload a fixed build.`}
                            className={`ml-2 inline-block rounded-full px-2.5 py-0.5 text-[12px] font-sans font-medium ${STAT_TONE.warning}`}>
                            rollback {esc(r.fw_rejected)}
                          </span>
                        )}
                        {r.wifi_rollback === true && (
                          <span
                            title="The last Wi-Fi change never reached the bot, so the node went back to its previous network."
                            className={`ml-2 inline-block rounded-full px-2.5 py-0.5 text-[12px] font-sans font-medium ${STAT_TONE.warning}`}>
                            wifi rolled back
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          <Switch
                            checked={pendingMode[r.room] === 'sticky' || (!pendingMode[r.room] && (status?.session_modes?.[r.room] === 'sticky'))}
                            onChange={sticky => toggleMode(r.room, sticky)}
                            title="Sticky keeps conversation memory across reconnects and bot restarts; fresh clears it (privacy default)." />
                          <span className="text-[13px] text-[var(--muted)]">
                            {pendingMode[r.room] || status?.session_modes?.[r.room] || 'fresh'}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-2">{actions}</td>
                    </tr>
                    {r.online && openDetails[r.room] && (
                      <tr data-role="node-details">
                        <td className="px-4 pb-3" colSpan={6}><NodeDetails room={r.room} /></td>
                      </tr>
                    )}
                    </>
                  )}
                </NodeActions>
              ))}
              {/* A failed /status shows in the message strip above. */}
              <TableState cols={6} loading={!status} empty={!status?.rooms?.length && 'No nodes yet.'} />
            </tbody>
          </table>
        </div>
      </Section>

      <ZoneList mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} />
    </>
  )
}

/* Zones: sliders + test clip + discover + add */
function ZoneList({ mapping, setMsg, reloadAll }) {
  const [discovered, setDiscovered] = useState(null)
  const [newZone, setNewZone] = useState({ name: '', backend: 'sonos', ip: '', player_uid: '' })
  // The slider shows what the zone ACTUALLY
  // plays at — the server's effective_volumes (live > per-zone > the
  // zone's own backend global > one code default). The old `?? 70`
  // fallback here disagreed with the backends (35) and showed WiiM zones
  // Sonos's global.
  const effVols = mapping?.effective_volumes || {}

  const saveVolume = async (name, vol) => {
    try {
      const r = await api(`/api/dashboard/zones/${encodeURIComponent(name)}/volume`, {
        method: 'PUT', json: { volume: vol } })
      setMsg(`${name}: voice volume ${r.volume} — applies to the next clip, no restart.`, 'ok', 'Zones')
      reloadAll()
      return true
    } catch (e) { setMsg('Volume save failed: ' + e.message, 'err', 'Zones'); return false }
  }

  const discover = async () => {
    setDiscovered({ scanning: true })
    try {
      const r = await api('/api/dashboard/zones/discover', { method: 'POST', json: {} })
      setDiscovered(r)
      setMsg(`Discovered ${Array.isArray(r.found) ? r.found.length : 0} Sonos players.`, 'ok', 'Zones')
    } catch (e) { setDiscovered(null); setMsg('Discovery failed: ' + e.message, 'err', 'Zones') }
  }

  const addZone = async () => {
    if (!newZone.name.trim()) { setMsg('Zone name required.', 'err', 'Zones'); return }
    try {
      // player_uid is Sonos-only — WiiM zones send none at all.
      const body = { name: newZone.name, backend: newZone.backend, ip: newZone.ip }
      if (newZone.backend === 'sonos') body.player_uid = newZone.player_uid
      await api('/api/dashboard/zones', { method: 'POST', json: body })
      setMsg(`Zone ${newZone.name} added.`, 'ok', 'Zones')
      setNewZone({ name: '', backend: 'sonos', ip: '', player_uid: '' })
      reloadAll()
    } catch (e) { setMsg('Zone add failed: ' + e.message, 'err', 'Zones') }
  }

  const speakers = mapping?.speakers || {}
  const saveSpeakerVolume = async (room, vol) => {
    try {
      const r = await api('/api/dashboard/node-speaker/volume', { method: 'PUT', json: { room, volume: vol } })
      setMsg(`${room} built-in speaker: volume ${r.volume} — live, no restart.`, 'ok', 'Zones')
      reloadAll()
      return true
    } catch (e) { setMsg('Volume save failed: ' + e.message, 'err', 'Zones'); return false }
  }
  const testSpeaker = async room => {
    try {
      await api('/api/dashboard/node-speaker/test', { method: 'POST', json: { room } })
      setMsg(`Test clip playing on ${room}'s built-in speaker.`, 'ok', 'Zones')
    } catch (e) { setMsg('Test clip failed: ' + e.message, 'err', 'Zones') }
  }

  const testClip = async name => {
    try {
      await api(`/api/dashboard/zones/${encodeURIComponent(name)}/test`, { method: 'POST', json: {} })
      setMsg(`Test clip playing on ${name}.`, 'ok', 'Zones')
    } catch (e) { setMsg('Test clip failed: ' + e.message, 'err', 'Zones') }
  }

  // A mistyped IP or a replaced player: remove the zone and add it again.
  const removeZone = async name => {
    if (!window.confirm(`Remove zone ${name}? Rooms mapped to it lose their zone.`)) return
    try {
      const r = await api(`/api/dashboard/zones/${encodeURIComponent(name)}`, { method: 'DELETE' })
      const dropped = r?.dropped_room_mappings || []
      setMsg(`Removed ${name}.` + (dropped.length ? ` ${dropped.join(', ')} ${dropped.length === 1 ? 'is' : 'are'} now unmapped.` : ''),
        'ok', 'Zones')
      reloadAll()
    } catch (e) { setMsg('Remove failed: ' + e.message, 'err', 'Zones') }
  }

  return (
    <Section title="Zones"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] overflow-x-auto">
        <table className="w-full text-[14px]">
          <thead>
            <tr className="text-[12px] text-[var(--muted)]">
              <th className="text-left px-4 py-2">Name</th>
              <th className="text-left px-4 py-2">Backend</th>
              <th className="text-left px-4 py-2">Volume</th>
              <th className="text-left px-4 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(mapping?.zones || {}).map(([name, z]) => (
              <tr key={name} className="border-t border-[var(--separator)]">
                <td className="px-4 py-2">
                  <div>{esc(name)}</div>
                  {z?.ip && <div className="font-mono text-[12px] text-[var(--muted)]">{esc(z.ip)}</div>}
                </td>
                <td className="px-4 py-2">
                  <span className="inline-block rounded-full bg-[var(--default)] px-2.5 py-0.5 text-[12px] font-medium">
                    {esc(z?.backend) || '—'}
                  </span>
                </td>
                <td className="px-4 py-2">
                  <VolSlider name={name} initial={effVols[name]} onCommit={saveVolume} />
                </td>
                <td className="px-4 py-2 text-right">
                  <button className={BTN_DEFAULT} onClick={() => testClip(name)}>Play test clip</button>
                  <button className={`${BTN_DANGER} ml-2`} onClick={() => removeZone(name)}
                    aria-label={`Remove zone ${name}`}>Remove</button>
                </td>
              </tr>
            ))}
            {/* A node's built-in speaker: private to that node, appears by
                itself, no edit/delete. */}
            {Object.entries(speakers).map(([room, sp]) => {
              const inUse = sp?.zone === BUILTIN
              return (
              <tr key={`spk:${room}`} data-speaker-zone={room} className="border-t border-[var(--separator)]">
                <td className="px-4 py-2">
                  <div>
                    {esc(room)} <span className="text-[var(--muted)]">· built-in</span>
                    {!sp?.online && <span className="ml-2 inline-block rounded-full px-2 py-0.5 text-[11px] font-medium text-[var(--muted)] border border-[var(--separator)]">offline</span>}
                    {sp?.speaker_ok === false && <span className="ml-2 inline-block rounded-full px-2 py-0.5 text-[11px] font-medium text-[var(--danger)] border border-[var(--danger)]">speaker fault</span>}
                  </div>
                  {!inUse && (
                    <div className="text-[12px] text-[var(--muted)]">
                      {sp?.zone ? `Not in use — replies go to ${esc(sp.zone)}.` : 'Not in use — replies play nowhere until this room is mapped.'}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2">
                  <span className="inline-block rounded-full bg-[var(--default)] px-2.5 py-0.5 text-[12px] font-medium">esp32</span>
                </td>
                <td className="px-4 py-2">
                  <VolSlider name={room} initial={sp?.volume} onCommit={saveSpeakerVolume} />
                </td>
                <td className="px-4 py-2 text-right">
                  <button className={BTN_DEFAULT} disabled={!inUse || !sp?.online}
                    title={!inUse ? 'Map this node to its built-in speaker first' : (!sp?.online ? 'Node is offline' : undefined)}
                    onClick={() => testSpeaker(room)}>Play test clip</button>
                </td>
              </tr>
              )
            })}
            <TableState cols={4} error={mapping?.loadError} loading={!mapping?.zones}
              empty={!Object.keys(mapping?.zones || {}).length && !Object.keys(speakers).length && 'No speaker zones yet.'} />
          </tbody>
        </table>
      </div>

      {discovered && Array.isArray(discovered.found) && discovered.found.length > 0 && (
        <div className="rounded-2xl bg-[var(--surface-secondary)] p-4 grid gap-2">
          <span className="text-[12px] font-medium text-[var(--muted)]">Discovered on LAN</span>
          {discovered.found.map(d => (
            <div key={d.name || d.ip} className="flex items-center gap-4 flex-wrap text-[13px]">
              <span>{esc(d.name)}</span>
              <span className="font-mono text-[12px] text-[var(--muted)]">{esc(d.ip)}</span>
              <button className={BTN_DEFAULT}
                onClick={() => setNewZone(nz => ({ ...nz, name: d.name || '', backend: 'sonos', ip: d.ip || '', player_uid: d.uid || '' }))}>
                Fill into add form
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <div className="flex items-center gap-2 flex-wrap">
          <input className={`${INPUT_CLS} w-[170px]`} placeholder="zone name"
            value={newZone.name} onChange={e => setNewZone({ ...newZone, name: e.target.value })} />
          <select className={`${INPUT_CLS} w-[110px]`} value={newZone.backend}
            onChange={e => setNewZone({ ...newZone, backend: e.target.value })}>
            <option value="sonos">sonos</option>
            <option value="wiim">wiim</option>
          </select>
          <input className={`${INPUT_CLS} w-[185px] font-mono`} placeholder="ip"
            value={newZone.ip} onChange={e => setNewZone({ ...newZone, ip: e.target.value })} />
          {/* Sonos only: WiiM zones have no player UID, so the field is
              not shown at all (a greyed-out box read as "fill me in"). */}
          {newZone.backend === 'sonos' && (
            <input className={`${INPUT_CLS} flex-1 min-w-[230px] font-mono`} placeholder="RINCON_… player UID"
              title="Sonos RINCON player UID"
              value={newZone.player_uid} onChange={e => setNewZone({ ...newZone, player_uid: e.target.value })} />
          )}
        </div>
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <button className={BTN_DEFAULT} onClick={discover}>
            {discovered?.scanning ? 'Scanning LAN…' : 'Discover Sonos on LAN'}
          </button>
          <button className={BTN_PRIMARY} onClick={addZone}>Add zone</button>
        </div>
      </div>
    </Section>
  )
}

function VolSlider({ name, initial, onCommit }) {
  const [val, setVal] = useState(initial)
  useEffect(() => { setVal(initial) }, [initial])
  // A refused save leaves `initial` unchanged, so the effect above never
  // re-runs: put the slider back to the saved value ourselves.
  const commit = async () => { if (!(await onCommit(name, val))) setVal(initial) }
  // No server number yet (older bot / first paint): show — and lock the
  // slider rather than invent a default that may not be what plays.
  if (val == null) {
    return (
      <div className="flex items-center gap-2">
        <input type="range" min="0" max="100" value={0} disabled readOnly
          className="w-[140px] accent-[var(--accent)] opacity-40" aria-label={`${name} volume`} />
        <span className="font-mono text-[12px] w-[30px] text-right">—</span>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2">
      <input type="range" min="0" max="100" value={val}
        onChange={e => setVal(+e.target.value)}
        onMouseUp={commit}
        onTouchEnd={commit}
        onKeyUp={e => {
          // Only VALUE-MOVING keys commit — the old bare
          // onKeyUp fired on Tab too, so walking the form with the
          // keyboard PUTed an unchanged volume into config.
          if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
               'Home', 'End', 'PageUp', 'PageDown'].includes(e.key))
            commit()
        }}
        className="w-[140px] accent-[var(--accent)]"
        aria-label={`${name} volume`} />
      <span className="font-mono text-[12px] w-[30px] text-right">{val}</span>
    </div>
  )
}

/* ── Firmware push (operator side of firmware_server.py) ───── */
function FirmwarePanel({ status, setMsg, reloadAll }) {
  const [fwFile, setFwFile] = useState(null)
  const [pushing, setPushing] = useState(false)
  // NO default track — a preselected line made the
  // dangerous choice the lazy one. The server also refuses an image whose
  // own identity names the other line. Push stays disabled
  // until chosen.
  const [fwTrack, setFwTrack] = useState('')
  // After a push: which online nodes to nudge. NOTHING pre-checked — the
  // bot can't tell which line a node runs (fw_push sends version only),
  // so it never guesses; restarting the other line's node is harmless
  // (it reads its own track) but costs that room its voice for ~15s.
  const [pickup, setPickup] = useState(null)  // {version, track, rooms: {room: bool}}
  const trackLabel = t => (t || '').toUpperCase()

  const push = async () => {
    if (!fwFile) { setMsg('Pick a .bin to push.', 'err', 'Firmware'); return }
    if (!fwTrack) { setMsg('Pick the hardware line (track) this .bin is for.', 'err', 'Firmware'); return }
    // Filename hint: warn (never block) when the name says the other line.
    const fname = fwFile.name.toLowerCase()
    // (node_esp32.bin was BOTH XVF lines' build name before 1.2.9 — never a hint.)
    const hinted = fname.includes('waveshare') ? 'waveshare'
      : fname.includes('flex') ? 'flex'
      : /array|respeaker-xvf/.test(fname) ? 'array' : null
    const mismatch = hinted !== null && hinted !== fwTrack
    if (!window.confirm(
      `Push ${fwFile.name} to the ${trackLabel(fwTrack)} track?\n\n` +
      `Every ${trackLabel(fwTrack).toLowerCase()} node OTAs this image on its next check. ` +
      `Wrong line = dead node (USB rescue).` +
      (mismatch ? `\n\nThe filename looks like a ${trackLabel(hinted)} build, but you picked ${trackLabel(fwTrack)}.` : '')))
      return
    setPushing(true)
    setPickup(null)
    setMsg(`Uploading ${fwFile.name} to the ${trackLabel(fwTrack).toLowerCase()} track…`, 'pulse', 'Firmware')
    let upj = null
    try {
      const fd = new FormData()
      fd.append('file', fwFile, fwFile.name)
      fd.append('track', fwTrack)
      upj = await api('/api/dashboard/firmware/upload', { method: 'POST', body: fd })
      setMsg(`Firmware ${upj.version} served (${trackLabel(upj.track).toLowerCase()} track).`, 'ok', 'Firmware')
    } catch (e) {
      setMsg('Firmware push failed: ' + e.message, 'err', 'Firmware')
      return
    } finally { setPushing(false) }
    const online = (status?.rooms || []).filter(n => n.online)
    if (online.length) {
      setPickup({ version: upj.version, track: upj.track,
                  rooms: Object.fromEntries(online.map(n => [n.room, false])) })
    }
  }

  const restartPicked = async () => {
    const picked = Object.entries(pickup?.rooms || {}).filter(([, on]) => on).map(([r]) => r)
    if (!picked.length) { setMsg('No nodes checked — nothing restarted.', 'muted', 'Firmware'); return }
    setMsg(`Restarting ${picked.join(', ')}…`, 'pulse', 'Firmware')
    const failed = []
    for (const room of picked) {
      try {
        await api(`/api/dashboard/nodes/${encodeURIComponent(room)}/restart`, { method: 'POST' })
      } catch (e) { failed.push(`${room} (${e.message})`) }
    }
    const ok = picked.filter(r => !failed.some(f => f.startsWith(r + ' ')))
    if (failed.length) setMsg(`Restart failed: ${failed.join('; ')}` + (ok.length ? ` — sent to ${ok.join(', ')}.` : ''), 'err', 'Firmware')
    else setMsg(`Restart sent — ${ok.join(', ')} reboot${ok.length > 1 ? '' : 's'} and check for ${pickup.version} (~15s).`, 'ok', 'Firmware')
    setPickup(null)
    await reloadAll()
  }

  return (
    <Section title="Firmware"
      note="Push a firmware .bin — nodes pick it up over OTA on their next boot.">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[12px] font-medium text-[var(--muted)]">Track</span>
          <select className={`${INPUT_CLS} w-[150px]`} value={fwTrack}
            onChange={e => setFwTrack(e.target.value)}
            title="Which hardware line this .bin is for — the image and manifest go to that track's directory only. Cross-flashing the other line kills its node.">
            <option value="" disabled>pick a line…</option>
            <option value="array">array</option>
            <option value="flex">flex</option>
            <option value="waveshare">waveshare</option>
          </select>
          <FilePick accept=".bin" file={fwFile} onPick={setFwFile} />
          <button className={BTN_PRIMARY} style={{ marginLeft: 'auto' }} disabled={pushing || !fwFile || !fwTrack} onClick={push}
            title={!fwTrack ? 'Pick the track first.' : !fwFile ? 'Pick a .bin first.' : ''}>
            {pushing ? 'Uploading…' : 'Push firmware'}
          </button>
        </div>
        {pickup && (
          <div className="grid gap-2 border-t border-[var(--separator)] pt-3">
            <span className="text-[13px]">
              {pickup.version} is on the {trackLabel(pickup.track).toLowerCase()} track. Restart nodes now so they grab it
              (check only {trackLabel(pickup.track).toLowerCase()} nodes — others would just reboot for nothing):
            </span>
            <div className="flex gap-4 flex-wrap">
              {Object.entries(pickup.rooms).map(([room, on]) => (
                <label key={room} className="flex items-center gap-2 text-[13px] font-mono">
                  <input type="checkbox" checked={on}
                    onChange={e => setPickup(p => ({ ...p, rooms: { ...p.rooms, [room]: e.target.checked } }))} />
                  {esc(room)}
                </label>
              ))}
            </div>
            <div className="flex gap-2">
              <button className={BTN_PRIMARY} onClick={restartPicked}>Restart checked</button>
              <button className={BTN_DEFAULT} onClick={() => { setPickup(null); setMsg('Skipped — nodes grab it on their next check.', 'muted', 'Firmware') }}>Skip</button>
            </div>
          </div>
        )}
      </div>
    </Section>
  )
}
const NS_LINK = { boot: 'starting', wifi_connecting: 'joining wi-fi', wifi_up: 'no host yet', bot: 'online' }
const NS_STAGE = { boot: 'Restarting…', wifi_connecting: 'Joining Wi-Fi…', wifi_up: 'Wi-Fi up, reaching the host…', bot: 'Online.' }
const NS_CHIP = { checking: 'checking…', ok: 'sound ok', silent: 'no sound', unknown: 'unknown', wrong: 'wrong image' }
const XMOS_STEPS = {
  array: 'Unplug the board. Hold Mute and plug the port next to the headphone jack into this computer; let go when the red light blinks.',
  flex: 'Unplug the board. Hold Boot and plug the core board’s USB-C (next to RST) into this computer; let go after 3 seconds.',
}
const XMOS_DONE = 'Done. Move the cable back to the XIAO’s USB-C port and click Connect to check the sound.'
const XMOS_HELP = {
  windows: 'Windows needs a one-time driver: download Zadig (zadig.akeo.ie), choose Options → List All Devices, pick the reSpeaker safe-mode entry, select WinUSB and click Replace Driver.',
  linux: 'Linux needs a udev rule: add SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", MODE="0666" to /etc/udev/rules.d/99-respeaker.rules, run sudo udevadm control --reload, then replug.',
}

function NodeSetupPanel({ setMsg }) {
  const linkRef = useRef(null)
  const [phase, setPhase] = useState('idle')        // idle | connecting | ready | unknown | saving | flashing | watching
  const [info, setInfo] = useState(null)            // last info reply; null = board didn't answer
  const [form, setForm] = useState({ wifi_ssid: '', wifi_pass: '', name: '', bot_url: '' })
  const [pickTrack, setPickTrack] = useState('')    // unknown boards only, no default
  const [progress, setProgress] = useState(null)    // { part, pct } while flashing
  const [stage, setStage] = useState('')
  const [chip, setChip] = useState({ state: '', ver: '' })   // sound check: '' | checking | ok | silent | unknown
  const [dfu, setDfu] = useState(null)                       // audio chip step; null = closed
  const dfuRef = useRef(null)
  const recheck = useRef(false)                              // after an audio chip flash, the next Connect checks the sound
  const checkGen = useRef(0)                                 // only the latest sound check may report
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false; dfuRef.current?.close() }, [])
  const M = (text, kind) => setMsg(text, kind, 'Node setup')
  useEffect(() => () => { linkRef.current?.close() }, [])

  const fill = i => setForm({ wifi_ssid: i.wifi_ssid || '', wifi_pass: '', name: i.name || '',
                              bot_url: hostOfferUrl(window.location.origin) })
  const changed = () => {
    const f = {}
    if (!info || form.name !== info.name) f.name = form.name
    if (!info || form.bot_url !== info.bot_url || !info.bot_url_saved) f.bot_url = form.bot_url
    if (form.wifi_pass || (info && form.wifi_ssid !== info.wifi_ssid)) { f.wifi_ssid = form.wifi_ssid; f.wifi_pass = form.wifi_pass }
    return f
  }
  const checkSound = async (name, track, expect = false) => {
    if (!['array', 'flex'].includes(track)) return
    const gen = ++checkGen.current
    let denied = false   // a cancelled or refused token: asking again every read would nag
    setChip({ state: 'checking', ver: '' })
    const got = await watchSound({
      read: async () => {
        try { return await api(`/api/dashboard/nodes/${encodeURIComponent(name)}/info`, { promptForToken: true }) }
        catch (e) { if (e.status === 401) denied = true; throw e }
      },
      sleep: ms => new Promise(res => setTimeout(res, ms)),
      stop: () => denied || gen !== checkGen.current || !alive.current,
    })
    if (!alive.current || gen !== checkGen.current) return
    if (denied) { setChip({ state: 'unknown', ver: '' }); M('The sound check needs the dashboard token.', 'muted'); return }
    const r = expect ? expectVersion(got, track) : got
    setChip({ state: r.state, ver: r.ver })
    if (r.state === 'wrong') M(`The audio chip reads v${r.ver}, not v${r.expected} for this ${track} board. Flash it again with the ${track} image.`, 'err')
    else if (r.state === 'silent') M('The node is online but its audio chip sends no sound. Flash the audio chip to fix it.', 'err')
    else if (r.state === 'unknown') M('Couldn’t read the node’s audio. Check it on the Rooms tab.', 'muted')
    else M(`${name}’s audio chip sends sound${r.ver ? ` (v${r.ver})` : ''}.`, 'ok')
  }
  const connect = async () => {
    checkGen.current++; setChip({ state: '', ver: '' })   // a new board: the last one's sound check is not its own
    setPhase('connecting'); M('Pick the node’s USB port…', 'pulse')
    try {
      const link = await NodeLink.request(); linkRef.current = link
      M('Asking the node who it is…', 'pulse')
      try {
        const i = await link.info(); setInfo(i); fill(i); setPhase('ready')
        if (recheck.current) { recheck.current = false; void checkSound(i.name, i.track, true) }
        M(`Connected: ${i.name} (${i.track}, ${i.fw}).`, 'ok')
      } catch {
        setInfo(null); setForm(f => ({ ...f, bot_url: hostOfferUrl(window.location.origin) })); setPhase('unknown')
        M('No LenoxVI firmware answered: blank board, another firmware, or older than 1.2.47.', 'muted')
      }
    } catch (e) { setPhase('idle'); M('Connect failed: ' + e.message, 'err') }
  }
  const watch = async () => {
    setPhase('watching'); setStage('boot')
    try {
      await linkRef.current.watchLink({ onState: setStage })
      const i = await linkRef.current.info(); setInfo(i); fill(i); setPhase('ready')
      let host = i.bot_url
      try { host = new URL(i.bot_url).host } catch { host = i.bot_url }
      M(`${i.name} is online on ${host}.`, 'ok')
      void checkSound(i.name, i.track)
    } catch (e) { setPhase('ready'); M(e.message, 'err') }
  }
  const save = async () => {
    const fields = changed()
    if (!Object.keys(fields).length) { M('Nothing changed.', 'muted'); return }
    // A new network needs its password: an empty field would save it as an open network.
    if ('wifi_ssid' in fields && !fields.wifi_pass) { M('Enter the password for that Wi-Fi network.', 'err'); return }
    const bad = validateSettings(fields)
    if (bad) { M(bad, 'err'); return }
    setPhase('saving'); M('Saving to the node…', 'pulse')
    try {
      const saved = await linkRef.current.save(fields)
      setForm(f => ({ ...f, wifi_pass: '' }))
      // The node holds these now, whatever the restart and watch do next: the next Save compares against them.
      setInfo(i => i && { ...i,
        ...('name' in fields && { name: fields.name }),
        ...('wifi_ssid' in fields && { wifi_ssid: fields.wifi_ssid }),
        ...('bot_url' in fields && { bot_url: fields.bot_url, bot_url_saved: fields.bot_url !== '' }) })
      M(`Saved ${saved.join(', ')}. Restarting the node…`, 'pulse')
      await linkRef.current.restart()
      await watch()
    } catch (e) { setForm(f => ({ ...f, wifi_pass: '' })); setPhase(info ? 'ready' : 'unknown'); M('Save failed: ' + e.message, 'err') }
  }
  const flash = async () => {
    const track = info?.track || pickTrack
    if (!track) { M('Pick the board’s line first.', 'err'); return }
    if (!window.confirm(`Flash the ${track.toUpperCase()} firmware to this board?\n\n` +
        'Wi-Fi, name and host address are kept. Wrong line = dead node (USB rescue).')) return
    setPhase('flashing'); setProgress({ part: '', pct: 0 })
    try {
      await linkRef.current.flash(track, { onProgress: setProgress })
      M('Flashed. Waiting for the node…', 'pulse')
      try {
        const i = await linkRef.current.info({ timeoutMs: 30000 }); setInfo(i); fill(i); setPhase('ready')
        M(`Flashed ${i.fw}.` + (i.bot_url_saved ? '' : ' Save the host address to finish.'), 'ok')
      } catch { setPhase('unknown'); M('Flashed, but the node didn’t answer on USB.', 'err') }
    } catch (e) { setPhase(info ? 'ready' : 'unknown'); M('Flash failed: ' + e.message, 'err') }
    finally { setProgress(null) }
  }
  const disconnect = async () => {
    checkGen.current++; setChip({ state: '', ver: '' })
    await linkRef.current?.close(); linkRef.current = null
    setInfo(null); setPhase('idle'); M('Disconnected.', 'muted')
  }
  const openChip = () => setDfu({ line: ['array', 'flex'].includes(info?.track) ? info.track : '', phase: 'pick', pct: 0, file: null, fileObj: null, help: '' })
  const closeChip = async () => { await dfuRef.current?.close(); dfuRef.current = null; setDfu(null) }
  const chipConnect = async () => {
    setDfu(s => ({ ...s, phase: 'connecting', help: '' })); M('Pick the audio chip in the USB list…', 'pulse')
    try {
      const d = await XmosDfu.request()
      await d.open(); dfuRef.current = d
      setDfu(s => ({ ...s, phase: 'ready' })); M('Audio chip found in safe mode.', 'ok')
    } catch (e) {
      const help = e.code === 'access-denied' ? (XMOS_HELP[platformOf()] || '') : ''
      setDfu(s => ({ ...s, phase: 'pick', help })); M('Audio chip: ' + e.message, 'err')
    }
  }
  const chipFile = async f => {
    if (!f) { setDfu(s => ({ ...s, file: null, fileObj: null })); return }
    if (!dfu?.line) { M('Pick the board first.', 'err'); return }
    try {
      const bytes = await checkXmosImage(new Uint8Array(await f.arrayBuffer()), dfu.line)
      setDfu(s => ({ ...s, file: bytes, fileObj: f })); M('File checked: it matches the pinned image.', 'ok')
    } catch (err) { setDfu(s => ({ ...s, file: null, fileObj: null })); M(err.message, 'err') }
  }
  const chipFlash = async () => {
    const line = dfu.line, im = XMOS_IMAGES[line]
    try {
      setDfu(s => ({ ...s, phase: 'downloading' }))
      if (!dfu.file) M('Downloading the audio chip firmware from GitHub…', 'pulse')
      const bytes = dfu.file || await fetchXmosImage(line)
      if (!window.confirm(`Flash the ${line.toUpperCase()} audio chip firmware v${im.version}?\n\nThe chip keeps its recovery copy.`)) {
        setDfu(s => ({ ...s, phase: 'ready' })); return
      }
      setDfu(s => ({ ...s, phase: 'flashing', pct: 0 }))
      await dfuRef.current.flash(bytes, { onProgress: pct => setDfu(s => s && { ...s, pct }) })
      await dfuRef.current.close(); dfuRef.current = null
      // The cable moved to the audio chip's port, so the serial link is gone: back to Connect,
      // which checks the sound (and this line's version) once the cable is back.
      checkGen.current++; setChip({ state: '', ver: '' })
      const link = linkRef.current; linkRef.current = null; void link?.close().catch(() => {})
      setInfo(null); setPhase('idle')
      recheck.current = true
      setDfu(s => ({ ...s, phase: 'done' }))
      M('Done. Move the cable back to the XIAO port and click Connect to check the sound.', 'ok')
    } catch (e) {
      const before = ['fetch-failed', 'hash-mismatch', 'size-mismatch'].includes(e.code)   // nothing was written
      if (!before) { await dfuRef.current?.close(); dfuRef.current = null }
      setDfu(s => s && { ...s, phase: before ? 'ready' : 'pick' })
      M(before ? e.message : 'Audio chip flash failed: ' + e.message + ' Unplug, enter safe mode again and retry; the chip keeps its recovery copy.', 'err')
    }
  }
  const busy = ['connecting', 'saving', 'flashing', 'watching'].includes(phase)
  const chipBusy = !!dfu && ['connecting', 'downloading', 'flashing'].includes(dfu.phase)
  const set = k => e => setForm(f => ({ ...f, [k]: e.target.value }))

  return (
    <Section title="Node setup" note="Flash and set up a node over USB: new boards, a new host, a changed Wi-Fi.">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        {!isSupported() ? (
          <span className="text-[13px]">Node setup needs Chrome or Edge on a desktop computer.</span>
        ) : phase === 'idle' || phase === 'connecting' ? (
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px]">Plug the node into this computer. Flex and array boards: use the XIAO’s USB-C port, not the one by the headphone jack.</span>
            <button className={BTN_PRIMARY} style={{ marginLeft: 'auto' }} disabled={busy} onClick={connect}>
              {phase === 'connecting' ? 'Connecting…' : 'Connect'}
            </button>
          </div>
        ) : (
          <>
            {info && (
              <div className="flex items-center gap-2 flex-wrap">
                <span className={CHIP_CLS}>{esc(info.track)}</span>
                <span className={CHIP_CLS}>{esc(info.fw)}</span>
                <span className={CHIP_CLS}>{NS_LINK[info.link] || esc(info.link)}</span>
                <span className="text-[13px] font-mono">{esc(info.name)}</span>
                <span className="text-[12px] font-medium text-[var(--muted)]">{info.bot_url_saved ? 'host: saved' : 'host: built-in'}</span>
              </div>
            )}
            {phase === 'unknown' && (
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[12px] font-medium text-[var(--muted)]">Line</span>
                <select className={`${INPUT_CLS} w-[150px]`} value={pickTrack} onChange={e => setPickTrack(e.target.value)}>
                  <option value="" disabled>pick a line…</option>
                  <option value="array">array</option>
                  <option value="flex">flex</option>
                  <option value="waveshare">waveshare</option>
                </select>
              </div>
            )}
            {info && (
              <div className="grid gap-2">
                <Field label="Wi-Fi network"><input className={INPUT_CLS} value={form.wifi_ssid} onChange={set('wifi_ssid')} /></Field>
                <Field label="Wi-Fi password"><input className={INPUT_CLS} type="password" autoComplete="new-password" placeholder="unchanged" value={form.wifi_pass} onChange={set('wifi_pass')} /></Field>
                <Field label="Room name"><input className={INPUT_CLS} value={form.name} onChange={set('name')} /></Field>
                <Field label="Host address"><input className={INPUT_CLS} value={form.bot_url} onChange={set('bot_url')} /></Field>
              </div>
            )}
            {progress && <span className="text-[13px]">Flashing {esc(progress.part)}… {Math.round(progress.pct)}%</span>}
            {phase === 'watching' && <span className="text-[13px]">{NS_STAGE[stage] || 'Restarting…'}</span>}
            <div className="flex gap-2 flex-wrap">
              {info && <button className={BTN_PRIMARY} disabled={busy} onClick={save}>Save to node</button>}
              <button className={BTN_DEFAULT} disabled={busy || (!info && !pickTrack)} onClick={flash}>Flash firmware</button>
              <button className={BTN_DEFAULT} style={{ marginLeft: 'auto' }} disabled={busy} onClick={disconnect}>Disconnect</button>
            </div>
          </>
        )}
        {isSupported() && (!info || ['array', 'flex'].includes(info.track)) && (
          <div className="grid gap-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[12px] font-medium text-[var(--muted)]">Audio chip</span>
              {chip.state && <span className={CHIP_CLS}>{NS_CHIP[chip.state]}</span>}
              {chip.ver && <span className="text-[13px] font-mono">v{esc(chip.ver)}</span>}
              {!dfu && (
                <button className={['silent', 'wrong'].includes(chip.state) ? BTN_PRIMARY : BTN_DEFAULT} style={{ marginLeft: 'auto' }}
                        disabled={busy || !isUsbSupported()} onClick={openChip}>Flash audio chip…</button>
              )}
            </div>
            {dfu && (
              <div className="grid gap-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[12px] font-medium text-[var(--muted)]">Board</span>
                  <select className={`${INPUT_CLS} w-[150px]`} value={dfu.line} disabled={dfu.phase !== 'pick'}
                          onChange={e => setDfu(s => ({ ...s, line: e.target.value, file: null, fileObj: null }))}>
                    <option value="" disabled>pick a board…</option>
                    <option value="array">array</option>
                    <option value="flex">flex</option>
                  </select>
                </div>
                {dfu.line && <span className="text-[13px]">{dfu.phase === 'done' ? XMOS_DONE : XMOS_STEPS[dfu.line]}</span>}
                {(dfu.help || platformOf() === 'windows') && <span className="text-[13px]">{dfu.help || XMOS_HELP.windows}</span>}
                {dfu.phase === 'flashing' && <span className="text-[13px]">Flashing the audio chip… {dfu.pct}%</span>}
                {['pick', 'ready'].includes(dfu.phase) && (
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[12px] font-medium text-[var(--muted)]">Or a downloaded file</span>
                    <FilePick accept=".bin" file={dfu.fileObj} onPick={chipFile} />
                  </div>
                )}
                {['pick', 'ready'].includes(dfu.phase) && dfu.line && (
                  <span className="text-[13px] font-mono text-[var(--muted)]">
                    <a href={xmosImageUrl(dfu.line)} target="_blank" rel="noreferrer" className="hover:underline">{xmosImageName(dfu.line)}</a>
                    {' · sha256 '}{XMOS_IMAGES[dfu.line].sha256.slice(0, 12)}…
                  </span>
                )}
                <div className="flex gap-2 flex-wrap">
                  {(dfu.phase === 'pick' || dfu.phase === 'connecting') && (
                    <button className={BTN_PRIMARY} disabled={!dfu.line || busy || chipBusy} onClick={chipConnect}>
                      {dfu.phase === 'connecting' ? 'Connecting…' : 'Connect audio chip'}
                    </button>
                  )}
                  {['ready', 'downloading', 'flashing'].includes(dfu.phase) && (
                    <button className={BTN_PRIMARY} disabled={busy || chipBusy} onClick={chipFlash}>
                      {dfu.phase === 'ready' ? 'Flash audio chip' : dfu.phase === 'downloading' ? 'Downloading…' : 'Flashing…'}
                    </button>
                  )}
                  <button className={BTN_DEFAULT} style={{ marginLeft: 'auto' }} disabled={chipBusy} onClick={closeChip}>Close</button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </Section>
  )
}
// Wake name (top of the Wake word card): what the house is called. A bare
// "Hey <name>." (the wake word said again while the room listens) is dropped,
// mis-spellings included — matched by sound, so there's no list to keep.
// Applies live (no restart), unlike the model below it.
function WakeNameField({ setMsg }) {
  const [w, setW] = useState(null)
  const [loadErr, setLoadErr] = useState('')
  useEffect(() => {
    let alive = true
    api('/api/dashboard/wake')
      .then(j => { if (alive) { setW(j); setLoadErr('') } })
      .catch(e => { if (alive) setLoadErr(e.message) })
    return () => { alive = false }
  }, [])
  const save = async (body, note, say = setMsg) => {
    try {
      const r = await api('/api/dashboard/wake', { method: 'PUT', json: body })
      setW(prev => ({ ...prev, ...r }))
      say(note || 'Saved — live now.', 'ok', 'Wake name')
      return true
    } catch (e) {
      say('Save failed: ' + e.message, 'err', 'Wake name')
      return false
    }
  }
  const name = useDraft({ name: w?.name ?? '' })
  const saveName = async say => {
    const sent = name.snap(['name'])
    const ok = await save({ name: (name.val('name') ?? '').trim() }, null, say)
    if (ok) name.settle(sent)
    return ok
  }
  useUnsaved('wake-name-card', { label: 'wake name', count: name.changed('name') ? 1 : 0, save: saveName, discard: name.discard })
  // Not loaded yet: the field keeps its place (rendering nothing made the
  // card jump when it arrived) and its id, so #wake-name-card finds its tab.
  if (!w) return (
    <div id="wake-name-card" className={`text-[13px] ${loadErr ? 'text-[var(--danger)]' : 'text-[var(--muted)]'}`}>
      {loadErr ? `Couldn't load: ${loadErr}` : 'Loading…'}
    </div>
  )
  return (
    <div id="wake-name-card">
      <Field label="Wake name">
        <input id="wake-name" className={`${INPUT_CLS} w-[180px]`} maxLength={30} placeholder="not set"
          value={name.val('name') ?? ''} onChange={e => name.set({ name: e.target.value })} />
      </Field>
    </div>
  )
}

function OwwPanel({ setMsg, reloadAll }) {
  const [models, setModels] = useState([])
  const [loaded, setLoaded] = useState(false)  // [] before the first load is not "no models"
  const [loadErr, setLoadErr] = useState('')   // shown in the list, not as a toast
  const [scanning, setScanning] = useState(false)
  const [newModel, setNewModel] = useState(null)
  // The picked model is a draft for the bar (saved = the active one).
  const pick = useDraft({ model: models.find(m => m.selected)?.path || '' })
  const sel = pick.val('model')

  const load = async () => {
    try {
      const r = await api('/api/dashboard/oww/models')
      setModels(r.models || [])
      setLoaded(true)
      setLoadErr('')
    } catch (e) { setLoadErr(e.message) }
  }
  useEffect(() => { load() }, [])

  const select = async say => {
    // The dropdown value is the row's full path — resolve the
    // ROW (basename + source) so a same-basename root/models/
    // collision activates the exact copy the picker named; a name-only
    // select always resolved to the models/ copy.
    const row = models.find(m => m.path === sel)
    const name = row?.name || sel.split('/').pop()
    // Stock rows are pre-trained models bundled with openwakeword,
    // selected by KEY (no .onnx name).
    if (!name || (row?.source !== 'stock' && !name.endsWith('.onnx'))) { say('No model selected.', 'err', 'Wake word model'); return false }
    const body = { model: name, ...(row?.source ? { source: row.source } : {}) }
    const sent = pick.snap(['model'])
    try {
      await api('/api/dashboard/oww/select', { method: 'PUT', json: body })
      say('Saved — restart to apply.', 'ok', 'Wake word model')
      await load()
      reloadAll()
      pick.settle(sent)
      return true
    } catch (e) { say('Select failed: ' + e.message, 'err', 'Wake word model'); return false }
  }
  useUnsaved('wake-model', { label: 'wake model', count: pick.changed('model') ? 1 : 0, save: select, discard: pick.discard })

  const del = async (name, source) => {
    if (!window.confirm(`Delete model '${name}' (${source})?`)) return
    try {
      await api(`/api/dashboard/oww/models/${encodeURIComponent(name)}?source=${encodeURIComponent(source)}`, { method: 'DELETE' })
      setMsg(`Deleted '${name}' (${source}).`, 'ok', 'Wake word model')
      await load()
    } catch (e) { setMsg('Delete failed: ' + e.message, 'err', 'Wake word model') }
  }

  const upload = async () => {
    const f = newModel
    if (!f) { setMsg('No file selected.', 'err', 'Wake word model'); return }
    const fd = new FormData()
    fd.append('file', f, f.name)
    setScanning(true)
    try {
      const r = await api('/api/dashboard/oww/models', { method: 'POST', body: fd })
      setMsg(`Model '${r.name}' uploaded (${fmtBytes(r.size || 0)}). Select it to activate.`, 'ok', 'Wake word model')
      setNewModel(null)
      await load()
    } catch (e) { setMsg('Upload failed: ' + e.message, 'err', 'Wake word model') }
    finally { setScanning(false) }
  }

  return (
    <Section id="wake-model" title="Wake word"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <WakeNameField setMsg={setMsg} />
        <Field label="Active model / upload" between>
          <div className="flex items-center gap-2 flex-wrap">
            <select className={`${INPUT_CLS} w-[260px] max-w-full`} value={sel}
              onChange={e => pick.set({ model: e.target.value })}>
              {/* No listed model is active (e.g. the active one lives outside
                  the list): without this the browser SHOWS the first model
                  while the value is '', and Save refuses it. */}
              {models.length > 0 && !models.some(m => m.path === sel) &&
                <option value="" disabled>pick a model…</option>}
              {models.map(m => (
                <option key={m.path} value={m.path}>
                  {esc(m.name)}{m.source === 'root' ? ' (root)' : m.source === 'stock' ? ' (stock)' : ''}
                </option>
              ))}
              {models.length === 0 && <option value="">{loadErr ? "couldn't load" : loaded ? 'no models found' : 'loading…'}</option>}
            </select>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <FilePick accept=".onnx" file={newModel} onPick={setNewModel} />
            <button className={BTN_DEFAULT} disabled={scanning || !newModel} onClick={upload}>
              {scanning ? 'Uploading…' : 'Upload'}
            </button>
          </div>
        </Field>
        <div className="rounded-2xl bg-[var(--surface)] overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] text-[var(--muted)]">
                <th className="text-left px-4 py-2">Model</th>
                <th className="text-left px-4 py-2">Source</th>
                <th className="text-left px-4 py-2">Size</th>
                <th className="text-left px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {models.map(m => (
                <tr key={m.path} className="border-t border-[var(--separator)]">
                  <td className="px-4 py-2 font-mono text-[13px]">{esc(m.name)}</td>
                  <td className="px-4 py-2">
                    {esc(m.source)}
                    {m.source === 'stock' &&
                      <div className="text-[12px] text-[var(--muted)]">{esc(m.license || 'non-commercial')}</div>}
                  </td>
                  <td className="px-4 py-2 font-mono text-[13px]">{fmtBytes(m.size || 0)}</td>
                  <td className="px-4 py-2 text-right">
                    {m.selected
                      ? <span className="text-[12px] text-[var(--muted)]">in use</span>
                      : m.source !== 'stock' &&
                        <button className={BTN_DANGER} onClick={() => del(m.name, m.source)}>Delete</button>}
                  </td>
                </tr>
              ))}
              <TableState cols={4} error={loadErr} loading={!loaded} empty={models.length === 0 && 'No models yet — upload one.'} />
            </tbody>
          </table>
        </div>
      </div>
    </Section>
  )
}
// Pick-lists come from GET /tts `catalog` —
// the server's accepted values, one owner. The UI used to hardcode its
// own copies (12 of 40 Aura, 16 of 36 Flux): most voices were unpickable
// and a configured aura-2-zeus-en couldn't display.
const EL_MODEL_LABELS = { eleven_flash_v2_5: 'flash v2.5', eleven_v4_turbo: 'v4 turbo' }
const ttsLabel = v =>
  v.startsWith('aura-2-') ? 'aura-2 ' + v.slice(7).replace(/-en$/, '')
  : v.startsWith('flux-') ? 'flux ' + v.slice(5).replace(/-en$/, '')
  : (EL_MODEL_LABELS[v] || v)
// Kokoro voice ids: accent (a American, b British), f/m, name.
// 'af_heart' -> 'Heart — American female'.
const kokoroLabel = v => {
  const [, accent, sex, name] = /^([ab])([fm])_(\w+)$/.exec(v) || []
  return name
    ? `${name[0].toUpperCase()}${name.slice(1)} — ${accent === 'a' ? 'American' : 'British'} ${sex === 'f' ? 'female' : 'male'}`
    : v
}
// Each provider's SAVED value lives in its own field. The old sync loaded
// tts.model (the ElevenLabs model) for every provider, so a Deepgram
// config DISPLAYED the first voice while holding 'eleven_flash_v2_5' —
// the first Save 422'd.
const savedFor = (t, p) =>
  p === 'elevenlabs' ? t?.model : p === 'deepgram' ? t?.deepgram_voice
  : p === 'pocket' ? t?.pocket_voice : p === 'kokoro' ? t?.kokoro_voice
  : p === 'localai' ? t?.localai_model_tts : t?.deepgram_flux_voice

function Voice({ tts, tuning, status, setMsg, reloadAll }) {
  const catalog = tts?.catalog || {}
  const num = (v, parse) => (v != null ? parse(v) : null)
  // Drafts against the saved TTS config (useDraft): the 5 s poll never
  // clobbers a pick or a drag in progress, untouched fields follow it.
  // Each provider keeps its own model (its saved one, else its first).
  // Knobs stay null when unset, so Save leaves them alone (the route's
  // None = leave-as-is); blank is what their sliders show meanwhile.
  // ElevenLabs flash-v2.5: speed 0.7–1.2, stability 0.0–1.0, text
  // normalization auto | on | off (auto is OFF on Flash). Fallback = a
  // local hot standby (Pocket) behind a cloud provider; its voice is
  // tts.pocket_voice, the same key Pocket-as-primary uses.
  const t = useDraft({
    provider: tts?.provider || 'elevenlabs',
    ...Object.fromEntries(Object.keys(TTS_NAMES).map(p => [`model:${p}`, savedFor(tts, p) || catalog[p]?.[0] || ''])),
    voice_id: tts?.voice_id || '',
    el_speed: num(tts?.elevenlabs_speed, parseFloat),
    el_stab: num(tts?.elevenlabs_stability, parseFloat),
    el_norm: tts?.elevenlabs_text_normalization || null,
    flux_speed: num(tts?.deepgram_flux_speed, parseFloat),
    flux_expr: num(tts?.deepgram_flux_expressivity, v => parseInt(v, 10)),
    // Unset = the bot's default 1.0, so the slider at 1.0 is not a change.
    kokoro_speed: num(tts?.kokoro_speed, parseFloat) ?? 1.0,
    fallback: ['pocket', 'kokoro'].includes(tts?.fallback) ? tts.fallback : 'off',
    pocket_voice: tts?.pocket_voice || 'alba',
    la_voice: tts?.localai_voice || '',
  }, { blank: { el_speed: 1.0, el_stab: 0.5, el_norm: 'auto', flux_speed: 1.0, flux_expr: 1 } })
  const provider = t.val('provider')
  const model = t.val(`model:${provider}`)
  const voiceId = t.val('voice_id')
  const elSpeed = t.val('el_speed'), elStab = t.val('el_stab'), elNorm = t.val('el_norm')
  const fluxSpeed = t.val('flux_speed') ?? 1.0, fluxExpr = t.val('flux_expr') ?? 1
  const fallback = t.val('fallback'), pocketVoice = t.val('pocket_voice')
  const kokoroSpeed = t.val('kokoro_speed') ?? 1.0
  const laVoice = t.val('la_voice') || ''
  // LocalAI: its voice models come from the server; the voice list follows
  // the backend that runs the picked model (Kokoro / Pocket presets, else
  // free text).
  const laFound = useLocalAIModels(provider === 'localai')
  // LocalAI names backends its own way ('pocket-tts', 'kokoro'): match the family.
  const laRaw = String((laFound?.tts || []).find(m => m.id === model)?.backend || '').toLowerCase()
  const laBackend = laRaw.startsWith('kokoro') ? 'kokoro' : laRaw.startsWith('pocket') ? 'pocket' : laRaw
  const laVoices = laBackend === 'kokoro' ? (catalog.kokoro || []) : laBackend === 'pocket' ? (catalog.pocket || []) : null
  // What Save sends for this provider, so the count matches the save.
  const ttsFields = ['provider', `model:${provider}`,
    ...(provider === 'elevenlabs' ? ['voice_id', 'el_speed', 'el_stab', 'el_norm'] : []),
    ...(provider === 'deepgram-flux' ? ['flux_speed', 'flux_expr'] : []),
    ...(provider === 'kokoro' ? ['kokoro_speed'] : []),
    ...(provider === 'localai' ? ['la_voice'] : []),
    ...(provider !== 'pocket' && provider !== 'localai'
      ? ['fallback', ...(fallback === 'pocket' ? ['pocket_voice'] : [])] : [])]

  // Switching provider resets the model menu to that
  // provider's own list — and lands on THAT provider's saved
  // value when it has one, not blindly on the first entry.
  const options = provider === 'localai' ? (laFound?.tts || []).map(m => m.id) : (catalog[provider] || [])
  const onProviderChange = p => t.set({ provider: p, [`model:${p}`]: undefined })

  const saveTTS = async say => {
    const voice_id = (voiceId || '').trim()
    const body = { provider }
    if (provider === 'elevenlabs') {
      if (!voice_id) { say("Voice ID can't be empty — TTS breaks with no voice.", 'err', 'TTS'); return false }
      body.model = model
      body.voice_id = voice_id
      // Flash-v2.5 knobs ride on save (null = leave as-is/defaults).
      if (elSpeed != null) body.elevenlabs_speed = elSpeed
      if (elStab != null) body.elevenlabs_stability = elStab
      if (elNorm != null) body.elevenlabs_text_normalization = elNorm
    } else if (provider === 'deepgram') {
      body.deepgram_voice = model
    } else if (provider === 'deepgram-flux') {
      body.deepgram_flux_voice = model
      // Knobs ride ONLY when the server had one (an edit) or
      // the user moved the slider — an untouched placeholder on an
      // unset knob writes nothing (the route's None = leave-as-is).
      if (t.changed('flux_speed') || tts?.deepgram_flux_speed != null)
        body.deepgram_flux_speed = parseFloat(fluxSpeed)
      if (t.changed('flux_expr') || tts?.deepgram_flux_expressivity != null)
        body.deepgram_flux_expressivity = parseInt(fluxExpr, 10)
    } else if (provider === 'pocket') {
      body.pocket_voice = model
    } else if (provider === 'kokoro') {
      body.kokoro_voice = model
      // Same rule as the Flux knobs: an untouched unset speed writes nothing.
      if (t.changed('kokoro_speed') || tts?.kokoro_speed != null) body.kokoro_speed = kokoroSpeed
    } else if (provider === 'localai') {
      // An empty pick would leave every reply on the Kokoro fallback.
      if (!model) { say('Pick a LocalAI voice model first.', 'err', 'TTS'); return false }
      if (laVoices && !laVoice.trim()) { say('Pick a voice first.', 'err', 'TTS'); return false }
    }
    // The fallback rides with a cloud or Kokoro primary; the server drops
    // it when Pocket itself is the provider. LocalAI has its own (Kokoro).
    if (provider !== 'pocket' && provider !== 'localai') body.fallback = fallback
    if (provider !== 'pocket' && fallback === 'pocket') body.pocket_voice = pocketVoice
    const sent = t.snap(ttsFields)
    try {
      // LocalAI's model and voice live with the LocalAI settings, saved after
      // /tts accepts the provider (a refused save changes nothing).
      await api('/api/dashboard/tts', { method: 'PUT', json: body })
      if (provider === 'localai')
        await api('/api/dashboard/localai', { method: 'PUT', json: { model_tts: model || '', voice: laVoice.trim() } })
      say('Saved — restart to apply.', 'ok', 'TTS')
      await reloadAll()
      t.settle(sent)
      return true
    } catch (e) { say('Save failed: ' + e.message, 'err', 'TTS'); return false }
  }
  useUnsaved('voice', { label: 'tts', count: ttsFields.filter(t.changed).length, save: saveTTS, discard: t.discard })

  return (
    <>
      <Section id="voice" title="TTS"
        note="">
        <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
          <Field label="Provider / model">
            <select className={`${INPUT_CLS} w-[200px]`} value={provider || 'elevenlabs'}
              onChange={e => onProviderChange(e.target.value)}>
              <option value="elevenlabs">ElevenLabs (cloud)</option>
              <option value="deepgram">Deepgram Aura (cloud)</option>
              <option value="deepgram-flux">Deepgram Flux (cloud)</option>
              <option value="pocket" disabled={!tts?.pocket_installed && provider !== 'pocket'}>
                Pocket TTS (local){tts?.pocket_installed ? '' : ' — not installed'}
              </option>
              <option value="kokoro" disabled={!tts?.kokoro_installed && provider !== 'kokoro'}
                title={tts?.kokoro_problem || undefined}>
                Kokoro (local){tts?.kokoro_installed ? '' : tts?.kokoro_files_missing ? ' — model files missing' : ' — not installed'}
              </option>
              <option value="localai" disabled={!!tts?.localai_problem && provider !== 'localai'}
                title={tts?.localai_problem || undefined}>
                LocalAI (host){tts?.localai_problem ? ' — set it up on System' : ''}
              </option>
            </select>
            <select className={`${INPUT_CLS} w-[200px]`} value={model || ''}
              onChange={e => t.set({ [`model:${provider}`]: e.target.value })}>
              {/* A saved value the catalog doesn't carry (hand-edited config)
                  shows honestly instead of faking the first entry. */}
              {model && !options.includes(model) && (provider !== 'localai' || laFound) &&
                <option value={model} disabled>
                  {provider === 'localai' ? `${model} — not served by LocalAI` : `${ttsLabel(model)} (not accepted)`}</option>}
              {!options.length && <option value="" disabled>
                {provider === 'localai' && laFound ? 'no voice models on LocalAI' : 'loading…'}</option>}
              {provider === 'localai' && !model && options.length > 0 && <option value="" disabled>pick a model</option>}
              {options.map(v => <option key={v} value={v}>{provider === 'kokoro' ? kokoroLabel(v) : ttsLabel(v)}</option>)}
            </select>
            {provider === 'localai' && <span className="text-[12px] font-medium text-[var(--muted)]">Voice</span>}
            {provider === 'localai' && (laVoices
              ? <select id="tts-localai-voice" className={`${INPUT_CLS} w-[200px]`} value={laVoice}
                  onChange={e => t.set({ la_voice: e.target.value })} aria-label="LocalAI voice">
                  {!laVoices.includes(laVoice) && <option value={laVoice} disabled>{laVoice || 'pick a voice'}</option>}
                  {laVoices.map(v => <option key={v} value={v}>{laBackend === 'kokoro' ? kokoroLabel(v) : v}</option>)}
                </select>
              : <input id="tts-localai-voice" value={laVoice} onChange={e => t.set({ la_voice: e.target.value })}
                  placeholder="Voice name" className={`${INPUT_CLS} w-[200px] font-mono`} aria-label="LocalAI voice" />)}
            {provider === 'elevenlabs' && <span className="text-[12px] font-medium text-[var(--muted)]">Voice</span>}
            {provider === 'elevenlabs' &&
              <input id="tts-voice" value={voiceId} onChange={e => t.set({ voice_id: e.target.value })} placeholder="Voice id (fq1S…)"
                className={`${INPUT_CLS} w-[260px] font-mono`} />}
            {provider === 'deepgram-flux' && (
              <>
                <span className="text-[12px] font-medium text-[var(--muted)]">Flux speed</span>
                <input id="tts-flux-speed" type="range" min="0.5" max="1.5" step="0.05"
                  value={fluxSpeed} onChange={e => t.set({ flux_speed: parseFloat(e.target.value) })}
                  className="w-[140px] accent-[var(--accent)]" aria-label="Flux speed" />
                <span className="font-mono text-[12px] w-[36px] text-right">{fluxSpeed.toFixed(2)}</span>
                <span className="text-[12px] font-medium text-[var(--muted)]">expressivity</span>
                <input id="tts-flux-expressivity" type="range" min="-2" max="2" step="1"
                  value={fluxExpr} onChange={e => t.set({ flux_expr: parseInt(e.target.value, 10) })}
                  className="w-[140px] accent-[var(--accent)]" aria-label="Flux expressivity" />
                <span className="font-mono text-[12px] w-[20px] text-right">{fluxExpr}</span>
              </>
            )}
            {provider === 'kokoro' && (
              <>
                <span className="text-[12px] font-medium text-[var(--muted)]">Speed</span>
                <input id="tts-kokoro-speed" type="range" min="0.5" max="1.5" step="0.05"
                  value={kokoroSpeed} onChange={e => t.set({ kokoro_speed: parseFloat(e.target.value) })}
                  className="w-[140px] accent-[var(--accent)]" aria-label="Kokoro speed" />
                <span className="font-mono text-[12px] w-[36px] text-right">{kokoroSpeed.toFixed(2)}</span>
              </>
            )}
          </Field>
          {provider === 'elevenlabs' && model === 'eleven_flash_v2_5' && (
            <Field label="Speed / stability / normalization (flash v2.5)">
              <span className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-[var(--muted)]">speed</span>
                <input id="tts-el-speed" type="range" min="0.7" max="1.2" step="0.05"
                  value={elSpeed ?? 1.0} onChange={e => t.set({ el_speed: parseFloat(e.target.value) })}
                  className="w-[140px] accent-[var(--accent)]" aria-label="ElevenLabs speed" />
                <span className="font-mono text-[12px] w-[36px] text-right">{(elSpeed ?? 1.0).toFixed(2)}</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-[var(--muted)]">stability</span>
                <input id="tts-el-stability" type="range" min="0" max="1" step="0.05"
                  value={elStab ?? 0.5} onChange={e => t.set({ el_stab: parseFloat(e.target.value) })}
                  className="w-[140px] accent-[var(--accent)]" aria-label="ElevenLabs stability" />
                <span className="font-mono text-[12px] w-[36px] text-right">{(elStab ?? 0.5).toFixed(2)}</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-[var(--muted)]">normalization</span>
                <select id="tts-el-norm" className={`${INPUT_CLS} w-[110px]`} value={elNorm ?? 'auto'}
                  onChange={e => t.set({ el_norm: e.target.value })} aria-label="ElevenLabs text normalization">
                  <option value="auto">Auto</option>
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </select>
              </span>
            </Field>
          )}
          {provider === 'localai' && (
            <span className="text-[12px] text-[var(--muted)]">If LocalAI is down: Kokoro on this appliance.</span>
          )}
          {provider === 'localai' && laFound && !laFound.ok && (
            <span className="text-[13px] text-[var(--danger)]">LocalAI: {laFound.reason}</span>
          )}
          {provider !== 'pocket' && provider !== 'localai' && (
            <Field label="Fallback">
              <select id="tts-fallback" className={`${INPUT_CLS} w-[200px]`} value={fallback}
                onChange={e => t.set({ fallback: e.target.value })}
                title="Speak with a local voice while this provider is down.">
                <option value="off">Off</option>
                {/* Each stays pickable while it's the saved one, so a fallback
                    saved before it went missing can always be turned off. */}
                <option value="kokoro" disabled={(!tts?.kokoro_installed || provider === 'kokoro') && fallback !== 'kokoro'}>
                  Kokoro{tts?.kokoro_installed ? '' : ' — not installed'}
                </option>
                <option value="pocket" disabled={!tts?.pocket_installed && fallback !== 'pocket'}>
                  Pocket TTS{tts?.pocket_installed ? '' : ' — not installed'}
                </option>
              </select>
              {fallback === 'pocket' && (
                <select id="tts-fallback-voice" className={`${INPUT_CLS} w-[200px]`} value={pocketVoice}
                  onChange={e => t.set({ pocket_voice: e.target.value })}
                  disabled={!tts?.pocket_installed}
                  aria-label="Fallback voice" title="The Pocket TTS voice used while the cloud provider is down.">
                  {(catalog.pocket || []).includes(pocketVoice) ? null :
                    <option value={pocketVoice} disabled>{pocketVoice} (not accepted)</option>}
                  {(catalog.pocket || []).map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              )}
            </Field>
          )}
          {TTS_KEY_HINT[provider] && tts && !tts[TTS_KEY_HINT[provider]] && (
            <MissingKey label={provider === 'elevenlabs' ? 'ElevenLabs' : provider === 'localai' ? 'LocalAI' : 'Deepgram'} />
          )}
        </div>
      </Section>

      <SttCard status={status} setMsg={setMsg} />
      <Tuning tuning={tuning} reloadAll={reloadAll} />
      <OwwPanel setMsg={setMsg} reloadAll={reloadAll} />
    </>
  )
}

/* ── STT card ───────────────────────────────────────────────────
   Chips = what's RUNNING (load state + per-command time, from the 5s
   /status poll). Selects = what's SAVED. Four benchmarked models; the
   server writes device/compute with the model and downloads a missing
   model before switching config (restart only offered once it's on disk). */
const CHIP_CLS = 'inline-block rounded-full bg-[var(--default)] px-2.5 py-0.5 text-[12px] font-medium'
// Soft status tones (brand Chip rule), same tokens as the status bar.
const WARN_CHIP_CLS = 'inline-block rounded-full bg-[var(--warning-soft)] text-[var(--warning-soft-foreground)] px-2.5 py-0.5 text-[12px] font-medium'
const OK_CHIP_CLS = 'inline-block rounded-full bg-[var(--success-soft)] text-[var(--success-soft-foreground)] px-2.5 py-0.5 text-[12px] font-medium'
const ERR_CHIP_CLS = 'inline-block rounded-full bg-[var(--danger-soft)] text-[var(--danger-soft-foreground)] px-2.5 py-0.5 text-[12px] font-medium'

const fmtBytes = b => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.round(b / 1e3)} KB`

function sttStateChip(st, dl) {
  if (dl?.state === 'downloading')
    return { text: `downloading${dl.percent != null ? ` ${dl.percent}%` : ''}`, cls: `${CHIP_CLS} text-[var(--muted)]`, title: `Downloading ${dl.model}` }
  if (!st) return { text: '…', cls: `${CHIP_CLS} text-[var(--muted)]` }
  if (st.state === 'ready') return { text: 'running', cls: OK_CHIP_CLS }
  if (st.state === 'error') return { text: 'load failed', cls: ERR_CHIP_CLS, title: st.error }
  if (st.state === 'loading') return { text: 'loading', cls: `${CHIP_CLS} text-[var(--muted)]` }
  return { text: 'not loaded', cls: `${CHIP_CLS} text-[var(--muted)]` }
}

function SttCard({ status, setMsg }) {
  const [data, setData] = useState(null)
  // setMsg is a new function on every parent render (the 5 s poll): keep it
  // in a ref so the effects below don't re-run on every poll.
  const msgRef = useRef(setMsg)
  useEffect(() => { msgRef.current = setMsg })

  const apply = d => { setData(d); return d }
  const load = () => api('/api/dashboard/stt').then(apply)
    .catch(e => { msgRef.current('STT load failed: ' + e.message, 'err', 'STT'); return null })
  // A key saved on the Keys card (or a restart): refresh the data.
  useEffect(() => {
    const h = () => api('/api/dashboard/stt').then(d => setData(d)).catch(() => {})
    window.addEventListener('lenoxvi:reload-all', h)
    return () => window.removeEventListener('lenoxvi:reload-all', h)
  }, [])
  // Fetch on mount AND whenever the loaded model or its load state changes
  // (a restart onto another model, a load finishing or failing): the model
  // list's "in use" flags depend on what's loaded. Keyed on the stt part of
  // the 5 s /status poll only — unchanged polls don't refetch.
  const runKey = `${status?.stt?.model}|${status?.stt?.state}`
  useEffect(() => {
    let alive = true
    api('/api/dashboard/stt')
      .then(d => { if (alive) setData(d) })
      .catch(e => { if (alive) msgRef.current('STT load failed: ' + e.message, 'err', 'STT') })
    return () => { alive = false }
  }, [runKey])

  // While a download runs — started by Save, or already in progress when the
  // page loaded — poll every 0.5 s. When it lands, the bar fills to 100%
  // "Downloaded ✓" and holds a beat before "restart to apply", so even a
  // 4-second download visibly finishes. A failed poll just tries again; the
  // cleanup stops polling when the state flips.
  const downloading = data?.download?.state === 'downloading'
  const pendingModel = downloading ? data.download.model : null   // already on its way
  // Drafts against the saved selects (useDraft): a refetch never clobbers
  // a pick in progress. A model already downloading shows as the saved one.
  // LocalAI's saved model (it lives with the LocalAI settings, not stt:).
  const [laSaved, setLaSaved] = useState(null)
  useEffect(() => {
    const get = () => api('/api/dashboard/localai').then(setLaSaved).catch(() => {})
    get()
    window.addEventListener('lenoxvi:reload-all', get)
    return () => window.removeEventListener('lenoxvi:reload-all', get)
  }, [])
  const s = useDraft({ model: pendingModel || data?.current?.model, language: data?.current?.language,
    provider: data?.current?.provider, la_model: laSaved?.model_stt || '' })
  const model = s.val('model'), language = s.val('language'), provider = s.val('provider')
  const laModel = s.val('la_model') || ''
  const onLocalAIPick = provider === 'localai'
  const laFound = useLocalAIModels(onLocalAIPick)
  const laModels = laFound?.stt || []
  // What Save sends: the picks that differ from the saved config.
  const sttBody = () => {
    const body = {}
    if (!data) return body
    if (model !== data.current.model && model !== pendingModel) body.model = model
    if (language !== data.current.language) body.language = language
    if (provider && provider !== data.current.provider) body.provider = provider
    // LocalAI's model is saved with the LocalAI settings (its own route;
    // save() takes it out of the /stt body).
    if (onLocalAIPick && laSaved != null && laModel !== (laSaved.model_stt || '')) body.la_model = laModel
    return body
  }
  const [finished, setFinished] = useState(null)   // { model, bytes } during the 100% beat
  useEffect(() => {
    if (!downloading) return
    let alive = true
    const t = setInterval(() => {
      api('/api/dashboard/stt').then(d => {
        if (!alive) return
        const dl = d.download || {}
        if (dl.state === 'ready') {
          const m = (d.models || []).find(x => x.name === dl.model)
          setFinished({ model: dl.model, bytes: m?.disk_bytes || 0 })
          setTimeout(() => {
            setFinished(null)
            msgRef.current(`Downloaded ${dl.model} — restart to apply.`, 'ok', 'STT')
            window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))  // the switch is saved now
          }, 1000)
        } else if (dl.state === 'error') {
          msgRef.current(`Download failed (${dl.error}) — config unchanged.`, 'err', 'STT')
        }
        setData(d)
      }).catch(() => {})
    }, 500)
    return () => { alive = false; clearInterval(t) }
  }, [downloading])

  const [deleting, setDeleting] = useState(null)
  const del = async m => {
    const size = m.disk_bytes ? ` (${fmtBytes(m.disk_bytes)})` : ''
    if (!window.confirm(`Delete ${m.name}${size}? It downloads again if you pick it later.`)) return
    setDeleting(m.name)
    try {
      const r = await api(`/api/dashboard/stt/model/${encodeURIComponent(m.name)}`, { method: 'DELETE' })
      await load()
      setMsg(`Deleted ${m.name} — freed ${fmtBytes(r.freed_bytes || 0)}.`, 'ok', 'STT')
    } catch (e) { setMsg('Delete failed: ' + e.message, 'err', 'STT') }
    finally { setDeleting(null) }
  }

  const save = async say => {
    if (!data) return false
    const body = sttBody()
    if (!Object.keys(body).length) { say('Nothing changed.', 'ok', 'STT'); return true }
    // One download at a time: a new model waits for the one on its way.
    if (downloading && body.model) { say(`${pendingModel} is still downloading — save once it's done.`, 'err', 'STT'); return false }
    const sent = s.snap(['model', 'language', 'provider'])
    const laSent = s.snap(['la_model'])
    const laPick = body.la_model
    delete body.la_model
    try {
      if (laPick !== undefined) await api('/api/dashboard/localai', { method: 'PUT', json: { model_stt: laPick } })
      const r = Object.keys(body).length
        ? await api('/api/dashboard/stt', { method: 'PUT', json: body })
        : { restart_needed: true }
      await load()
      if (laPick !== undefined) setLaSaved(await api('/api/dashboard/localai'))
      s.settle(sent)
      s.settle(laSent)
      if (r.download === 'started') {
        say(`Downloading ${model} — restart to apply once it's on disk.`, 'ok', 'STT')
      } else if (r.restart_needed) {
        say(r.key_needed ? `No ${STT_CLOUD[r.current?.provider]?.name || 'cloud'} key yet — add it on System · Keys, then restart.`
          : 'Saved — restart to apply.', 'ok', 'STT')
        window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))
      } else {
        say('Saved — applies to the next thing you say.', 'ok', 'STT')
      }
      return true
    } catch (e) { say('Save failed: ' + e.message, 'err', 'STT'); return false }
  }
  useUnsaved('stt', { label: 'stt', count: Object.keys(sttBody()).length, save, discard: s.discard })

  const st = status?.stt || data?.status
  const state = sttStateChip(st, data?.download)
  const cloud = STT_CLOUD[st?.provider]
  const onCloud = !!cloud && st.active === st.provider
  const onFallback = !!cloud && st.active === 'whisper'
  // LocalAI: same chips, its own names (Whisper base is its fallback).
  const onLocalAI = st?.provider === 'localai' && st.active === 'localai'
  const onLocalAIFallback = st?.provider === 'localai' && st.active === 'whisper'
  const cloudPick = STT_CLOUD[provider]
  const ms = st?.latency_ms_median
  const models = data?.models || []
  const custom = data?.current?.custom_model
  const customLang = data?.current?.custom_language
  return (
    <Section id="stt" title="STT" note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <div className="flex flex-wrap items-center gap-2" id="stt-chips">
          {onFallback || onLocalAIFallback
            ? <span className={WARN_CHIP_CLS} title={st.fallback_reason}>whisper fallback</span>
            : <span className={state.cls} title={state.title}>{state.text}</span>}
          {onLocalAI ? (
            <>
              <span className={CHIP_CLS}>localai</span>
              {st.localai_model && <span className={CHIP_CLS}>{st.localai_model}</span>}
            </>
          ) : onCloud ? (
            <>
              <span className={CHIP_CLS}>{cloud.chip(st)}</span>
              <span className={CHIP_CLS}>cloud</span>
            </>
          ) : (
            <>
              {st?.model && <span className={CHIP_CLS}>{st.model}</span>}
              {st?.device && <span className={CHIP_CLS}>
                {st.device === 'cuda' ? `gpu · ${data?.gpu || 'cuda'}` : 'cpu'}</span>}
              {st?.compute && <span className={CHIP_CLS}>{st.compute}</span>}
            </>
          )}
          {ms != null && <span className={CHIP_CLS} title="Median speech-to-text time of the last 10 commands since restart">
            {`${(ms / 1000).toFixed(2)} s / command`}</span>}
        </div>
        {(downloading || finished) && (
          <div className="grid gap-2">
            <div id="stt-progress" className="w-full rounded-full bg-[var(--default)] overflow-hidden" style={{ height: 6 }}>
              <div className="rounded-full bg-[var(--accent)]"
                style={{ height: 6, width: finished ? '100%' : `${Math.max(2, data.download.percent || 0)}%` }} />
            </div>
            <span className="text-[12px] text-[var(--muted)]">
              {finished
                ? `Downloaded ✓${finished.bytes ? ` · ${fmtBytes(finished.bytes)}` : ''}`
                : data.download.total_bytes
                  ? `${fmtBytes(data.download.done_bytes || 0)} / ${fmtBytes(data.download.total_bytes)}`
                  : 'Starting download…'}
            </span>
          </div>
        )}
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Provider">
            <select id="stt-provider" className={`${INPUT_CLS} w-auto`} value={provider || 'whisper'}
              onChange={e => s.set({ provider: e.target.value })} disabled={!data}>
              {(data?.providers || [{ name: 'whisper', label: 'Whisper (local)', available: true }]).map(p => (
                <option key={p.name} value={p.name} disabled={!p.available} title={p.reason || ''}>
                  {p.label}{p.available ? '' : p.name === 'localai' ? ' — set it up on System' : ' — not installed'}
                </option>
              ))}
            </select>
          </Field>
          {onLocalAIPick && (
            <Field label="Model">
              <select id="stt-localai-model" className={`${INPUT_CLS} w-auto`} value={laModel}
                onChange={e => s.set({ la_model: e.target.value })}>
                {!laModels.includes(laModel) && <option value={laModel} disabled>
                  {!laFound ? 'loading…' : laModel ? `${laModel} — not served by LocalAI` : 'pick a model'}</option>}
                {laModels.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </Field>
          )}
          {!onLocalAIPick && <Field label={cloudPick ? 'Fallback model' : 'Model'}>
            <select id="stt-model" className={`${INPUT_CLS} w-auto`} style={{ maxWidth: '100%' }} value={model || ''}
              onChange={e => s.set({ model: e.target.value })} disabled={!data}>
              {!data && <option value="" disabled>loading…</option>}
              {custom && <option value={data.current.model} disabled>{data.current.model} (set in config)</option>}
              {models.map(m => (
                <option key={m.name} value={m.name} disabled={!m.available}>
                  {m.label} ({m.size_gb} GB){!m.available ? ' — needs a GPU' : !m.cached ? ' — downloads first' : ''}
                </option>
              ))}
            </select>
          </Field>}
          <Field label="Language">
            <select id="stt-language" className={`${INPUT_CLS} w-[200px]`} value={language || ''}
              onChange={e => s.set({ language: e.target.value })} disabled={!data}>
              {!data && <option value="" disabled>loading…</option>}
              {customLang && <option value={data.current.language} disabled>{data.current.language} (set in config)</option>}
              {(data?.languages || []).map(l => <option key={l.code} value={l.code}>{l.label}</option>)}
            </select>
          </Field>
        </div>
        {cloudPick && data && !data[cloudPick.key] && <MissingKey label={cloudPick.name} />}
        {onLocalAIPick && (
          <span className="text-[12px] text-[var(--muted)]">If LocalAI is down: Whisper base on this appliance.</span>
        )}
        {onLocalAIPick && laFound && !laFound.ok && (
          <span className="text-[13px] text-[var(--danger)]">LocalAI: {laFound.reason}</span>
        )}
        <table id="stt-models" className="w-full text-[14px]">
          <thead>
            <tr className="text-[12px] text-[var(--muted)]">
              <th className="text-left px-4 py-2">Model</th>
              <th className="text-left px-4 py-2">On disk</th>
              <th className="text-left px-4 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {models.map(m => (
              <tr key={m.name} data-model={m.name} className="border-t border-[var(--separator)]">
                <td className="px-4 py-2 font-mono text-[13px]">{m.name}</td>
                <td className="px-4 py-2 font-mono text-[13px]">
                  {m.cached ? fmtBytes(m.disk_bytes || 0) : m.disk_bytes ? `partial · ${fmtBytes(m.disk_bytes)}` : '—'}</td>
                <td className="px-4 py-2 text-right">
                  {m.deletable
                    ? <button className={BTN_DANGER} disabled={deleting !== null} onClick={() => del(m)}>Delete</button>
                    : m.cached && <span className="text-[12px] text-[var(--muted)]">in use</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}

const TUNING_FIELDS = [
  ['wake_threshold', 'Wake threshold', 0.30, 0.60, 0.01,
    'Wake-word score needed to wake. Real wakes usually score well above TV/ad audio — tune by ear.'],
  ['wake_awake_timeout', 'Awake timeout (s)', 5, 60, 1,
    'Seconds of silence before she stops listening.'],
  ['vad_confidence', 'VAD confidence', 0.30, 0.90, 0.05,
    'How sure Silero must be you are speaking.'],
  ['vad_min_volume', 'VAD min volume', 0.10, 0.90, 0.05,
    'Quiet-speech floor — the ignores-the-TV knob.'],
  ['lull_secs', 'Turn-end lull (s)', 0.2, 2.0, 0.1,
    'Silence before your sentence flushes to the intelligence.'],
  ['dedup_window', 'Echo dedup window (s)', 2, 30, 1,
    'How long she suppresses re-hearing the same words off her own speakers.'],
]

function Tuning({ tuning, reloadAll }) {
  // Save sends only the knobs that differ from the saved value. Sending all
  // six from a sync-once draft rewrote the other five with whatever this tab
  // loaded, clobbering edits made elsewhere (another tab, another device).
  // Untouched sliders keep following the server on every poll (useDraft),
  // and a slider moved back to its saved value is no change.
  const vals = useDraft(Object.fromEntries(TUNING_FIELDS.map(([f]) => [f, tuning?.[f]])))
  const dirtyFields = TUNING_FIELDS.map(([f]) => f).filter(vals.changed)
  const save = async say => {
    if (!dirtyFields.length) { say('Nothing changed — no knobs touched.', 'muted', 'Voice tuning'); return true }
    const body = {}
    for (const f of dirtyFields) body[f] = parseFloat(vals.val(f))
    const sent = vals.snap(dirtyFields)
    try {
      await api('/api/dashboard/voice-tuning', { method: 'PUT', json: body })
      say('Saved — restart to apply.', 'ok', 'Voice tuning')
      await reloadAll()  // pull the saved truth back
      vals.settle(sent)
      return true
    } catch (e) { say('Tuning save failed: ' + e.message, 'err', 'Voice tuning'); return false }
  }
  useUnsaved('tuning', { label: 'voice tuning', count: dirtyFields.length, save, discard: vals.discard })

  return (
    <Section id="tuning" title="Voice tuning"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <div className="grid sm:grid-cols-2 gap-x-8 gap-y-4">
          {TUNING_FIELDS.map(([f, label, min, max, step, hint]) => (
            <div key={f} className="grid gap-1">
              <div className="flex items-center justify-between gap-4">
                <span className="text-[13px] font-medium">{label}{vals.changed(f) ? ' •' : ''}</span>
                <span className="font-mono text-[13px]">{vals.val(f) ?? '—'}</span>
              </div>
              <input type="range" min={min} max={max} step={step} value={vals.val(f) ?? min}
                onChange={e => vals.set({ [f]: e.target.value })}
                className="accent-[var(--accent)]" aria-label={label} />
              <span className="text-[12px] text-[var(--muted)]">{hint}</span>
            </div>
          ))}
        </div>
      </div>
    </Section>
  )
}

/* ── System: Intelligence ─────────────────────────────────── */
// LocalAI on the LAN: its address (the key lives on the Keys card), a Test
// that lists the models it serves, and nothing else — models are added on
// LocalAI itself, never from here.
function LocalAICard({ setMsg }) {
  const [saved, setSaved] = useState(null)
  const [url, setUrl] = useState('')
  const [test, setTest] = useState(null)
  const [busy, setBusy] = useState(false)
  const msgRef = useRef(setMsg)
  useEffect(() => { msgRef.current = setMsg })
  const load = () => api('/api/dashboard/localai')
    .then(d => { setSaved(d); setUrl(shortUrl(d.url) || ''); return d })
    .catch(e => { msgRef.current('LocalAI load failed: ' + e.message, 'err', 'LocalAI'); return null })
  useEffect(() => {
    api('/api/dashboard/localai')
      .then(d => { setSaved(d); setUrl(shortUrl(d.url) || '') })
      .catch(e => msgRef.current('LocalAI load failed: ' + e.message, 'err', 'LocalAI'))
    const h = () => api('/api/dashboard/localai').then(setSaved).catch(() => {})
    window.addEventListener('lenoxvi:reload-all', h)
    return () => window.removeEventListener('lenoxvi:reload-all', h)
  }, [])
  const changed = !!saved && (url.trim() || '') !== (shortUrl(saved.url) || '')
  // Saved from the unsaved-changes bar, like every settings card.
  const save = async say => {
    try {
      await api('/api/dashboard/localai', { method: 'PUT', json: { url: url.trim() } })
      await load()
      setTest(null)
      say('Saved — restart to apply.', 'ok', 'LocalAI')
      window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))
      return true
    } catch (e) { say('Save failed: ' + e.message, 'err', 'LocalAI'); return false }
  }
  useUnsaved('localai', { label: 'localai', count: changed ? 1 : 0, save,
    discard: () => setUrl(shortUrl(saved?.url) || '') })
  const runTest = async () => {
    setBusy(true)
    try { setTest(await api('/api/dashboard/localai/test', { method: 'POST' })) }
    catch (e) { setTest({ ok: false, reason: e.message, stt: [], tts: [] }) }
    finally { setBusy(false) }
  }
  return (
    <Section id="localai" title="LocalAI" note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <Field label="Address">
          <input id="localai-url" value={url} onChange={e => setUrl(e.target.value)} placeholder="192.0.2.50:8080"
            className={`${INPUT_CLS} w-[260px] font-mono`} disabled={!saved} />
          <button className={BTN_DEFAULT} disabled={busy || !saved?.url || changed} onClick={runTest}
            title={changed ? 'Save the address first' : undefined}>{busy ? 'Testing…' : 'Test'}</button>
        </Field>
        {saved && !saved.key_set && <MissingKey label="LocalAI" />}
        {test && (test.ok ? (
          <div id="localai-test" className="grid gap-1 text-[13px]">
            <span><span className={OK_CHIP_CLS}>ok</span> <span className="font-mono">{test.ms} ms</span></span>
            <span>Speech-to-text: {test.stt.length ? test.stt.join(', ') : 'none'}</span>
            <span>Voices: {test.tts.length ? test.tts.map(m => m.id).join(', ') : 'none'}</span>
          </div>
        ) : (
          <div id="localai-test" className="text-[13px] text-[var(--danger)]">{test.reason || 'LocalAI did not answer'}</div>
        ))}
        <span className="text-[12px] text-[var(--muted)]">
          Pick LocalAI on the STT or TTS card. If it's down, this appliance listens with Whisper base and speaks with Kokoro.
        </span>
      </div>
    </Section>
  )
}

function System({ intelligence, status, setMsg, reloadAll }) {
  // Agents: per-kind selection (like URLs) + the provider's discovered
  // list. Discovery runs server-side (keys never reach the browser) on
  // first load, on provider switch, on URL change, and on the refresh
  // button. Each entry carries key status + the masked key hint.
  const [found, setFound] = useState({})  // kind -> {agents, note, selected, loading}
  // Drafts against the saved config (useDraft). Per-kind URL and agent
  // (2026-10-08): the lanes live at different addresses, so each kind keeps
  // its own drafts (url:<kind>, agent:<kind>) and switching kinds only
  // changes which ones show. An agent left unpicked shows the saved one,
  // else the first listed (a lane that runs without an agent keeps '': its
  // own model), so picking that one back is no change.
  const savedKind = intelligence?.kind || LANES[0].kind
  const savedUrls = { ...(intelligence?.urls || {}), ...(intelligence?.url ? { [savedKind]: intelligence.url } : {}) }
  const firstAgent = k => (laneOf(k)?.agent_required === false ? '' : found[k]?.selected || found[k]?.agents?.[0]?.id || '')
  const kinds = LANES.map(l => l.kind)
  const d = useDraft({
    kind: savedKind,
    ...Object.fromEntries(kinds.map(k => [`url:${k}`, shortUrl(savedUrls[k])])),
    ...Object.fromEntries(kinds.map(k => [`agent:${k}`, intelligence?.agents?.[k] || null])),
  }, { blank: Object.fromEntries(kinds.map(k => [`agent:${k}`, firstAgent(k)])) })
  const intKind = d.val('kind')
  const intUrl = d.val(`url:${intKind}`)
  const intAgent = d.val(`agent:${intKind}`) || ''
  const setIntAgent = a => d.set({ [`agent:${intKind}`]: a })
  const intFields = ['kind', `url:${intKind}`, `agent:${intKind}`]
  const lastDiscoverUrl = useRef({})
  const discSeq = useRef({})  // kind -> newest request; only its reply counts
  const discover = async (kind, url) => {
    lastDiscoverUrl.current[kind] = url || ''
    const seq = (discSeq.current[kind] || 0) + 1
    discSeq.current[kind] = seq
    setFound(f => ({ ...f, [kind]: { ...(f[kind] || {}), loading: true } }))
    const body = { kind }
    // Any typed URL, host:port included: the server fills in http:// and /v1
    // (Lane.normalize_url), and only attaches the saved key for the saved host.
    if ((url || '').trim()) body.url = url.trim()
    try {
      const r = await api('/api/dashboard/intelligence/agents', { method: 'POST', json: body })
      if (seq !== discSeq.current[kind]) return
      setFound(f => ({ ...f, [kind]: { ...r, loading: false } }))
    } catch (e) {
      if (seq !== discSeq.current[kind]) return
      setFound(f => ({ ...f, [kind]: { agents: [], note: 'Agent list failed: ' + e.message, loading: false } }))
    }
  }
  // A key saved on the Keys card: re-run discovery so the agent list's
  // key status (and the "No key" link) follow it.
  const rediscover = useRef(null)
  useEffect(() => { rediscover.current = () => discover(intKind, document.getElementById('int-url')?.value.trim()) })
  useEffect(() => {
    const h = () => rediscover.current?.()
    window.addEventListener('lenoxvi:reload-all', h)
    return () => window.removeEventListener('lenoxvi:reload-all', h)
  }, [])
  // First load: list the saved kind's agents.
  const intSynced = useRef(false)
  useEffect(() => {
    if (intelligence && !intSynced.current) {
      intSynced.current = true
      const kind = intelligence.kind || LANES[0].kind
      discover(kind, intelligence.url || intelligence.urls?.[kind])
    }
  }, [intelligence])
  const switchKind = kind => {
    d.set({ kind })
    discover(kind, (d.val(`url:${kind}`) || '').trim())
  }
  const agentList = found[intKind]?.agents || []
  // Nothing picked for this kind yet: the saved one, else the first listed.
  // A lane that runs without an agent never falls back: '' is a real
  // choice (its own model), and a fallback would pin whatever model it
  // lists today on the next Save.
  const agentSel = laneOf(intKind)?.agent_required === false ? intAgent
    : intAgent || found[intKind]?.selected || agentList[0]?.id || ''
  const agentEntry = agentList.find(a => a.id === agentSel)
  const agentNote = found[intKind]?.note
  const save = async say => {
    const kind = intKind
    const url = (intUrl || '').trim()
    const label = laneLabel(kind)
    // Only the URL on screen is saved, under the kind on screen.
    // The other kind's draft stays here: the stored key goes to a
    // kind's saved URL, so it must be saved while looking at it.
    if (!url) { say(`Enter the ${label} URL first.`, 'err', 'Intelligence'); return false }
    const body = { kind, url }
    // A lane that runs without an agent always sends its pick: '' clears
    // a saved model.
    if (agentSel || laneOf(kind)?.agent_required === false) body.agent = agentSel
    const sent = d.snap(intFields), sentUrl = d.snap([`url:${kind}`])
    try {
      const r = await api('/api/dashboard/intelligence', { method: 'PUT', json: body })
      if (r?.url_only) {
        // New OpenClaw setup: the URL is saved, so discovery can
        // now use the stored key. Nothing live changed yet (the kind
        // stays a draft).
        say(`${label} URL saved — pick an agent, then Save again to switch to ${label}.`, 'ok', 'Intelligence')
        await reloadAll()
        d.settle(sentUrl)
        discover(kind, url)
        return true
      }
      say('Saved — restart to apply.', 'ok', 'Intelligence')
      await reloadAll()
      d.settle(sent)
      // A lane that runs without an agent can save with none picked; its
      // URL is saved now, so discovery can use the stored key and list its
      // agents.
      if (laneOf(kind)?.agent_required === false) discover(kind, url)
      return true
    } catch (e) { say('Save failed: ' + e.message, 'err', 'Intelligence'); return false }
  }
  useUnsaved('intelligence', { label: 'intelligence', count: intFields.filter(d.changed).length, save, discard: d.discard })
  return (
    <>
    <KeysCard setMsg={setMsg} />
    <LocalAICard setMsg={setMsg} />
    <Section id="intelligence" title="Intelligence"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <Field label="Provider / agent / API URL">
          <select id="int-kind" value={intKind} onChange={e => switchKind(e.target.value)}
            className={`${INPUT_CLS} w-[160px]`}>
            {LANES.map(l => <option key={l.kind} value={l.kind}>{l.label}</option>)}
          </select>
          <select id="int-agent" value={agentSel} onChange={e => setIntAgent(e.target.value)}
            title={laneOf(intKind)?.agent_help}
            className={`${INPUT_CLS} w-[220px] font-mono`}>
            {laneOf(intKind)?.agent_required === false
              ? <option value="">{laneOf(intKind).no_agent_label}</option>
              : !agentSel && <option value="">{found[intKind]?.loading ? 'loading…' : 'no agents'}</option>}
            {agentList.map(a => (
              <option key={a.id} value={a.id}>
                {a.id}{laneOf(intKind)?.per_agent_keys && !a.key_set ? ' — no key' : ''}
              </option>
            ))}
            {agentSel && !agentList.some(a => a.id === agentSel) && (
              <option value={agentSel}>{agentSel} (not found)</option>
            )}
          </select>
          <button type="button" className={BTN_DEFAULT} title="Refresh the agent list"
            aria-label="Refresh the agent list"
            onClick={() => discover(intKind, document.getElementById('int-url')?.value.trim())}>
            {found[intKind]?.loading ? '…' : '⟳'}
          </button>
          <span className="text-[12px] font-medium text-[var(--muted)]">URL</span>
          <input id="int-url" value={intUrl || ''} onChange={e => d.set({ [`url:${intKind}`]: e.target.value })}
            placeholder={laneOf(intKind)?.url_placeholder}
            title="Host:port is enough: http:// and /v1 are filled in. Type a full URL for https or another path."
            onBlur={e => {
              const u = e.target.value.trim()
              if (u !== (lastDiscoverUrl.current[intKind] || '')) discover(intKind, u)
            }}
            className={`${INPUT_CLS} flex-1 min-w-[280px] font-mono`} />
        </Field>
        {agentNote ? <div className="text-[12px] text-[var(--muted)] -mt-2">{agentNote}</div> : null}
        {intelligence && !(agentEntry ? agentEntry.key_set : intelligence.keys_set?.[intKind]) && (
          <MissingKey label={!laneOf(intKind)?.per_agent_keys ? laneLabel(intKind)
            : `${laneLabel(intKind)}${agentSel && agentSel !== laneOf(intKind).default_agent ? ` · ${agentSel}` : ''}`} />
        )}
        {/* Footer: the test reads the saved config; edits save from the bar at the page bottom */}
        <div className="flex items-center justify-between gap-4 flex-wrap">
            <button id="btn-int-test" className={BTN_DEFAULT}
              onClick={async ev => {
                const btn = ev.currentTarget
                btn.disabled = true; btn.textContent = 'Testing…'
                try {
                  const r = await api('/api/dashboard/intelligence/test', { method: 'POST', json: {} })
                  if (r.ok) {
                    setMsg(`✓ ${r.model || '?'} — ${r.latency_ms ?? '?'} ms — "${(r.reply || '').slice(0, 40)}"`, 'ok', 'Intelligence')
                  } else {
                    setMsg('Test failed: ' + (r.error || 'unknown'), 'err', 'Intelligence')
                  }
                } catch (e) {
                  setMsg('Test failed: ' + e.message, 'err', 'Intelligence')
                } finally {
                  btn.disabled = false; btn.textContent = 'Test saved config'
                }
              }} title="Tests the SAVED config on disk (config.yaml) — Save first if you edited kind, URL, or agent above.">Test saved config</button>
        </div>
      </div>
    </Section>

    <FirmwarePanel status={status} setMsg={setMsg} reloadAll={reloadAll} />
    <NodeSetupPanel setMsg={setMsg} />
    </>
  )
}

export default function App() {
  const [status, setStatus] = useState(null)
  const [mapping, setMapping] = useState(null)
  const [tts, setTts] = useState(null)
  const [tuning, setTuning] = useState(null)
  const [intelligence, setIntelligence] = useState(null)
  const [msg, setMsg] = useMsg()
  const [rebooting, setRebooting] = useState(false)
  const [, setLanesEpoch] = useState(0)
  // Lane list: fetched once; a failure (older bot) keeps BUILTIN_LANES.
  useEffect(() => {
    api('/api/dashboard/intelligence/lanes')
      .then(d => { if (Array.isArray(d?.lanes) && d.lanes.length) { LANES = d.lanes; setLanesEpoch(n => n + 1) } })
      .catch(() => {})
  }, [])
  const tab = useHashTab()

  // Unsaved changes (useUnsaved): each card's save and discard, current
  // every render, and its label and count for the bar.
  const cards = useRef({})
  const [units, setUnits] = useState({})
  const putUnsaved = useCallback((key, card) => {
    if (card) cards.current[key] = card
    else delete cards.current[key]
    const label = card?.label || '', count = card?.count || 0
    setUnits(u => (u[key]?.label === label && u[key]?.count === count ? u : { ...u, [key]: { label, count } }))
  }, [])
  const unsaved = UNSAVED_ORDER.reduce((n, k) => n + (units[k]?.count || 0), 0)
  useEffect(() => {
    if (!unsaved) return
    const warn = e => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [unsaved])

  // Poll hygiene. One refresh cycle at a time —
  // a call that lands mid-cycle doesn't stack a second wave of 5 GETs
  // (slow /mapping used to pile them up); it marks the cycle dirty and
  // gets the CURRENT cycle's promise, which loops once more so a save's
  // `await reloadAll()` still resolves on post-save data. Hidden tabs skip
  // the interval; becoming visible refreshes immediately.
  const reloadInFlight = useRef(null)
  const reloadAgain = useRef(false)
  const reloadAll = () => {
    if (reloadInFlight.current) { reloadAgain.current = true; return reloadInFlight.current }
    reloadInFlight.current = (async () => {
      try {
        do { reloadAgain.current = false; await reloadOnce() } while (reloadAgain.current)
      } finally { reloadInFlight.current = null }
    })()
    return reloadInFlight.current
  }
  const reloadOnce = async () => {
    const jobs = [
      api('/api/dashboard/status').then(setStatus).catch(e => {
        if (!e.network) setMsg('status unavailable: ' + e.message, 'err', 'Status')
      }),
      // A failed /mapping rides on the mapping state (the last good data
      // stays) so the Rooms and Zones tables can show it; it used to be
      // swallowed and the empty list read "No mic nodes yet". The next good
      // load replaces it. Network errors (bot restarting) stay quiet, as for
      // /status.
      api('/api/dashboard/mapping').then(setMapping).catch(e => {
        if (!e.network) setMapping(m => ({ ...m, loadError: e.message }))
      }),
      api('/api/dashboard/tts').then(setTts).catch(() => {}),
      api('/api/dashboard/voice-tuning').then(setTuning).catch(() => {}),
      api('/api/dashboard/intelligence').then(setIntelligence).catch(() => {}),
    ]
    await Promise.allSettled(jobs)
  }

  useEffect(() => {
    reloadAll()
    const t = setInterval(() => { if (!document.hidden) reloadAll() }, 5000)
    const h = () => reloadAll()
    const vis = () => { if (!document.hidden) reloadAll() }
    window.addEventListener('lenoxvi:reload-all', h)
    document.addEventListener('visibilitychange', vis)
    return () => {
      clearInterval(t)
      window.removeEventListener('lenoxvi:reload-all', h)
      document.removeEventListener('visibilitychange', vis)
    }
  }, [])

  return (
    <UnsavedCtx.Provider value={putUnsaved}>
    <div className="bg-[var(--background)] text-[var(--foreground)] min-h-screen font-sans">
      {/* Room under the last card for the unsaved bar (it wraps on a phone). */}
      <div className="max-w-[1120px] mx-auto px-6 pt-5 pb-12 grid grid-cols-1 gap-10"
        style={unsaved ? { paddingBottom: 'calc(10rem + env(safe-area-inset-bottom))' } : undefined}>
        <header className="sticky top-0 z-10 -mx-6 px-6 pt-2 pb-3 bg-[var(--background)]">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <Wordmark />
            <div className="flex items-center gap-4 flex-wrap">
              <nav className="flex gap-2" aria-label="Tabs">
                {TABS.map(t => (
                  <a key={t.id} href={`#${t.id}`} aria-current={tab === t.id ? 'page' : undefined}
                    className={`rounded-lg px-2.5 py-1.5 text-[13px] no-underline
                      ${tab === t.id
                        ? 'text-[var(--accent)] font-semibold'
                        : 'text-[var(--muted)] hover:text-[var(--foreground)]'}`}>
                    {t.label}
                  </a>
                ))}
              </nav>
              <RestartButton status={status} setMsg={setMsg} setRebooting={setRebooting} rebooting={rebooting} unsaved={unsaved > 0} />
            </div>
          </div>
          <AppMsg msg={msg} />
        </header>

        <Overview status={status} tts={tts} intelligence={intelligence} mapping={mapping} />
        {/* Every tab stays mounted, only the active one is shown: a card's
            unsaved edits and its own loading survive a tab switch. */}
        <div data-tab="rooms" hidden={tab !== 'rooms'} className="grid grid-cols-1 gap-10">
          <Rooms status={status} mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} />
        </div>
        <div data-tab="voice" hidden={tab !== 'voice'} className="grid grid-cols-1 gap-10">
          <Voice tts={tts} tuning={tuning} status={status} setMsg={setMsg} reloadAll={reloadAll} />
        </div>
        <div data-tab="system" hidden={tab !== 'system'} className="grid grid-cols-1 gap-10">
          <System intelligence={intelligence} status={status} setMsg={setMsg} reloadAll={reloadAll} />
        </div>
      </div>
      <UnsavedBar cards={cards} units={units} setMsg={setMsg} />
      <TokenDialog />
    </div>
    </UnsavedCtx.Provider>
  )
}