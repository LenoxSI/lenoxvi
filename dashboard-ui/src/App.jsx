// lenoxvi dashboard — app. Wireframe-C structure:
// single scrolling page, sticky header + section rail, no tabs.
// Brand: dark theme default, one lime element leads, sentence case,
// no emoji. Live data via the same /api/dashboard/* routes the old
// page.html used — routes are the contract, every shape verified
// against the live rig.

import { useEffect, useRef, useState } from 'react'

/* ── API layer ─────────────────────────────────────────────── */
const TOKEN_KEY = 'lenoxvi-dashboard-token'
// LVI_DASHBOARD_TOKEN on the bot makes every
// mutation 401 without X-Dashboard-Token (guard.py). The React port sent
// a stored token but never ASKED for one — setting the token bricked
// every save. On a mutation 401: prompt (once, shared across concurrent
// callers), remember in localStorage, retry the request once. A token
// that 401s again is dropped so the next save asks fresh. Reads stay
// open server-side, so GETs never prompt.
let tokenPrompt = null
function askToken(wasStored) {
  if (!tokenPrompt) {
    tokenPrompt = Promise.resolve().then(() => {
      const t = window.prompt(wasStored
        ? 'Saved dashboard token was rejected. Enter the token (LVI_DASHBOARD_TOKEN on the bot):'
        : 'This dashboard requires a token (LVI_DASHBOARD_TOKEN on the bot):')
      return (t || '').trim() || null
    }).finally(() => { setTimeout(() => { tokenPrompt = null }, 0) })
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
  if (r.status === 401 && method !== 'GET' && !_retried) {
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

/* ── restart lane ──────────────────────────────────────────── */
// restartBot has three call sites (Overview's button + offerRestart
// from the OWW and Voice save lanes) and the React `rebooting` state can
// race the second caller — two concurrent restarts meant two transient
// restart units and two 90s wait loops fighting over one message chip. A
// module-level latch is the only airtight double-fire guard.
let _botRestarting = false
async function restartBot(setMsg, setRebooting) {
  if (_botRestarting) return false
  _botRestarting = true
  setRebooting(true)
  setMsg('Restarting…', 'pulse', 'Bot control')
  let postErr = null
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await api('/api/dashboard/restart', { method: 'POST' })
      postErr = null
      break
    } catch (e) {
      postErr = e.message
      await new Promise(r => setTimeout(r, 1500))
    }
  }
  let up = 0
  for (let i = 0; i < 45; i++) {
    await new Promise(r => setTimeout(r, 2000))
    try {
      const s = await api('/api/dashboard/status')
      if (s.uptime_secs != null && s.uptime_secs < 30) { up = 1; break }
    } catch { /* still down */ }
    setMsg(`Restarting… ${Math.min(90, (i + 1) * 2)}s`, 'pulse', 'Bot control')
  }
  if (up) {
    setMsg('Bot is back — reloading panels…', 'ok', 'Bot control')
    window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))
  } else {
    setMsg((postErr ? `restart request failed (${postErr}) — ` : '') +
      'uptime never reset within 90s; the old bot process is still running.', 'err', 'Bot control')
  }
  setRebooting(false)
  _botRestarting = false
  return !!up
}

function offerRestart(setMsg, setRebooting) {
  if (!window.confirm('Saved. Restart the bot now to apply?')) return Promise.resolve(null)
  return restartBot(setMsg, setRebooting)
}

