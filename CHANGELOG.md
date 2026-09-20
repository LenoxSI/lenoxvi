# Changelog

All notable changes to lenoxvi are documented here.

Rig-level versioning only — firmware carries its own `project(VERSION)` in its
CMakeLists, bumped together with the server manifest on every OTA deploy
(see notes notes/v2/016). The two version streams are independent.

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