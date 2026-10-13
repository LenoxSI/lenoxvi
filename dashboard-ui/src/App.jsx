// lenoxvi dashboard — app. Wireframe-C structure (design note):
// single scrolling page, sticky header + section rail, no tabs.
// Brand: dark theme default, one lime element leads, sentence case,
// no emoji. Live data via the same /api/dashboard/* routes the old
// page.html used — routes are the contract, every shape verified
// against the live rig (2026-10-07 bug audit, 8 findings).

import { useEffect, useRef, useState } from 'react'

/* ── API layer ─────────────────────────────────────────────── */
const TOKEN_KEY = 'lenoxvi-dashboard-token'
// design note M6 (2026-10-13): LVI_DASHBOARD_TOKEN on the bot makes every
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
async function restartBot(setMsg, setRebooting) {
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
// epoch zero (bug 5: "20,713d ago" = new Date(seconds-as-ms) → 1970).
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
            // section (the owner 2026-10-08): strip " · <agent>" from the label.
            const brand = (esc(status?.intelligence) || '—').split(' · ')[0]
            const s = brand === 'OpenClaw'
              ? { backgroundColor: 'rgba(23, 23, 23, 0.9)', color: '#ef4444' }
              : { backgroundColor: 'rgba(255, 255, 255, 0.92)', color: '#2563eb' }
            return <span className="inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium" style={s}>{brand}</span>
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
function NodeActions({ room, online, lastSeen, setMsg, reloadAll }) {
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
  // design note H4 (2026-10-13): ask once per CONNECTION, not once per mount.
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
  const [infoTick, setInfoTick] = useState(0)  // bumped only by the retry timer
  useEffect(() => {
    if (!online) { askedAtRef.current = null; return }
    const stamp = String(lastSeen ?? 'online')
    if (askedAtRef.current === stamp) return
    if (askedAtRef.current !== `${stamp}#retry`) triesRef.current = 0  // new connection
    askedAtRef.current = stamp
    triesRef.current += 1
    // Staleness = "a newer connection took over" (stamp moved), NOT effect
    // cleanup — StrictMode's dev double-invoke would cancel the only ask.
    api(`/api/dashboard/nodes/${encodeURIComponent(room)}/info`)
      .then(r => {
        if (askedAtRef.current !== stamp) return
        if (r?.ok) { setMuted(!!r?.data?.muted); setMuteUnknown(false) }
        else throw new Error('info not ok')
      })
      .catch(() => {
        if (askedAtRef.current !== stamp) return
        // Out of tries: say so — never keep showing the LAST connection's state.
        if (triesRef.current >= 3) { setMuteUnknown(true); return }
        setTimeout(() => {
          if (askedAtRef.current !== stamp) return
          askedAtRef.current = `${stamp}#retry`  // same connection, ask again
          setInfoTick(n => n + 1)
        }, 4000)
      })
  }, [room, online, lastSeen, infoTick])

  // Raw POST — callers own the busy lifecycle (the wifi flow is
  // two-stage: busy must STAY 'wifi' after a successful scan; a
  // helper that auto-cleared busy killed the SSID form — caught
  // live 2026-10-07, "scan complete but no list").
  const post = (verb, json) =>
    api(`/api/dashboard/nodes/${encodeURIComponent(room)}/${verb}`, { method: 'POST', json })

  const toggleMute = async () => {
    if (muteBusy) return
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
      setMsg(`Wi-Fi applying — ${room} disconnects, reconnects with new creds; a bad password auto-rolls back.`, 'ok', 'Nodes')
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
            : muted ? 'Mic is muted — click to unmute.' : 'Software mute (flex nodes; array nodes use their physical mute button).'}>
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
  // Bug-class fix (2026-10-07 #3): the 5s poll refetches mapping and
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
  // design note H2 (2026-10-13): rows = mapped rooms ∪ connected nodes. Rows
  // used to come from room_zones alone, so a node that had connected but
  // was never mapped had NO row — a fresh install could not map its first
  // node from the React UI (regression vs page.html; also the 036 "third
  // node can't onboard" residual). Mapped rooms keep config order;
  // unmapped nodes follow in registry order.
  const mappedRooms = Object.keys(mapping?.room_zones || {})
  const unmappedRooms = (status?.rooms || []).map(r => r.room)
    .filter(r => r && !mappedRooms.includes(r))
  const roomRows = [...mappedRooms, ...unmappedRooms]
  const saveMapping = async room => {
    const zone = mapDraft?.[room]
    if (!zone) { setMsg('Pick a zone first.', 'muted', 'Room → zone mapping'); return }
    // PUT /mapping takes the FULL room_zones dict WRAPPED in a
    // room_zones key (MappingPayload contract, routes.py) — a bare
    // merged dict 422s (or worse: pre-forbid, silently wiped the
    // mapping via Pydantic's default_factory). Never unwrapped.
    const body = { room_zones: { ...(mapping?.room_zones || {}), [room]: zone } }
    try {
      await api('/api/dashboard/mapping', { method: 'PUT', json: body })
      setMapDirty(false)
      setMsg(`${room} → ${zone} saved — replies now route to this zone (live, no restart).`, 'ok', 'Room → zone mapping')
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
                const zone = mapDraft?.[room] ?? mapping?.room_zones?.[room] ?? ''
                const unmapped = !mapping?.room_zones?.[room]
                return (
                <tr key={room} className="border-t border-[var(--separator)]">
                  <td className="px-4 py-2 font-mono text-[13px]">
                    {esc(room)}
                    {unmapped && <span className="ml-2 inline-block rounded-full px-2 py-0.5 text-[11px] font-sans font-medium text-[var(--muted)] border border-[var(--separator)]">unmapped</span>}
                  </td>
                  <td className="px-4 py-2">
                    {/* Empty placeholder (041 H2): without it an unmapped row
                        DISPLAYS the first zone while its value is '' — a
                        fake selection that Save would reject as "pick a zone". */}
                    <select value={zone}
                      onChange={e => { setMapDraft({ ...mapDraft, [room]: e.target.value }); setMapDirty(true) }}
                      className={`${INPUT_CLS} w-[200px]`}>
                      {(zone === '' || !zoneNames.includes(zone)) &&
                        <option value="" disabled>{zoneNames.length ? 'pick a zone…' : 'add a zone below first'}</option>}
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
                  </td>
                  <td className="px-4 py-2">{fmtAgo(r.last_seen)}</td>
                  <td className="px-4 py-2 font-mono text-[13px]">{esc(r.fw) || '—'}</td>
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
                    <NodeActions room={r.room} online={r.online} lastSeen={r.last_seen} setMsg={setMsg} reloadAll={reloadAll} />
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
  const zoneVols = mapping?.zone_volumes || {}
  const clipVolume = mapping?.clip_volume ?? 70

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
      // player_uid is Sonos-only (041 H3) — WiiM zones send none at all.
      const body = { name: newZone.name, backend: newZone.backend, ip: newZone.ip }
      if (newZone.backend === 'sonos') body.player_uid = newZone.player_uid
      await api('/api/dashboard/zones', { method: 'POST', json: body })
      setMsg(`Zone ${newZone.name} added — applies on next bot restart.`, 'ok', 'Zones')
      setNewZone({ name: '', backend: 'sonos', ip: '', player_uid: '' })
      reloadAll()
    } catch (e) { setMsg('Zone add failed: ' + e.message, 'err', 'Zones') }
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
                  <VolSlider name={name} initial={zoneVols[name] ?? clipVolume} onCommit={saveVolume} />
                </td>
                <td className="px-4 py-2 text-right">
                  <button className={BTN_DEFAULT} onClick={() => testClip(name)}>Play test clip</button>
                </td>
              </tr>
            ))}
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
          <input className={`${INPUT_CLS} flex-1 min-w-[230px] font-mono`} placeholder="RINCON_… (sonos only)"
            disabled={newZone.backend !== 'sonos'}
            title={newZone.backend === 'sonos' ? 'Sonos RINCON player UID' : 'Sonos only — not used by WiiM zones'}
            value={newZone.player_uid} onChange={e => setNewZone({ ...newZone, player_uid: e.target.value })} />
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
  return (
    <div className="flex items-center gap-2">
      <input type="range" min="0" max="100" value={val}
        onChange={e => setVal(+e.target.value)}
        onMouseUp={() => onCommit(name, val)}
        onTouchEnd={() => onCommit(name, val)}
        onKeyUp={() => onCommit(name, val)}
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
  // 041 M2 (2026-10-13): NO default track. The image carries no board
  // identity yet (fw design note M4), so the human's pick is the ONLY thing
  // between a Flex .bin and an array node — a preselected 'node' made the
  // dangerous choice the lazy one. Push stays disabled until chosen.
  const [fwTrack, setFwTrack] = useState('')
  // After a push: which online nodes to nudge. NOTHING pre-checked — the
  // bot can't tell which line a node runs (fw_push sends version only),
  // so it never guesses; restarting the other line's node is harmless
  // (it reads its own track) but costs that room its voice for ~15s.
  const [pickup, setPickup] = useState(null)  // {version, track, rooms: {room: bool}}
  const trackLabel = t => (t === 'flex' ? 'FLEX' : 'ARRAY')

  const push = async () => {
    if (!fwFile) { setMsg('Pick a .bin to push.', 'err', 'Firmware'); return }
    if (!fwTrack) { setMsg('Pick the hardware line (track) this .bin is for.', 'err', 'Firmware'); return }
    // Filename hint: warn (never block) when the name says the other line.
    const fname = fwFile.name.toLowerCase()
    const hintsFlex = fname.includes('flex')
    // (node_esp32.bin is BOTH lines' build name — never a hint.)
    const hintsArray = /array|respeaker-xvf/.test(fname) && !hintsFlex
    const mismatch = (fwTrack === 'node' && hintsFlex) || (fwTrack === 'flex' && hintsArray)
    if (!window.confirm(
      `Push ${fwFile.name} to the ${trackLabel(fwTrack)} track?\n\n` +
      `Every ${trackLabel(fwTrack).toLowerCase()} node OTAs this image on its next check. ` +
      `Wrong line = dead node (USB rescue).` +
      (mismatch ? `\n\n⚠ The filename looks like a ${hintsFlex ? 'FLEX' : 'ARRAY'} build, but you picked ${trackLabel(fwTrack)}.` : '')))
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
            <option value="node">array</option>
            <option value="flex">flex</option>
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
    const name = sel.split('/').pop()
    if (!name || !name.endsWith('.onnx')) { setMsg('No model selected.', 'err', 'Wake word model'); return }
    try {
      await api('/api/dashboard/oww/select', { method: 'PUT', json: { model: name } })
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
                  {esc(m.name)}{m.source === 'root' ? ' (root)' : ''}
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
                  <td className="px-4 py-2">{esc(m.source)}</td>
                  <td className="px-4 py-2 font-mono text-[13px]">{((m.size || 0) / 1024).toFixed(0)} KB</td>
                  <td className="px-4 py-2 text-right">
                    <button className={BTN_DANGER} onClick={() => del(m.name, m.source)}>Delete</button>
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
// design note M1 (2026-10-13): pick-lists come from GET /tts `catalog` —
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
  p === 'elevenlabs' ? t?.model : p === 'deepgram' ? t?.deepgram_voice : t?.deepgram_flux_voice

function Voice({ tts, tuning, setMsg, setRebooting, reloadAll }) {
  const [provider, setProvider] = useState(null)
  const [model, setModel] = useState(null)
  // Bug-class fix (2026-10-07 #3): the 5s poll refetches tts state and
  // resyncing from server would clobber the user's in-progress provider
  // selection. The server is the source of truth for the FIRST sync
  // only; after that, drafts are local until saved.
  const [ttsSynced, setTtsSynced] = useState(false)
  // Flux knobs are sliders (2026-10-07): controlled, synced from the
  // server exactly ONCE (same draft law — the 5s poll must not clobber
  // a drag in progress); committed on Save via the same DOM read.
  const [fluxSpeed, setFluxSpeed] = useState(1.0)
  const [fluxExpr, setFluxExpr] = useState(1)
  // ElevenLabs flash-v2.5 knobs (2026-10-07): speed 0.7–1.2, stability
  // 0.0–1.0. Same draft law — synced once, committed on Save.
  const [elSpeed, setElSpeed] = useState(null)   // null = not set = ElevenLabs default
  const [elStab, setElStab] = useState(null)

  useEffect(() => {
    if (tts && !ttsSynced) {
      setProvider(tts.provider || 'elevenlabs')
      const prov = tts.provider || 'elevenlabs'
      setModel(savedFor(tts, prov) || tts.catalog?.[prov]?.[0] || '')
      setFluxSpeed(parseFloat(tts?.deepgram_flux_speed ?? 1.0))
      setFluxExpr(parseInt(tts?.deepgram_flux_expressivity ?? 1, 10))
      setElSpeed(tts?.elevenlabs_speed != null ? parseFloat(tts.elevenlabs_speed) : null)
      setElStab(tts?.elevenlabs_stability != null ? parseFloat(tts.elevenlabs_stability) : null)
      setTtsSynced(true)
    }
  }, [tts, ttsSynced])

  // bug 8 fix: switching provider resets the model menu to that
  // provider's own list — and (041 M1) lands on THAT provider's saved
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
      // Flux sliders ride along — live-tuned values from the controlled
      // state (committed on Save; omitting them is also fine = leave as-is).
      body.deepgram_flux_speed = parseFloat(fluxSpeed)
      body.deepgram_flux_expressivity = parseInt(fluxExpr, 10)
    }
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
                  value={fluxSpeed} onChange={e => setFluxSpeed(parseFloat(e.target.value))}
                  className="w-[140px] accent-[var(--accent)]" aria-label="Flux speed" />
                <span className="font-mono text-[12px] w-[36px] text-right">{fluxSpeed.toFixed(2)}</span>
                <span className="text-[12px] font-medium text-[var(--muted)]">expressivity</span>
                <input id="tts-flux-expressivity" type="range" min="-2" max="2" step="1"
                  value={fluxExpr} onChange={e => setFluxExpr(parseInt(e.target.value, 10))}
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
          <Field label="API key">
            <div className="flex items-center gap-2 flex-wrap flex-1">
              <input id="tts-key" type="password"
                placeholder={provider === 'elevenlabs'
                  ? (tts?.elevenlabs_key_set ? 'key set ✓' : 'paste ElevenLabs key…')
                  : (tts?.deepgram_key_set ? 'key set ✓' : 'paste Deepgram key…')}
                className={`${INPUT_CLS} flex-1 min-w-[280px]`} />
            </div>
          </Field>
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <button className={BTN_DEFAULT} onClick={async () => {
              const key = document.getElementById('tts-key').value.trim()
              if (!key) { setMsg('Nothing to save — no key entered.', 'muted', 'TTS'); return }
              const route = provider === 'elevenlabs'
                ? '/api/dashboard/tts/key/elevenlabs' : '/api/dashboard/tts/key/deepgram'
              try {
                await api(route, { method: 'PUT', json: { api_key: key } })
                setMsg('Key saved to .env — applies on next bot restart.', 'ok', 'TTS')
                reloadAll()
              } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'TTS') }
            }}>Save key</button>
            <button className={BTN_PRIMARY} onClick={saveTTS}>Save TTS</button>
          </div>
        </div>
      </Section>

      <Tuning tuning={tuning} setMsg={setMsg} setRebooting={setRebooting} />
      <OwwPanel setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
    </>
  )
}

