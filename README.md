# lenoxvi

**v2 house-voice appliance for Willow.** A pipecat bot on appliance-host that
turns mic nodes into a house-wide voice assistant:

    mic node (reSpeaker) → WiFi/WebRTC → wake gate → whisper STT →
    Hermes brain (API) → ElevenLabs TTS → Sonos reply in the room's zone

v1 (`LenoxVI/lenoxvi`) is **reference, not foundation** — never copy it in.

## Layout

- `bot.py` — pipecat pipeline: transport, wake gate, VAD, STT, brain, TTS, Sonos out
- `core/` — the pieces the pipeline wires together
  - `wake_word.py` — openWakeWord gate + turn lock (IDLE/AWAKE/LOCKED;
    default awake window 15s, tunable via `wake.awake_timeout`)
  - `hermes_agent.py` — the real brain (Hermes API over LAN, bearer key);
    **production runs this** (`brain.kind: hermes` in config.yaml)
  - `brain_stub.py` — deterministic echo brain for bench/dev runs without
    the Hermes API (the code default when `brain.kind` is unset — a
    misconfigured deploy answers with canned lines)
  - `stt_local.py` — faster-whisper large-v3-turbo on the GPU
  - `sonos_output.py` — replies as Sonos audioClip overlays (S2 only, vol ~70)
  - `echo_gate.py` — room-echo containment (permanent safety net behind the turn lock)
  - `firmware_server.py` — `/firmware/*` OTA delivery for mic nodes
- `scripts/` — ops & bench tests (each has a docstring header):
  - `restart.sh` — systemd wrapper: restarts the bot unit on appliance-host (no flags)
  - `deploy_firmware.sh` — build + version + ship a node firmware OTA (VM-side)
  - `sync_host.sh` — bundle-based repo sync VM → host (no creds on hosts)
  - `serial_watch.py` — node serial capture (survives replug)
  - `*-test.py` — headless bench tests for the core pieces
  - `make-cert.sh <bot-host-LAN-IP>` — self-signed HTTPS certs for the bot
- `firmware/` — node firmware images + OTA manifests
  - `xvf3800/` — XMOS images; **master** = the one that works, slave = reference only
    (slave bytes live in the notes, not here)
  - `node/` — scp-populated at deploy time by `deploy_firmware.sh` (empty in
    a fresh clone by design); served by firmware_server
- `config.example.yaml` — **the tuning surface template**: every behavior knob
  in one file (wake threshold/timeout/model, VAD params, STT/TTS/brain settings,
  Sonos zones, brain lull/dedup). Copy to `config.yaml` (gitignored, per-machine)
  and edit. Precedence: `config.yaml` > `TK_*` env var > code default
  (`core/config.py`). Secrets stay in `.env`. Edit + `scripts/restart.sh` to
  apply — no rebuild.
- `logs/` — gitignored runtime logs (`bot.log` lives on appliance-host)

## Fresh clone → running bot

1. `pip install -r requirements.txt` (host Python 3.12+; pinned to the
   appliance's 3.14)
2. `cp config.example.yaml config.yaml` and fill in every `<PLACEHOLDER>`
   (brain URL, Sonos zone IPs/UIDs, clip-server host IP)
3. `.env` from `.env.example` — all three keys:
   `ELEVENLABS_API_KEY`, `LVI_TTS_VOICE_ID`, `LVI_HERMES_KEY`
4. Wake-word model: a trained `.onnx` at repo root (gitignored — train your
   own with [openWakeWord], or set `wake.enabled: false` in config.yaml
   until you have one). Point `wake.model` at it if the name differs from
   the default.
5. `bash scripts/make-cert.sh <bot-host-LAN-IP>` (mic access needs HTTPS)
6. `python bot.py`, then open `https://<bot-host-LAN-IP>:7860/client`

[openWakeWord]: https://github.com/dscripka/openWakeWord

## Where the knowledge lives

- **Dev notes:** the notes — `/path/to/your/notes/notes/v2/` (source of truth
  for intent, decisions, and bring-up records; the repo deliberately does not
  carry them). Start at `TODO.md`, then the numbered notes.
- **Node firmware:** `LenoxVI/lenoxvi-firmware` (branch `respeaker-xvf3800`) —
  the node is pure firmware; lenoxvi only serves it (`/api/offer` + `/firmware/*`).

## Run

On appliance-host: `bash scripts/restart.sh` (systemd wrapper — the unit
`lenoxvi-bot.service` owns restart policy and the CUDA wheels'
LD_LIBRARY_PATH). Config/secrets in gitignored `.env` (see `.env.example`)
and `config.yaml` (see `config.example.yaml`).