/* ── shared bits ───────────────────────────────────────────── */
const sec = s => s == null ? '—' :
  Math.floor(s / 86400) + 'd ' + new Date(s * 1000).toISOString().slice(11, 16) + 'h'

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
    <section id={id} className="grid gap-3 scroll-mt-[110px]">
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
function KeyInput({ id, hint, placeholder }) {
  const [dirty, setDirty] = useState(false)
  const [val, setVal] = useState('')
  return (
    <input id={id} data-dirty={dirty ? '1' : '0'} type={dirty ? 'password' : 'text'}
      value={dirty ? val : (hint || '')} placeholder={placeholder} autoComplete="off" spellCheck={false}
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

/* ── rail ──────────────────────────────────────────────────── */
const SECTIONS = [
  { id: 'overview', label: 'Overview' },
  { id: 'rooms', label: 'Rooms' },
  { id: 'voice', label: 'Voice' },
  { id: 'system', label: 'System' },
]

function useActiveSection() {
  const [active, setActive] = useState('overview')
  useEffect(() => {
    const obs = new IntersectionObserver(
      entries => {
        for (const en of entries) if (en.isIntersecting) setActive(en.target.id)
      },
      { rootMargin: '-20% 0px -70% 0px' })
    SECTIONS.forEach(s => {
      const el = document.getElementById(s.id)
      if (el) obs.observe(el)
    })
    return () => obs.disconnect()
  }, [])
  return active
}

/* ── Overview ──────────────────────────────────────────────── */
function Overview({ status, setMsg, setRebooting, rebooting }) {
  return (
    <Section id="overview" title="Overview"
      note=""
      action={
        <button className={BTN_DANGER} disabled={rebooting}
          onClick={() => {
            if (window.confirm('Restart the bot? Config changes apply on restart.')) {
              restartBot(setMsg, setRebooting)
            }
          }}>
          Restart bot
        </button>
      }>
      <div className="flex flex-wrap gap-8 rounded-2xl bg-[var(--surface)] p-4">
        <Stat label="Uptime">{sec(status?.uptime_secs)}</Stat>
        <Stat label="Nodes online">
          {status ? `${status.rooms.filter(r => r.online).length}/${status.rooms.length}` : '—'}
        </Stat>
        <Stat label="Last wake">
          {status?.last_wake ? `${esc(status.last_wake.room)}, ${fmtAgo(status.last_wake.time)}` : 'none since boot'}
        </Stat>
        <Stat label="Version"><span className="font-mono text-[13px]">{esc(status?.version) || '…'}</span></Stat>
        <Stat label="Intelligence">
          {(() => {
            // Brand chips (2026-10-08): OpenClaw = black/red, Hermes = white/blue.
            // Inline styles — these are foreign brands, they don't earn design tokens.
            // Chip shows the brand only — the agent id lives in the Intelligence
            // section: strip " · <agent>" from the label.
            const brand = (esc(status?.intelligence) || '—').split(' · ')[0]
            // Only a real brand wears brand colors — the loading
            // placeholder ('—') used to render in Hermes white/blue.
            const s = brand === 'OpenClaw'
              ? { backgroundColor: 'rgba(23, 23, 23, 0.9)', color: '#ef4444' }
              : brand === 'Hermes'
              ? { backgroundColor: 'rgba(255, 255, 255, 0.92)', color: '#2563eb' }
              : null
            return <span
              className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium${s ? '' : ' text-[var(--muted)]'}`}
              style={s}>{brand}</span>
          })()}
        </Stat>
      </div>
    </Section>
  )
}

function Stat({ label, children }) {
  return (
    <div className="grid gap-0.5">
      <span className="text-[12px] leading-4 font-medium text-[var(--muted)]">{label}</span>
      <span className="text-[14px] leading-5 font-medium">{children}</span>
    </div>
  )
}

/* ── node row actions ─────────────────────────────────────── */
function NodeActions({ room, online, lastSeen, reportedMuted, setMsg, reloadAll }) {
  const [busy, setBusy] = useState(null)  // 'restart' | 'rename' | 'wifi' | 'mute'
  const [renameVal, setRenameVal] = useState('')
  const [wifiStage, setWifiStage] = useState(null)  // {nets} | {ssid}
  const [wifiPass, setWifiPass] = useState('')
  const [muted, setMuted] = useState(false)  // confirmed from the node's ack, never optimistic
  const [muteUnknown, setMuteUnknown] = useState(false)  // every /info try failed this connection
  const [muteBusy, setMuteBusy] = useState(false)

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
    api(`/api/dashboard/nodes/${encodeURIComponent(room)}/info`)
      .then(r => {
        if (askedAtRef.current !== stamp || muteSeqRef.current !== seq) return
        if (r?.ok) { setMuted(!!r?.data?.muted); setMuteUnknown(false) }
        else throw new Error('info not ok')
      })
      .catch(() => {
        if (askedAtRef.current !== stamp || muteSeqRef.current !== seq) return
        // Out of tries: say so — never keep showing the LAST connection's state.
        if (triesRef.current >= 3) { setMuteUnknown(true); return }
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
    if (!renameVal.trim()) { setMsg('New name required.', 'err', 'Nodes'); return }
    if (!/^[A-Za-z0-9_-]+$/.test(renameVal)) {
      setMsg('Name: letters, numbers, _ or - only (max 31).', 'err', 'Nodes'); return }
    setBusy('rename')
    setMsg(`Renaming ${room} to ${renameVal}…`, 'pulse', 'Nodes')
    try {
      await post('name', { name: renameVal })
      setMsg(`Renamed to '${renameVal}' — hostname follows on the router.`, 'ok', 'Nodes')
      setRenameVal('')
      setBusy(null)
      await reloadAll()
    } catch (e) { setMsg('Rename failed: ' + e.message, 'err', 'Nodes') }  // form stays open to retry
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
    if (!wifiStage?.ssid) { setMsg('Pick a network first.', 'err', 'Nodes'); return }
    if (!wifiPass) { setMsg('Wi-Fi password required.', 'err', 'Nodes'); return }
    setMsg(`Applying Wi-Fi to ${room}…`, 'pulse', 'Nodes')
    try {
      await post('wifi', { ssid: wifiStage.ssid, password: wifiPass })
      setMsg(`Wi-Fi saved — ${room} reboots onto the new network. If it can't reach the bot there, it switches back to the old network on its own within about 5 minutes.`, 'ok', 'Nodes')
      setWifiStage(null); setWifiPass('')
      setBusy(null)
      await reloadAll()
    } catch (e) { setMsg('Wi-Fi set failed: ' + e.message, 'err', 'Nodes') }  // form stays open to retry
  }

  return (
    <div className="grid gap-2 justify-items-end">
      <div className="flex gap-2 flex-wrap justify-end">
        <button className={BTN_DEFAULT} disabled={!online || busy}
          onClick={restart} title="Reboot the node (~15s reconnect).">
          {busy === 'restart' ? 'Restarting…' : 'Restart'}
        </button>
        <button className={muted ? BTN_PRIMARY : BTN_DEFAULT} disabled={!online || busy || muteBusy}
          onClick={toggleMute}
          title={muteUnknown ? "Couldn't read this node's mute state since it reconnected — click to mute it."
            : muted ? 'Mic is muted — click to unmute.' : "Mute this node's mic. Array nodes: same as the physical mute button (red LED) — pressing either one updates both."}>
          {muteBusy ? '…' : muteUnknown ? 'Mute?' : muted ? 'Muted' : 'Mute'}
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
          <button className={BTN_PRIMARY} onClick={rename}>Save name</button>
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
            <button className={BTN_PRIMARY} onClick={applyWifi}>Apply</button>
            <button className={BTN_DEFAULT} onClick={() => { setBusy(null); setWifiStage(null) }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}
function Rooms({ status, mapping, setMsg, reloadAll }) {
  const [mapDraft, setMapDraft] = useState(null)
  // Bug-class fix: the 5s poll refetches mapping and
  // used to clobber unsaved select edits — dirty holds user drafts.
  const [mapDirty, setMapDirty] = useState(false)
  // Session-mode switches: optimistic pending until status confirms.
  const [pendingMode, setPendingMode] = useState({})

  useEffect(() => {
    if (mapping && !mapDirty) setMapDraft(mapping.room_zones || {})
  }, [mapping, mapDirty])

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
  // node couldn't onboard). Mapped rooms keep config order;
  // unmapped nodes follow in registry order.
  const mappedRooms = Object.keys(mapping?.room_zones || {})
  const unmappedRooms = (status?.rooms || []).map(r => r.room)
    .filter(r => r && !mappedRooms.includes(r))
  const roomRows = [...mappedRooms, ...unmappedRooms]
  // Speaker nodes (server-side: board line) get their own built-in speaker
  // as a private zone. Unmapped = that speaker, by default.
  const speakers = mapping?.speakers || {}
  const zoneLabel = z => (z === BUILTIN ? 'Built-in speaker' : z)
  // An unmapped speaker room defaults to its own speaker only once the
  // server resolves it there (the node proved its speaker this session).
  const defaultZone = room => (speakers[room]?.zone === BUILTIN ? BUILTIN : undefined)
  const saveMapping = async room => {
    const zone = mapDraft?.[room] ?? defaultZone(room)
    if (!zone) { setMsg('Pick a zone first.', 'muted', 'Room → zone mapping'); return }
    // PUT /mapping takes the FULL room_zones dict WRAPPED in a
    // room_zones key (MappingPayload contract, routes.py) — a bare
    // merged dict 422s (or worse: pre-forbid, silently wiped the
    // mapping via Pydantic's default_factory). Never unwrapped.
    // Spread the DRAFT map, not the server copy — the server
    // copy dropped every OTHER row's unsaved pick the moment one row
    // saved. The draft holds synced server values + all row edits, so
    // one Save persists the whole visible map (and the full-dict
    // wipe-guard contract stays honest).
    const body = { room_zones: { ...(mapDraft || mapping?.room_zones || {}), [room]: zone } }
    try {
      await api('/api/dashboard/mapping', { method: 'PUT', json: body })
      setMapDirty(false)
      setMsg(`${room} → ${zoneLabel(zone)} saved — replies now route to this zone (live, no restart).`, 'ok', 'Room → zone mapping')
      reloadAll()
    } catch (e) { setMsg('Mapping save failed: ' + e.message, 'err', 'Room → zone mapping') }
  }

  return (
    <>
      <Section id="rooms" title="Room → zone mapping"
        note="Pick a speaker zone for each mic node. New nodes appear here as soon as they connect.">
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
                <th className="text-left px-4 py-2">Room (mic node)</th>
                <th className="text-left px-4 py-2">Zone</th>
                <th className="text-left px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {roomRows.map(room => {
                const hasSpeaker = !!speakers[room]
                const unmapped = !mapping?.room_zones?.[room]
                const zone = mapDraft?.[room] ?? mapping?.room_zones?.[room] ?? defaultZone(room) ?? ''
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
                      onChange={e => { setMapDraft({ ...mapDraft, [room]: e.target.value }); setMapDirty(true) }}
                      className={`${INPUT_CLS} w-[200px]`}>
                      {(zone === '' || (!zoneNames.includes(zone) && !(hasSpeaker && zone === BUILTIN))) &&
                        <option value="" disabled>{zoneNames.length ? 'pick a zone…' : 'add a zone below first'}</option>}
                      {hasSpeaker && <option value={BUILTIN}>{unmapped && defaultZone(room) ? 'Built-in speaker (default)' : 'Built-in speaker'}</option>}
                      {hasSpeaker && zoneNames.length > 0 && <option disabled value="__sep">──────────</option>}
                      {zoneNames.map(z => <option key={z} value={z}>{z}</option>)}
                    </select>
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button className={BTN_PRIMARY} onClick={() => saveMapping(room)}>Save</button>
                  </td>
                </tr>
                )
              })}
              {roomRows.length === 0 &&
                <tr><td className="px-4 py-2 text-[var(--muted)]" colSpan="3">No mic nodes yet — power one on and it will appear here.</td></tr>}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Nodes"
        note="">
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
                <th className="text-left px-4 py-2">Node</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Last seen</th>
                <th className="text-left px-4 py-2">Firmware</th>
                <th className="text-left px-4 py-2">Session</th>
                <th className="text-right px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(status?.rooms || []).map(r => (
                <tr key={r.room} className="border-t border-[var(--separator)]">
                  <td className="px-4 py-2 font-mono text-[13px]">{esc(r.room)}</td>
                  <td className="px-4 py-2">
                    <span className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium
                      ${r.online ? 'bg-[var(--accent-soft)] text-[var(--success)]' : 'text-[var(--muted)]'}`}>
                      {r.online ? 'online' : 'offline'}
                    </span>
                    {r.online && r.tts_usable === false && (
                      <span
                        title={`TTS (${r.tts_provider || 'provider unknown'}) is down — this node hears you but can't speak. Reboot the node (or the bot) to rebuild its session.`}
                        className="inline-block rounded-full px-2.5 py-0.5 ml-2 text-[12px] font-medium bg-[var(--danger)] text-[var(--danger-foreground)]">
                        tts down
                      </span>
                    )}
                    {r.online && r.tts_usable !== false && String(r.tts_provider || '').endsWith('(fallback)') && (
                      <span
                        title="The cloud TTS is down — this room is speaking with local Pocket TTS until it comes back."
                        className="inline-block rounded-full px-2.5 py-0.5 ml-2 text-[12px] font-medium bg-[var(--warning)] text-[var(--warning-foreground)]">
                        tts fallback
                      </span>
                    )}
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
                  <td className="px-4 py-2">
                    <NodeActions room={r.room} online={r.online} lastSeen={r.last_seen} reportedMuted={r.muted} setMsg={setMsg} reloadAll={reloadAll} />
                  </td>
                </tr>
              ))}
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
    } catch (e) { setMsg('Volume save failed: ' + e.message, 'err', 'Zones') }
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
      setMsg(`Zone ${newZone.name} added — applies on next bot restart.`, 'ok', 'Zones')
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
    } catch (e) { setMsg('Volume save failed: ' + e.message, 'err', 'Zones') }
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

  return (
    <Section title="Zones"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
        <table className="w-full text-[14px]">
          <thead>
            <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
              <th className="text-left px-4 py-2">Name</th>
              <th className="text-left px-4 py-2">Backend</th>
              <th className="text-left px-4 py-2">Volume</th>
              <th className="text-left px-4 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(mapping?.zones || {}).map(([name, z]) => (
              <tr key={name} className="border-t border-[var(--separator)]">
                <td className="px-4 py-2">{esc(name)}</td>
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
                      {`Not in use — replies go to ${esc(sp?.zone) || 'the default zone'}.`}
                    </div>
                  )}
                </td>
                <td className="px-4 py-2">
                  <span className="inline-block rounded-full bg-[var(--default)] px-2.5 py-0.5 text-[12px] font-medium">ESP32</span>
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
        onMouseUp={() => onCommit(name, val)}
        onTouchEnd={() => onCommit(name, val)}
        onKeyUp={e => {
          // Only VALUE-MOVING keys commit — the old bare
          // onKeyUp fired on Tab too, so walking the form with the
          // keyboard PUTed an unchanged volume into config.
          if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
               'Home', 'End', 'PageUp', 'PageDown'].includes(e.key))
            onCommit(name, val)
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
      (mismatch ? `\n\n⚠ The filename looks like a ${trackLabel(hinted)} build, but you picked ${trackLabel(fwTrack)}.` : '')))
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
          <input type="file" accept=".bin" className="text-[13px]"
            onChange={e => setFwFile(e.target.files?.[0] || null)} />
          <button className={BTN_PRIMARY} disabled={pushing || !fwFile || !fwTrack} onClick={push}
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
// Wake name card: what the house is called, the spellings speech-to-text
// writes for it, and what it heard right after recent wakes — click a word
// to add it as a heard-as alias. Everything here applies live (no restart).
// Trim edge punctuation only; anything else outside letters/'/- means the
// word can't be an alias as-is (R2D2 must not silently become "rd").
const aliasFrom = word => word.toLowerCase().replace(/^[^\p{L}]+|[^\p{L}]+$/gu, '')
  .replace(/[\u2018\u2019]/g, "'")