const TUNING_FIELDS = [
  ['wake_threshold', 'Wake sensitivity', 0.30, 0.60, 0.01,
    'OWW score needed to wake (real Hey-Willow lands 0.84–1.00; TV audio fired 0.40–0.51).'],
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
  // Same bug-class fix as TTS (#3): sync from server once; drafts are
  // local until saved — the 5s poll must not clobber slider edits.
  const [tuned, setTuned] = useState(false)
  useEffect(() => {
    if (tuning && !tuned) {
      setVals({
        wake_threshold: tuning.wake_threshold,
        wake_awake_timeout: tuning.wake_awake_timeout,
        vad_confidence: tuning.vad_confidence,
        vad_min_volume: tuning.vad_min_volume,
        lull_secs: tuning.lull_secs,
        dedup_window: tuning.dedup_window,
      })
      setTuned(true)
    }
  }, [tuning, tuned])

  const save = async () => {
    const body = {}
    for (const [f] of TUNING_FIELDS) body[f] = parseFloat(vals[f])
    try {
      await api('/api/dashboard/voice-tuning', { method: 'PUT', json: body })
      setMsg('Tuning saved — applies on next bot restart.', 'ok', 'Voice tuning')
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
                <span className="text-[13px] font-medium">{label}</span>
                <span className="font-mono text-[13px]">{vals[f] ?? '—'}</span>
              </div>
              <input type="range" min={min} max={max} step={step} value={vals[f] ?? min}
                onChange={e => setVals({ ...vals, [f]: e.target.value })}
                className="accent-[var(--accent)]" aria-label={label} />
              <span className="text-[12px] text-[var(--muted)]">{hint}</span>
            </div>
          ))}
        </div>
        <div className="flex justify-end">
          <button className={BTN_PRIMARY} onClick={save}>Save tuning</button>
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
  // Per-kind URL memory (2026-10-08, 036 M-3): the two lanes live at
  // different addresses. The field carries a PER-KIND DRAFT — switching
  // kinds files the current text under the outgoing kind and loads the
  // incoming kind's draft (or empty). The old urlTouched latch never
  // reset, so one manual edit disabled swapping forever and a save
  // filed the wrong URL under the kind.
  const urlDrafts = useRef({})   // {hermes: url, openclaw: url}
  useEffect(() => {
    if (intelligence && !intSynced) {
      setIntKind(intelligence.kind || 'hermes')
      const d = { ...(intelligence.urls || {}) }
      if (intelligence.url) d[intelligence.kind || 'hermes'] = intelligence.url
      urlDrafts.current = d
      setIntSynced(true)
    }
  }, [intelligence, intSynced])
  const switchKind = (kind) => {
    const urlEl = document.getElementById('int-url')
    if (urlEl) {
      urlDrafts.current[intKind] = urlEl.value.trim()  // file the outgoing kind's draft
      urlEl.value = urlDrafts.current[kind] || ''      // always swap — per-kind context
    }
    setIntKind(kind)
  }
  return (
    <>
    <Section id="system" title="Intelligence"
      note="">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-4">
        <Field label="Provider / API URL / agent">
          <select id="int-kind" value={intKind} onChange={e => switchKind(e.target.value)}
            className={`${INPUT_CLS} w-[220px]`}>
            <option value="hermes">Hermes</option>
            <option value="openclaw">OpenClaw</option>
            <option value="claude" disabled>Claude — coming soon</option>
            <option value="openai" disabled>Custom OpenAI-compatible — coming soon</option>
          </select>
          <span className="text-[12px] font-medium text-[var(--muted)]">URL</span>
          <input id="int-url" defaultValue={intelligence?.url || ''} placeholder="http://<intelligence-host>:8642/v1"
            title="Base URL of the OpenAI-compatible chat endpoint — the lane appends /chat/completions."
            className={`${INPUT_CLS} flex-1 min-w-[280px] font-mono`} />
          <span className="text-[12px] font-medium text-[var(--muted)]">agent</span>
          <input id="int-agent" defaultValue={intelligence?.agent || ''} placeholder="lenoxvi-test"
            disabled={intKind !== 'openclaw'}
            title={intKind === 'openclaw'
              ? 'OpenClaw agent id — the model field becomes openclaw/<agent>.'
              : 'OpenClaw only — the Hermes lane addresses by URL, no agent id.'}
            className={`${INPUT_CLS} w-[180px] font-mono`} />
        </Field>
        <Field label="API key">
          <div className="flex items-center gap-2 flex-wrap flex-1">
            <input id="int-key" type="password" placeholder="paste key…"
              className={`${INPUT_CLS} flex-1 min-w-[280px]`} />
            {/* design note H1: chip reflects the kind ON SCREEN (keys_set per kind) */}
            {intelligence?.keys_set?.[intKind] ? <span className="text-[12px] text-[var(--muted)]">key set ✓</span> : null}
          </div>
        </Field>
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <button className={BTN_DEFAULT} onClick={async () => {
            const keyEl = document.getElementById('int-key')
            const key = keyEl.value.trim()
            if (!key) { setMsg('Nothing to save — no key entered.', 'muted', 'Intelligence'); return }
            try {
              // design note H1: send the kind on screen — the server writes THAT
              // kind's variable, never the one config.yaml happens to hold.
              const r = await api('/api/dashboard/intelligence/key', { method: 'PUT', json: { kind: intKind, api_key: key } })
              keyEl.value = ''  // don't leave the pasted key sitting in the DOM
              setMsg(`Key saved to ${r?.var || '.env'} — applies on next bot restart.`, 'ok', 'Intelligence')
              reloadAll()
            } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'Intelligence') }
          }}>Save key</button>
          <div className="flex gap-2 flex-wrap">
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
                  btn.disabled = false; btn.textContent = 'Test connection'
                }
              }}>Test connection</button>
            <button className={BTN_PRIMARY} onClick={async () => {
              const kind = document.getElementById('int-kind').value
              const url = document.getElementById('int-url').value.trim()
              const agent = document.getElementById('int-agent').value.trim()
              const body = { kind, url }
              if (kind === 'openclaw') body.agent = agent
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

  // design note M5 (2026-10-13): poll hygiene. One refresh cycle at a time —
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
        <Voice tts={tts} tuning={tuning} setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
        <System intelligence={intelligence} status={status} setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
      </div>
    </div>
  )
}