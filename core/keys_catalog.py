"""Every API key the appliance uses, as rows for the dashboard's Keys card.

Pure (no I/O): the caller passes the saved env (.env now), the live env
(what the running bot loaded) and the config sections. Rows carry presence
and the masked hint only — never a value.
"""
from __future__ import annotations

import re

import intelligence_target

# Static rows, in card order. used_by = features that read the key.
STATIC = [
    {"var": "ELEVENLABS_API_KEY", "label": "ElevenLabs", "used_by": ["TTS"], "kind": "elevenlabs"},
    {"var": "DEEPGRAM_API_KEY", "label": "Deepgram", "used_by": ["TTS", "STT"], "kind": "deepgram"},
    {"var": "LVI_HERMES_KEY", "label": "Hermes", "used_by": ["Intelligence"], "kind": "hermes", "agent": "default"},
    {"var": "LVI_OPENCLAW_KEY", "label": "OpenClaw", "used_by": ["Intelligence"], "kind": "openclaw"},
]
_AGENT_VAR = re.compile(r"^LVI_HERMES_KEY_([A-Z0-9_]+)$")


def agent_of(var: str):
    """'LVI_HERMES_KEY_HA_BUTLER' -> 'ha-butler' (Hermes profile names use
    hyphens); None for anything else. Checked against key_var, so a name
    that wouldn't map back to this variable is refused."""
    m = _AGENT_VAR.match(var or "")
    if not m:
        return None
    agent = m.group(1).lower().replace("_", "-")
    return agent if intelligence_target.key_var("hermes", agent) == var else None


def target_for(var: str):
    """The row template for a variable (static or a Hermes agent key), or None."""
    for row in STATIC:
        if row["var"] == var:
            return dict(row)
    agent = agent_of(var)
    if agent:
        return {"var": var, "label": f"Hermes · {agent}", "used_by": ["Intelligence"], "kind": "hermes",
                "agent": agent}
    return None


def _in_use(row: dict, cfg: dict) -> bool:
    tts = cfg.get("tts") or {}
    stt = cfg.get("stt") or {}
    icfg = cfg.get("intelligence") or {}
    tts_provider = str(tts.get("provider") or "elevenlabs")
    if row["var"] == "ELEVENLABS_API_KEY":
        return tts_provider == "elevenlabs"
    if row["var"] == "DEEPGRAM_API_KEY":
        return tts_provider in ("deepgram", "deepgram-flux") or str(stt.get("provider") or "whisper") == "deepgram"
    kind = str(icfg.get("kind") or "hermes")
    if row["kind"] != kind:
        return False
    return intelligence_target.key_var(kind, intelligence_target.agent_for(icfg, kind)) == row["var"]


def rows(saved_env: dict, live_env: dict, cfg: dict) -> list[dict]:
    templates = [dict(r) for r in STATIC]
    for var in sorted(saved_env):
        t = target_for(var) if agent_of(var) else None
        if t is not None:
            templates.append(t)
    out = []
    for row in templates:
        saved = saved_env.get(row["var"]) or ""
        live = live_env.get(row["var"]) or ""
        out.append({
            **row,
            "set": bool(saved),
            "hint": intelligence_target.key_hint(saved),
            # Saved in .env but not what the running bot loaded.
            "pending_restart": bool(saved) and saved != live,
            "in_use": _in_use(row, cfg),
        })
    return out