const ALIAS_OK = /^\p{L}[\p{L}' -]{0,29}$/u
const ALIAS_RESERVED = new Set(['hey', 'ok', 'okay', 'hi', 'stop', 'yes', 'no', 'cancel'])

function WakeNameCard({ setMsg }) {
  const [w, setW] = useState(null)
  const [loadErr, setLoadErr] = useState('')
  const [draft, setDraft] = useState(null)   // local name draft once edited
  const [aliasDraft, setAliasDraft] = useState('')   // the "add a spelling" box
  const aliasRef = useRef(null)              // latest aliases: quick clicks never lose one
  // A poll that overlapped any save (started during one, or a save began/ended
  // while it was out) carries stale data and is dropped.
  const saveGen = useRef(0)                  // bumps on every save start and end
  const inflight = useRef(0)                 // saves currently in flight
  useEffect(() => {
    // Poll for the heard-after-wake list; state is set only from the
    // async result, and a load error shows inline (no toast every 5 s).
    let alive = true
    const tick = () => {
      const gen = saveGen.current
      return api('/api/dashboard/wake')
        .then(j => {
          if (!alive || gen !== saveGen.current || inflight.current > 0) return
          setW(j); setLoadErr(''); aliasRef.current = j.heard_as || []
        })
        .catch(e => { if (alive) setLoadErr(e.message) })
    }
    tick()
    const t = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  const save = async (body, note) => {
    if (body.heard_as) aliasRef.current = body.heard_as
    saveGen.current += 1
    inflight.current += 1
    try {
      const r = await api('/api/dashboard/wake', { method: 'PUT', json: body })
      setW(prev => ({ ...prev, ...r }))
      aliasRef.current = r.heard_as || aliasRef.current
      setMsg(note || 'Saved — live now.', 'ok', 'Wake name')
      return true
    } catch (e) {
      setMsg('Save failed: ' + e.message, 'err', 'Wake name')
      return false
    } finally {
      inflight.current -= 1
      saveGen.current += 1
    }
  }
  if (!w) return loadErr
    ? <Section title="Wake name"><div className="text-[13px] text-[var(--muted)]">Couldn't load wake settings: {esc(loadErr)}</div></Section>
    : null
  const aliases = w.heard_as || []
  // One path for a clicked word and a typed spelling; true only when saved.
  const addAlias = async word => {
    const a = aliasFrom(word)
    const current = aliasRef.current ?? aliases
    if (!a) return false
    if (a === (w.name || '').toLowerCase()) { setMsg(`'${a}' is the wake name itself.`, 'err', 'Wake name'); return false }
    if (current.includes(a)) { setMsg(`'${a}' is already there.`, 'err', 'Wake name'); return false }
    if (!ALIAS_OK.test(a)) { setMsg(`'${word}' can't be an alias — letters, spaces, ' or - only.`, 'err', 'Wake name'); return false }
    if (ALIAS_RESERVED.has(a)) { setMsg(`'${a}' is a filler word — as an alias it would swallow real commands.`, 'err', 'Wake name'); return false }
    return save({ heard_as: [...current, a] }, `Added '${a}' — live now.`)
  }
  const addTyped = async () => { if (await addAlias(aliasDraft)) setAliasDraft('') }
  const hhmm = t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return (
    <Section title="Wake name"
      note="What the house is called. A bare wake phrase in any of these spellings is dropped, not answered.">
      <div id="wake-name-card" className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        {/* 2x2 on wide screens: name row, then the spelling row with the chips
            beside it (their label sits just above them, at the name row's foot).
            Phones stack: name, spelling box, label, chips. */}
        <div className="grid md:grid-cols-[auto_1fr] gap-x-8 gap-y-2">
          <div className="md:col-start-1 md:row-start-1">
            <Field label="Wake name">
              <input id="wake-name" className={`${INPUT_CLS} w-[180px]`} maxLength={30} placeholder="not set"
                value={draft ?? w.name ?? ''} onChange={e => setDraft(e.target.value)} />
              <button className={`${BTN_DEFAULT} w-[136px]`}
                onClick={async () => { if (await save({ name: (draft ?? w.name ?? '').trim() })) setDraft(null) }}>Set wake word</button>
            </Field>
          </div>
          <div className="flex items-center gap-2 flex-wrap md:col-start-1 md:row-start-2">
            <input id="wake-alias" className={`${INPUT_CLS} w-[180px]`} maxLength={30} placeholder="add a spelling"
              value={aliasDraft} onChange={e => setAliasDraft(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTyped() } }} />
            <button className={`${BTN_DEFAULT} w-[136px]`} disabled={!aliasDraft.trim()} onClick={addTyped}>Add</button>
          </div>
          <span className="text-[12px] font-medium text-[var(--muted)] mt-2 md:mt-0 md:self-end md:col-start-2 md:row-start-1">Also heard as</span>
          {/* min height = the input row's, so chips center on the Add button */}
          <div className="flex items-center gap-2 flex-wrap min-h-[37px] md:col-start-2 md:row-start-2">
            {aliases.map(a => (
              <span key={a} data-alias={a}
                className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[14px] font-medium bg-[var(--default)] text-[var(--default-foreground)]">
                {esc(a)}
                <button type="button" aria-label={`Remove ${a}`} className="text-[var(--muted)] cursor-pointer"
                  onClick={() => save({ heard_as: (aliasRef.current ?? aliases).filter(x => x !== a) }, `Removed '${a}' — live now.`)}>×</button>
              </span>
            ))}
            {aliases.length === 0 && <span className="text-[13px] text-[var(--muted)]">none yet</span>}
          </div>
        </div>

        <div className="grid gap-1">
          <div className="text-[12px] uppercase tracking-wide text-[var(--muted)]">Heard right after a wake</div>
          {(w.recent || []).length === 0 &&
            <div className="text-[13px] text-[var(--muted)]">Nothing yet — say the wake word. Kept in memory only.</div>}
          {(w.recent || []).map((r, i) => (
            <div key={`${r.time}-${i}`} data-heard="" className="flex flex-wrap items-center gap-1 text-[14px] py-1 border-t border-[var(--separator)]">
              <span className="text-[12px] text-[var(--muted)] w-[44px]">{hhmm(r.time)}</span>
              <span className="font-mono text-[12px] text-[var(--muted)] mr-2">{esc(r.room || '?')}</span>
              {String(r.text || '').split(/\s+/).filter(Boolean).map((word, j) => (
                <button key={j} type="button" title="Add as an alias"
                  className="rounded-md px-1 hover:bg-[var(--default)] cursor-pointer"
                  onClick={() => addAlias(word)}>{esc(word)}</button>
              ))}
              {r.dropped &&
                <span className="ml-2 inline-block rounded-full px-2 py-0.5 text-[11px] font-medium text-[var(--muted)] border border-[var(--separator)]">dropped</span>}
            </div>
          ))}
        </div>
      </div>
    </Section>
  )
}

function OwwPanel({ setMsg, setRebooting, reloadAll }) {
  const [models, setModels] = useState([])
  const [sel, setSel] = useState('')
  const [scanning, setScanning] = useState(false)
  const [newModel, setNewModel] = useState(null)

  const load = async () => {
    try {
      const r = await api('/api/dashboard/oww/models')
      setModels(r.models || [])
      const active = (r.models || []).find(m => m.selected)
      setSel(active?.path || '')
    } catch (e) { setMsg('Model list load failed: ' + e.message, 'err', 'Wake word model') }
  }
  useEffect(() => { load() }, [])

  const select = async () => {
    // The dropdown value is the row's full path — resolve the
    // ROW (basename + source) so a same-basename root/models/
    // collision activates the exact copy the picker named; a name-only
    // select always resolved to the models/ copy.
    const row = models.find(m => m.path === sel)
    const name = row?.name || sel.split('/').pop()
    // Stock rows are pre-trained models bundled with openwakeword,
    // selected by KEY (no .onnx name).
    if (!name || (row?.source !== 'stock' && !name.endsWith('.onnx'))) { setMsg('No model selected.', 'err', 'Wake word model'); return }
    const body = { model: name, ...(row?.source ? { source: row.source } : {}) }
    try {
      await api('/api/dashboard/oww/select', { method: 'PUT', json: body })
      setMsg(`Active model set to '${name}' — applies on next bot restart.`, 'ok', 'Wake word model')
      await load()
      offerRestart(setMsg, setRebooting)
    } catch (e) { setMsg('Select failed: ' + e.message, 'err', 'Wake word model') }
  }

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
      setMsg(`Model '${r.name}' uploaded (${(r.size / 1024).toFixed(0)} KB). Select it to activate.`, 'ok', 'Wake word model')
      setNewModel(null)
      await load()
    } catch (e) { setMsg('Upload failed: ' + e.message, 'err', 'Wake word model') }
    finally { setScanning(false) }
  }

  return (
    <Section title="Wake word model"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <Field label="Active model / upload" between>
          <div className="flex items-center gap-2">
            <select className={`${INPUT_CLS} w-[260px]`} value={sel}
              onChange={e => setSel(e.target.value)}>
              {models.map(m => (
                <option key={m.path} value={m.path}>
                  {esc(m.name)}{m.source === 'root' ? ' (root)' : m.source === 'stock' ? ' (stock)' : ''}
                </option>
              ))}
              {models.length === 0 && <option value="">no models found</option>}
            </select>
            <button className={BTN_DEFAULT} onClick={select}>Set active</button>
          </div>
          <div className="flex items-center gap-2">
            <input type="file" accept=".onnx" className="text-[13px]"
              onChange={e => setNewModel(e.target.files?.[0] || null)} />
            <button className={BTN_DEFAULT} disabled={scanning || !newModel} onClick={upload}>
              {scanning ? 'Uploading…' : 'Upload'}
            </button>
          </div>
        </Field>
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
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
                  <td className="px-4 py-2 font-mono text-[13px]">{((m.size || 0) / 1024).toFixed(0)} KB</td>
                  <td className="px-4 py-2 text-right">
                    {m.source !== 'stock' &&
                      <button className={BTN_DANGER} onClick={() => del(m.name, m.source)}>Delete</button>}
                  </td>
                </tr>
              ))}
              {models.length === 0 &&
                <tr><td className="px-4 py-2 text-[var(--muted)]" colSpan="4">No models yet — upload one.</td></tr>}
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
// Each provider's SAVED value lives in its own field. The old sync loaded
// tts.model (the ElevenLabs model) for every provider, so a Deepgram
// config DISPLAYED the first voice while holding 'eleven_flash_v2_5' —
// the first Save 422'd.
const savedFor = (t, p) =>
  p === 'elevenlabs' ? t?.model : p === 'deepgram' ? t?.deepgram_voice
  : p === 'pocket' ? t?.pocket_voice : t?.deepgram_flux_voice

