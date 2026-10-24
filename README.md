<picture>
  <source media="(prefers-color-scheme: dark)" srcset="dashboard-ui/public/lenoxvi-horizontal-on-dark.svg">
  <img alt="LenoxVI" src="dashboard-ui/public/lenoxvi-horizontal-on-light.svg" width="420">
</picture>

# LenoxVI

High quality multi-zone audio for your AI agent, with mic and room routing + audio ducking. Local, cloud or hybrid based on your use case. Audio routes through Sonos or WiiM audio speakers. Mics from ReSpeaker (Array, Flex) or Waveshare, including in-ceiling microphones matching architectural speakers. Web dashboard for easy and fast tuning of voice pipeline!

    mic node (reSpeaker) → WiFi/WebRTC → wake gate → speech to text →
    intelligence (Hermes / OpenClaw / Nanobot) → text to speech →
    speaker in the room (Sonos / WiiM / the node's own speaker)

The wake word always runs on your hardware. Speech and voice can stay local or go to the cloud: you choose, per lane. Wake-gated, not a chat box. The dashboard operates the appliance. It does not talk to it.

![Rooms view of the dashboard, demo data](docs/readme/rooms.png)

## Quick start

Python 3.12+. From the repo root:

1. `pip install -r requirements.txt`
2. `cp config.example.yaml config.yaml` and fill every `<PLACEHOLDER>`
3. `cp .env.example .env` and set the keys: `ELEVENLABS_API_KEY` and `LVI_TTS_VOICE_ID` for the default voice, plus the key for your intelligence (`LVI_HERMES_KEY`, `LVI_OPENCLAW_KEY` or `LVI_NANOBOT_KEY`)
4. `bash scripts/make-cert.sh <bot-host-LAN-IP>` — mic access needs HTTPS
5. `python bot.py`, then open `https://<bot-host-LAN-IP>:7860/dashboard`

**Hey Jarvis** works out of the box. `/client` on the same host is a browser mic for a first test. Local voices and the wake-word license are in [Fresh clone](#fresh-clone--running-bot).

## Features

| Feature | What |
|---|---|
| Wake gate | "Hey Jarvis" out of the box. Train your own. |
| Turn-taking | Barge-in, echo containment, and filler lines while the agent thinks. |
| Rooms | Each mic node maps to a speaker zone and can use its own agent. Sessions are sticky or fresh per room. |
| Local or cloud | Whisper, Pocket and Kokoro run on your hardware. Cloud providers fall back to a local model. |
| Firmware | Push node images over the air from the dashboard. |
| Dashboard | LAN-only. Keys, zones, voices and models in one place. Mutations are guarded. |

## Dashboard

Demo data. Rooms is at the top.

### Voice

![Voice view of the dashboard](docs/readme/voice.png)

### System

![System view of the dashboard](docs/readme/system.png)

## Speech to text

✅ runs on your hardware. ❌ goes to the cloud.

| Provider | Local | Fallback |
|---|---|---|
| Whisper | ✅ | — |
| Deepgram Nova-3 | ❌ | The configured Whisper model |
| ElevenLabs Scribe v2 | ❌ | The configured Whisper model |

### Whisper models

Four faster-whisper models. The "best for" line is the dashboard catalog label, from a benchmark on a 12 GB GPU and a 4-core CPU. Turbo and Medium need a GPU. The dashboard writes the runtime with the model: GPU is `cuda` / `float16`, no GPU is `cpu` / `int8`.

| Model | Size | Needs | Best for |
|---|---|---|---|
| `large-v3-turbo` | 1.6 GB | GPU | Best all-round. The default. |
| `medium` | 1.5 GB | GPU | Faster, solid in noise. |
| `small` | 0.5 GB | CPU is fine | Snappy in quiet rooms. |
| `base` | 0.15 GB | CPU is fine | No GPU, or the fastest option. |

## Intelligence

| System | Talks to | Session history | Spoken style |
|---|---|---|---|
| Hermes | Hermes API, bearer key. Agents come from the gateway. | Hermes keeps the conversation. | LenoxVI sends voice instructions: short spoken sentences, no markdown. |
| OpenClaw | OpenClaw Gateway. Model `openclaw/<agent>`. | Sticky per room via `x-openclaw-session-key`. A fresh room sends no key; the agent may still recall from its own files. | Same voice instructions as Hermes. |
| Nanobot | Nanobot's OpenAI-compatible API. One user message per turn, plus `session_id`. | Nanobot keeps the history. | The agent's own instructions. LenoxVI cannot send a system message. |

## Text to speech

✅ runs on your hardware. ❌ goes to the cloud.

| Provider | Local | Fallback |
|---|---|---|
| ElevenLabs Flash v2.5 or v4 Turbo | ❌ | Pocket, if installed |
| Deepgram Flux or Aura-2 | ❌ | Pocket, if installed |
| Pocket | ✅ | — |
| Kokoro | ✅ | Pocket, if installed |

## Speakers

Replies play on a Sonos or WiiM zone. Each zone picks its own backend. The difference that matters is ducking.

| | Sonos | WiiM |
|---|---|---|
| How a reply plays | Audio clip overlay, mixed over whatever is playing | Replaces what is playing |
| Music while she talks | Ducks and keeps playing, without pausing | Stops, and is not resumed |
| Stays ducked while the room listens | ✅ | ❌ |
| Reply volume | Set per clip | Sets the main volume, then puts your music volume back |
| Source limits | Any source | Spotify Connect, AirPlay and Bluetooth sessions cannot be resumed |
| Connects with | Local websocket control: IP and player UID | HTTPS HTTP API: IP only |
| Find it in the dashboard | Discover button | Add by IP |
| Built and tested on | Port, Amp, Era 100 SL | Pro |

WiiM's prompt-overlay command does nothing on the firmware we tested, so the WiiM backend interrupts instead of ducking. Other WiiM models use the same HTTP API and are untested.

### Sonos speakers that can duck

Ducking needs the `AUDIO_CLIP` capability. [Sonos documents](https://docs.sonos.com/reference/audioclip) that every player running S2 software has it and that S1-only players do not. The list below is Sonos's own [compatibility list](https://support.sonos.com/en-us/article/sonos-app-version-compatibility) sorted that way. Tested on a Port, an Amp and an Era 100 SL; the rest follow Sonos's documentation. At startup the bot checks each configured zone and logs whether it has `AUDIO_CLIP`.

| Can duck | Models |
|---|---|
| ✅ S2 only | Arc, Arc SL, Arc Ultra, Beam (Gen 2), Beam Ultra, Era 100, Era 100 Pro, Era 100 SL, Era 300, Five, Move 2, One SL, Ray, Roam, Roam 2, Roam SL, Sonos Play, Amp Multi, SYMFONISK Bookshelf (Gen 2), Floor lamp, Picture frame, Table lamp (Gen 2) |
| ✅ When set up on the S2 app | Amp, Beam (Gen 1), Move, One (Gen 1), One (Gen 2), Play:1, Play:3, Play:5 (Gen 2), Playbar, Playbase, Port, Connect (Gen 2), Connect:Amp (Gen 2), SYMFONISK Bookshelf (Gen 1), SYMFONISK Table lamp (Gen 1) |
| ❌ S1 only | Connect (Gen 1), Connect:Amp (Gen 1), Play:5 (Gen 1), ZonePlayer 80, 90, 100, 120 and S5, CR200 |

Subs, Boost, Bridge and the Ace headphones are left out: they are not speaker zones. Sonos says not to rely on model names, so the logged capability check is the final word.

## License

No project license is set. Until one is, do not treat this repository as open source.

The wake-word models that install with openWakeWord are CC BY-NC-SA 4.0 — non-commercial only. See [Wake word](#wake-word).

## Help

Bugs and questions: [open an issue](https://github.com/LenoxSI/lenoxvi/issues).

## Layout

- `bot.py` — pipecat pipeline: transport, wake gate, VAD, STT, intelligence, TTS, speaker out
- `core/` — the pieces the pipeline wires together
  - `wake_word.py` — openWakeWord gate + turn lock (IDLE/AWAKE/LOCKED;
    default awake window 15s, tunable via `wake.awake_timeout`)
    + 2-frame wake-word barge-in (`wake.barge_in`)
  - `intelligence_agent.py` — the intelligence agent (`intelligence.kind`):
    `hermes` (Hermes API over LAN, bearer key), `openclaw` (OpenClaw
    Gateway, `model: openclaw/<agent>`, sticky rooms via
    `x-openclaw-session-key`) or `nanobot` (nanobot's OpenAI-compatible API:
    one user message per turn plus a `session_id`, nanobot keeps the history
    — see [Nanobot](#nanobot)). Keys are strict per-kind: `hermes` reads
    `LVI_HERMES_KEY`, `openclaw` reads `LVI_OPENCLAW_KEY`, `nanobot` reads
    `LVI_NANOBOT_KEY` — no cross-kind fallback; a missing key fails the turn
    with a clear message instead of bricking construction. Set
    `intelligence.kind`: left unset, the bot answers with canned lines.
  - `intelligence_stub.py` — the canned-line stand-in used when
    `intelligence.kind` is unset
  - `stt_local.py` — faster-whisper large-v3-turbo on the GPU
  - `sonos_output.py` / `wiim_output.py` — replies as Sonos / WiiM
    audio overlays (Sonos S2 only, volume ~70; WiiM `setPlayerCmd` stop path)
  - `echo_gate.py` — room-echo containment (permanent safety net behind
    the turn lock; `relax()` on confirmed wake/barge-in, ~0.5s tail)
  - `room_recorder.py` — time-boxed raw mic capture of one room to a WAV
  - `led_state.py` — node ring-LED state machine
  - `firmware_server.py` — `/firmware/*` OTA delivery for mic nodes
  - `dashboard/` — the operator surface (see below)
- `scripts/` — ops tools (each has a header comment explaining what it does)
  - `restart.sh` — systemd wrapper: restarts the bot's systemd unit (no flags)
  - `sync_host.sh` — bundle-based repo sync from a dev machine to the bot host (no creds on the host)
  - `deploy_firmware.sh` — build + version + ship a node firmware OTA (run from the dev machine)
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

## Dashboard reference

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
- **STT** — provider (Local — Whisper, or Deepgram Nova-3 or ElevenLabs
  Scribe v2 in the cloud, each with local Whisper as its automatic
  fallback; Scribe uses the same `ELEVENLABS_API_KEY` as the ElevenLabs
  voice), Whisper model (download with progress, delete unused), language,
  live status and latency
- **Intelligence** — provider picker (Hermes / OpenClaw / Nanobot), an agent
  dropdown filled from the provider (OpenClaw and Nanobot: their
  `/v1/models`; Hermes:
  the profiles on a multiplexing gateway, read from the Hermes
  dashboard's status page — falls back to the default agent), API URL
  (provider-agnostic: a stranger's Hermes lives at their URL), Test
  connection (health + one minimal completion, latency)
- **Keys** — every API key in one place (ElevenLabs, Deepgram, Hermes,
  OpenClaw, Nanobot, each Hermes agent): what uses it, set / missing / waiting for
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
header; nanobot against a local fake server that enforces nanobot's
contract), key precedence + no-KeyError, key POST hygiene (never echoes
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

### Recording a room

The bot can save one room's raw mic audio (what the wake word hears, before
any gating) to a WAV on the appliance, for diagnostics or voice samples.
Off unless asked; up to 10 minutes per recording; one recording per room;
files land in `captures/` (owner-only, never committed) and are deleted
after 7 days.

Recording needs `LVI_DASHBOARD_TOKEN` set in `.env`: without it every route
below answers 403, so nobody on the LAN can listen in. Each request sends
the token in the `X-Dashboard-Token` header.

```
POST   /api/dashboard/nodes/<room>/record      {"seconds": 120}
POST   /api/dashboard/nodes/<room>/record/stop
GET    /api/dashboard/recordings               # live recordings + saved files
GET    /api/dashboard/recordings/<file>        # download
DELETE /api/dashboard/recordings/<file>
```

## Fresh clone → running bot

1. `pip install -r requirements.txt` (host Python 3.12+; pinned to the
   appliance's 3.14)
2. `cp config.example.yaml config.yaml` and fill in every
   `<PLACEHOLDER>` (intelligence URL, Sonos zone IPs/UIDs, clip-server
   host IP)
3. `.env` from `.env.example` — the secrets:
   `ELEVENLABS_API_KEY`, `LVI_TTS_VOICE_ID`, `LVI_HERMES_KEY` (or
   `LVI_OPENCLAW_KEY` for the OpenClaw lane, `LVI_NANOBOT_KEY` for the
   Nanobot lane), `DEEPGRAM_API_KEY` (optional alternative provider)
4. Wake word: works out of the box with **"Hey Jarvis"**, a stock model
   that ships inside the openwakeword package (see [Wake word](#wake-word)).
5. `bash scripts/make-cert.sh <bot-host-LAN-IP>` (mic access needs HTTPS)
6. `python bot.py`, then open `https://<bot-host-LAN-IP>:7860/client`
   (run from the repo root — config and clips paths are relative)

[openWakeWord]: https://github.com/dscripka/openWakeWord

## Nanobot

LenoxVI can talk to a [nanobot](https://github.com/HKUDS/nanobot) agent
through nanobot's OpenAI-compatible API server. Set it up from the dashboard:

1. **System · Intelligence** — pick **Nanobot**, enter the API server's URL
   (for example `http://<nanobot-host>:8900`; `/v1` at the end is optional),
   and press **Save**. No agent is needed to save.
2. **System · Keys** — add the **Nanobot** key (`LVI_NANOBOT_KEY`, the key
   your nanobot API server expects). **Test** checks it against the saved
   URL's `/models`; a wrong key reads "rejected".
3. Back on the Intelligence card, the agent dropdown now lists the model
   your nanobot server runs (its `/v1/models`). Picking it is optional: with
   one picked, every turn names it; with none, nanobot uses its own. Save.
4. Restart the bot when the page offers it, then **Test saved config**.

How the lane talks to nanobot:

- Each turn sends only what was just said, as one user message. nanobot
  keeps the conversation itself, per `session_id`: a sticky room uses
  `voice-<room>` (one long-lived conversation per room), a fresh room gets a
  new conversation each time its node connects. The Test button uses its own
  throwaway session, so a test never lands in a room's conversation.
- **Spoken style is up to the nanobot agent.** nanobot accepts no system
  message, so LenoxVI's voice instructions (short, plain spoken sentences,
  no markdown, no lists, no links) do not reach it. Put them in the nanobot
  agent's own instructions, or replies may be read out with formatting.
- If you change the model on the nanobot server, pick the agent again on the
  Intelligence card: nanobot refuses a turn that names a model it does not
  run (the bot log shows nanobot's own message).
- Per-room agent overrides (Rooms → Agent) work as for the other systems.

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

**The name:** `wake.name` is what the assistant is called — set it in the
dashboard's Wake name card (or in config.yaml) to match your wake word. It is
never guessed from the model file, so change it when you change models.
Saying the wake word again while a room is already listening gives a bare
"Hey <name>." transcript; it is dropped instead of answered, including the
spellings speech-to-text invents for a rare name ("Jarvas", "Jervis" for
"Jarvis" — anything within two letters of a name of five or more letters).
Only the whole utterance is matched, never a word inside a real sentence.

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

[Pocket TTS]: https://github.com/kyutai-labs/pocket-tts

## Local TTS (optional): Kokoro

[Kokoro] is an 82M-parameter text-to-speech model that runs on the CPU
through ONNX Runtime: no API key, and nothing leaves your network. It needs
no torch, and onnxruntime is already installed for the wake word.

    pip install -r requirements-kokoro.txt
    bash scripts/fetch_kokoro_models.sh

The script downloads the model and voice files (about 354 MB) into
`~/.cache/lenoxvi/kokoro` (or `$XDG_CACHE_HOME/lenoxvi/kokoro`) and
checks each against a pinned sha256. Run it as the user the bot runs as. The
bot never downloads anything itself: if the files are missing, it logs an
error naming this script. One model is loaded per process and shared by every
room.

Then, in the dashboard's TTS panel, pick **Kokoro (local)** as the provider,
choose a voice and a speed (0.5–1.5). There are 28 English voices: `af_`/`am_`
are American female/male, `bf_`/`bm_` British female/male (default
`af_heart`). In config.yaml these are `tts.kokoro_voice` and
`tts.kokoro_speed`. **Fallback → Pocket (local)** works behind Kokoro the same
way it does behind the cloud providers. Changes apply on the next bot restart.

Both local voices (Pocket and Kokoro) get a number pass before they speak:
"9:06" is read as "nine oh six", "$12.50" as "twelve dollars and fifty
cents", "75%" as "seventy-five percent". The cloud providers don't: they
normalise text themselves.

Licences: the Kokoro model is Apache-2.0 and kokoro-onnx is MIT. kokoro-onnx
converts text to phonemes with espeak-ng, which is GPL-3.0; the
espeakng-loader package ships it as a shared library that is loaded into the
bot's process at run time. LenoxVI does not bundle or modify espeak-ng.

[Kokoro]: https://huggingface.co/hexgrad/Kokoro-82M
