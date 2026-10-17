# lenoxvi

**house-voice appliance.** A pipecat bot that turns mic nodes into a
house-wide voice assistant with any intelligence you want:

    mic node (reSpeaker) → WiFi/WebRTC → wake gate → whisper STT →
    intelligence lane (Hermes / OpenClaw / …) → ElevenLabs TTS →
    speaker reply in the room's zone (Sonos / WiiM)

## What it does

A mic node (ESP32-S3 + reSpeaker) listens for the wake word, streams
audio over WebRTC, local whisper transcribes, the configured
intelligence lane answers, and the reply is spoken in the room via
Sonos or WiiM audio overlays. Wake-word turn-taking (supersede,
barge-in, echo containment), tiered fillers, and the full operator
surface — a web dashboard — run on the appliance.

**Not a chat box.** Voice-first, wake-gated. No chat UI, no skills
harness — the dashboard's job is operating the rig, not talking to it.

## Layout

- `bot.py` — pipecat pipeline: transport, wake gate, VAD, STT, intelligence, TTS, speaker out
- `core/` — the pieces the pipeline wires together
  - `wake_word.py` — openWakeWord gate + turn lock (IDLE/AWAKE/LOCKED;
    default awake window 15s, tunable via `wake.awake_timeout`)
    + 2-frame wake-word barge-in (`wake.barge_in`)
  - `intelligence_agent.py` — the intelligence agent (`intelligence.kind`):
    `hermes` (Hermes API over LAN, bearer key) — **production runs
    this**; `openclaw` (OpenClaw Gateway, `model: openclaw/<agent>`,
    sticky rooms via `x-openclaw-session-key`) — the second intelligence;
    `stub` — deterministic echo intelligence for bench/dev runs (the code
    default when `intelligence.kind` is unset — a misconfigured deploy
    answers with canned lines). Keys are strict per-kind:
    `hermes` reads `LVI_HERMES_KEY`, `openclaw` reads
    `LVI_OPENCLAW_KEY` — no cross-kind fallback; a missing key fails the
    turn with a clear message instead of bricking construction.
  - `intelligence_stub.py` — the stub intelligence's implementation (canned lines)
  - `stt_local.py` — faster-whisper large-v3-turbo on the GPU
  - `sonos_output.py` / `wiim_output.py` — replies as Sonos / WiiM
    audio overlays (S2 only, vol ~70; WiiM `setPlayerCmd` stop path)
  - `echo_gate.py` — room-echo containment (permanent safety net behind
    the turn lock; `relax()` on confirmed wake/barge-in, ~0.5s tail)
  - `led_state.py` — node ring-LED state machine
  - `firmware_server.py` — `/firmware/*` OTA delivery for mic nodes
  - `dashboard/` — the operator surface (see below)
- `scripts/` — ops tools (each has a header comment explaining what it does)
  - `restart.sh` — systemd wrapper: restarts the bot unit on the
    appliance host (no flags)
  - `sync_host.sh` — bundle-based repo sync VM → host (no creds on hosts)
  - `deploy_firmware.sh` — build + version + ship a node firmware OTA (VM-side)
  - `app_bin_version.py` — reads the version/line baked into a firmware image
  - `fetch_xmos_fw.sh` — fetches the XMOS image at node bring-up
  - `serial_watch.py` — node serial capture (survives replug)
  - `make-cert.sh <bot-host-LAN-IP>` — self-signed HTTPS certs for the bot
- `tests/` — automated checks, safe to run any time
  - `test_dashboard.py` — **the permanent offline suite** (red-line
    discipline: every bug class gets a check; runs on the appliance
    venv in a scratch dir, never restarts the real bot, never deletes
    prod files). Covers the dashboard API, TTS lane dispatch,
    load-anchored speech timers, epoch turns + barge-in, passthrough,
    echo-gate relax, and the Intelligence lane.
  - `containment_test.py`, `wake_phrase_test.py` — echo-containment and
    wake-phrase filtering (run from the repo root)
  - `test_deploy_guard.sh` — deploy_firmware.sh preflight matrix
  - `manual/` — **live probes**: they use real hardware or services
    (GPU, wake model, intelligence API, a speaker — `sonos_output_test`
    plays a tone on the configured zone). Run deliberately, not in CI.
- `firmware/` — node firmware images + OTA manifests (gitignored)
  - `xvf3800/` — XMOS image fetched via `scripts/fetch_xmos_fw.sh` at
    bring-up (upstream ships **no license** — the binary is not
    redistributed here; see the script header for md5 + provenance)
  - `array/`, `flex/` — one OTA track per node line, filled at deploy
    time by `deploy_firmware.sh --track array|flex` or a dashboard upload
    (empty in a fresh clone by design); served by firmware_server
