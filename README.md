# lenoxvi

**v2 house-voice appliance.** A pipecat bot that
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
  - `restart.sh` — systemd wrapper: restarts the bot unit on the appliance host (no flags)
  - `deploy_firmware.sh` — build + version + ship a node firmware OTA (VM-side)
  - `sync_host.sh` — bundle-based repo sync VM → host (no creds on hosts)
  - `serial_watch.py` — node serial capture (survives replug)
  - `*_test.py` — headless bench tests for the core pieces
  - `make-cert.sh <bot-host-LAN-IP>` — self-signed HTTPS certs for the bot
- `firmware/` — node firmware images + OTA manifests
  - `xvf3800/` — XMOS image fetched via `scripts/fetch_xmos_fw.sh` at
    bring-up (upstream ships **no license** — the binary is not
    redistributed here; see the script header for md5 + provenance)
  - `node/` — scp-populated at deploy time by `deploy_firmware.sh` (empty in
    a fresh clone by design); served by firmware_server
- `config.example.yaml` — **the tuning surface template**: every behavior knob
  in one file (wake threshold/timeout/model, VAD params, STT/TTS/brain settings,
  Sonos zones, brain lull/dedup). Copy to `config.yaml` (gitignored, per-machine)
  and edit. Precedence: `config.yaml` > `LVI_*` env var > code default
  (`core/config.py`). Secrets stay in `.env`. Edit + `scripts/restart.sh` to
  apply — no rebuild.
- `logs/` — gitignored runtime logs (the bot itself logs to journald:
  `journalctl --user -u lenoxvi-bot`)

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
   (run from the repo root — config and clips paths are relative)

[openWakeWord]: https://github.com/dscripka/openWakeWord

## Where the knowledge lives

- **Dev notes:** the original project keeps them in a private notes outside
  the repo; bring your own. The repo README plus `AGENTS.md`-style docs
  are the self-contained story.
- **Node firmware:** `LenoxVI/lenoxvi-firmware` (branch `respeaker-xvf3800`) —
  the node is pure firmware; lenoxvi only serves it (`/api/offer` + `/firmware/*`).

## Run

### Quick test (foreground)

`python bot.py --host <bot-host-LAN-IP> --port 7860 --esp32` from the repo
root — that's the whole launch line. `--esp32` enables SDP munging for the
ESP32-S3 mic nodes; `--host` must be a LAN-reachable address (not
localhost) or the mic nodes can't connect.

### As a service (always-on)

The production rig runs the bot as a systemd **user** unit — policy config
(launch flags, `.env` loading, CUDA `LD_LIBRARY_PATH` for whisper's
CTranslate2, restart-on-failure, SIGKILL after 10s on a hung WebRTC
session) lives in the unit, not the code. Copy the sanitized unit:
`deploy/lenoxvi-bot.service.example` → `~/.config/systemd/user/`,
edit the `<PLACEHOLDER>`s (venv path, python version in the library paths,
your LAN IP), then `systemctl --user daemon-reload && systemctl --user
enable --now lenoxvi-bot`. Logs: `journalctl --user -u lenoxvi-bot -f`.

### Security posture (read this before exposing the port)

The dashboard is **LAN-only by design, no auth on reads** (design note):
anyone on your LAN can view status and mappings. Mutating routes
(POST/PUT/DELETE under `/api/dashboard/`) carry two guard rails
(`core/dashboard/guard.py`):

- **Origin/Host check** — same-origin browser requests pass; a
  cross-origin `fetch` from any website you visit is rejected (403), as
  is any public-DNS Host header (DNS-rebinding shape). This kills the
  "malicious tab pushes firmware over your LAN" vector without touching
  the no-auth LAN contract. Non-browser callers (curl, scripts) send no
  Origin and pass untouched.
- **Optional token** — set `LVI_DASHBOARD_TOKEN` in `.env` and mutating
  routes also require `X-Dashboard-Token` to match. Leave it empty on a
  trusted LAN (nothing changes); set it if you ignore the LAN-only
  advice or want belt-and-suspenders. The page prompts for it once.

Firmware images are additionally validated before serving (embedded
app-descriptor magic check), so even a pushed image must be a real
ESP32-S3 app image.

On the appliance: `bash scripts/restart.sh` (systemd wrapper — the unit
`lenoxvi-bot.service` owns restart policy and the CUDA wheels'
LD_LIBRARY_PATH). Config/secrets in gitignored `.env` (see `.env.example`)
and `config.yaml` (see `config.example.yaml`).