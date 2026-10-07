// lenoxvi dashboard — app shell. Wireframe-C structure (design note):
// single scrolling page, sticky top bar + section rail, no tabs. All
// panels visible in sequence. Brand: dark theme default, one lime
// element leads (the selected rail item / primary button), sentence
// case, no emoji. Live data via the same /api/dashboard/* routes the
// old page.html used — IDs and routes are the contract.

import { useEffect, useRef, useState } from 'react'

/* ── API layer ───────────────────────────────────────────────
   Same routes, same shapes as page.html's api(). Network errors are
   tagged .network so restart-window chatter shows quiet. */
const TOKEN_KEY = 'lenoxvi-dashboard-token'
async function api(path, opts = {}) {
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
  let body = {}
  try { body = await r.json() } catch { /* empty body */ }
  if (!r.ok) {
    // Pydantic 422s arrive as detail: [{loc, msg}] — render fields.
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

/* ── common message strip (the 10-06 contract, React-native) ──
   One strip in the sticky header. Owner label first: "Intelligence —
   Saved to config.yaml…". Restart countdown outranks net chatter. */
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
    <p aria-live="polite" className={`min-h-[22px] text-[13.5px] ${MSG_CLASSES[cls] || MSG_CLASSES.muted}`}>
      {text ? (owner ? `${owner} — ${text}` : text) : ''}
    </p>
  )
}