function Voice({ tts, tuning, status, setMsg, setRebooting, reloadAll }) {
  const [provider, setProvider] = useState(null)
  const [ttsKeyEpoch, setTtsKeyEpoch] = useState(0)  // remounts the key box after a save
  const [model, setModel] = useState(null)
  // Bug-class fix: the 5s poll refetches tts state and
  // resyncing from server would clobber the user's in-progress provider
  // selection. The server is the source of truth for the FIRST sync
  // only; after that, drafts are local until saved.
  const [ttsSynced, setTtsSynced] = useState(false)
  // Flux knobs are sliders (2026-10-07): controlled, synced from the
  // server exactly ONCE (same draft law — the 5s poll must not clobber
  // a drag in progress); committed on Save via the same DOM read.
  const [fluxSpeed, setFluxSpeed] = useState(1.0)
  const [fluxExpr, setFluxExpr] = useState(1)
  // A Flux knob rides the save ONLY if the server had one set
  // (editing it) or the user actually moved the slider. An untouched
  // placeholder on an unset knob used to be written as an explicit
  // config value — pinning Flux to numbers nobody chose.
  const fluxTouched = useRef({ speed: false, expr: false })
  // ElevenLabs flash-v2.5 knobs (2026-10-07): speed 0.7–1.2, stability
  // 0.0–1.0. Same draft law — synced once, committed on Save.
  const [elSpeed, setElSpeed] = useState(null)   // null = not set = ElevenLabs default
  const [elStab, setElStab] = useState(null)
  // Local hot standby (Pocket) behind a cloud provider. Same draft law.
  const [fallback, setFallback] = useState('off')
  // The fallback's voice (tts.pocket_voice — the same key Pocket-as-primary
  // uses). Same draft law: synced once, committed on Save.
  const [pocketVoice, setPocketVoice] = useState('alba')

  useEffect(() => {
    if (tts && !ttsSynced) {
      setProvider(tts.provider || 'elevenlabs')
      const prov = tts.provider || 'elevenlabs'
      setModel(savedFor(tts, prov) || tts.catalog?.[prov]?.[0] || '')
      setFluxSpeed(parseFloat(tts?.deepgram_flux_speed ?? 1.0))
      setFluxExpr(parseInt(tts?.deepgram_flux_expressivity ?? 1, 10))
      setElSpeed(tts?.elevenlabs_speed != null ? parseFloat(tts.elevenlabs_speed) : null)
      setElStab(tts?.elevenlabs_stability != null ? parseFloat(tts.elevenlabs_stability) : null)
      setFallback(tts?.fallback === 'pocket' ? 'pocket' : 'off')
      setPocketVoice(tts?.pocket_voice || 'alba')
      setTtsSynced(true)
    }
  }, [tts, ttsSynced])

  // Switching provider resets the model menu to that
  // provider's own list — and lands on THAT provider's saved
  // value when it has one, not blindly on the first entry.
  const catalog = tts?.catalog || {}
  const options = catalog[provider] || []
  const onProviderChange = p => {
    setProvider(p)
    setModel(savedFor(tts, p) || catalog[p]?.[0] || '')
  }

  const saveTTS = async () => {
    const voice_id = document.getElementById('tts-voice')?.value.trim() || ''
    const body = { provider }
    if (provider === 'elevenlabs') {
      if (!voice_id) { setMsg("Voice ID can't be empty — TTS breaks with no voice.", 'err', 'TTS'); return }
      body.model = model
      body.voice_id = voice_id
      // Flash-v2.5 knobs ride on save (null = leave as-is/defaults).
      if (elSpeed != null) body.elevenlabs_speed = elSpeed
      if (elStab != null) body.elevenlabs_stability = elStab
    } else if (provider === 'deepgram') {
      body.deepgram_voice = model
    } else if (provider === 'deepgram-flux') {
      body.deepgram_flux_voice = model
      // Knobs ride ONLY when the server had one (an edit) or
      // the user moved the slider — an untouched placeholder on an
      // unset knob writes nothing (the route's None = leave-as-is).
      if (fluxTouched.current.speed || tts?.deepgram_flux_speed != null)
        body.deepgram_flux_speed = parseFloat(fluxSpeed)
      if (fluxTouched.current.expr || tts?.deepgram_flux_expressivity != null)
        body.deepgram_flux_expressivity = parseInt(fluxExpr, 10)
    } else if (provider === 'pocket') {
      body.pocket_voice = model
    }
    // The fallback only rides with a cloud primary; the server drops it
    // when Pocket itself is the provider.
    if (provider !== 'pocket') body.fallback = fallback
    if (provider !== 'pocket' && fallback === 'pocket') body.pocket_voice = pocketVoice
    try {
      await api('/api/dashboard/tts', { method: 'PUT', json: body })
      setMsg('Saved to config.yaml — applies on next bot restart.', 'ok', 'TTS')
      reloadAll()
      offerRestart(setMsg, setRebooting)
    } catch (e) { setMsg('Save failed: ' + e.message, 'err', 'TTS') }
  }

  return (
    <>
      <Section id="voice" title="TTS"
        note="">
        <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
          <Field label="Provider / model">
            <select className={`${INPUT_CLS} w-[200px]`} value={provider || 'elevenlabs'}
              onChange={e => onProviderChange(e.target.value)}>
              <option value="elevenlabs">ElevenLabs</option>
              <option value="deepgram">Deepgram Aura</option>
              <option value="deepgram-flux">Deepgram Flux</option>
              <option value="pocket" disabled={!tts?.pocket_installed && provider !== 'pocket'}>
                Pocket TTS{tts?.pocket_installed ? '' : ' — not installed'}
              </option>
            </select>
            <select className={`${INPUT_CLS} w-[200px]`} value={model || ''}
              onChange={e => setModel(e.target.value)}>
              {/* A saved value the catalog doesn't carry (hand-edited config)
                  shows honestly instead of faking the first entry. */}
              {model && !options.includes(model) &&
                <option value={model} disabled>{ttsLabel(model)} (not accepted)</option>}
              {!options.length && <option value="" disabled>loading…</option>}
              {options.map(v => <option key={v} value={v}>{ttsLabel(v)}</option>)}
            </select>
            {provider === 'elevenlabs' &&
              <input id="tts-voice" defaultValue={tts?.voice_id || ''} placeholder="Voice id (fq1S…)"
                className={`${INPUT_CLS} w-[260px] font-mono`} />}
            {provider === 'deepgram-flux' && (
              <>
                <span className="text-[12px] font-medium text-[var(--muted)]">Flux speed</span>
                <input id="tts-flux-speed" type="range" min="0.5" max="1.5" step="0.05"
                  value={fluxSpeed} onChange={e => { setFluxSpeed(parseFloat(e.target.value)); fluxTouched.current.speed = true }}
                  className="w-[140px] accent-[var(--accent)]" aria-label="Flux speed" />
                <span className="font-mono text-[12px] w-[36px] text-right">{fluxSpeed.toFixed(2)}</span>
                <span className="text-[12px] font-medium text-[var(--muted)]">expressivity</span>
                <input id="tts-flux-expressivity" type="range" min="-2" max="2" step="1"
                  value={fluxExpr} onChange={e => { setFluxExpr(parseInt(e.target.value, 10)); fluxTouched.current.expr = true }}
                  className="w-[140px] accent-[var(--accent)]" aria-label="Flux expressivity" />
                <span className="font-mono text-[12px] w-[20px] text-right">{fluxExpr}</span>
              </>
            )}
          </Field>
          {provider === 'elevenlabs' && model === 'eleven_flash_v2_5' && (
            <Field label="Speed / stability (flash v2.5)">
              <span className="text-[12px] font-medium text-[var(--muted)]">speed</span>
              <input id="tts-el-speed" type="range" min="0.7" max="1.2" step="0.05"
                value={elSpeed ?? 1.0} onChange={e => setElSpeed(parseFloat(e.target.value))}
                className="w-[140px] accent-[var(--accent)]" aria-label="ElevenLabs speed" />
              <span className="font-mono text-[12px] w-[36px] text-right">{(elSpeed ?? 1.0).toFixed(2)}</span>
              <span className="text-[12px] font-medium text-[var(--muted)]">stability</span>
              <input id="tts-el-stability" type="range" min="0" max="1" step="0.05"
                value={elStab ?? 0.5} onChange={e => setElStab(parseFloat(e.target.value))}
                className="w-[140px] accent-[var(--accent)]" aria-label="ElevenLabs stability" />
              <span className="font-mono text-[12px] w-[36px] text-right">{(elStab ?? 0.5).toFixed(2)}</span>
            </Field>
          )}
          {provider !== 'pocket' && (
            <Field label="Fallback">
              <select id="tts-fallback" className={`${INPUT_CLS} w-[200px]`} value={fallback}
                onChange={e => setFallback(e.target.value)}
                // Stays usable while it holds 'pocket', so a fallback saved
                // before Pocket went missing can always be turned off.
                disabled={!tts?.pocket_installed && fallback !== 'pocket'}
                title={tts?.pocket_installed
                  ? 'Speak with Pocket TTS (local) while this provider is down.'
                  : 'Install Pocket TTS to enable a local fallback.'}>
                <option value="off">Off</option>
                <option value="pocket">Pocket TTS</option>
              </select>
              {fallback === 'pocket' && (
                <select id="tts-fallback-voice" className={`${INPUT_CLS} w-[200px]`} value={pocketVoice}
                  onChange={e => setPocketVoice(e.target.value)}
                  disabled={!tts?.pocket_installed}
                  aria-label="Fallback voice" title="The Pocket TTS voice used while the cloud provider is down.">
                  {(catalog.pocket || []).includes(pocketVoice) ? null :
                    <option value={pocketVoice} disabled>{pocketVoice} (not accepted)</option>}
                  {(catalog.pocket || []).map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              )}
            </Field>
          )}
          {provider !== 'pocket' && (
            <Field label="API key">
              <div className="flex items-center gap-2 flex-wrap flex-1">
                <KeyInput id="tts-key" key={`tts-${provider}-${ttsKeyEpoch}`}
                  hint={provider === 'elevenlabs' ? tts?.elevenlabs_key_hint : tts?.deepgram_key_hint}
                  placeholder={provider === 'elevenlabs' ? 'paste ElevenLabs key…' : 'paste Deepgram key…'} />
                <button className={BTN_DEFAULT} onClick={async () => {
                  const key = readKey('tts-key')
                  if (!key) { setMsg('Nothing to save — paste a new key first.', 'muted', 'TTS'); return }
                  const route = provider === 'elevenlabs'
                    ? '/api/dashboard/tts/key/elevenlabs' : '/api/dashboard/tts/key/deepgram'
                  try {
                    await api(route, { method: 'PUT', json: { api_key: key } })
                    setMsg('Key saved — applies on next bot restart.', 'ok', 'TTS')
                    setTtsKeyEpoch(n => n + 1)
                    reloadAll()
                  } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'TTS') }
                }}>Save key</button>
              </div>
            </Field>
          )}
          <div className="flex items-center justify-end gap-4 flex-wrap">
            <button className={BTN_PRIMARY} onClick={saveTTS}>Save</button>
          </div>
        </div>
      </Section>

      <SttCard status={status} setMsg={setMsg} setRebooting={setRebooting} />
      <Tuning tuning={tuning} setMsg={setMsg} setRebooting={setRebooting} />
      <WakeNameCard setMsg={setMsg} />
      <OwwPanel setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
    </>
  )
}

