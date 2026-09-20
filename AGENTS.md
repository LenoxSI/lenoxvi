# lenoxvi — agent working rules

House voice rig (v2). Read `notes/v2/` first — that tree is the source of truth for intent and findings. This file is only the guardrails.

**Where docs live:** lenoxvi's notes and docs live in the notes on the NAS at `/path/to/your/notes/notes/` (Obsidian notes `notes`, same SMB share as `rag/`). The repo no longer holds them. All `notes/...` paths in this file are relative to that notes root.

## Guardrails

- **v1 is reference, not foundation.** Never copy code from `~/projects/lenoxvi`. Quote it in notes if useful.
- **Staged ≠ built.** No installs, firmware flashes, or hardware orders without the owner's explicit go. Read-only recon on appliance-host (the appliance host, 203.0.113.185) is fine; state-changing work gets asked by class first.
- **Mic-only nodes.** No speaker, AEC, or barge-in on satellites. Ducking handles overlap; sonos_output is the **Sonos audioClip overlay** (ratified 2026-09-15; see `notes/v2/001-sonos-output.md`). Triad analog paging is dead.
- **Survivability is the point.** The appliance lives on appliance-host and talks to Hermes over a URL — a Hermes rebuild must never take house voice down.
- **This repo goes public one day.** Keep it permanently clone-capable for collaborators: a stranger must be able to clone, follow the README, and reach a running bot. That means — no secrets in committed files or history (`.env` is secrets-only), no host-only paths or machine-specific values baked into tracked files, venv patching avoided in favor of upstream fixes (if a patch is unavoidable, it lives in `patches/` with an apply script), and the README always documents the fresh-clone path (certs, `.env.example`, config, model acquisition). Before flipping public: run the fresh-clone audit (design note).
- `logs/` contents are gitignored; don't commit them.

## Landmines (measured, not lore)

- **sonos_output**: S2 Port/Amp only (`notes/v2/001-sonos-output.md` — clip volume ~70, ~200ms load, WiiM cannot overlay, never route through HA).
- **Node firmware** (reSpeaker, measured 2026-09-19; runbook = `notes/v2/014`): XMOS **I2S-master image only** (slave image = boot lottery); I2C command maps are **per-firmware-image** (Seeed wiki map for v1.0.x); never I2C-scan the bus; ESP32 internal RAM is exhausted post-WiFi/WebRTC — big stacks need static/PSRAM allocation, and mbedtls needs `MBEDTLS_EXTERNAL_MEM_ALLOC` or every TLS call fails alloc.
- **OTA deploys**: bump `project(VERSION)` in the firmware CMakeLists *and* the server manifest together — a mismatch re-flashes the same firmware every boot (design note). USB is retired; the only flash path is `scripts/deploy_firmware.sh`.

## Style

- Append numbered notes in `notes/v2/`; never rewrite history in place.
- Decisions go in notes/, not in chat memory.
- **Tunables go in `config.yaml`** — never hardcode a knob in source or bury it in `.env`. Precedence is config.yaml > `TK_*` env > code default (`core/config.py`); `.env` is secrets only.