/* ── restart lane (same semantics as restartBot/offerRestart) ── */
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
const fmtAgo = t => {
  if (!t) return '—'
  const s = (Date.now() - new Date(t).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return Math.round(s / 60) + 'm ago'
  if (s < 86400) return Math.round(s / 3600) + 'h ago'
  return Math.round(s / 86400) + 'd ago'
}
const esc = s => String(s ?? '')

function Wordmark() {
  return (
    <span className="wm">
      <img className="wm-dark" src="./lenoxvi-horizontal-on-dark.svg" alt="LenoxVI" />
      <img className="wm-light" src="./lenoxvi-horizontal-on-light.svg" alt="LenoxVI" />
    </span>
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
          {note ? <p className="text-[14px] leading-5 text-[var(--muted)] m-0 max-w-[640px]">{note}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

/* ── rail + sticky bar (wireframe C) ────────────────────────── */
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

/* ── Overview section ──────────────────────────────────────── */
function Overview({ status, msg, setMsg, setRebooting, rebooting }) {
  return (
    <Section id="overview" title="Overview"
      note="Mic-room → speaker-zone voice assistant. LAN-only; no login by design. Config edits save to config.yaml and apply on the next bot restart."
      action={
        <button
          className="rounded-full border border-[var(--danger)] text-[var(--danger)] px-3 py-1.5 text-[14px] font-medium bg-transparent cursor-pointer disabled:opacity-50"
          disabled={rebooting}
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
        <Stat label="Active room">{esc(status?.active_room) || '—'}</Stat>
        <Stat label="Last wake">
          {status?.last_wake ? `${esc(status.last_wake.room)}, ${fmtAgo(status.last_wake.time)}` : 'none since boot'}
        </Stat>
        <Stat label="Version"><span className="font-mono text-[13px]">{esc(status?.version) || '…'}</span></Stat>
        <Stat label="Intelligence"><span className="font-mono text-[13px]">{esc(status?.intelligence) || '—'}</span></Stat>
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

/* ── Rooms section: mapping + nodes + zones ─────────────────── */
function Rooms({ status, nodes, zones, mapping, reloadAll }) {
  return (
    <>
      <Section id="rooms" title="Room → zone mapping"
        note="Pick a speaker zone for each mic room. Rooms appear once their node has connected at least once.">
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
                <th className="text-left px-4 py-2">Room (mic node)</th>
                <th className="text-left px-4 py-2">Zone</th>
                <th className="text-left px-4 py-2">Volume</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(mapping?.room_zones || {}).map(([room, zone]) => (
                <tr key={room} className="border-t border-[var(--separator)]">
                  <td className="px-4 py-2 font-mono text-[13px]">{esc(room)}</td>
                  <td className="px-4 py-2">{esc(zone)}</td>
                  <td className="px-4 py-2">{esc(mapping?.zone_volumes?.[zone] ?? mapping?.clip_volume ?? '—')}</td>
                </tr>
              ))}
              {Object.keys(mapping?.room_zones || {}).length === 0 &&
                <tr><td className="px-4 py-2 text-[var(--muted)]" colSpan="3">No rooms mapped yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Nodes"
        note="Firmware 1.1.0+ required for restart / rename / Wi-Fi over the air. Older firmware shows status only.">
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
                <th className="text-left px-4 py-2">Node</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Last seen</th>
                <th className="text-left px-4 py-2">Session</th>
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
                  <td className="px-4 py-2">{r.last_seen ? fmtAgo(r.last_seen) : '—'}</td>
                  <td className="px-4 py-2 font-mono text-[13px]">{esc(r.session) || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Zones"
        note="Sonos zones use the audioClip overlay (tested on S2 Port/Amp). WiiM zones play as main media — music stops while she talks.">
        <div className="rounded-2xl bg-[var(--surface)] overflow-hidden">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-[12px] uppercase tracking-wide text-[var(--muted)]">
                <th className="text-left px-4 py-2">Name</th>
                <th className="text-left px-4 py-2">Backend</th>
                <th className="text-left px-4 py-2">Volume</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(zones || {}).map(([name, z]) => (
                <tr key={name} className="border-t border-[var(--separator)]">
                  <td className="px-4 py-2">{esc(name)}</td>
                  <td className="px-4 py-2">
                    <span className="inline-block rounded-full bg-[var(--default)] px-2.5 py-0.5 text-[12px] font-medium">
                      {esc(z?.backend) || '—'}
                    </span>
                  </td>
                  <td className="px-4 py-2">{z?.volume != null ? esc(z.volume) : (mapping?.zone_volumes?.[name] ?? mapping?.clip_volume ?? '—')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  )
}

/* ── Voice section: TTS card + tuning + OWW ────────────────── */
function Voice({ tts, setMsg, setRebooting, reloadAll }) {
  return (
    <>
      <Section id="voice" title="TTS"
        note="One provider per install. Models: flash v2.5 (fast) or v4 turbo (expressive). Voice id is account data. API keys never render back after save.">
        <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-3 max-w-[640px]">
          <Field label="Provider">
            <select id="tts-provider" defaultValue={tts?.provider || 'elevenlabs'}
              className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[220px]">
              <option value="elevenlabs">ElevenLabs</option>
              <option value="deepgram">Deepgram</option>
              <option value="deepgram-flux">Deepgram Flux</option>
            </select>
          </Field>
          <Field label="Model">
            <select id="tts-model" defaultValue={tts?.model || 'eleven_flash_v2_5'}
              className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[220px]">
              <option value="eleven_flash_v2_5">flash v2.5</option>
              <option value="eleven_v4_turbo">v4 turbo</option>
            </select>
          </Field>
          <Field label="Voice id">
            <input id="tts-voice" defaultValue={tts?.voice_id || ''} placeholder="fq1S…"
              className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[280px] font-mono" />
          </Field>
          <Field label="API key">
            <div className="flex items-center gap-2 flex-wrap">
              <input id="tts-key" type="password" placeholder={tts?.elevenlabs_key_set ? 'key set ✓' : 'paste key…'}
                className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[280px]" />
              {tts?.elevenlabs_key_set ? <span className="text-[12px] text-[var(--muted)]">key set ✓</span> : null}
              <SaveBtn onClick={async () => {
                const key = document.getElementById('tts-key').value.trim()
                if (!key) { setMsg('Nothing to save — no key entered.', 'muted', 'TTS'); return }
                try {
                  await api('/api/dashboard/tts/key/elevenlabs', { method: 'PUT', json: { api_key: key } })
                  setMsg('Key saved to .env — applies on next bot restart.', 'ok', 'TTS')
                  reloadAll()
                } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'TTS') }
              }}>Save key</SaveBtn>
            </div>
          </Field>
          <SaveBtn primary onClick={async () => {
            const provider = document.getElementById('tts-provider').value
            const model = document.getElementById('tts-model').value
            const voice_id = document.getElementById('tts-voice').value.trim()
            if (provider === 'elevenlabs' && !voice_id) {
              setMsg("Voice ID can't be empty — TTS breaks with no voice.", 'err', 'TTS'); return
            }
            try {
              await api('/api/dashboard/tts', { method: 'PUT', json: { provider, voice_id, model } })
              setMsg('Saved to config.yaml — applies on next bot restart.', 'ok', 'TTS')
              reloadAll()
              offerRestart(setMsg, setRebooting)
            } catch (e) { setMsg('Save failed: ' + e.message, 'err', 'TTS') }
          }}>Save TTS</SaveBtn>
        </div>
      </Section>
    </>
  )
}

function Field({ label, children }) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[12px] font-medium text-[var(--muted)] min-w-[92px]">{label}</span>
      {children}
    </div>
  )
}

function SaveBtn({ children, onClick, primary }) {
  return (
    <button onClick={onClick}
      className={`rounded-full px-3 py-1.5 text-[14px] font-medium cursor-pointer
        ${primary
          ? 'bg-[var(--accent)] text-[var(--accent-foreground)]'
          : 'bg-[var(--default)] text-[var(--default-foreground)]'}`}>
      {children}
    </button>
  )
}

/* ── System section: Intelligence + Firmware ───────────────── */
function System({ intelligence, setMsg, setRebooting, reloadAll }) {
  return (
    <Section id="system" title="Intelligence"
      note="Hermes runs this rig in production. OpenClaw answers from your own agent over the LAN. Claude and custom OpenAI-compatible endpoints are designed-for.">
      <div className="rounded-2xl bg-[var(--surface)] p-4 grid gap-3 max-w-[640px]">
        <Field label="Provider">
          <select id="int-kind" defaultValue={intelligence?.kind || 'hermes'}
            className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[220px]">
            <option value="hermes">Hermes</option>
            <option value="openclaw">OpenClaw</option>
            <option value="claude" disabled>Claude — coming soon</option>
            <option value="openai" disabled>Custom OpenAI-compatible — coming soon</option>
          </select>
        </Field>
        <Field label="API URL">
          <input id="int-url" defaultValue={intelligence?.url || ''} placeholder="http://<brain-host>:8642/v1"
            title="Base URL of the OpenAI-compatible chat endpoint — the lane appends /chat/completions."
            className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[320px] font-mono" />
        </Field>
        <Field label="Agent">
          <input id="int-agent" defaultValue={intelligence?.agent || ''} placeholder="lenoxvi-test"
            title="OpenClaw agent id — the model field becomes openclaw/<agent>."
            className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[220px] font-mono" />
        </Field>
        <Field label="API key">
          <div className="flex items-center gap-2 flex-wrap">
            <input id="int-key" type="password" placeholder={intelligence?.key_set ? 'key set ✓' : 'paste key…'}
              className="rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2 w-[280px]" />
            {intelligence?.key_set ? <span className="text-[12px] text-[var(--muted)]">key set ✓</span> : null}
            <SaveBtn onClick={async () => {
              const key = document.getElementById('int-key').value.trim()
              if (!key) { setMsg('Nothing to save — no key entered.', 'muted', 'Intelligence'); return }
              try {
                await api('/api/dashboard/intelligence/key', { method: 'PUT', json: { api_key: key } })
                setMsg('Key saved to .env — applies on next bot restart.', 'ok', 'Intelligence')
                reloadAll()
              } catch (e) { setMsg('Key save failed: ' + e.message, 'err', 'Intelligence') }
            }}>Save key</SaveBtn>
          </div>
        </Field>
        <div className="flex gap-2 flex-wrap">
          <SaveBtn primary onClick={async () => {
            const kind = document.getElementById('int-kind').value
            const url = document.getElementById('int-url').value.trim()
            const agent = document.getElementById('int-agent').value.trim()
            const body = { kind, url }
            if (kind === 'openclaw') body.agent = agent
            try {
              await api('/api/dashboard/intelligence', { method: 'PUT', json: body })
              setMsg('Saved to config.yaml — applies on next bot restart.', 'ok', 'Intelligence')
              reloadAll()
              offerRestart(setMsg, setRebooting)
            } catch (e) { setMsg('Save failed: ' + e.message, 'err', 'Intelligence') }
          }}>Save</SaveBtn>
          <button id="btn-int-test"
            className="rounded-full bg-[var(--default)] text-[var(--default-foreground)] px-3 py-1.5 text-[14px] font-medium cursor-pointer disabled:opacity-50"
            onClick={async (ev) => {
              const btn = ev.currentTarget
              btn.disabled = true; btn.textContent = 'Testing…'
              try {
                const r = await api('/api/dashboard/intelligence/test', { method: 'POST', json: {} })
                setMsg(`✓ ${r.label || 'ok'} — ${r.latency_ms ?? '?'} ms`, 'ok', 'Intelligence')
              } catch (e) {
                setMsg('Test failed: ' + e.message, 'err', 'Intelligence')
              } finally {
                btn.disabled = false; btn.textContent = 'Test connection'
              }
            }}>Test connection</button>
        </div>
      </div>
    </Section>
  )
}

export default function App() {
  const [status, setStatus] = useState(null)
  const [mapping, setMapping] = useState(null)
  const [zones, setZones] = useState(null)
  const [tts, setTts] = useState(null)
  const [intelligence, setIntelligence] = useState(null)
  const [msg, setMsg] = useMsg()
  const [rebooting, setRebooting] = useState(false)
  const active = useActiveSection()

  const reloadAll = async () => {
    const jobs = [
      api('/api/dashboard/status').then(setStatus).catch(e => { if (!e.network) setMsg('status unavailable: ' + e.message, 'err', 'Status') }),
      api('/api/dashboard/mapping').then(m => { setMapping(m); setZones(m.zones || []) }).catch(() => {}),
      api('/api/dashboard/tts').then(setTts).catch(() => {}),
      api('/api/dashboard/intelligence').then(setIntelligence).catch(() => {}),
    ]
    await Promise.allSettled(jobs)
  }

  useEffect(() => {
    reloadAll()
    const t = setInterval(reloadAll, 5000)
    const h = () => reloadAll()
    window.addEventListener('lenoxvi:reload-all', h)
    return () => { clearInterval(t); window.removeEventListener('lenoxvi:reload-all', h) }
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

        <Overview status={status} msg={msg} setMsg={setMsg} setRebooting={setRebooting} rebooting={rebooting} />
        <Rooms status={status} mapping={mapping} zones={zones} />
        <Voice tts={tts} setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
        <System intelligence={intelligence} setMsg={setMsg} setRebooting={setRebooting} reloadAll={reloadAll} />
      </div>
    </div>
  )
}