# lenoxvi — agent working rules

House voice rig (v2). Read `notes/v2/` first — that tree is the source of truth for intent and findings. This file is only the guardrails.

**Where docs live:** lenoxvi's notes and docs live in the notes on the NAS at `/path/to/your/notes/notes/` (Obsidian notes `notes`, same SMB share as `rag/`). The repo no longer holds them. All `notes/...` paths in this file are relative to that notes root.

## Guardrails

- **v1 is reference, not foundation.** Never copy code from `~/projects/lenoxvi`. Quote it in notes if useful.
- **Staged ≠ built.** No installs, firmware flashes, or hardware orders without the owner's explicit go. Read-only recon on appliance-host (the appliance host, 203.0.113.185) is fine; state-changing work gets asked by class first.
- **Mic-only nodes.** No speaker, AEC, or barge-in on satellites. Ducking handles overlap; sonos_output is the **Sonos audioClip overlay** (ratified 2026-09-15; see `notes/v2/001-sonos-output.md`). Triad analog paging is dead.
- **Survivability is the point.** The appliance lives on appliance-host and talks to Hermes over a URL — a Hermes rebuild must never take house voice down.
- `logs/` contents are gitignored; don't commit them.

## Landmines (measured, not lore)

- **sonos_output**: S2 Port/Amp only (`notes/v2/001-sonos-output.md` — clip volume ~70, ~200ms load, WiiM cannot overlay, never route through HA).
- **Node reality**: WiFi first; ESP32-S3 with real PSRAM or the firmware starves; see v1 post-mortem before designing nodes.

## Style

- Append numbered notes in `notes/v2/`; never rewrite history in place.
- Decisions go in notes/, not in chat memory.
