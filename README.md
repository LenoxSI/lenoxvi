# LenoxVI

**house-voice appliance.** A pipecat bot that turns mic nodes into a
house-wide voice assistant with any intelligence you want:

    mic node (reSpeaker) → WiFi/WebRTC → wake gate → whisper STT →
    intelligence lane (Hermes / OpenClaw / …) → ElevenLabs TTS →
    speaker reply in the room's zone (Sonos / WiiM / the node's own speaker)

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
  - `array/`, `flex/`, `waveshare/` — one OTA track per node line, filled
    at deploy time by `deploy_firmware.sh --track array|flex|waveshare` or a
    dashboard upload
    (empty in a fresh clone by design); served by firmware_server
- `deploy/lenoxvi-bot.service.example` — sanitized systemd user unit
  (launch flags, `.env` loading, CUDA LD_LIBRARY_PATH for whisper's
    CTranslate2, restart policy, a ~2 s graceful stop with a 10 s hard limit)
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

Panels (each saves to `config.yaml` through comment-preserving
read-merge-write; most apply on the next bot restart, which the page offers):

- **Rig facts** — version, TTS, STT, OWW model, Intelligence line
- **Bot control** — restart the bot (with restart countdown popup)
- **TTS** — provider (ElevenLabs / Deepgram / Deepgram Flux), model
  (flash v2.5 / v4 turbo), voice
- **STT** — provider (Local — Whisper, or Deepgram Nova-3 in the cloud
  with local Whisper as its automatic fallback), Whisper model (download
  with progress, delete unused), language, live status and latency
- **Intelligence** — provider picker (Hermes / OpenClaw), an agent
  dropdown filled from the provider (OpenClaw: its `/v1/models`; Hermes:
  the profiles on a multiplexing gateway, read from the Hermes
  dashboard's status page — falls back to the default agent), API URL
  (provider-agnostic: a stranger's Hermes lives at their URL), Test
  connection (health + one minimal completion, latency)
- **Keys** — every API key in one place (ElevenLabs, Deepgram, Hermes,
  OpenClaw, each Hermes agent): what uses it, set / missing / waiting for
  a restart (first 4 characters only), Replace, Remove (with a backup),
  Test, and one Add picker; other panels link here when a key is missing
- **Voice Tuning** — filler/pause tunables
- **Wake Word Model (OWW)** — stock + trained wake-word models: upload,
  select, delete (stock models are selectable, never deletable)
- **Zones / Nodes / Room→zone mapping** — rooms, mic nodes, speaker zones
  (a node with a built-in speaker can use it as its own private zone), and
  a per-room agent (a room can talk to a different agent than the
  Intelligence panel's default)
- **Firmware** — node image push, FW version column

The suite (`tests/test_dashboard.py`) exercises every panel through
its TestClient: config round-trips (comment preservation), guard rails,
422 shapes, lane dispatch (openclaw → `openclaw/<agent>` +
session-key header; hermes round-trip restores conversation + Hermes
header), key precedence + no-KeyError, key POST hygiene (never echoes
material), test-probe ok/error paths.

Developing the dashboard: the prebuilt `dashboard-ui/dist/` is committed,
so running LenoxVI needs no node toolchain. To work on the UI, run the Vite
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
4. Wake word: works out of the box with **"Hey Jarvis"**, a stock model
   that ships inside the openwakeword package (see [Wake word](#wake-word)).
5. `bash scripts/make-cert.sh <bot-host-LAN-IP>` (mic access needs HTTPS)
6. `python bot.py`, then open `https://<bot-host-LAN-IP>:7860/client`
   (run from the repo root — config and clips paths are relative)

[openWakeWord]: https://github.com/dscripka/openWakeWord

## Wake word

The default wake word is **"Hey Jarvis"** (`wake.model: hey_jarvis`). It is
one of openWakeWord's pre-trained models, which install with the
`openwakeword` package — LenoxVI does not ship or host any model file. The
stock choices are `hey_jarvis`, `hey_mycroft` and `alexa`, selectable in the
dashboard's Wake Word Model panel.

**License:** openWakeWord's code is Apache-2.0, but its pre-trained models
are **CC BY-NC-SA 4.0 — non-commercial use only**. Fine for a home; for
anything commercial, train your own.

**Your own wake word (recommended):** train a model with openWakeWord's
training notebook (see the [openWakeWord] README), upload the `.onnx` in the
dashboard's Wake Word Model panel, set it active, and restart. Community
model collections exist too — check each model's license before using it.

**The name:** `wake.name` is what the house is called — set it in the
dashboard's Wake name card (or in config.yaml) to match your wake word. It is
never guessed from the model file, so change it when you change models.
Speech-to-text sometimes mis-spells a rare name; add those spellings to
`wake.heard_as` (or click them in the card) so a bare "Hey <name>." is
dropped instead of answered.

## Local TTS (optional): Pocket TTS

LenoxVI can speak with [Pocket TTS], a small CPU-only model from Kyutai: no
API key, and nothing leaves your network. It runs about 3x faster than real
time on a 4-core CPU, so it leaves the GPU to speech recognition.

    pip install -r requirements-pocket.txt
    bash scripts/fetch_pocket_weights.sh

The first command installs the CPU build of torch (no CUDA wheels). The
second caches the model and the 21 preset voices (about 350 MB), so the box
can speak without internet. Then, in the dashboard's TTS panel, either:

- pick **Pocket (local)** as the provider, or
- keep a cloud provider and set **Fallback → Pocket (local)**. If the cloud
  provider goes down, replies continue in a Pocket voice and switch back on
  their own once it recovers (checked every `tts.revive_cooldown_secs`,
  default 30 s). The reply that was playing when the provider failed may be
  lost.

Changes apply on the next bot restart. Preset voices and their licences:
<https://huggingface.co/kyutai/tts-voices>. Voice cloning
(`tts.pocket_voice: /path/to/voice.wav` in config.yaml) needs the gated
`kyutai/pocket-tts` weights and your own Hugging Face login.

[Pocket TTS]: https://github.com/kyutai-labs/pocket-tts⟫
