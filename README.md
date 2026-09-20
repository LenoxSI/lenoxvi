# lenoxvi

**v2 house-voice appliance for Willow.** A pipecat bot on appliance-host that
turns mic nodes into a house-wide voice assistant:

    mic node (reSpeaker) → WiFi/WebRTC → wake gate → whisper STT →
    Hermes brain (API) → ElevenLabs TTS → Sonos reply in the room's zone

v1 (`LenoxVI/lenoxvi`) is **reference, not foundation** — never copy it in.

## Layout

- `bot.py` — pipecat pipeline: transport, wake gate, VAD, STT, brain, TTS, Sonos out
- `core/` — the pieces the pipeline wires together
  - `wake_word.py` — openWakeWord gate + turn lock (IDLE/AWAKE/LOCKED, 5s window)
  - `hermes_agent.py` — the real brain (Hermes API over LAN, bearer key);
    **production runs this** (`LVI_BRAIN=hermes` in appliance-host's .env)
  - `brain_stub.py` — deterministic echo brain for bench/dev runs without
    the Hermes API (the code default via `LVI_BRAIN` unset; a misconfigured
    deploy answers with canned lines — fail-loudly by setting LVI_BRAIN)
  - `stt_local.py` — faster-whisper large-v3-turbo on the GPU
  - `sonos_output.py` — replies as Sonos audioClip overlays (S2 only, vol ~70)
  - `echo_gate.py` — room-echo containment (safety net behind the turn lock)
  - `firmware_server.py` — `/firmware/*` OTA delivery for mic nodes
- `scripts/` — ops & bench tests (each has a docstring header):
  - `restart.sh` — bot restart on appliance-host (`--esp32 --host` flags)
  - `deploy_firmware.sh` — build + version + ship a node firmware OTA (VM-side)
  - `serial_watch.py` / `replay_clip.py` — node serial capture / clip replay
  - `*-test.py` — headless bench tests for the core pieces
  - `make-cert.sh` — self-signed HTTPS certs for the bot
- `firmware/` — node firmware images + OTA manifests
  - `node/` — served by firmware_server (`version.json` + current bin)
  - `xvf3800/` — XMOS images; **master** = the one that works, slave = reference only
- `config.yaml` — Sonos zone map (IP + UID per zone)
- `logs/` — gitignored runtime logs (`bot.log` lives on appliance-host)

## Where the knowledge lives

- **Dev notes:** the notes — `/path/to/your/notes/notes/v2/` (source of truth
  for intent, decisions, and bring-up records; the repo deliberately does not
  carry them). Start at `TODO.md`, then the numbered notes.
- **Node firmware:** `LenoxVI/lenoxvi-firmware` (branch `respeaker-xvf3800`) —
  the node is pure firmware; lenoxvi only serves it (`/api/offer` + `/firmware/*`).
- **Working rules:** `AGENTS.md`.

## Run

On appliance-host: `bash scripts/restart.sh` (see the script header for the
LD_LIBRARY_PATH quirk and why it kills -9). Config/secrets in gitignored
`.env` (see `.env.example`).