# Changelog

All notable changes to lenoxvi are documented here.

Rig-level versioning only — firmware carries its own `project(VERSION)` in its
CMakeLists, bumped together with the server manifest on every OTA deploy
(see notes notes/v2/016). The two version streams are independent.

## [Unreleased]

## [0.6.2] — 2026-10-13 — Dashboard hardening + the naming law

The Opus 5.5 dashboard review (design note) closed end to end — every
HIGH and MED plus L3 — along with two bugs nobody had flagged, the
"one lane, one variable" naming law, and the note-028 smalls. Every fix
ships with suite pins that fail against the pre-fix code (red-probed);
suite 162 → 188 checks. Live-verified on the house rig after each deploy.

### Upgrading from 0.6.1
- **Config section `brain:` is now `intelligence:`** — rename it in
  `config.yaml` (no fallback reads the old name). Env overrides follow:
  `LVI_INTELLIGENCE_KIND` / `LVI_INTELLIGENCE_AGENT` / `LVI_INTELLIGENCE_MAX_TOKENS`.
- **`LVI_BRAIN_KEY` is gone.** Each lane reads exactly one variable:
  Hermes → `LVI_HERMES_KEY`, OpenClaw → `LVI_OPENCLAW_KEY`. No cross-kind
  fallback — a missing key is one clear error line, never a wrong-token 401.
- **API contract tightened** (scripts calling the dashboard API):
  `PUT /intelligence/key` requires `kind`; `POST /firmware/upload`
  requires `track` (no silent array default); every write payload
  rejects unknown fields (422); `POST /nodes/{room}/mute` requires an
  explicit `mute`; `/mapping` returns Sonos display names only with
  `?names=1`.

### Added
- **Voice catalog from the server** — `GET /tts` returns `catalog`
  (2 ElevenLabs models, 40 Aura, 36 Flux); the dashboard's pick-lists
  are built from it. All 76 Deepgram voices are now pickable (the UI
  listed only 28 — 48 were unreachable).
- **Per-kind Intelligence URL memory** — switching provider swaps in
  that lane's remembered URL; the OpenClaw lane can't be pointed at the
  Hermes api_server by accident.
- **Unmapped nodes are mappable** — the mapping table lists every
  connected node with an "unmapped" chip, so a fresh install (or a
  third node) can be mapped from the dashboard.
- **Dashboard token prompt** — with `LVI_DASHBOARD_TOKEN` set, a save
  prompts once, remembers the token, and retries (previously every save
  just failed).
- **Firmware upload safety** — no default track; confirm names the
  line and warns when the filename hints at the other one; after a push,
  pick which nodes restart (nothing pre-checked).
- `deploy_firmware.sh --track flex`, plus a version-from-image gate that
  refuses to ship on a manifest/binary mismatch.
- `effective_volumes` on `/mapping` — what each zone actually plays at.

### Fixed — dashboard
- Intelligence key save follows the provider on screen, not the one in
  config (the UI could write the OpenClaw token into `LVI_HERMES_KEY`).
- Adding a WiiM zone 422'd every time (`player_uid` was required for all
  backends) — now Sonos-only.
- Mute button after a node reboot: re-reads the node's state per
  connection; unknown state shows "Mute?" and a click mutes.
- Restart / Wi-Fi routes returned 200 when the node said no — now 502;
  the password is never echoed.
- Misspelled fields silently no-op'd (a typo'd tuning key rewrote
  `wake.threshold`); an empty mute body muted the node.
- Deepgram TTS first save 422'd (the voice menu held the ElevenLabs model).
- Tuning saves sent all six knobs and reverted edits made elsewhere —
  now only touched knobs.
- Polling: one refresh cycle at a time, hidden tabs skip, and the 5 s
  poll no longer runs Sonos lookups (it was probing the WiiM too).
- One volume default (35) for both output backends and the dashboard —
  Sonos defaulted to 70 when config omitted `clip_volume`; WiiM sliders
  showed the Sonos global.
- `fw=None` in the Nodes table (fw_push / offer-tee race); mute errors
  and tooltip tell the per-line truth (array = physical button).

### Fixed — security / robustness
- **Form-upload 422s returned 500** and **key-route 422s logged the full
  API key to the journal** — pipecat's app-wide validation handler read
  the request body and logged rejected values. Dashboard routes now
  answer their own 422s (no body read, rejected values stripped). Journal
  audit back to 2026-10-03: no real key was ever logged.
- Wake barge-in interrupt never ran (async callback was never awaited).
- Note-028 smalls: config anchored to the repo root (cwd-proof), clip
  server can't half-bind, TTS secrets checked at construction, zone
  delete is one atomic multi-section write.
