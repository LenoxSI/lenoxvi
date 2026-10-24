"""Every API key the appliance uses, as rows for the dashboard's Keys card.

Pure (no I/O): the caller passes the saved env (.env now), the live env
(what the running bot loaded) and the config sections. Rows carry presence
and the masked hint only — never a value.
"""
from __future__ import annotations

import re

import intelligence
import intelligence_target

# Static rows, in card order. used_by = features that read the key
# (ElevenLabs adds STT while Scribe is the STT provider: _used_by).
# One row per intelligence lane, in registry order; a per-agent-key lane's
# row is its default agent's.
STATIC = [
    {"var": "ELEVENLABS_API_KEY", "label": "ElevenLabs", "used_by": ["TTS"], "kind": "elevenlabs"},
    {"var": "DEEPGRAM_API_KEY", "label": "Deepgram", "used_by": ["TTS", "STT"], "kind": "deepgram"},
    *({"var": lane.base_key_var, "label": lane.label, "used_by": ["Intelligence"], "kind": lane.kind,
       **({"agent": lane.default_agent} if lane.per_agent_keys else {})}
      for lane in intelligence.LANES.values()),
]
# The lane whose agents each have their own key variable (<base var>_<AGENT>);
# those keys get agent rows of their own.
_AGENT_LANE = next(lane for lane in intelligence.LANES.values() if lane.per_agent_keys)
_AGENT_VAR = re.compile(rf"^{re.escape(_AGENT_LANE.base_key_var)}_([A-Z0-9_]+)$")


def tts_secret(provider: str | None) -> str | None:
    """The .env key a TTS provider needs (None = keyless, e.g. pocket)."""
    p = str(provider or "")
    if p.startswith("eleven"):
        return "ELEVENLABS_API_KEY"
    if p.startswith("deepgram"):
        return "DEEPGRAM_API_KEY"
    return None


def session_tts_provider(configured: str, boot: str | None, environ) -> tuple[str, str | None]:
    """The TTS provider a new session can actually build, plus a warning.

    Dashboard config edits reach new sessions at once, but keys load only at
    restart. A provider switch saved together with its new key would make
    every reconnecting room fail to build a pipeline (and reboot-loop) until
    the restart. Until then, keep the provider the bot booted with."""
    need = tts_secret(configured)
    if not need or environ.get(need):
        return configured, None
    if boot and boot != configured:
        boot_need = tts_secret(boot)
        if not boot_need or environ.get(boot_need):
            return boot, (f"tts.provider is {configured!r} but {need} isn't loaded yet "
                          f"(keys load at restart) - using {boot!r} until the bot restarts")
    return configured, None


def agent_of(var: str, known=()):
    """The Hermes agent a variable belongs to: a known agent name (config,
    discovery) whose key_var IS this variable wins; otherwise a best guess
    ('LVI_HERMES_KEY_HA_BUTLER' -> 'ha-butler', profile names use hyphens).
    None for anything that isn't an agent key."""
    m = _AGENT_VAR.match(var or "")
    if not m:
        return None
    for name in known:
        if name and intelligence_target.key_var(_AGENT_LANE.kind, str(name)) == var:
            return str(name)
    return m.group(1).lower().replace("_", "-")


def target_for(var: str, known=()):
    """The row template for a variable (static or a Hermes agent key), or None."""
    for row in STATIC:
        if row["var"] == var:
            return dict(row)
    agent = agent_of(var, known)
    if agent:
        return {"var": var, "label": f"{_AGENT_LANE.label} · {agent}", "used_by": ["Intelligence"],
                "kind": _AGENT_LANE.kind, "agent": agent}
    return None


def known_agents(cfg: dict) -> list:
    """Hermes agent names the config uses: the default plus every room
    override — so a key's variable maps back to its real agent name."""
    icfg = cfg.get("intelligence") or {}
    agents = icfg.get("agents") or {}
    kind = _AGENT_LANE.kind
    out = [agents.get(kind)] if isinstance(agents, dict) and agents.get(kind) else []
    rooms = (cfg.get("room_agents") or {}).get(kind) or {}
    for a in (rooms.values() if isinstance(rooms, dict) else []):
        if a and str(a) not in out:
            out.append(str(a))
    return out


def _stt_provider(cfg: dict) -> str:
    return str((cfg.get("stt") or {}).get("provider") or "whisper").strip().lower()


def _used_by(row: dict, cfg: dict) -> list:
    # The ElevenLabs key serves STT only while Scribe is the STT provider.
    if row["var"] == "ELEVENLABS_API_KEY" and _stt_provider(cfg) == "elevenlabs":
        return [*row["used_by"], "STT"]
    return row["used_by"]


def _in_use(row: dict, cfg: dict) -> bool:
    tts = cfg.get("tts") or {}
    icfg = cfg.get("intelligence") or {}
    tts_provider = str(tts.get("provider") or "elevenlabs").strip().lower()
    if row["var"] == "ELEVENLABS_API_KEY":
        return tts_provider == "elevenlabs" or _stt_provider(cfg) == "elevenlabs"
    if row["var"] == "DEEPGRAM_API_KEY":
        return tts_provider in ("deepgram", "deepgram-flux") or _stt_provider(cfg) == "deepgram"
    kind = str(icfg.get("kind") or intelligence.DEFAULT_KIND).strip().lower()
    if row["kind"] != kind:
        return False
    if intelligence_target.key_var(kind, intelligence_target.agent_for(icfg, kind)) == row["var"]:
        return True
    # A room's agent override (room_agents.<kind>.<room>) uses its key too.
    overrides = (cfg.get("room_agents") or {}).get(kind) or {}
    return any(intelligence_target.key_var(kind, str(a)) == row["var"]
               for a in (overrides.values() if isinstance(overrides, dict) else []) if a)


def rows(saved_env: dict, live_env: dict, cfg: dict, file_env=None) -> list[dict]:
    """file_env: what .env itself holds — agent rows come only from there
    (a stray process variable never becomes a row Test would send)."""
    templates = [dict(r) for r in STATIC]
    known = known_agents(cfg)
    for var in sorted(saved_env if file_env is None else file_env):
        t = target_for(var, known) if agent_of(var) else None
        if t is not None:
            templates.append(t)
    # An agent the config uses (house default or a room override) whose key
    # isn't in .env still gets a row, so the card shows it as Missing.
    listed = {t["var"] for t in templates}
    for name in known:
        var = intelligence_target.key_var(_AGENT_LANE.kind, str(name))
        t = target_for(var, known) if var not in listed else None
        if t is not None and _in_use(t, cfg):
            templates.append(t)
            listed.add(var)
    out = []
    for row in templates:
        # .env is the store: with the file given, a removed line is
        # "not set" even while the running process still holds the value.
        saved = (file_env if file_env is not None else saved_env).get(row["var"]) or ""
        live = live_env.get(row["var"]) or ""
        out.append({
            **row,
            "used_by": _used_by(row, cfg),
            "set": bool(saved),
            "hint": intelligence_target.key_hint(saved),
            # .env differs from what the running bot loaded (saved, replaced
            # or removed since the last restart).
            "pending_restart": saved != live,
            "in_use": _in_use(row, cfg),
        })
    return out