- `deploy/lenoxvi-bot.service.example` — sanitized systemd user unit
  (launch flags, `.env` loading, CUDA LD_LIBRARY_PATH for whisper's
    CTranslate2, restart policy, SIGKILL-after-10s on hung WebRTC)
- `config.example.yaml` — **the tuning surface template**: every
  behavior knob in one file (wake threshold/timeout/model/barge-in,
  VAD params, STT/TTS/intelligence settings, Sonos/WiiM zones, intelligence
  lull/dedup, session modes). Copy to `config.yaml` (gitignored,
  per-machine) and edit. Precedence: `config.yaml` > `LVI_*` env var >
  code default (`core/config.py`). Secrets stay in `.env`. Edit +
  `scripts/restart.sh` to apply — no rebuild.
- `logs/` — gitignored runtime logs (the bot logs to journald:
  `journalctl --user -u lenoxvi-bot`)

## The dashboard (operator surface)

`https://<bot-host-LAN-IP>:7860/dashboard` — LAN-only by design, no
auth on reads. Mutating routes carry two guard rails
(`core/dashboard/guard.py`): an Origin/Host check (kills
cross-origin-tab and DNS-rebinding shapes) and an optional
`LVI_DASHBOARD_TOKEN`. The page prompts for it once.

Panels, Settings tab (each panel saves to `config.yaml` through
comment-preserving read-merge-write, applies on next bot restart):

- **Rig facts** — version, TTS, STT, OWW model, Intelligence line
- **Bot control** — restart the bot (with restart countdown popup)
- **TTS** — provider (ElevenLabs / Deepgram / Deepgram Flux), model
  (flash v2.5 / v4 turbo), voice, API keys (never rendered back)
- **Intelligence** — provider picker (Hermes / OpenClaw; Claude +
  custom OpenAI-compatible designed-for), per-kind fields — API URL
  (provider-agnostic, shown for every kind: a stranger's Hermes lives
  at their URL), agent id (OpenClaw only), API key set ✓, Test
  connection (health + one minimal completion, latency)
- **Voice Tuning** — filler/pause tunables
- **Wake Word Model (OWW)** — trained wake-word model management
- **Zones / Nodes / Room→zone mapping** — rooms, mic nodes, speaker zones
- **Firmware** — node image push, FW version column

The suite (`tests/test_dashboard.py`) exercises every panel through
its TestClient: config round-trips (comment preservation), guard rails,
422 shapes, lane dispatch (openclaw → `openclaw/<agent>` +
session-key header; hermes round-trip restores conversation + Hermes
header), key precedence + no-KeyError, key POST hygiene (never echoes
material), test-probe ok/error paths.

Developing the dashboard: the prebuilt `dashboard-ui/dist/` is committed,
so running lenoxvi needs no node toolchain. To work on the UI, run the Vite
dev server and point its `/api` proxy at a running bot (default
`https://localhost:7860`):

```
cd dashboard-ui && npm install
LENOXVI_DEV_BOT_URL=https://<bot-host>:7860 npx vite --port 5179
```

`npm run build` regenerates `dist/` — commit it with the UI change.

## Fresh clone → running bot

1. `pip install -r requirements.txt` (host Python 3.12+; pinned to the
   appliance's 3.14)
2. `cp config.example.yaml config.yaml` and fill in every
   `<PLACEHOLDER>` (intelligence URL, Sonos zone IPs/UIDs, clip-server
   host IP)
3. `.env` from `.env.example` — the secrets:
   `ELEVENLABS_API_KEY`, `LVI_TTS_VOICE_ID`, `LVI_HERMES_KEY` (or
   `LVI_OPENCLAW_KEY` for the OpenClaw lane), `DEEPGRAM_API_KEY` (optional
   alternative provider)
4. Wake-word model: a trained `.onnx` at repo root (gitignored — train
   your own with [openWakeWord], or set `wake.enabled: false` in
   config.yaml until you have one). Point `wake.model` at it if the
   name differs from the default.
5. `bash scripts/make-cert.sh <bot-host-LAN-IP>` (mic access needs HTTPS)
6. `python bot.py`, then open `https://<bot-host-LAN-IP>:7860/client`
   (run from the repo root — config and clips paths are relative)

[openWakeWord]: https://github.com/dscripka/openWakeWord⟫