- `firmware/flex/` gitignored (flex images bake fallback Wi-Fi creds).

## [0.6.1] — 2026-10-07 — Software mute + ElevenLabs voice shaping

Node mute becomes a first-class control and the flash-v2.5 voice gets
tunable character — both live-proven the night they shipped.

### Added — software mic mute + per-model OTA
- Dashboard **Mute button** (Nodes card, per row): zeroes the mic at
  the node — same effect as the array board's physical button. The
  flex board has no physical mute, so this is software: flex fw 1.1.0
  adds the `set_mute` command (toggle or explicit) and reports
  `muted` in `get_info`; the mic is silenced in the same place the
  turn lock silences it. Button state is confirmed from the node's
  ack, never optimistic, and re-derived from the node on every page
  load (new `GET /nodes/{room}/info`) — a refresh can't lie about a
  muted mic. Online-gated; firmware 1.1.0+.
- **Per-model OTA tracks** (design note follow-up): `/firmware/{track}/
  version` + `/firmware/{track}/{bin}` routes; the flex node reads
  `/firmware/flex/version`, the array node's legacy URLs stay
  byte-identical, and a track without a deployed manifest 404s = no
  update (safe). First flex OTA shipped fw 1.1.0 over the air to
  room-a — no bench, no cable.
- **ElevenLabs flash-v2.5 speed + stability**: config keys
  `tts.elevenlabs_speed` (0.7–1.2) and `tts.elevenlabs_stability`
  (0.0–1.0) with dashboard sliders (draft law — synced once, local
  until saved). Absent config = API defaults, never a made-up value.
  The v3/v4 dialogue lane deliberately gets no knobs (ElevenLabs
  guidance: the model drives expressivity there).

### Fixed — dashboard
- Discover's "Fill into add form" silently dropped the Sonos UID —
  it read `d.player_uid` but the route returns `uid`. Now fills the
  complete zone (name, backend, IP, RINCON) in one click.
- Mute toggle double-fire race — rapid clicks during ack latency
  could flip the node twice; dedicated `muteBusy` flag + ask-once
  ref guard on the mount-time info fetch.

## [0.6.0] — 2026-10-07 — HeroUI dashboard + node two (Flex)

The dashboard becomes a React app, and lenoxvi gets its second mic node.
Both live-proven on the house rig the same day they shipped.

### Added — HeroUI dashboard (replaces legacy page.html)
- React + Vite + Tailwind v4 + HeroUI v3 single-page dashboard
  (`dashboard-ui/`), built on the LenoxVI design kit; prebuilt `dist/`
  committed so a fresh clone needs zero node toolchain. Wireframe-C
  single page: Overview, Rooms, Nodes, Zones, Voice (TTS + tuning),
  Wake word model, Intelligence, Firmware (design note).
- Common message strip — every section's status/error/success renders
  in one header area with explicit owner labels (no more int-msg debug
  output in the strip).
- Node row actions (inline forms, no `prompt()`): Restart (confirm-
  gated), Rename (pattern-validated), Wi-Fi (scan → RSSI-sorted SSID
  list → password → apply; auto-rollback on bad password). All
  online-gated; firmware 1.1.0+ required.
- Firmware upload panel ported (multipart POST, version read from the
  image's ESP app descriptor, atomic manifest write, optional
  per-node restart nudge) — the last legacy-page feature.
- Zones: add-zone form with Sonos/WiiM backend pick, Discover Sonos
  on LAN, per-zone volume sliders (live override — next clip, no
  restart), per-row test + remove.
- Statusbar duplicate-reconnect fix: last-good facts are kept during
  restart countdown; NET chatter no longer duplicates the line.
- Dashboard suite pins the new contracts (9b series through 9b11;
  mapping-wipe guards included).

### Fixed — dashboard correctness (audit + live finds)
- Mapping wipe: the React save sent a BARE merged dict (no
  `room_zones` wrapper) — Pydantic's `default_factory` silently
  turned it into `{}` and the route wiped all mappings; three real
  wipes hit production config (2 user saves, 1 test harness). Fix at
  three layers: UI wraps the body; `MappingPayload` is
  `extra="forbid"` (unwrapped = loud 422); the route refuses an
  explicit empty `room_zones` (400). Suite pins both guards.
- Room mappings are LIVE — the bot reads `room_zones` per reply and
  the dashboard write clears the config cache in-process; the
  "applies on next bot restart" message was stale copy. UI now says
  the truth.
- Kind-aware brain key resolution: the hermes lane auths with
  `LVI_HERMES_KEY`, the openclaw lane with `LVI_BRAIN_KEY` — a kind
  flip can no longer cross tokens (bug-7 landmine; suite 9b11 pins
  both lanes). Same fix in the intelligence probe.