/* ── STT card ───────────────────────────────────────────────────
   Chips = what's RUNNING (load state + per-command time, from the 5s
   /status poll). Selects = what's SAVED. Four benchmarked models; the
   server writes device/compute with the model and downloads a missing
   model before switching config (restart only offered once it's on disk). */
const CHIP_CLS = 'inline-block rounded-full bg-[var(--default)] px-2.5 py-0.5 text-[12px] font-medium'

const fmtBytes = b => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`

function sttStateChip(st, dl) {
  if (dl?.state === 'downloading')
    return { text: `Downloading ${dl.model}…${dl.percent != null ? ` ${dl.percent}%` : ''}`, cls: 'text-[var(--muted)]' }
  if (!st) return { text: '…', cls: 'text-[var(--muted)]' }
  if (st.state === 'ready') return { text: '● Running', cls: 'text-[var(--success)]' }
  if (st.state === 'error') return { text: '● Failed to load', cls: 'text-[var(--danger)]', title: st.error }
  if (st.state === 'loading') return { text: 'Loading…', cls: 'text-[var(--muted)]' }
  return { text: 'Not loaded yet', cls: 'text-[var(--muted)]' }
}

function SttCard({ status, setMsg, setRebooting }) {
  const [data, setData] = useState(null)
  const [model, setModel] = useState(null)
  const [language, setLanguage] = useState(null)
  const [busy, setBusy] = useState(false)
  // setMsg is a new function on every parent render (the 5 s poll): keep it
  // in a ref so the effects below don't re-run on every poll.
  const msgRef = useRef(setMsg)
  useEffect(() => { msgRef.current = setMsg })

  // Draft law: the server seeds the selects once; after that they're
  // local until saved (a refetch must not clobber a pick in progress).
  const apply = d => {
    setData(d)
    setModel(m => m ?? d.current.model)
    setLanguage(l => l ?? d.current.language)
    return d
  }
  const load = () => api('/api/dashboard/stt').then(apply)
    .catch(e => { msgRef.current('STT load failed: ' + e.message, 'err', 'STT'); return null })
  useEffect(() => {
    let alive = true
    api('/api/dashboard/stt')
      .then(d => {
        if (!alive) return
        setData(d)
        setModel(m => m ?? d.current.model)
        setLanguage(l => l ?? d.current.language)
      })
      .catch(e => { if (alive) msgRef.current('STT load failed: ' + e.message, 'err', 'STT') })
    return () => { alive = false }
  }, [])

  // While a download runs — started by Save, or already in progress when the
  // page loaded — poll every 2 s; offer the restart once it's on disk. A
  // failed poll just tries again; the cleanup stops it when the state flips.
  const downloading = data?.download?.state === 'downloading'
  const pendingModel = downloading ? data.download.model : null   // already on its way
  useEffect(() => {
    if (!downloading) return
    let alive = true
    const t = setInterval(() => {
      api('/api/dashboard/stt').then(d => {
        if (!alive) return
        setData(d)
        const dl = d.download || {}
        if (dl.state === 'ready') {
          msgRef.current(`Downloaded ${dl.model} — saved; restart to load it.`, 'ok', 'STT')
          offerRestart(msgRef.current, setRebooting)
        } else if (dl.state === 'error') {
          msgRef.current(`Download failed (${dl.error}) — config unchanged.`, 'err', 'STT')
        }
      }).catch(() => {})
    }, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [downloading, setRebooting])

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

  const save = async () => {
    if (!data) return
    const body = {}
    if (model !== data.current.model && model !== pendingModel) body.model = model
    if (language !== data.current.language) body.language = language
    if (!Object.keys(body).length) { setMsg('Nothing changed.', 'ok', 'STT'); return }
    setBusy(true)
    try {
      const r = await api('/api/dashboard/stt', { method: 'PUT', json: body })
      await load()
      if (r.download === 'started') {
        setMsg(`Downloading ${model} — restart offered when it's on disk.`, 'ok', 'STT')
      } else if (r.restart_needed) {
        setMsg('Saved to config.yaml — the model loads on bot restart.', 'ok', 'STT')
        offerRestart(setMsg, setRebooting)
      } else {
        setMsg('Saved — applies to the next thing you say.', 'ok', 'STT')
      }
    } catch (e) { setMsg('Save failed: ' + e.message, 'err', 'STT') }
    finally { setBusy(false) }
  }

  const st = status?.stt || data?.status
  const state = sttStateChip(st, data?.download)
  const ms = st?.latency_ms_median
  const models = data?.models || []
  const custom = data?.current?.custom_model
  const customLang = data?.current?.custom_language
  return (
    <Section id="stt" title="STT" note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <div className="flex flex-wrap items-center gap-2" id="stt-chips">
          <span className={`${CHIP_CLS} ${state.cls}`} title={state.title}>{state.text}</span>
          {st?.model && <span className={CHIP_CLS}>{st.model}</span>}
          {st?.device && <span className={CHIP_CLS}>
            {st.device === 'cuda' ? `GPU · ${data?.gpu || 'CUDA'}` : 'CPU'}</span>}
          {st?.compute && <span className={CHIP_CLS}>{st.compute}</span>}
          <span className={CHIP_CLS} title="Median speech-to-text time of the last 10 commands since restart">
            {ms != null ? `${(ms / 1000).toFixed(2)} s / command` : '— s / command'}</span>
        </div>
        {downloading && (
          <div className="grid gap-2">
            <div id="stt-progress" className="w-full rounded-full bg-[var(--default)] overflow-hidden" style={{ height: 6 }}>
              <div className="rounded-full bg-[var(--accent)]"
                style={{ height: 6, width: `${Math.max(2, data.download.percent || 0)}%` }} />
            </div>
            <span className="text-[12px] text-[var(--muted)]">
              {data.download.total_bytes
                ? `${fmtBytes(data.download.done_bytes || 0)} / ${fmtBytes(data.download.total_bytes)}`
                : 'Starting download…'}
            </span>
          </div>
        )}
        <table id="stt-models" className="w-full text-[14px]">
          <thead>
            <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
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
        <Field label="Model">
          <select id="stt-model" className={`${INPUT_CLS} w-[260px]`} value={model || ''}
            onChange={e => setModel(e.target.value)} disabled={!data}>
            {!data && <option value="" disabled>loading…</option>}
            {custom && <option value={data.current.model} disabled>{data.current.model} (set in config)</option>}
            {models.map(m => (
              <option key={m.name} value={m.name} disabled={!m.available}>
                {m.label} ({m.size_gb} GB){!m.available ? ' — needs a GPU' : !m.cached ? ' — downloads first' : ''}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Language">
          <select id="stt-language" className={`${INPUT_CLS} w-[200px]`} value={language || ''}
            onChange={e => setLanguage(e.target.value)} disabled={!data}>
            {!data && <option value="" disabled>loading…</option>}
            {customLang && <option value={data.current.language} disabled>{data.current.language} (set in config)</option>}
            {(data?.languages || []).map(l => <option key={l.code} value={l.code}>{l.label}</option>)}
          </select>
        </Field>
        <div className="flex items-center justify-end gap-4 flex-wrap">
          <button className={BTN_PRIMARY} onClick={save}
            disabled={!data || busy || (downloading && model !== data.current.model && model !== pendingModel)}>Save</button>
        </div>
      </div>
    </Section>
  )
}

const TUNING_FIELDS = [
  ['wake_threshold', 'Wake sensitivity', 0.30, 0.60, 0.01,
    'OWW score needed to wake. Real wakes usually score well above TV/ad audio — tune by ear.'],
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

function Tuning({ tuning, setMsg, setRebooting }) {
  const [vals, setVals] = useState({})
  // Dirty-set saves. Save used to send ALL six
  // knobs from a sync-once draft — nudging one slider rewrote the other
  // five with whatever this tab loaded, clobbering edits made elsewhere
  // (another tab, another device). Now: Save sends only touched knobs, and
  // UNTOUCHED sliders keep following the server on every poll. The dirty
  // set lives in a ref too so the resync effect runs on `tuning` changes
  // ONLY — keying it on dirty would snap sliders back to the stale
  // pre-save values the moment a save clears the set.
  const [dirty, setDirty] = useState({})
  const dirtyRef = useRef({})
  const markDirty = d => { dirtyRef.current = d; setDirty(d) }
  useEffect(() => {
    if (!tuning) return
    setVals(prev => {
      const next = { ...prev }
      for (const [f] of TUNING_FIELDS) if (!dirtyRef.current[f]) next[f] = tuning[f]
      return next
    })
  }, [tuning])

  const dirtyFields = TUNING_FIELDS.map(([f]) => f).filter(f => dirty[f])
  const save = async () => {
    if (!dirtyFields.length) { setMsg('Nothing changed — no knobs touched.', 'muted', 'Voice tuning'); return }
    const body = {}
    for (const f of dirtyFields) body[f] = parseFloat(vals[f])
    try {
      await api('/api/dashboard/voice-tuning', { method: 'PUT', json: body })
      markDirty({})
      window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))  // pull the saved truth back
      setMsg(`Tuning saved (${dirtyFields.length} knob${dirtyFields.length > 1 ? 's' : ''}) — applies on next bot restart.`, 'ok', 'Voice tuning')
      offerRestart(setMsg, setRebooting)
    } catch (e) { setMsg('Tuning save failed: ' + e.message, 'err', 'Voice tuning') }
  }

  return (
    <Section title="Voice tuning"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <div className="grid sm:grid-cols-2 gap-x-8 gap-y-4">
          {TUNING_FIELDS.map(([f, label, min, max, step, hint]) => (
            <div key={f} className="grid gap-1">
              <div className="flex items-center justify-between gap-4">
                <span className="text-[13px] font-medium">{label}{dirty[f] ? ' •' : ''}</span>
                <span className="font-mono text-[13px]">{vals[f] ?? '—'}</span>
              </div>
              <input type="range" min={min} max={max} step={step} value={vals[f] ?? min}
                onChange={e => { setVals({ ...vals, [f]: e.target.value }); markDirty({ ...dirtyRef.current, [f]: true }) }}
                className="accent-[var(--accent)]" aria-label={label} />
              <span className="text-[12px] text-[var(--muted)]">{hint}</span>
            </div>
          ))}
        </div>
        <div className="flex justify-end">
          <button className={BTN_PRIMARY} onClick={save} disabled={!dirtyFields.length}
            title={dirtyFields.length ? `Saves only the ${dirtyFields.length} knob(s) you changed (•).` : 'Move a slider first.'}>
            {dirtyFields.length ? `Save (${dirtyFields.length})` : 'Save'}
          </button>
        </div>
      </div>
    </Section>
  )
}

/* ── System: Intelligence ─────────────────────────────────── */
function System({ intelligence, status, setMsg, setRebooting, reloadAll }) {
  // Kind is UI state (2026-10-07): the agent input grays out for
  // non-OpenClaw kinds, so the select must be controlled. Synced from
  // the server exactly once (draft law); save still reads the DOM.
  const [intKind, setIntKind] = useState('hermes')
  const [intSynced, setIntSynced] = useState(false)
  // Per-kind URL memory (2026-10-08): the two lanes live at
  // different addresses. The field carries a PER-KIND DRAFT — switching
  // kinds files the current text under the outgoing kind and loads the
  // incoming kind's draft (or empty). The old urlTouched latch never
  // reset, so one manual edit disabled swapping forever and a save
  // filed the wrong URL under the kind.
  const urlDrafts = useRef({})   // {hermes: url, openclaw: url}
  // Agents: per-kind selection (like URLs) + the provider's discovered
  // list. Discovery runs server-side (keys never reach the browser) on
  // first sync, on provider switch, on URL change, and on the refresh
  // button. Each entry carries key status + the masked key hint.
  const [intAgent, setIntAgent] = useState('')
  const agentDrafts = useRef({})  // {hermes: 'default', openclaw: '<agent>'}
  const [found, setFound] = useState({})  // kind -> {agents, note, selected, loading}
  const lastDiscoverUrl = useRef({})
  const [keyEpoch, setKeyEpoch] = useState(0)  // remounts the key box after a save
  const discover = async (kind, url) => {
    lastDiscoverUrl.current[kind] = url || ''
    setFound(f => ({ ...f, [kind]: { ...(f[kind] || {}), loading: true } }))
    const body = { kind }
    if (/^https?:\/\//.test(url || '')) body.url = url
    try {
      const r = await api('/api/dashboard/intelligence/agents', { method: 'POST', json: body })
      setFound(f => ({ ...f, [kind]: { ...r, loading: false } }))
    } catch (e) {
      setFound(f => ({ ...f, [kind]: { agents: [], note: 'Agent list failed: ' + e.message, loading: false } }))
    }
  }
  useEffect(() => {
    if (intelligence && !intSynced) {
      const kind = intelligence.kind || 'hermes'
      setIntKind(kind)
      const d = { ...(intelligence.urls || {}) }
      if (intelligence.url) d[kind] = intelligence.url
      urlDrafts.current = d
      agentDrafts.current = { ...(intelligence.agents || {}) }
      setIntAgent(agentDrafts.current[kind] || '')
      setIntSynced(true)
      discover(kind, d[kind])
    }
  }, [intelligence, intSynced])
  const switchKind = (kind) => {
    const urlEl = document.getElementById('int-url')
    if (urlEl) {
      urlDrafts.current[intKind] = urlEl.value.trim()  // file the outgoing kind's draft
      urlEl.value = urlDrafts.current[kind] || ''      // always swap — per-kind context
    }
    agentDrafts.current[intKind] = agentSel
    setIntAgent(agentDrafts.current[kind] || '')
    setIntKind(kind)
    discover(kind, urlDrafts.current[kind])
  }
  const agentList = found[intKind]?.agents || []
  // Nothing picked for this kind yet: the saved one, else the first listed.
  const agentSel = intAgent || found[intKind]?.selected || agentList[0]?.id || ''
  const agentEntry = agentList.find(a => a.id === agentSel)
  const keyHint = agentEntry ? agentEntry.key_hint : intelligence?.key_hints?.[intKind]
  const agentNote = found[intKind]?.note
  return (
    <>
    <Section id="system" title="Intelligence"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <Field label="Provider / agent / API URL">
          <select id="int-kind" value={intKind} onChange={e => switchKind(e.target.value)}
            className={`${INPUT_CLS} w-[160px]`}>
            <option value="hermes">Hermes</option>
            <option value="openclaw">OpenClaw</option>
          </select>
          <select id="int-agent" value={agentSel} onChange={e => setIntAgent(e.target.value)}
            title={intKind === 'openclaw'
              ? 'Agents on this OpenClaw Gateway (its /v1/models).'
              : 'Agents (profiles) on this Hermes gateway. Each one needs its own API key.'}
            className={`${INPUT_CLS} w-[220px] font-mono`}>
            {!agentSel && <option value="">{found[intKind]?.loading ? 'loading…' : 'no agents'}</option>}
            {agentList.map(a => (
              <option key={a.id} value={a.id}>
                {a.id}{intKind === 'hermes' && !a.key_set ? ' — no key' : ''}
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
          <input id="int-url" defaultValue={intelligence?.url || ''} placeholder="http://<intelligence-host>:8642/v1"
            title="Base URL of the OpenAI-compatible chat endpoint — the lane appends /chat/completions."
            onBlur={e => {
              const u = e.target.value.trim()
              if (u !== (lastDiscoverUrl.current[intKind] || '')) discover(intKind, u)
            }}
            className={`${INPUT_CLS} flex-1 min-w-[280px] font-mono`} />
        </Field>
        {agentNote ? <div className="text-[12px] text-[var(--muted)] -mt-2">{agentNote}</div> : null}
        <Field label="API key">
          <div className="flex items-center gap-2 flex-wrap flex-1">
            <KeyInput id="int-key" key={`int-${intKind}-${agentSel}-${keyEpoch}`} hint={keyHint}
              placeholder={intKind === 'hermes' && agentSel && agentSel !== 'default'
                ? `paste the ${agentSel} profile's API key…` : 'paste key…'} />
              <button className={BTN_DEFAULT} onClick={async () => {
                const key = readKey('int-key')
                if (!key) { setMsg('Nothing to save — paste a new key first.', 'muted', 'Intelligence'); return }
                try {
                  // Send the kind (and Hermes profile) on screen — the server
                  // writes THAT key, never the one config.yaml happens to hold.
                  const named = intKind === 'hermes' && agentSel && agentSel !== 'default'
                  const json = { kind: intKind, api_key: key, ...(named ? { agent: agentSel } : {}) }
                  await api('/api/dashboard/intelligence/key', { method: 'PUT', json })
                  setKeyEpoch(n => n + 1)  // fresh box: shows the new key's hint, drops the typed value
                  const who = intKind === 'openclaw' ? 'OpenClaw' : `Hermes · ${agentSel || 'default'}`
                  setMsg(`Key saved for ${who} — applies on next bot restart.`, 'ok', 'Intelligence')
                  discover(intKind, document.getElementById('int-url')?.value.trim())
                  reloadAll()
                } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'Intelligence') }
              }}>Save key</button>
          </div>
        </Field>
        {/* Footer: secondary action bottom-left, green commit bottom-right */}
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
            <button className={BTN_PRIMARY} onClick={async () => {
              const kind = document.getElementById('int-kind').value
              const url = document.getElementById('int-url').value.trim()
              const body = { kind, url }
              if (agentSel) body.agent = agentSel
              // Ship the full per-kind recall map so the backend files
              // the other kind's URL too (swap-on-switch memory).
              const mem = { ...urlDrafts.current, [kind]: url }
              if (mem.hermes || mem.openclaw) body.urls = mem
              try {
                await api('/api/dashboard/intelligence', { method: 'PUT', json: body })
                setMsg('Saved to config.yaml — applies on next bot restart.', 'ok', 'Intelligence')
                reloadAll()
                offerRestart(setMsg, setRebooting)
              } catch (e) { setMsg('Save failed: ' + e.message, 'err', 'Intelligence') }
            }}>Save</button>
        </div>
      </div>
    </Section>

    <FirmwarePanel status={status} setMsg={setMsg} reloadAll={reloadAll} />
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
  const active = useActiveSection()

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
      api('/api/dashboard/mapping').then(setMapping).catch(() => {}),
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
    <div className="bg-[var(--background)] text-[var(--foreground)] min-h-screen font-sans">
      <div className="max-w-[1120px] mx-auto px-6 pt-5 pb-12 grid gap-10">
        <header className="sticky top-0 z-10 -mx-6 px-6 pt-2 pb-3 bg-[var(--background)]">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <Wordmark />
            <nav className="flex gap-2" aria-label="Sections">
              {SECTIONS.map(s => (
                <a key={s.id} href={`#${s.id}`}
                  className={`rounded-lg px-2.5 py-1.5 text-[13px] no-underline
                    ${active === s.id
                      ? 'text-[var(--accent)] font-semibold'
                      : 'text-[var(--muted)] hover:text-[var(--foreground)]'}`}>
                  {s.label}
                </a>
              ))}
            </nav>
          </div>
          <AppMsg msg={msg} />
        </header>

        <Overview status={status} setMsg={setMsg} setRebooting={setRebooting} rebooting={rebooting} />
        <Rooms status={status} mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} />
        <Voice tts={tts} tuning={tuning} status={status} setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
        <System intelligence={intelligence} status={status} setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
      </div>
    </div>
  )
}