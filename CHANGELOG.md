# Changelog

All notable changes to lenoxvi are documented here.

Rig-level versioning only — firmware carries its own `project(VERSION)` in its
CMakeLists, bumped together with the server manifest on every OTA deploy
(see notes notes/v2/016). The two version streams are independent.

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