- Intelligence test button renders the body's `ok` field, never the
  HTTP status (the route answers 200 with `{ok: false}` on failure —
  caught live when the strip said "✓ ok" while the backend logged
  401).
- Supersede ack rate-limited: one "Hold on — switching." per wake
  window (`brain.supersede_ack_gap`, default 45s, re-armed on fresh
  turns) — a two-person math session triple-fired it in 10s.
- TTS provider switch sent a stale elevenlabs model string under
  deepgram (the Pydantic pattern correctly 422'd it) — provider-
  aware body restored; Flux speed/expressivity UI restored.

### Added — node two: reSpeaker Flex (room-a)
- Second mic node live end-to-end: wake score 1.00, whisper
  transcription, brain reply, zone playback — multi-turn with
  conversation memory, from across the room (2026-10-07 21:48).
- New firmware track `flex-xvf3800` (private repo `lenoxvi-firmware`,
  tagged `v1.0.0`): inthost XMOS image architecture — the XVF3800 is
  the I2S clock master (formatBCE inthost v1.0.7), ESP32 on the
  proven slave config. Stock Flex images + XIAO-as-master falsified
  at 48k (no APLL on S3) and 16k (stable bus, garbage data).
- Per-model OTA track: flex builds address `/firmware/flex/version`
  (404 today = no update, safe); unset track stays byte-identical to
  the legacy URL — cross-flashing is impossible.
- Dashboard Firmware column self-identifies the node model (1.0.0 =
  flex, 1.1.3 = array node).

### Docs
- README full rewrite: dashboard surface, WiiM backend, turn-taking,
  Intelligence lanes (rides to this tag per "leave for next tag").

## [0.5.0] — 2026-10-05 — Intelligence + turn-taking hardening

Three feature arcs in one day, live-proven on the house rig.

### Added — Intelligence lane (design note)
- `brain.kind: openclaw` — voice turns answered by an OpenClaw Gateway
  agent (`model: openclaw/<agent>`, sticky rooms via
  `x-openclaw-session-key`, Hermes-only `conversation` field omitted).
  Requires `gateway.http.endpoints.chatCompletions.enabled: true` on the
  OpenClaw side (off by default, 2026.9.8).
- Key resolution `LVI_BRAIN_KEY` → `LVI_HERMES_KEY` fallback; a missing key
  no longer bricks construction (audit finding #4 class) — the turn fails
  with a clear message and the dashboard reports it.
- Dashboard "Intelligence" section (Settings): provider picker
  (Hermes / OpenClaw; Claude + custom OpenAI-compatible designed-for),
  per-kind fields, key set ✓, Test-connection button (health + one
  minimal completion, latency), rig-facts line.
- Reasoning-preamble meter: OpenClaw/GLM planning text can merge into
  `content` (measured 2/8 spike calls) — logged with a counter, never
  silently stripped.

### Added — Turn-taking (design note)
- B3 epoch turns: wake-word speech runs as a background task with
  generation epochs; a new wake **supersedes** — old reply discarded at
  every await, no history write, spoken ack "Hold on — switching."
- Wake-word barge-in: 2 consecutive OWW frames over threshold interrupt
  the sounding clip (`wake.barge_in`, default false; live: true) — both
  backends `stop_current_clip()` + `_speech_epoch` bump.
- Fix A load-anchored timers: speech/duck timers anchor at clip-load
  with epoch guard + generation-time fallback — the ring LED no longer
  desyncs from the speaker on long brain calls.
- Echo-gate relax: confirmed wake or barge-in relaxes the duration gate
  to a ~0.5s tail; text containment stays armed.

### Fixed
- 84-minute deaf outage — `process_frame`'s final passthrough lost in B3
  churn; suite gap closed with 9b9 (agent-eats-frames red line).
- The 20:56 "stub-brain" incident — bot.py dispatch only knew
  `== "hermes"`, so `kind: openclaw` fell to the echo dummy; regression
  check added (construction dispatch AST).
- Dashboard 422s render as `[object Object]` — Pydantic detail arrays now
  stringify with fields; first hit: empty `agent` on Intelligence save.
- API URL hidden for Hermes — provider-agnostic plumbing now shows for
  every kind (public-flip: a stranger's Hermes lives at their URL).

## [0.4.0] — 2026-10-04

The config-editable dashboard release. The dashboard stops being
read-only-plus-sliders and becomes the primary way to operate the rig:
TTS provider/voice/model, Flux's speed + expressivity, and the six
pipecat ear-tuning knobs all live in the GUI now, with a restart button
and a save→apply flow that never forces a tab hop.

### Added
- **TTS settings panel** — provider, voice and model configurable from
  the dashboard; ElevenLabs voice ID + model (eleven_flash_v2_5 / v2),
  keys stored in `.env` via a dedicated key route (set/unset status
  only — never echoed back, never in config.yaml), settings in
  config.yaml
- **Provider dropdown with three backends** — ElevenLabs, Deepgram
  Aura-2 (40 cloud voices, model IS the voice), Deepgram Flux
  (voice-agent-first, voices incl. rufus/hannah/bree); provider-aware
  routes with 422 on unknown voices; per-clip sample-rate reset on
  Sonos (24k↔16k chipmunk prevention)
- **Flux speed + expressivity knobs** — sliders (0.5–1.5× in 0.05
  steps; expressivity -2..2), server-side range validation, partial
  updates preserve tuned values
- **Voice Tuning panel** — the six ear-tuning knobs as sliders: wake
  sensitivity (0.30–0.60), awake timeout (5–60s), VAD confidence
  (0.30–0.90), VAD min volume (0.10–0.90), turn-end lull (0.2–2.0s),
  echo dedup window (2–30s). Writes MERGE per section — brain.fillers,
  max_tokens, wake.enabled can never be clobbered by a tuning save
- **Restart bot button** — POST /api/dashboard/restart via a detached
  systemd transient unit (survives the bot's own death); UI polls
  uptime reset, auto-reloads panels; honest error surfaces (no silent
  catch)
- **Post-save "restart now?" popup** — every Voice-tab save offers a
  one-click apply; cancel keeps apply-later
- **Three-tab dashboard** — Zones (mapping/nodes/zones) / Voice (TTS
  + tuning) / Settings (rig facts, bot control, firmware)
- **Rig facts bar** atop Settings — Bot Version, TTS provider, STT
  model, OWW model, all resolved through the same config > env >
  default chains the consumers use (moves with the host, never static)
- **Live version in statusbar** — git describe at boot, updates every
  commit
- **brain.max_tokens: 300** — guardrail against runaway brain replies
  (config-gated, default 300)

### Fixed
- Key-save form natively submitted — page reloaded to Zones mid-save,
  masking the 200 OK
- Restart handler silently swallowed fetch errors — now retries once
  and surfaces real failures
- Statusbar selector lost its `#` — dumped raw template literal into
  the header

## [0.3.5] — 2026-10-04

The second-output-backend release. The WiiM Pro arrives and becomes a
first-class voice lane in one afternoon — plus the session-visibility fix
that makes every voice conversation auditable in the Hermes dashboard.

### Added
- **WiiM output backend** (`core/wiim_output.py`) — interrupt-no-restore
  reply playback on WiiM/LinkPlay streamers, ratified as the affordable
  dedicated AI-out lane (Sonos stays the full-integration lane). Same
  FrameProcessor contract as SonosOutput (speaking LEDs, echo gate,
  wake-gate speech hints), no duck holder — the interrupt IS the duck.
  Volume snapshot → set → play → restore around every clip. Arrival
  verify first (design note correction): `playPromptUrl` is a STUB on
  current firmware (Linkplay 4.8.827634) — acks OK, never fetches — so
  replies play via `setPlayerCmd:play` as main media. Music stops when
  she speaks, nothing restores (client-owned sources are unresumable).
  `X-Hermes-Session-Key` rides every brain call: sticky rooms merge into
  ONE long-lived dashboard session per room, fresh pipelines get one
  session per connection — both visible and auditable in the Hermes
  admin dash (previously voice sessions were unkeyed and invisible there)
- **Per-zone backend dispatch** — a room mapped to a `wiim:` zone gets
  WiiMOutput, `sonos:` zones keep the audioClip overlay path. One merged
  zone namespace in the dashboard with backend tags; Add-zone takes a
  backend selector (WiiM zones need no player_uid); delete / volume
  slider / test tone all dispatch on backend. Adding a zone to a backend
  implies enabling that backend

### Fixed
- **WiiM hardware-fade compensation** — the Pro ramps volume in over the
  first ~1s of main-media playback and ramps out at the end; short
  replies ramped down before finishing ("quiet, ramps up, ramps down" —
  the owner's ear). No firmware knob exists, so reply clips carry 0.25s
  lead-in silence + 0.35s tail. Sonos lane untouched
- **Voice sessions invisible in the Hermes dashboard** — see
  X-Hermes-Session-Key above
- Three WiiM-backend boot bugs from the first deploy: hallucinated
  `cfg_env` API (killed every pipeline construction — mic wouldn't wake),
  missing `enabled: true` on dashboard-added wiim zones (silent disabled
  backend), and the shared clip server never started when no SonosOutput
  existed in the pipeline (LEDs cycled, zero audio). Deploy gate added:
  host-venv import smoke before every restart touching core/

### Changed
- **Dashboard zone UX** — native-name prefill from discovery (no more
  lowercase mangling), no parenthetical echo when key = native name,
  backend column without decorative markers (the owner rulings, all
  screenshot-caught)

## [0.3.4] — 2026-10-04

The zone-tuning release. The matrix switch leaves the audio path and two
config fossils follow it out: zone keys now match the players' native Sonos
names, and volume becomes a by-ear dashboard control instead of a
config-file number.

### Added
- **Per-zone volume sliders** — a Volume column in the dashboard zones table
  with a slider (0–100) per zone and a live numeric readout. Two layers, one
  write: durable `sonos.zone_volumes` in config.yaml PLUS a live in-memory
  override in `sonos_output`, so a slider change applies to the very NEXT
  clip — no bot restart. Volume tuning is a by-ear loop; the restart
  contract would make the feature useless. Resolution: live override >
  config per-zone > global `sonos.clip_volume`; sliding back to the global
  value removes the override (config stays minimal). All three play sites
  honor it — replies, duck-holder, and the test tone, so the tone IS the
  tuning instrument. New guarded `PUT /zones/{name}/volume`; mapping GET
  carries `zone_volumes` + `clip_volume`
- **`brain.zone` documented as the conversation label prefix** — fresh-mode
  pipelines mint `voice-<zone>-<id>` from it; with native-name zone keys the
  labels read sensibly in Hermes conversation history

### Fixed
- **Test tone silent on zone keys containing spaces** — the tone's filename
  embedded the raw zone key (`test-Sonos Port-….mp3`), and Sonos's audioClip
  fetcher fails silently on unencoded spaces: the tone never sounded on
  'Sonos Port' while 'living_room' worked, and replies dodged it by
  generating space-free names. Slugify the filename at the route and
  percent-encode the path in `url_for` — no future zone name can break a
  clip fetch

### Changed
- **Zone keys = native Sonos player names** — 'Sonos Port' → 'Dining Room',
  'living_room' → 'Living Room' across config (zones, default_zone,
  room_zones, `brain.zone` label prefix, zone_volumes overrides). The
  dashboard already displayed native names; now the keys match them, so a
  user reading Sonos's app and the dashboard sees one name per player.
  Keyed lookups all the way down (room_zones → zones → play), so a
  consistent rename is safe, and the slug fix means spaces in keys are fine
- **`sonos.clip_volume` 70 → 35** — recalibrated for the matrix-free direct
  path (was measured through the matrix switch, design note); the owner
  ear-tuned it the same day

## [0.3.3] — 2026-10-03

The rename + voice-UX release: the project takes its public name. One silent-failure bug found by a live test,
one lying verify found by a deliberate wrong-password rollback test, and two
conversation features shipped and live-proven the same day.

### Added
- **Per-node session modes (fresh | sticky)** — dashboard checkbox per node.
  Sticky rooms keep conversation memory across node reconnects AND bot
  restarts: history stash `data/sessions.json` (atomic write after every
  successful turn — crash-safe, no teardown hook; gitignored) restored on
  pipeline construction, plus the stable conversation label `voice-<room>`
  so Hermes server-side conversation continuity applies. Fresh is the
  privacy default — unmapped rooms start every session clean. New guarded
  `POST /nodes/{room}/session-mode`; status carries `session_modes`.
  Ratified spec: **sticky = stash + server-side continuity** (user ruling
  2026-10-03). Live-proven: a "what's the last thing I asked you" test
  across a node restart AND a bot restart answered correctly from restored
  history (`01619f5`)
- **Tiered fillers** — one spoken line per latency tier so a slow brain turn
  no longer goes silent after the first ack: tier 1 acks at 3s (the classic
  pool), tier 2 reports progress at 12s, tier 3 reassures at 25s, then she
  waits quietly. Config `brain.fillers` is a tiered list; a flat string list
  still parses as a single ack tier at `brain.filler_after_secs` (legacy
  configs unchanged). All lines <2s spoken; tier 2/3 lines meet the ≥3-word
  echo-containment contract. Live-proven same day: all three tiers fired in
  sequence on a 48s turn, rotation never repeated a line across the session
  (`31ef583`)
- **Wake-window forensics** — an AWAKE window that closes without VAD now
  logs delivered-frame count, peak amplitude, echo-gate state, and
  whether VAD ever fired, so the next silent failure names its own cause
  (frames=0 = stalled track; low peak = below-threshold speech;
  echo_blocked = a Sonos clip swallowed it). The 2026-10-03 09:00 silent
  failure (wake 0.98, then 7s of nothing, undiagnosable from the journal)
  motivated it (`6df33ee`)

### Fixed
- **pc_id capture was a silent no-op** (H1 regression) — `SmallWebRTCTransport`
  is a facade holding `._client`, not `._webrtc_connection`; the runner args
  carry the connection directly. Every wake since 10-01 had recorded
  `room: null` in the status API (`6df33ee`)
- **Wi-Fi `set_wifi` lying verify** (firmware 1.1.1) — NVS writes never
  reached the live WiFi driver (config only pushed at boot), so a wrong
  password re-associated on the OLD creds and acked "connected" in 1.2s —
  rollback unreachable. 1.1.1 pushes creds into the driver on `set_wifi`
  AND after rollback restore; a wrong password now produces 3 auth strikes
  → auto-restore → rejoin (journal-proven: 44s dark, node healthy after).
  Found by the deliberate wrong-password rollback test; incident recovery
  via USB full-flash + NVS region erase (see design note)
- **Rename sweep coherence** — project renamed across 25 files: the env
  prefix, the systemd unit example and the GitHub repo follow the new name; host dirs/venv/unit renamed
  (lenoxvi-bot.service active); firmware factory default room name = "lenoxvi"
  (fresh flashes boot as lenoxvi; deployed nodes keep their dashboard names —
  user spec). Node renamed back to bedroom-mic after a mistaken rename
  (recorded as the cautionary tale it is)

### Known issues
- Node command acks that fire while the node is off-network (rollback
  notifications) never reach the dashboard — the node reappearing online
  is the signal. Rider queued: queue the ack for delivery after rejoin
- The 2026-10-03 17:45–17:52 disconnect loop (22s session deaths,
  media-stream errors) occurred only on a bot process that booted while
  the node was mid-restart; the fresh process at 18:01 held one clean
  session with zero errors. Watch for recurrence; forensics now in place

## [0.3.2] — 2026-10-01

The Opus 5.5 review release. A full-repo read-only review (design note,
7 high / 15 medium findings) drove four fix batches — 13 findings — ending
the reviewer's core verdict that "the design still assumes one node."
Behavior is unchanged on a single-node rig; the fixes are correctness,
resilience, and the multi-node foundation.

### Added
- **Zone capability audit at startup**: every configured zone is checked
  against the all-S2 audioClip scope at boot — an unsupported player is
  logged before it can eat a reply mid-conversation (design note, `fb2e585`)

### Fixed
- **Brain failure after the filler no longer goes silent**: the post-filler
  brain await has its own try/except (a sibling except clause cannot catch
  an exception raised in another handler's body) — the house hears
  "Sorry — I can't reach my brain right now" instead of dead air, and the
  outage reply never enters the server-side history as a fake memory
  (`897de5d`, review H3)
- **Duck-holder hardening**: every Sonos call is timeout-bounded; a dead
  websocket is dropped and reconnected on the next poll tick instead of
  retrying forever on the corpse; the wake gate's LOCKED state has a
  safety cap (refined by real clip duration via `note_speech`) so a lost
  stop-frame can no longer deafen the mic; the 8s silence tail is padded
  only when the holder is enabled and the turn is live, and lands AFTER
  the echo gate so the mic still reopens at her last syllable; duck
  ownership starts when the clip LOADS, not at generation end; the holder
  dies in `cleanup()`, not just on CancelFrame (`634af8f`, review
  H7/M1/M2/M3/M13)
- **Event loop unblocked**: MP3 encodes (reply clips, duck clip, dashboard
  test tones) and soco calls (name lookups, UPnP discovery) run in
  threads; dashboard node commands (restart/scan/rename/Wi-Fi) ride the
  direct data-channel wire via the output transport — a 40s brain call
  can no longer stall a command into a 504 (`7ae9ecb`, review M4–M7)
- **Multi-node architecture**: room identity is per-pipeline — replies,
  wake records, and ducks resolve through THIS connection's pc_id instead
  of the global last-connected room; one openWakeWord Model per pipeline
  (the class-shared model mixed concurrent sessions' rolling buffers and
  every reconnect reset the live session's buffers); two physical nodes
  sharing a room name now log a loud rename warning instead of silently
  evicting each other into a reconnect loop (`a23187a`, review H1/H2/M8)

## [0.3.1] — 2026-09-30

Node management + audio UX — the rig gets self-service and learns to
duck. All fixes and adjustments to existing features (per the
versioning rule: settings ship on main; a tag marks the day's shipped
state).

### Added
- **Duck-holder** (`sonos.duck_while_awake`, default on): the Sonos
  audioClip duck now spans the ENTIRE awake window, not just reply
  clips — an eternal loop holds a 30s silence clip on the wake room's
  zone while the gate is awake (refresh at 24s), so full-volume music/TV
  can no longer feed STT as phantom user turns (the "Nantucket." lyric
  transcription). Reply clips carry an 8s silence TAIL so the holder's
  refresh lands silence-on-silence — the duck is gapless from wake to
  sleep, no full-volume blips between reply and listening. Gate close
  cancels the holder and the music unducks once. Echo gate + LED timing
  stay tied to SPEECH-only duration (`9b4000d`, `3d28fa7`, `20d5d25`)
- **LED direct wire** (`core/led_state.py`): LED state messages ride
  the WebRTC data channel directly (output transport `send_message`),
  bypassing the pipeline — a blocked brain call stalls pipeline frames
  3-40s in both directions, which left the ring holding stale purple
  through the whole "On it." → answer gap (measured 12:23). Speaking
  state now fires from SonosOutput's real-time TTS frames downstream
  of the brain; end-of-speaking is duration-timed to actual playback
  (`d02075a`, `69d9c5f`)
- Dashboard: firmware push reads the version from the uploaded image's
  embedded app descriptor (magic-checked; manual version entry removed);
  uploads stage to a temp file and swap atomically — a rejected upload
  can never clobber the served image (`6f04906`, `a09aa80`)

### Changed
- Firmware binary renamed `node_esp32.bin` → `node_esp32.bin` across
  the OTA chain (served name, manifest, deploy script, upload dest,
  firmware CMake project name) — generic naming for the public repo;
  version untouched, node unaffected (equal-version skip) (`c73832c`)
- Zone keys are now the player's native Sonos name (ruling: a player's
  native name IS its name) — dashboard displays live Sonos names
  verbatim; `living_room` ghost name retired (`bbed6cd`, `c2062f3`)

### Fixed
- Command channel end-to-end: `on_app_message` transport handler
  (sender = pc_id string), awaited `queue_frame`, `"type":"cmd_result"`
  envelopes, room eviction for stale same-room sessions (504s into a
  zombie session), NVS writes on an internal-RAM stack (PSRAM stack +
  flash-cache suspend = double fault), asymmetric liveness protocol
  (probe/pong without reply-to-reply loop) (`fd3fb32`…`3a9da0d`)
- Renames carry the `room_zones` mapping key — no ghost rows, no
  orphaned zone routing (`7465111`)
- Sync pipeline: host `config.yaml` is now authoritative —
  `sync_host.sh` PULLS it to the VM, never pushes (push-every-sync
  clobbered dashboard-written `room_zones` twice) (`26208bf`)
- Stale-session disconnects no longer mark a live room offline
  (`b7137d0`); payload schemas drop the redundant `room` field
  (`8777e85`)

## [0.3.0] — 2026-09-29

Web dashboard v1 — the rig gets a GUI. Mapping, node status, zone
management, and test clips, all served by the bot process itself.

### Added
- `core/dashboard/` package: web dashboard at `/dashboard` — room→zone
  mapping page, live node list, Sonos LAN discovery (soco), per-zone
  test clips, status strip. Served by the bot's own FastAPI app on
  :7860 (same pattern as firmware_server); no new process or port
  (`ce8b35f`)
- Offer tee (pure ASGI): correlates the node firmware's `room` field
  in POST /api/offer with pipecat's server-side pc_id at
  response-send time — the pipeline runs as the offer request's
  background task, so session-end correlation is minutes too late
  (measured live; `ce81afb`)
- Room-aware reply routing: wake gate `on_wake` hook + SonosOutput
  `zone_selector` — a wake in a mapped room plays the reply clip in
  that room's zone; unmapped falls back to `sonos.default_zone`
  (`ce8b35f`)
- Dashboard config writes: comment-preserving config.yaml round-trip
  (ruamel.yaml), atomic write + rolling backup, config cache
  invalidated so reads reflect writes immediately (`ce8b35f`)
- `room_zones:` and `dashboard.enabled:` config sections; new deps
  soco 0.31.4 + ruamel.yaml 0.19.1 (optional at boot — voice runs
  without them, `f47f6a0`)
- `scripts/test_dashboard.py`: 20-check offline rig (fakes /api/offer
  in both immediate and background-task shapes; exercises the tee,
  registry, config round-trips, validation, and a live Sonos test
  clip)

### Changed
- Comment de-personalization pass (public-repo prep): the voice system
  prompt and code comments no longer name the user; the OWW model test
  docstring defers its positive case to a live test instead of a person
- Removed the "never address the user as boss" rule comment — how the
  brain addresses its user is brain-side (Hermes) config, not repo policy
- Comment accuracy + formatting sweep (opus-5-5 review, design note):
  wake_word docstring now matches the 2026-09-19 OWW-always-fed behavior
  and the permanent echo gate; sonos_output says MP3 not WAV; _is_own_echo
  describes the >=60%/3-word overlap it actually does; deploy_firmware
  drop the fictional check-ota message; config.example awake_timeout
  aligned with the code default (15.0); README glob/journald fixes;
  unused imports removed; import order tightened; no logic changes

## [0.2.2] — 2026-09-20

Review Minors + efficiency pass (final findings from the 2026-09-20 review).

### Changed
- PipelineTask → PipelineWorker (deprecated alias, removed in pipecat 2.0);
  imports from `pipecat.pipeline.worker` — verified on host pipecat 1.11.0
- Whisper + OWW models now cached per-process, shared across pipelines:
  every node reconnect used to reload large-v3-turbo onto the GPU (seconds
  of deaf STT + VRAM churn); OWW shared model resets its rolling buffers
  per pipeline (`reset()`)
- continuity_test reads brain.url via the config surface (was env-only,
  KeyError against a secrets-only .env)
- SonosOutput reads config via core/config's CONFIG_PATH (LVI_CONFIG split-brain)
- stt_local: dead `settings.language` branch → the real `self._language`
- .env.example: real voice ID replaced with placeholder (account data)
- Version comments drifted to pipecat 1.11 (were 1.10)

## [0.2.1] — 2026-09-20

Cleanup/efficiency pass from the superpowers-driven review (all 14 findings).

### Fixed
- requirements.txt: fresh-clone booby traps — missing `lameenc`,
  `sonos-websocket`, `pipecat-ai-prebuilt` (fresh install died at the
  first spoken reply); TTS code default was the repo's own
  measured-broken `eleven_v3` (`3db43e4`)
- OWW wake gate fed 640 samples per chunk (1280 *bytes* labeled "80ms")
  — half openWakeWord's canonical 1280-sample frame, 2x inference on
  the 24/7 hot path; now 2560 bytes (`443b8e2`)
- containment_test Case 3 had `or True` — an assertion structurally
  unable to fail; now asserts real output (`766bd4d`)
- brain outage now speaks a fallback line instead of filler-then-silence

### Added
- `wake.model`, `brain.lull_secs`, `brain.dedup_window` config knobs
  (were documented-but-ignored or buried in constructor defaults)
- README fresh-clone section (deps → config → .env → wake model → cert → run)

### Removed
- config.yaml untracked (was in the index despite .gitignore — baked LAN
  IPs/Sonos UID into history); template is config.example.yaml
- 888KB never-flashable slave XMOS bin (notes preserves the bytes)
- Overdue debug scaffolding: aiortc DEBUG, once-per-second wake diag
  (~86k journald lines/day), dead attribute, no-op assignments

## [0.2.0] — 2026-09-20

First tagged snapshot: the house voice rig as it stands. Phase 1–4 pipeline
live (wake gate → VAD → local whisper STT → Hermes brain → ElevenLabs TTS →
Sonos audioClip overlay), running as a systemd service on the appliance host,
serving the house daily.

### Added
- Phase 1 bot: local whisper ears, stub brain, ElevenLabs mouth (`8a8e8a8`)
- sonos_output: Sonos audioClip overlay — clip volume ~70, ~200ms load,
  opportunistic clip pruning at startup (`20c2a72`)
- config.yaml as the single tuning surface (yaml > `LVI_*` env > code default) (`96ce51b`)
- systemd service: boot survival, crash-restart, journald logging (`3a588e0`)
- `scripts/sync_host.sh`: bundle-based host sync, no creds on hosts (`11b16eb`)
- requirements.txt with pinned top-level deps (pipecat 1.11.0) (`f4d4203`)

### Changed
- `.env` is secrets-only (3 lines); all behavior lives in config.yaml;
  `config.example.yaml` is the committed template (`1993d34`, `c67ff7c`)
- Clip retention 7d → 48h (serve-once garbage) (`ca0e4fb`)
- README rewritten: config surface and fresh-clone path documented (`fc10870`, `127f4a6`)
- AGENTS.md/CLAUDE.md carried by sync_host (reset deleted them) (`1434922`)

### Removed
- Chatterbox local TTS lane removed entirely — production TTS is
  ElevenLabs only (`fede243` → parked `5b593c8` → removed `1ce3941`)
- Dead one-shot scripts: replay_clip, sonos_voice_test, ws_probe (`05157f8`)

### Fixed
- TTS voice clobber: yaml default overrode production — pinned
  eleven_flash_v2_5 (`fb0cf94`)