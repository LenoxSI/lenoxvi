# Changelog

All notable changes to lenoxvi are documented here.

Rig-level versioning only — firmware carries its own `project(VERSION)` in its
CMakeLists, bumped together with the server manifest on every OTA deploy
(see notes notes/v2/016). The two version streams are independent.

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
Sonos audioClip overlay), running as a systemd service on appliance-host,
serving the house daily.

### Added
- Phase 1 bot: local whisper ears, stub brain, ElevenLabs mouth (`8a8e8a8`)
- sonos_output: Sonos audioClip overlay — clip volume ~70, ~200ms load,
  opportunistic clip pruning at startup (`20c2a72`)
- config.yaml as the single tuning surface (yaml > `TK_*` env > code default) (`96ce51b`)
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