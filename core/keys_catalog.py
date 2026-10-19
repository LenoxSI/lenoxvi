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


def agent_of(var: str, known=()):
    """The Hermes agent a variable belongs to: a known agent name (config,
    discovery) whose key_var IS this variable wins; otherwise a best guess
    ('LVI_HERMES_KEY_HA_BUTLER' -> 'ha-butler', profile names use hyphens).
    None for anything that isn't an agent key."""
    m = _AGENT_VAR.match(var or "")
    if not m:
        return None
    for name in known:
        if name and intelligence_target.key_var("hermes", str(name)) == var:
            return str(name)
    return m.group(1).lower().replace("_", "-")


def target_for(var: str, known=()):
    """The row template for a variable (static or a Hermes agent key), or None."""
    for row in STATIC:
        if row["var"] == var:
            return dict(row)
    agent = agent_of(var, known)
    if agent:
        return {"var": var, "label": f"Hermes · {agent}", "used_by": ["Intelligence"], "kind": "hermes",
                "agent": agent}
    return None


def known_agents(cfg: dict) -> list:
    icfg = cfg.get("intelligence") or {}
    agents = icfg.get("agents") or {}
    return [agents.get("hermes")] if isinstance(agents, dict) and agents.get("hermes") else []


def _in_use(row: dict, cfg: dict) -> bool:
    tts = cfg.get("tts") or {}
    stt = cfg.get("stt") or {}
    icfg = cfg.get("intelligence") or {}
    tts_provider = str(tts.get("provider") or "elevenlabs").strip().lower()
    if row["var"] == "ELEVENLABS_API_KEY":
        return tts_provider == "elevenlabs"
    if row["var"] == "DEEPGRAM_API_KEY":
        return tts_provider in ("deepgram", "deepgram-flux") or \
            str(stt.get("provider") or "whisper").strip().lower() == "deepgram"
    kind = str(icfg.get("kind") or "hermes").strip().lower()
    if row["kind"] != kind:
        return False
    return intelligence_target.key_var(kind, intelligence_target.agent_for(icfg, kind)) == row["var"]


def rows(saved_env: dict, live_env: dict, cfg: dict, file_env=None) -> list[dict]:
    """file_env: what .env itself holds — agent rows come only from there
    (a stray process variable never becomes a row Test would send)."""
    templates = [dict(r) for r in STATIC]
    known = known_agents(cfg)
    for var in sorted(saved_env if file_env is None else file_env):
        t = target_for(var, known) if agent_of(var) else None
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
