"""routes — the dashboard's JSON API (the stable contract under the skin).

All endpoints live under /api/dashboard/ so they can never collide with
pipecat's own /api/* routes. The HTML page (page.html) is a thin skin
over these; a restyle or a host-side integration consumes the same
endpoints.

No auth by design: LAN-only page. Anyone on
the LAN can read status and edit mappings — accepted risk.
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Literal
from os import getenv as _getenv

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config import DEFAULT_CLIP_VOLUME, cfg, cfg_env
import firmware_image
import firmware_signing
import intelligence
from intelligence import target as intelligence_target
import node_speaker
from paths import clips_dir, firmware_dir, models_dir, state_root, wake_root
import stt_catalog
import tts_catalog
import tts_kokoro
import wake_name

from . import config_store, node_command, registry
from .guard import mutation_guard

class _DashboardRoute(APIRoute):
    """Dashboard routes answer their OWN validation errors (2026-10-13).

    pipecat's runner installs an app-wide RequestValidationError handler
    (pipecat/runner/run.py:_configure_server_app) that (1) calls
    `await request.body()` — on a multipart upload the stream is already
    consumed, so every form 422 became a 500 RuntimeError("Stream
    consumed") on the live app (the suite's plain FastAPI never showed
    it); and (2) logs exc.errors() verbatim — Pydantic puts the rejected
    `input` in each error, so a key PUT that failed validation (e.g. an
    old cached UI omitting `kind`) wrote the FULL API KEY to journald.
    It can't be overridden at app level (register_dashboard runs before
    pipecat's main() re-registers its handler), so the dashboard router
    catches RequestValidationError per-route, before pipecat ever sees
    it: never reads the body, and strips `input`/`ctx` from both the
    log line and the response (loc + msg are what the UI renders).
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                errors = [{k: e[k] for k in ("type", "loc", "msg") if k in e}
                          for e in exc.errors()]
                logger.warning(f"dashboard: 422 on {request.url.path}: {errors}")
                return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errors}))

        return handler


router = APIRouter(prefix="/api/dashboard", route_class=_DashboardRoute)
# Every mutating route gets the Origin/Host guard (+optional
# LVI_DASHBOARD_TOKEN check) — see guard.py.
GUARD = [Depends(mutation_guard)]


# ── models ───────────────────────────────────────────────────────────

# Every intelligence kind, from the lane registry (a tuple subscript is the
# same Literal as listing the members).
_LaneKind = Literal[intelligence.KINDS]


class MappingPayload(BaseModel):
    # extra='forbid': an unwrapped dict (pre-2026-10-07 React bug) must
    # 422 LOUDLY, never silently become room_zones={} via the default
    # factory — that default was a silent-wipe landmine (three real
    # wipes shipped to config.yaml before this guard existed).
    model_config = ConfigDict(extra="forbid")
    room_zones: dict[str, str] = Field(default_factory=dict)
    # Per-room agent overrides, per intelligence system. Omitted = leave
    # them alone (an older page); a system sent = that system's map
    # replaced; "" = back to the house default.
    room_agents: dict[_LaneKind, dict[str, str]] | None = None


class SessionModePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    mode: str = Field(pattern="^(fresh|sticky)$")


class ZonePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=7, max_length=64)
    # player_uid is a SONOS concept (RINCON_…).
    # It used to be required for every backend with min_length=8, while the
    # UI sends '' for WiiM — so adding a WiiM zone from the dashboard 422'd
    # every time, and the suite papered over it with a fake RINCON.
    # Optional at the field; the validator below requires it for sonos only.
    player_uid: str | None = Field(default=None, max_length=64)
    backend: str = Field(default="sonos", pattern="^(sonos|wiim)$")

    @model_validator(mode="after")
    def _uid_per_backend(self):
        uid = (self.player_uid or "").strip()
        if self.backend == "sonos":
            if len(uid) < 8:
                raise ValueError("sonos zones need a player_uid (RINCON_…, 8+ chars)")
            self.player_uid = uid
        else:
            # WiiM's HTTP API is UID-less — accept-and-drop whatever the
            # form sent ('' today) so a stray value never lands in config.
            self.player_uid = None
        return self


# ── status ───────────────────────────────────────────────────────────


def _intelligence_label() -> str:
    """Rig-facts label for the Intelligence line: 'OpenClaw · <agent>'
    or 'Hermes'. Resolved through the same cfg_env chain the agent uses."""
    intelligence_cfg = config_store.read_section("intelligence") or {}
    default = intelligence.DEFAULT_KIND
    kind = str(cfg_env("intelligence", "kind", "LVI_INTELLIGENCE_KIND", intelligence_cfg.get("kind") or default) or default)
    # No intelligence picked yet: the bot answers from the echo stub, so say so
    # (a fresh install runs this way until a lane is set up).
    if kind.strip().lower() == "stub":
        return "echo"
    # An unknown kind shows the default lane's label.
    lane = _lane(kind) or intelligence.LANES[default]
    agent = lane.agent_for(intelligence_cfg)
    if lane.legacy_agent_key:
        agent = agent or str(_getenv("LVI_INTELLIGENCE_AGENT") or "")
    return f"{lane.label} · {agent}" if agent else lane.label


def _lane(kind):
    """The lane for `kind`, matched exactly. These routes always compared
    kind case-sensitively (a mixed-case kind took the default path), so
    they don't use intelligence.get(), which lowercases."""
    return intelligence.LANES.get(kind) if isinstance(kind, str) else None


@router.get("/status")
async def status():
    boot = registry.boot_time()
    tts_cfg = config_store.read_section("tts") or {}
    return {
        "uptime_secs": round(time.time() - boot, 1) if boot else None,
        "version": registry.version(),
        # Rig facts (Settings tab header, read-only) — resolved through
        # the SAME chains the consumers use, so the dashboard always
        # shows what's actually constructed, never static text:
        # config > env > code default.
        # STT card chips: load state + per-command transcription time (median
        # of the last 10), straight from the shared model's owner.
        "stt": _stt_status(),
        # Unset or unknown = Embedded (Kokoro), as the bot resolves it.
        "tts_provider": tts_catalog.resolve_provider(
            cfg_env("tts", "provider", "LVI_TTS_PROVIDER", tts_cfg.get("provider"))),
        # Intelligence: rig-facts label — same cfg_env chain
        # the agent reads, so the dashboard shows what will actually be
        # constructed. Config section, env vars, and UI all say
        # "intelligence" — one name, everywhere (naming rule).
        "intelligence": _intelligence_label(),
        "last_wake": registry.last_wake(),
        "rooms": [{k: v for k, v in r.items() if k != "speaker_ok"} for r in registry.known_rooms()],
        # Session modes (fresh | sticky per node) — the dashboard renders
        # these as the per-node checkbox; sticky rooms keep conversation
        # memory across reconnects/restarts.
        "session_modes": dict(config_store.read_section("session_modes") or {}),
        # Saved settings the running bot hasn't loaded yet (labels only).
        # Off the loop: the key half reads .env.
        "restart_pending": await asyncio.to_thread(_restart_pending),
    }


def _plain(v):
    """A config value with unset parts dropped ({}, [], "" and None all
    mean "not set"), so a save that writes an empty map isn't a change."""
    if isinstance(v, dict):
        v = {k: _plain(x) for k, x in v.items()}
        return {k: x for k, x in v.items() if x not in (None, "", {}, [])}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _restart_pending() -> list:
    """Labels of the saved settings the running bot isn't using yet: the
    boot snapshot vs the saved config, for the settings bot.py reads only
    when it starts (each such save logs "applies on next restart"), plus
    in-use keys whose .env value differs from the running process. Live
    settings (zones, volumes, room_zones, wake name, session modes) never
    appear. Sync: reads .env, so callers run it off the event loop."""
    import config as _config
    import keys_catalog

    boot, saved = _config.boot_config(), _config._load()

    def pick(conf, section, keys=None):
        sec = conf.get(section) or {}
        if keys is not None and isinstance(sec, dict):
            sec = {k: sec.get(k) for k in keys}
        return _plain(sec)

    def differs(section, keys=None):
        return pick(boot, section, keys) != pick(saved, section, keys)

    out = []
    # urls is the per-kind recall map: saving another kind's URL changes nothing live.
    if differs("intelligence", ("kind", "url", "agent", "agents", "model")):
        out.append("intelligence")
    if differs("tts"):
        out.append("tts")
    tuning = {}
    for section, key in _TUNING_TARGETS.values():
        tuning.setdefault(section, []).append(key)
    if any(differs(s, keys) for s, keys in tuning.items()):
        out.append("voice tuning")
    # A room override is read for the running kind only.
    kinds = {str(pick(c, "intelligence").get("kind") or os.getenv("LVI_INTELLIGENCE_KIND")
                 or intelligence.DEFAULT_KIND).lower()
             for c in (boot, saved)}
    if differs("room_agents", sorted(kinds)):
        out.append("room agents")
    if differs("wake", ("model",)):
        out.append("wake model")
    stt_keys = ["provider"]   # the Embedded model is fixed: nothing else in stt waits for a restart
    # Whisper reads the language live; a cloud provider fixes it when the session is built.
    if any(stt_catalog.is_cloud(str(pick(c, "stt").get("provider") or os.getenv("LVI_STT_PROVIDER") or "").lower())
           for c in (boot, saved)):
        stt_keys.append("language")
    if differs("stt", stt_keys):
        out.append("stt")
    # LocalAI settings matter only while a card uses LocalAI (an address
    # saved ahead of time needs no restart).
    def _uses_localai(c):
        return any(str(pick(c, s).get("provider") or os.getenv(env) or "").lower() == "localai"
                   for s, env in (("stt", "LVI_STT_PROVIDER"), ("tts", "LVI_TTS_PROVIDER")))
    if any(_uses_localai(c) for c in (boot, saved)) and differs("localai"):
        out.append("localai")
    # A key nothing uses yet needs no restart.
    for row in keys_catalog.rows(_saved_env(), dict(os.environ), _keys_cfg(), file_env=_file_env()):
        if row["in_use"] and row["pending_restart"]:
            out.append(f"{row['label'].lower()} key")
    return out


# ── mapping ───────────────────────────────────────────────────────────


def _zones() -> dict:
    return dict(cfg("sonos", "zones") or {})


def _wiim_zones() -> dict:
    return dict(cfg("wiim", "zones") or {})


def _all_zones() -> dict:
    """Merged zone namespace with backend tags: every zone (Sonos overlay
    or WiiM interrupt) in one dict. The backend tag decides which output
    path plays replies and how the test/volume routes behave
    (2026-10-04, WiiM arrival)."""
    return {**{k: {**v, "backend": "sonos"} for k, v in _zones().items()},
            **{k: {**v, "backend": "wiim"} for k, v in _wiim_zones().items()}}


def _backend_of(name: str) -> str:
    return "wiim" if name in _wiim_zones() else "sonos"


@router.get("/mapping")
async def get_mapping():
    zones = _all_zones()
    out = {
        "room_zones": dict(config_store.read_section("room_zones") or {}),
        "room_agents": dict(config_store.read_section("room_agents") or {}),
        **_agent_default(),
        "zones": {name: {k: v for k, v in z.items() if k != "player_uid"} for name, z in zones.items()},
        # What each zone ACTUALLY plays at — the slider's only
        # source (the UI's own `?? 70` fallback disagreed with the backends).
        "effective_volumes": {z: effective_zone_volume(z) for z in zones},
    }
    # speaker nodes — their private built-in zone (never in "zones").
    # Listed when the board can have one, or config already maps it there
    # (an offline node after a restart keeps its row).
    online = {r["room"]: r for r in registry.known_rooms()}
    spk_rooms = sorted(set(registry.capable_rooms())
                       | {r for r, z in out["room_zones"].items() if z == node_speaker.BUILTIN})
    out["speakers"] = {
        room: {"volume": node_speaker.volume_for(room, cfg("node_speaker", "volumes")),
               "zone": node_speaker.zone_for(room),
               "online": bool(online.get(room, {}).get("online")),
               "speaker_ok": online.get(room, {}).get("speaker_ok")}
        for room in spk_rooms
    }
    return out


def _agent_default() -> dict:
    """The intelligence system the bot runs and its house-default agent —
    what a room without an override uses. Same inputs as the bot
    (IntelligenceAgent.__init__), env fallbacks included."""
    default = intelligence.DEFAULT_KIND
    kind = str(cfg_env("intelligence", "kind", "LVI_INTELLIGENCE_KIND", default) or default).strip().lower()
    icfg = {"agent": cfg_env("intelligence", "agent", "LVI_INTELLIGENCE_AGENT", ""),
            "agents": dict(config_store.read_section("intelligence") or {}).get("agents") or {}}
    lane = _lane(kind)
    agent = intelligence_target.agent_for(icfg, kind) or ((lane.default_agent if lane else None) or "")
    return {"agent_kind": kind, "default_agent": agent}


def _merge_room_agents(sent: dict, room_zones: dict) -> dict:
    """room_agents after a PUT: each sent system's map replaces the stored
    one ("" drops a room = back to the default). Only a NEW or CHANGED pick
    must name a mapped or connected room — a leftover override (its zone
    deleted, its node retired) passes unchanged or cleared, so it can never
    wedge every later save."""
    known = set(room_zones) | {k["room"] for k in registry.known_rooms()}
    known.discard(registry.PROVISIONAL)
    agents = dict(config_store.read_section("room_agents") or {})
    for kind, rooms in sent.items():
        stored = dict(agents.get(kind) or {})
        picked = {}
        for room, agent in rooms.items():
            room, agent = room.strip(), (agent or "").strip()
            if not agent:
                continue
            if not intelligence_target.AGENT_RE.match(agent):
                raise HTTPException(status_code=400, detail=f"invalid agent name for room '{room}'")
            if agent != str(stored.get(room) or "") and room not in known:
                raise HTTPException(status_code=400, detail=f"room '{room}' is not mapped or connected")
            picked[room] = agent
        agents[kind] = picked
    return agents


@router.put("/mapping", dependencies=GUARD)
async def put_mapping(payload: MappingPayload):
    # Belt-and-suspenders: an explicit empty mapping is never a legal
    # dashboard save (the UI always saves at least one row). Refuse it
    # so no client bug can ever wipe all mappings in one request.
    if not payload.room_zones:
        raise HTTPException(
            status_code=400,
            detail="refusing to wipe all room mappings — send the full room_zones dict",
        )
    zones = _all_zones()
    # Validate BEFORE writing: every mapped zone must exist (either backend).
    rz = {r.strip(): z for r, z in payload.room_zones.items()}
    # `builtin` is a node's own speaker — only for a node that has PROVEN
    # one. An entry config already holds is left alone: after a restart the
    # node may be offline, and the UI always PUTs the full dict.
    current = dict(config_store.read_section("room_zones") or {})
    no_spk = sorted(r for r, z in rz.items()
                    if z == node_speaker.BUILTIN and not registry.has_speaker(r)
                    and current.get(r) != node_speaker.BUILTIN)
    if no_spk:
        raise HTTPException(
            status_code=400,
            detail=f"room(s) {no_spk} have no built-in speaker",
        )
    bad = {r: z for r, z in rz.items()
           if z not in zones and z != node_speaker.BUILTIN}
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown zone(s) {sorted(bad.values())} for room(s) {sorted(bad)}",
        )
    clean = {}
    for room, zone in payload.room_zones.items():
        room = room.strip()
        if not room:
            raise HTTPException(status_code=400, detail="empty room name")
        clean[room] = zone
    sections = {"room_zones": clean}
    try:
        with config_store.LOCK:  # read-merge-write of room_agents as one step
            before = config_store.read_section("room_zones")
            if payload.room_agents is not None:
                sections["room_agents"] = _merge_room_agents(payload.room_agents, clean)
            config_store.write_sections(sections)  # one atomic pass
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # exactly one output sounds a reply — tell every speaker node
    # whether it's now the one (a full-dict PUT can map OR unmap it).
    node_speaker.sync_all(registry.capable_rooms())
    return {"ok": True, "room_zones": clean, "room_agents": sections.get("room_agents"),
            "changed": (before or {}) != clean}


# ── node speaker (a node's built-in speaker zone) ────────────────


class NodeSpeakerVolumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room: str = Field(min_length=1, max_length=64)
    volume: int = Field(ge=0, le=100, strict=True)


class NodeSpeakerRoomPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room: str = Field(min_length=1, max_length=64)


def _speaker_room_or_404(room: str) -> str:
    """A board that has a speaker, or a room config maps to one (its node
    may be offline after a restart)."""
    room = room.strip()
    mapped = (config_store.read_section("room_zones") or {}).get(room)
    if not (registry.speaker_capable(room) or mapped == node_speaker.BUILTIN):
        raise HTTPException(status_code=404, detail=f"'{room}' has no built-in speaker")
    return room


@router.put("/node-speaker/volume", dependencies=GUARD)
async def put_node_speaker_volume(payload: NodeSpeakerVolumePayload):
    room = _speaker_room_or_404(payload.room)
    section = dict(config_store.read_section("node_speaker") or {})
    volumes = {**(section.get("volumes") or {}), room: payload.volume}
    try:
        config_store.write_section("node_speaker", {**section, "volumes": volumes})
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    sent = node_speaker.sync(room)
    return {"ok": True, "room": room, "volume": payload.volume, "sent": sent}


@router.post("/node-speaker/test", dependencies=GUARD)
async def test_node_speaker(payload: NodeSpeakerRoomPayload):
    """Play the test tone on a node's own speaker — queued into THAT
    node's output transport only (no Sonos/WiiM). Only when its replies
    actually go to the built-in speaker (otherwise the node is told to stay
    silent and the tone would vanish)."""
    room = _speaker_room_or_404(payload.room)
    if node_speaker.zone_for(room) != node_speaker.BUILTIN:
        raise HTTPException(status_code=409,
                            detail=f"'{room}' replies go to a shared zone, not its built-in speaker")
    out, _pc = registry.sender_for_room(room)
    if out is None:
        raise HTTPException(status_code=409, detail=f"'{room}' is offline")
    from pipecat.frames.frames import OutputAudioRawFrame

    pcm = _test_tone_pcm()
    # THIS room's gate(s), not the one global: with several nodes the
    # global belongs to whichever session connected last, and the tone
    # would block a different room's echo window.
    for gate in registry.gates_for_rooms([room]):
        gate.block(len(pcm) / (2 * 24000) + 1.5)  # same margin as the zone test
    await out.queue_frame(OutputAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1))
    return {"ok": True, "room": room}


# ── zones ────────────────────────────────────────────────────────────


@router.post("/zones/discover", dependencies=GUARD)
async def discover_zones():
    """Scan the LAN for Sonos players (soco UPnP discovery)."""
    try:
        import soco
    except ImportError:
        raise HTTPException(status_code=503, detail="soco not installed")
    found = []
    try:
        # soco UPnP discovery blocks 5s+ — off the event loop.
        for z in await asyncio.to_thread(soco.discover, timeout=5) or []:
            try:
                # player_name is cached by get_speaker_info (a per-device
                # UPnP call): run it in the thread so the read below never
                # blocks the event loop.
                await asyncio.to_thread(z.get_speaker_info)
                found.append({"ip": z.ip_address, "name": z.player_name, "uid": z.uid})
            except Exception as e:  # one flaky device must not kill the scan
                logger.warning(f"discover: skipped a device: {e!r}")
    except Exception as e:
        logger.warning(f"discover: scan failed: {e!r}")
        raise HTTPException(status_code=502, detail=f"discovery failed: {e}")
    found.sort(key=lambda d: d["name"].lower())
    return {"found": found}


class ZoneVolumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    volume: int = Field(ge=0, le=100)


# ── per-zone clip volume (dashboard slider) ───────────────────────────


@router.put("/zones/{name}/volume", dependencies=GUARD)
async def set_zone_volume(name: str, payload: ZoneVolumePayload):
    """Set a zone's voice-clip volume (0-100) from the dashboard slider.

    Two layers, one call: writes `sonos.zone_volumes` in config.yaml
    (durable, survives restarts) AND updates sonos_output's live override
    so the NEXT clip uses it immediately — volume tuning is a by-ear
    loop and the restart contract would make it useless (2026-10-04).
    Resolution: live override > config per-zone > global clip_volume.
    """
    from sonos_output import zone_volume_overrides
    from wiim_output import wiim_volume_overrides

    # Read-modify-write from the FILE, not the boot-cached cfg(): the cache
    # only refreshes on dashboard writes, so rebuilding the section from it
    # would silently undo any hand edit made since the last one.
    def _write():
        with config_store.LOCK:
            fresh = config_store.read_sections(["sonos", "wiim"])
            backend = "wiim" if name in ((fresh["wiim"] or {}).get("zones") or {}) else "sonos"
            section = dict(fresh[backend] or {})
            if name not in (section.get("zones") or {}):
                raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
            vols = {k: int(v) for k, v in (section.get("zone_volumes") or {}).items()}
            before = vols.get(name)
            if payload.volume == int(section.get("clip_volume", DEFAULT_CLIP_VOLUME)):
                # Setting a zone back to the global value = no override needed;
                # keep the config minimal (same pattern as session modes).
                vols.pop(name, None)
            else:
                vols[name] = payload.volume
            config_store.write_section(backend, {**section, "zone_volumes": vols})
            return backend, vols, before

    try:
        backend, vols, before = await asyncio.to_thread(_write)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # Live layer: next clip in this zone plays at the new volume.
    if backend == "wiim":
        wiim_volume_overrides(vols)
    else:
        zone_volume_overrides(vols)
    logger.info(f"dashboard: zone '{name}' ({backend}) clip volume -> {payload.volume} (live)")
    return {"ok": True, "zone": name, "volume": payload.volume, "before": before,
            "backend": backend}


@router.post("/zones", dependencies=GUARD)
async def add_zone(payload: ZonePayload):
    name = payload.name.strip()
    # '/' too: the zone's volume/test/delete routes carry the name as one
    # path segment, so a zone named with a slash could never be managed.
    if not name or any(c in name for c in ".:/"):
        raise HTTPException(status_code=400, detail="bad zone name: '.', ':' and '/' aren't allowed")
    if node_speaker.is_reserved_zone_name(name):
        raise HTTPException(status_code=400, detail=f"'{name}' is reserved (a node's built-in speaker)")

    # Fresh read under the store's lock, same reason as the volume route:
    # the cached cfg() would write back stale values over hand edits.
    def _write():
        with config_store.LOCK:
            fresh = config_store.read_sections(["sonos", "wiim"])
            sonos, wiim = dict(fresh["sonos"] or {}), dict(fresh["wiim"] or {})
            # Collision check across BOTH backends: one merged namespace (the
            # mapping routes and the room_zones config key see one zone list).
            if name in (sonos.get("zones") or {}) or name in (wiim.get("zones") or {}):
                raise HTTPException(status_code=400, detail=f"zone '{name}' already exists")
            if payload.backend == "wiim":
                # WiiM zones need no player_uid (HTTP API is UID-less); the
                # payload validator already dropped any sent value. Adding a zone to a
                # backend IMPLIES enabling it: a first WiiM zone once went
                # silent because the wiim section was written without
                # enabled: true and WiiMOutput constructed disabled (replies
                # dropped before ever reaching the network).
                zones = dict(wiim.get("zones") or {})
                zones[name] = {"ip": payload.ip.strip()}
                merged = {**wiim, "enabled": True, "zones": zones}
                if not merged.get("default_zone"):
                    merged["default_zone"] = name
                config_store.write_section("wiim", merged)
            else:
                zones = dict(sonos.get("zones") or {})
                zones[name] = {"ip": payload.ip.strip(), "player_uid": payload.player_uid.strip()}
                # Adding a zone turns its backend on (same as WiiM above).
                config_store.write_section("sonos", {**sonos, "enabled": True, "zones": zones})
            return zones

    try:
        zones = await asyncio.to_thread(_write)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "zones": zones, "backend": payload.backend}


@router.delete("/zones/{name}", dependencies=GUARD)
async def delete_zone(name: str):
    # Fresh read under the store's lock, same reason as the volume route:
    # the cached cfg() would write back stale values over hand edits, and
    # the default-zone handoff below must see the default as it is now.
    def _write():
        with config_store.LOCK:
            fresh = config_store.read_sections(["sonos", "wiim", "room_zones"])
            backend = "wiim" if name in ((fresh["wiim"] or {}).get("zones") or {}) else "sonos"
            sec = dict(fresh[backend] or {})
            zones = dict(sec.get("zones") or {})
            if name not in zones:
                raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
            del zones[name]
            sec["zones"] = zones
            # ONE atomic multi-section write — the old back-to-back
            # write_section pair was non-atomic (crash between them left a
            # deleted zone with dangling room mappings) and churned two
            # backups. Sections dict preserves every OTHER key untouched.
            sections = {backend: sec}
            # The backend's default zone must not point at the deleted zone: every
            # unmapped room's reply would go nowhere. Hand it to a remaining zone
            # (add-zone sets the first one as default), or clear it.
            new_default = sec.get("default_zone")
            if new_default == name:
                new_default = next(iter(zones), None)
                if new_default:
                    sec["default_zone"] = new_default
                else:
                    sec.pop("default_zone", None)
            # rooms mapped to a deleted zone become dangling — drop them in the
            # SAME write (they're a separate section, same atomic pass)
            mapping = dict(fresh["room_zones"] or {})
            dangling = [r for r, z in mapping.items() if z == name]
            if dangling:
                for r in dangling:
                    del mapping[r]
                sections["room_zones"] = mapping
            config_store.write_sections(sections)
            return backend, new_default, dangling

    try:
        backend, new_default, dangling = await asyncio.to_thread(_write)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # A speaker room mapped to the deleted zone is back on its own speaker
    # (the default) — tell its node, or it stays silent.
    node_speaker.sync_all(registry.capable_rooms())
    return {"ok": True, "dropped_room_mappings": dangling, "backend": backend,
            "default_zone": new_default}


# ── zone test (the proven clip path, zero API cost) ──────────────────


def _test_tone_pcm() -> bytes:
    """Two rising beeps, 24kHz mono int16 — short, unmistakable, cheap."""
    sr = 24000

    def beep(freq: float, dur: float) -> np.ndarray:
        t = np.linspace(0, dur, int(sr * dur), endpoint=False)
        w = np.sin(2 * np.pi * freq * t)
        fade = max(1, int(sr * 0.01))
        w[:fade] *= np.linspace(0, 1, fade)
        w[-fade:] *= np.linspace(1, 0, fade)
        return w

    audio = np.concatenate(
        [beep(880, 0.15), np.zeros(int(sr * 0.10)),
         beep(1320, 0.15), np.zeros(int(sr * 0.08)),
         beep(880, 0.12)]
    )
    return (audio * 20000).astype("<i2").tobytes()


async def _clip_server():
    """The process-wide clip server singleton: the same ONE server the output backends bind in their
    setup() — never a second bind, never an orphan the next pipeline
    can't reuse."""
    from sonos_output import shared_clip_server
    return await shared_clip_server()


@router.post("/zones/{name}/test", dependencies=GUARD)
async def test_zone(name: str):
    zones = _all_zones()
    if name not in zones:
        raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
    backend = _backend_of(name)
    import lameenc

    server = await _clip_server()
    pcm = _test_tone_pcm()
    # lameenc encode blocks — off the event loop.
    def _encode():
        enc = lameenc.Encoder()
        enc.set_bit_rate(96)
        enc.set_in_sample_rate(24000)
        enc.set_channels(1)
        enc.set_quality(2)
        return bytes(enc.encode(pcm)) + bytes(enc.flush())

    mp3 = await asyncio.to_thread(_encode)

    # Zone keys are free text (native Sonos names — "Sonos Port") but a
    # filename is not: the Port's audioClip fetcher fails on raw spaces
    # (2026-10-04 — test tone silent on 'Sonos Port', fine on
    # 'living_room'; replies never hit this because their names are
    # generated). Slugify before it reaches the filesystem/URL.
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    filename = f"test-{slug}-{int(time.time())}.mp3"
    path = clips_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mp3)
    url = server.url_for(filename)

    duration = len(pcm) / (2 * 24000)
    # Block the echo gates of every room mapped to THIS zone (same read
    # as the mapping routes) — the one global gate belonged to whichever
    # session connected last, so the tone blocked a different room's
    # echo window.
    mapped_rooms = [r for r, z in (config_store.read_section("room_zones") or {}).items()
                    if z == name]
    for gate in registry.gates_for_rooms(mapped_rooms):
        gate.block(duration + 1.5)  # same margin sonos_output uses

    zone = zones[name]
    if backend == "wiim":
        # WiiM lane: interrupt semantics — snap the unit's main volume,
        # set the voice vol, play the tone via the HTTP API, restore.
        # 'OK' is documented-cheap; getPlayerStatus is the real check.
        from wiim_output import wiim_api, wiim_get_status
        ip = zone["ip"]

        def _wiim_test():
            st = wiim_get_status(ip)
            raw = st.get("vol") if st else None
            before_vol = int(raw) if raw is not None else None
            if before_vol is not None:
                wiim_api(ip, f"setPlayerCmd:vol:{_zone_test_volume(name, backend)}")
            wiim_api(ip, "setPlayerCmd:play:" + urllib.parse.quote(url, safe=""))
            return before_vol

        try:
            before_vol = await asyncio.to_thread(_wiim_test)
            await asyncio.sleep(duration + 0.6)
            if before_vol is not None:
                await asyncio.to_thread(wiim_api, ip, f"setPlayerCmd:vol:{before_vol}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"WiiM refused the clip: {e}")
        logger.info(f"dashboard: test clip on '{name}' (wiim {ip}, {url})")
        return {"ok": True, "zone": name, "url": url, "backend": backend}

    from sonos_websocket import SonosWebsocket
    sw = SonosWebsocket(zone["ip"], player_id=zone["player_uid"])
    try:
        await sw.connect()
        # Honor the zone's slider volume (live override > config per-zone >
        # global) so the tone IS the tuning instrument it was meant to be.
        vol = _zone_test_volume(name, backend)
        await sw.play_clip(url, volume=vol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sonos refused the clip: {e}")
    finally:
        await sw.close()
    logger.info(f"dashboard: test clip on '{name}' ({url})")
    return {"ok": True, "zone": name, "url": url, "backend": backend}


def effective_zone_volume(name: str, backend: str | None = None) -> int:
    """THE volume a clip in `name` plays at:
    live override (dashboard slider) > `<backend>.zone_volumes` >
    `<backend>.clip_volume` > DEFAULT_CLIP_VOLUME — resolved in the zone's
    OWN backend section (the UI used to show WiiM zones Sonos's global).
    Same precedence the output backends apply at reply time; the
    dashboard displays this number instead of re-deriving it."""
    backend = backend or _backend_of(name)
    if backend == "wiim":
        from wiim_output import live_wiim_volume as _live
        section = cfg("wiim") or {}
    else:
        from sonos_output import live_zone_volume as _live
        section = cfg("sonos") or {}
    vol = _live(name)
    if vol is not None:
        return int(vol)
    zv = (section.get("zone_volumes") or {}).get(name)
    if zv is not None:
        return int(zv)
    gv = section.get("clip_volume")
    return int(gv) if gv is not None else DEFAULT_CLIP_VOLUME


def _zone_test_volume(name: str, backend: str) -> int:
    """Test-tone volume = the zone's effective volume (one resolver)."""
    return effective_zone_volume(name, backend)


# ── node commands (firmware 1.1.0: restart / scan / set_wifi / set_name) ──


class WifiPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    ssid: str = Field(min_length=1, max_length=32)
    # Up to 63 chars is a WPA passphrase; exactly 64 is a raw PSK and must be
    # hex (firmware 1.2.35+ stores it; the node applies the same rule).
    password: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _psk_is_hex(self):
        if len(self.password) == 64 and not re.fullmatch(r"[0-9A-Fa-f]{64}", self.password):
            raise ValueError("a 64-character password must be a hex PSK (0-9, a-f)")
        return self


class NamePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    name: str = Field(min_length=1, max_length=31, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def _not_reserved(self):
        # The registry's own labels: a node named "unknown" would read as a
        # session whose room isn't known yet (fw, mute and speaker never
        # bind), and one named "browser" would share its room with every
        # browser voice client.
        if self.name.lower() in (registry.PROVISIONAL, registry.BROWSER):
            raise ValueError(f"'{self.name}' is a reserved name - pick another name")
        return self


def _room_online(room: str) -> None:
    if not any(r["room"] == room and r["online"] for r in registry.known_rooms()):
        raise HTTPException(status_code=409, detail=f"node '{room}' is not online")


_HALF_OPEN_HINT = (
    "node not reachable — the session may be half-open (the node thinks "
    "it's connected, the bot can't reach it). The liveness watchdog "
    "reboots the node and it self-heals within ~10 min; try again after "
    "it reconnects."
)


async def _node_cmd(room: str, cmd: str, args: dict | None = None,
                    timeout: float = 12.0):
    """One node command with honest HTTP mapping.

    An in-flight duplicate is a CONFLICT: 409, never the old 504 that
    the UI rendered as a naked 'Gateway Timeout'. A bare transport
    timeout (empty detail — the bare restart error shape) and a
    half-open session both say WHAT happened and that it self-heals."""
    try:
        return await node_command.request(room, cmd, args, timeout)
    except node_command.CommandInFlight as e:
        raise HTTPException(status_code=409, detail=str(e))
    except node_command.NoLiveSession as e:
        # transport exceptions stay terse — the HTTP boundary owns the
        # human wording, so every NoLiveSession says the half-open shape
        # and that it self-heals, whatever the raiser said.
        raise HTTPException(status_code=504, detail=f"{e} — the session may "
              "be half-open; the liveness watchdog reboots the node and it "
              "self-heals within ~10 min")
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e) or _HALF_OPEN_HINT)


@router.post("/nodes/{room}/restart", dependencies=GUARD)
async def restart_node(room: str):
    _room_online(room)
    reply = await _node_cmd(room, "restart", timeout=8.0)
    # A node that answers ok:false did NOT restart — 502, never a
    # lying 200 (same contract as scan/name/mute/info).
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"restart failed: {reply.get('data')}")
    return reply


@router.post("/nodes/{room}/scan", dependencies=GUARD)
async def scan_node(room: str):
    _room_online(room)
    reply = await _node_cmd(room, "scan", timeout=20.0)
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"scan failed: {reply.get('data')}")
    return reply  # {"ok": true, "data": {"ssid": rssi, ...}}


@router.post("/nodes/{room}/wifi", dependencies=GUARD)
async def set_node_wifi(room: str, payload: WifiPayload):
    _room_online(room)
    # Fire-and-report: the node stores the new creds as a TRIAL and reboots
    # (firmware 1.2.23+). The trial passes only when the node reaches THIS
    # bot on the new network; after 3 failed boots (~90 s each) it rolls
    # back to the previous network by itself. Older firmware applied live
    # and called "got an IP" success.
    reply = await _node_cmd(room, "set_wifi",
                            {"ssid": payload.ssid, "pass": payload.password},
                            timeout=8.0)
    # The immediate ack is ok:false when the node REJECTED the creds
    # before trying them (fw cmd.cpp: "missing ssid/pass", "nvs error") —
    # the UI used to show "Wi-Fi applying…" on a 200 for a node that
    # applied nothing. Only ok:true ("stored, rebooting to verify"; older
    # firmware: "stored, connecting") means applying.
    # Never echo the password: the node's data is a status string.
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"wifi rejected by node: {reply.get('data')}")
    return reply


def _room_name_taken(name: str, room: str) -> str | None:
    """An existing room `name` would collide with (case-insensitive; the
    room itself doesn't count, so a case-only rename is fine)."""
    low = name.lower()
    if low == room.lower():
        return None
    names = {r["room"] for r in registry.known_rooms()}
    for section in ("room_zones", "session_modes"):
        names |= set((config_store.read_section(section) or {}).keys())
    for rooms in (config_store.read_section("room_agents") or {}).values():
        if isinstance(rooms, dict):
            names |= set(rooms.keys())
    names |= set(((config_store.read_section("node_speaker") or {}).get("volumes") or {}).keys())
    return next((str(n) for n in names if str(n).lower() == low), None)


# Names a rename is waiting on its node for (lowercase). The name only
# becomes taken when the node acks, up to 12 s later, so without this two
# renames onto one name would both pass the check.
_renames_in_flight: set = set()


@router.post("/nodes/{room}/name", dependencies=GUARD)
async def set_node_name(room: str, payload: NamePayload):
    _room_online(room)
    # A rename onto an existing room would overwrite its zone, agent,
    # volume and mode, and leave two sessions claiming one room.
    taken = _room_name_taken(payload.name, room)
    if taken:
        raise HTTPException(status_code=409,
                            detail=f"a room named '{taken}' already exists - pick another name")
    target = payload.name.lower()
    if target in _renames_in_flight:
        raise HTTPException(status_code=409,
                            detail=f"another node is being renamed '{payload.name}' - pick another name")
    _renames_in_flight.add(target)
    try:
        reply = await _node_cmd(room, "set_name", {"name": payload.name})
    finally:
        # the ack (if any) has relabeled the registry by now, so the
        # check above refuses the name from here on
        _renames_in_flight.discard(target)
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"rename failed: {reply.get('data')}")
    return reply


class MutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    # REQUIRED — the old default=True meant a body-less POST muted
    # a node. The UI always sends an explicit bool.
    mute: bool


@router.post("/nodes/{room}/mute", dependencies=GUARD)
async def set_node_mute(room: str, payload: MutePayload):
    """Mic mute. Flex since 1.1.0; BOTH lines since firmware 1.2.11
    (2026-10-16): persisted on the node (survives reboot/OTA), and on an
    array node it drives the same XMOS pin as the physical Mute button
    (red LED). The node echoes its resulting state in the ack; the room
    record follows it (and follows physical presses via mute_push).

    Per-line truth (an earlier error message here was misleading): an array node
    older than 1.2.11 answers 'unknown cmd' — the message says what fixes
    it for BOTH lines without guessing which one is asking."""
    _room_online(room)
    reply = await _node_cmd(room, "set_mute", {"mute": payload.mute})
    if not reply.get("ok"):
        if "unknown cmd" in str(reply.get("data") or ""):
            raise HTTPException(
                status_code=501,
                detail="this node's firmware has no dashboard mute — array "
                      "nodes need firmware 1.2.11+ (older ones: use the "
                      "physical mute button); flex nodes need 1.1.0+")
        raise HTTPException(status_code=502, detail=f"mute failed: {reply.get('data')}")
    data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
    if isinstance(data.get("muted"), bool):
        registry.note_muted_room(room, data["muted"])
    return reply  # {"ok": true, "data": {"muted": bool}}


@router.get("/nodes/{room}/info", dependencies=GUARD)
async def node_info(room: str):
    """Live node state (2026-10-07): the dashboard refreshes its Mute
    button from the node's OWN answer — get_info returns muted (flex
    1.1.0+; older firmware omits the key = unmuted). Component state
    dies on page refresh; the node's truth doesn't — this is the
    source of truth for it. One round-trip per page load, not per poll."""
    _room_online(room)
    reply = await _node_cmd(room, "get_info", timeout=8.0)
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"info failed: {reply.get('data')}")
    return reply  # {"ok": true, "data": {"name", "mac", "fw", "line"?, "muted"?}}


# ── session mode (fresh | sticky per node — dashboard checkbox) ──────


@router.post("/nodes/{room}/session-mode", dependencies=GUARD)
async def set_session_mode(room: str, payload: SessionModePayload):
    """Set fresh|sticky session mode for a node (dashboard checkbox).

    Config `session_modes` section; applies on the node's next reconnect
    (pipeline reconstruction) — the same contract as every other config
    edit. Sticky rooms persist conversation history to data/sessions.json
    (gitignored) and keep memory across reconnects AND bot restarts; fresh
    is the privacy default (unmapped rooms are fresh — config stays
    minimal, the dashboard can always read the effective mode).
    """
    if payload.mode not in ("fresh", "sticky"):
        raise HTTPException(status_code=400, detail="mode must be 'fresh' or 'sticky'")
    modes = dict(config_store.read_section("session_modes") or {})
    before = modes.get(room)
    if payload.mode == "fresh":
        modes.pop(room, None)  # fresh is the default — config stays minimal
    else:
        modes[room] = "sticky"
    try:
        config_store.write_section("session_modes", modes)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"dashboard: session-mode '{room}' -> {payload.mode}")
    return {"ok": True, "room": room, "mode": payload.mode, "before": before}


# ── TTS settings (Settings tab — the config-editable arc's first panel) ──

# Two providers, one panel: ElevenLabs (voice_id + model) and Deepgram
# Aura-2 (model name IS the voice: aura-2-<name>-en). API keys NEVER ride
# config.yaml and NEVER appear in any API response — .env only, on the
# appliance only. Key routes per provider; set/unset status only.

_DEEPGRAM_VOICES = (
    "aura-2-thalia-en", "aura-2-andromeda-en", "aura-2-helena-en",
    "aura-2-apollo-en", "aura-2-arcas-en", "aura-2-aries-en",
    "aura-2-asteria-en", "aura-2-athena-en", "aura-2-atlas-en",
    "aura-2-aurora-en", "aura-2-callista-en", "aura-2-cora-en",
    "aura-2-cordelia-en", "aura-2-delia-en", "aura-2-draco-en",
    "aura-2-electra-en", "aura-2-harmonia-en", "aura-2-hera-en",
    "aura-2-hermes-en", "aura-2-hyperion-en", "aura-2-iris-en",
    "aura-2-janus-en", "aura-2-juno-en", "aura-2-jupiter-en",
    "aura-2-luna-en", "aura-2-mars-en", "aura-2-minerva-en",
    "aura-2-neptune-en", "aura-2-odysseus-en", "aura-2-ophelia-en",
    "aura-2-orion-en", "aura-2-orpheus-en", "aura-2-pandora-en",
    "aura-2-phoebe-en", "aura-2-pluto-en", "aura-2-saturn-en",
    "aura-2-selene-en", "aura-2-theia-en", "aura-2-vesta-en",
    "aura-2-zeus-en",
)

# Flux voices (developers.deepgram.com/docs/flux-tts/voices — 36-voice
# catalog; format flux-<name>-en; same DEEPGRAM_API_KEY as Aura)
_DEEPGRAM_FLUX_VOICES = (
    "flux-hannah-en", "flux-kit-en", "flux-alexis-en", "flux-cliff-en",
    "flux-sienna-en", "flux-cole-en", "flux-brooke-en", "flux-colin-en",
    "flux-gemma-en", "flux-haley-en", "flux-heather-en", "flux-miles-en",
    "flux-sean-en", "flux-bree-en", "flux-brittany-en", "flux-bruce-en",
    "flux-conor-en", "flux-donovan-en", "flux-drew-en", "flux-elise-en",
    "flux-jack-en", "flux-kai-en", "flux-kelsey-en", "flux-maeve-en",
    "flux-marcelo-en", "flux-marcus-en", "flux-meena-en", "flux-meghan-en",
    "flux-naveen-en", "flux-paige-en", "flux-priya-en", "flux-rufus-en",
    "flux-sharon-en", "flux-tanner-en", "flux-wade-en", "flux-wes-en",
)


# Model trim: flash for speed, v4 turbo for expression.
# MUST match TTSPayload.model's pattern (suite pins it).
_ELEVENLABS_MODELS = ("eleven_flash_v2_5", "eleven_v4_turbo")

# LocalAI's pocket-tts backend speaks these preset voices. The TTS card's
# LocalAI voice picker lists them when the picked LocalAI model runs that
# backend (any other voice name is typed by hand).
_LOCALAI_POCKET_VOICES = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "eve", "fantine", "george", "jane", "jean",
    "javert", "marius", "mary", "michael", "paul", "peter_yearsley",
    "stuart_bell", "vera",
)

# The dashboard's pick-lists come FROM HERE via
# GET /tts — the UI used to hardcode its own copies (12 of 40 Aura, 16 of
# 36 Flux), so most accepted voices were unpickable and a configured
# aura-2-zeus-en couldn't even display. One catalog, one owner.
# "pocket" is not a provider: it is LocalAI's pocket-tts voice list.
TTS_CATALOG = {
    "elevenlabs": list(_ELEVENLABS_MODELS),
    "deepgram": list(_DEEPGRAM_VOICES),
    "deepgram-flux": list(_DEEPGRAM_FLUX_VOICES),
    "pocket": list(_LOCALAI_POCKET_VOICES),
    "kokoro": list(tts_kokoro.KOKORO_VOICES),
}


class TTSPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    # One of the TTS catalog's providers; unset = Embedded (Kokoro). A field
    # the payload no longer has (an old tab) 422s like any unknown key.
    provider: str = Field(default=tts_catalog.DEFAULT_PROVIDER,
                          pattern=f"^({'|'.join(tts_catalog.PROVIDER_NAMES)})$")
    voice_id: str | None = Field(default=None, min_length=8, max_length=64)
    # Model trim: flash for reliability/speed, v4
    # turbo for expressiveness — v2/v3/v4-regular have no use case in a
    # voice assistant (v2 legacy, v3 superseded by v4, v4-regular is the
    # slow non-Turbo bake).
    model: str | None = Field(default=None,
                              pattern=r"^eleven_(flash_v2_5|v4_turbo)$")
    deepgram_voice: str | None = Field(default=None)
    deepgram_flux_voice: str | None = Field(default=None)
    # Flux knobs: speed 0.5–1.5 in 0.05 steps; expressivity -2..2
    # (None/absent = Flux defaults). Pydantic validates range; the None
    # default means "don't touch" on partial updates.
    deepgram_flux_speed: float | None = Field(default=None, ge=0.5, le=1.5)
    deepgram_flux_expressivity: int | None = Field(default=None, ge=-2, le=2)
    # ElevenLabs flash-v2.5 knobs: speed 0.7–1.2,
    # stability 0.0–1.0 (lower = expressive, higher = consistent). Absent
    # = None = ElevenLabs defaults — never a made-up value. The v3/v4
    # dialogue lane deliberately has no knobs (ElevenLabs guidance).
    elevenlabs_speed: float | None = Field(default=None, ge=0.7, le=1.2)
    elevenlabs_stability: float | None = Field(default=None, ge=0.0, le=1.0)
    # ElevenLabs apply_text_normalization: auto (their default; OFF on
    # Flash) | on (spell numbers/times/dates; ~+0.15 s) | off.
    elevenlabs_text_normalization: str | None = Field(default=None, pattern="^(auto|on|off)$")
    # Kokoro: the Embedded voice, and the standby behind LocalAI and Cloud
    # (the card's "Fallback voice"). One of the 28 English voices; speed
    # 0.5–1.5 in 0.05 steps. Saved with any provider; None = leave as-is.
    kokoro_voice: str | None = Field(default=None)
    kokoro_speed: float | None = Field(default=None, ge=0.5, le=1.5)


def _validate_tts_payload(p: TTSPayload) -> None:
    if p.provider == "elevenlabs":
        if not p.voice_id or not p.model:
            raise HTTPException(status_code=422, detail="elevenlabs needs voice_id and model")
        # Lane picking is bot.py's job (model prefix -> WS lane); here we
        # only require a known model from the pattern above.
    elif p.provider == "deepgram":
        if not p.deepgram_voice or p.deepgram_voice not in _DEEPGRAM_VOICES:
            raise HTTPException(status_code=422, detail="deepgram_voice must be one of the Aura-2 voices (aura-2-thalia-en …)")
    elif p.provider == "deepgram-flux":
        if not p.deepgram_flux_voice or p.deepgram_flux_voice not in _DEEPGRAM_FLUX_VOICES:
            raise HTTPException(status_code=422, detail="deepgram_flux_voice must be one of the Flux voices (flux-hannah-en …)")
        if p.deepgram_flux_speed is not None and abs(p.deepgram_flux_speed * 20 - round(p.deepgram_flux_speed * 20)) > 1e-9:
            raise HTTPException(status_code=422, detail="speed must be 0.5–1.5 in 0.05 steps")
    elif p.provider == "kokoro":
        if not p.kokoro_voice or p.kokoro_voice not in tts_kokoro.KOKORO_VOICES:
            raise HTTPException(status_code=422, detail="kokoro_voice must be one of the Kokoro voices (af_heart …)")
        problem = tts_kokoro.setup_problem()
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    elif p.provider == "localai":
        problem = _localai_problem()
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    if p.kokoro_voice is not None and p.kokoro_voice not in tts_kokoro.KOKORO_VOICES:
        raise HTTPException(status_code=422, detail="kokoro_voice must be one of the Kokoro voices (af_heart …)")
    if p.kokoro_speed is not None and abs(p.kokoro_speed * 20 - round(p.kokoro_speed * 20)) > 1e-9:
        raise HTTPException(status_code=422, detail="kokoro_speed must be 0.5–1.5 in 0.05 steps")


class TTSKeyPayload(BaseModel):
    # Character-class pattern: no
    # newlines/quotes/backslashes — a `\n` in the value let a key PUT
    # smuggle a WHOLE extra env var into .env (e.g. LVI_RESTART_COMMAND),
    # which the next restart would execute. Alphanumeric + _ . - only.
    # Verified against the three real production keys before shipping:
    # all pass (2026-10-04).
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    api_key: str = Field(min_length=8, max_length=256,
                         pattern=r"^[A-Za-z0-9_.\-]+$")


def _tts_providers(kokoro_problem: str | None, localai_problem: str | None) -> list:
    """Every TTS provider in card order with its tier (tts_catalog). One
    that can't be picked yet says why: missing setup first, then its key."""
    out = []
    for p in tts_catalog.PROVIDERS:
        item = {"name": p["name"], "label": p["label"], "tier": p["tier"], "available": True}
        if p["name"] == "kokoro" and kokoro_problem:
            item.update(available=False, reason="model files missing" if tts_kokoro.kokoro_available() else "not installed")
        elif p["name"] == "localai" and localai_problem:
            item.update(available=False, reason=_LOCALAI_REASON)
        elif p["key_var"] and not _key_set(p["key_var"]):
            item.update(available=False, reason=_ADD_KEY_REASON)
        out.append(item)
    return out


@router.get("/tts")
async def get_tts():
    """Current TTS config + key status per provider. Never returns keys.
    Every knob the bot reads with an env var resolves file > LVI_* env >
    the bot's default (bot.py's cfg_env precedence) — the panel used to
    show the code default for a knob that lived only in .env. An unset or
    unknown provider reads as Embedded (Kokoro), as the bot resolves it."""
    import os as _os
    tts_cfg = config_store.read_section("tts") or {}
    kokoro_problem = tts_kokoro.setup_problem()
    localai_problem = _localai_problem()

    def _cfg_env(key, env_var, default=None):
        # same precedence as core.config.cfg_env, on the fresh file read
        v = tts_cfg.get(key)
        if v is None:
            v = _os.getenv(env_var)
        return default if v is None else v

    return {
        "provider": tts_catalog.resolve_provider(_cfg_env("provider", "LVI_TTS_PROVIDER")),
        "voice_id": _cfg_env("voice_id", "LVI_TTS_VOICE_ID"),
        "model": _cfg_env("model", "LVI_TTS_MODEL", "eleven_flash_v2_5"),
        "deepgram_voice": _cfg_env("deepgram_voice", "LVI_TTS_VOICE", "aura-2-thalia-en"),
        "deepgram_flux_voice": _cfg_env("deepgram_flux_voice", "LVI_TTS_FLUX_VOICE", "flux-hannah-en"),
        "deepgram_flux_speed": tts_cfg.get("deepgram_flux_speed"),
        "deepgram_flux_expressivity": tts_cfg.get("deepgram_flux_expressivity"),
        "elevenlabs_speed": tts_cfg.get("elevenlabs_speed"),
        "elevenlabs_stability": tts_cfg.get("elevenlabs_stability"),
        # None = unset = ElevenLabs' own default (auto).
        "elevenlabs_text_normalization": tts_cfg.get("elevenlabs_text_normalization"),
        "kokoro_voice": _cfg_env("kokoro_voice", "LVI_TTS_KOKORO_VOICE", tts_kokoro.DEFAULT_VOICE),
        # None = unset in file and env = the bot's default (1.0). Kept None
        # so the card doesn't pin 1.0 into config on its next save.
        "kokoro_speed": _cfg_env("kokoro_speed", "LVI_TTS_KOKORO_SPEED"),
        "localai_model_tts": _localai_saved()["model_tts"],
        "localai_voice": _localai_saved()["voice"],
        # First 4 characters of a set key, so a wrong key is recognisable.
        "elevenlabs_key_hint": intelligence_target.key_hint(_saved_env().get("ELEVENLABS_API_KEY")),
        "localai_key_hint": intelligence_target.key_hint(_saved_env().get("LVI_LOCALAI_KEY")),
        "deepgram_key_hint": intelligence_target.key_hint(_saved_env().get("DEEPGRAM_API_KEY")),
        # The three tiers and every provider in card order (tts_catalog): the
        # card's grouped dropdown, with a reason on each one it can't offer yet.
        "tiers": tts_catalog.TIERS,
        "providers": _tts_providers(kokoro_problem, localai_problem),
        # The accepted values per provider — the UI's only source for its
        # pick-lists (elevenlabs = model ids; deepgram* and kokoro = voices;
        # pocket = LocalAI's pocket-tts voices for the LocalAI voice picker).
        "catalog": TTS_CATALOG,
    }


@router.put("/tts", dependencies=GUARD)
async def put_tts(payload: TTSPayload):
    """Write provider + knobs to config.yaml (tts section). Applies on the
    next bot restart — same contract as every config edit."""
    current = dict(config_store.read_section("tts") or {})
    _validate_tts_payload(payload)
    current["provider"] = payload.provider
    if payload.provider == "elevenlabs":
        current["voice_id"] = payload.voice_id
        current["model"] = payload.model
        # Flash-v2.5 knobs ride along with None = "leave as-is" — same
        # partial-update law as the Flux pair.
        if payload.elevenlabs_speed is not None:
            current["elevenlabs_speed"] = payload.elevenlabs_speed
        if payload.elevenlabs_stability is not None:
            current["elevenlabs_stability"] = payload.elevenlabs_stability
        if payload.elevenlabs_text_normalization is not None:
            current["elevenlabs_text_normalization"] = payload.elevenlabs_text_normalization
    elif payload.provider == "deepgram":
        current["deepgram_voice"] = payload.deepgram_voice
    elif payload.provider == "deepgram-flux":
        current["deepgram_flux_voice"] = payload.deepgram_flux_voice
        # Flux knobs ride along — None means "leave as-is" (partial update
        # semantics: the panel always sends current values, but a bare
        # API PUT of just provider+voice must not wipe tuned knobs).
        if payload.deepgram_flux_speed is not None:
            current["deepgram_flux_speed"] = payload.deepgram_flux_speed
        if payload.deepgram_flux_expressivity is not None:
            current["deepgram_flux_expressivity"] = payload.deepgram_flux_expressivity
    # Kokoro's voice and speed serve every tier: the Embedded voice, and the
    # standby behind LocalAI and Cloud (the card's "Fallback voice"). Saved
    # with any provider (Kokoro itself requires the voice); None = leave as-is.
    if payload.kokoro_voice is not None:
        current["kokoro_voice"] = payload.kokoro_voice
    if payload.kokoro_speed is not None:
        current["kokoro_speed"] = payload.kokoro_speed
    try:
        config_store.write_section("tts", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"dashboard: tts config saved (provider={payload.provider}) — applies on next restart")
    return {"ok": True, "provider": payload.provider}


def _env_path():
    import paths as _paths
    return _paths.env_file()


def _file_env() -> dict:
    """What .env holds NOW, parsed exactly the way the bot loads it
    (python-dotenv: quotes, `export`, inline comments). Read-only."""
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover — dotenv ships with the bot
        dotenv_values = None
    path = _env_path()
    if not path.is_file():
        return {}
    if dotenv_values is not None:
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    view = {}
    for ln in path.read_text().splitlines():
        k, sep, v = ln.partition("=")
        if sep and k and not k.lstrip().startswith("#"):
            view[k.strip()] = v.strip()
    return view


def _saved_env() -> dict:
    """Process env overlaid with what .env holds NOW, so key status and
    hints show a key the moment it is saved (the voice agent itself still
    reads the process env, at the next restart). Read-only."""
    view = dict(os.environ)
    try:
        view.update(_file_env())
    except OSError:
        pass
    return view


# Why a cloud provider shows disabled on either speech card.
_ADD_KEY_REASON = "add the key on System · Keys"
# Reasons are short on purpose: the option text reads "<label> — <reason>";
# the LocalAI card on System explains what is missing.
_LOCALAI_REASON = "set it up on System"


def _key_set(var: str | None) -> bool:
    """A key is set: the live process env OR the saved .env (a key saved on
    the Keys card counts before the restart that loads it). Presence only —
    never the value."""
    return bool(var and (os.getenv(var) or _saved_env().get(var)))


_ENV_BACKUPS_KEPT = 3


def _backup_env(env_path) -> None:
    """Before a key is REPLACED: copy .env to .env.bak-<time> (0600) and
    keep the newest few — a bad paste stays recoverable. A key line is
    never deleted (the 2026-10-08 lesson), and neither is its last copy."""
    import os as _os
    import re as _re
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    # UTC + 'Z': names sort in time order all year (no DST repeat hour).
    stamp = _dt.now(_tz.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    bak = env_path.with_name(f"{env_path.name}.bak-{stamp}")
    fd = _os.open(bak, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
    with _os.fdopen(fd, "w") as f:
        f.write(env_path.read_text())
    # Prune only OUR backups; a hand-made .env.bak-whatever is never touched.
    ours = _re.compile(_re.escape(env_path.name) + r"\.bak-\d{8}-\d{6}-\d{6}Z")
    mine = sorted(p for p in env_path.parent.glob(f"{env_path.name}.bak-*") if ours.fullmatch(p.name))
    for old in mine[:-_ENV_BACKUPS_KEPT]:
        old.unlink(missing_ok=True)


async def _write_env_key(var: str, value: str) -> bool:
    """Replace-or-append `var=value` in .env. Returns True if replaced.

    Atomic write + 0600: write_text created a
    missing .env as 0644, and a crash mid-write could truncate it.
    TTSKeyPayload's character-class pattern is the upstream defense
    against newline smuggling — this is the belt to that suspenders.
    """
    env_path = _env_path()
    lines = env_path.read_text().splitlines() if env_path.is_file() else []
    out, replaced, changed = [], False, False
    for ln in lines:
        # Both line forms, like _remove_env_key: an `export VAR=` line
        # missed here was kept AND a duplicate appended. It keeps its prefix.
        if ln.startswith(var + "="):
            new = var + "=" + value
        elif ln.lstrip().startswith(f"export {var}="):
            new = ln[:ln.index("export ")] + f"export {var}=" + value
        else:
            out.append(ln)
            continue
        changed = changed or ln != new
        out.append(new)
        replaced = True
    if not replaced:
        out.append(var + "=" + value)
    if changed:
        _backup_env(env_path)
    _write_private(env_path, "\n".join(out) + "\n")
    return replaced


def _write_private(env_path, text: str) -> None:
    """Atomic 0600 write of .env. The temp file is 0600 from creation —
    created under the umask and chmod'ed after, it held every secret
    world-readable for a moment."""
    tmp = env_path.with_suffix(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # a leftover temp file keeps its old mode through O_CREAT
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, env_path)  # atomic on the same filesystem


@router.put("/tts/key/elevenlabs", dependencies=GUARD)
async def put_tts_key_elevenlabs(payload: TTSKeyPayload):
    """ElevenLabs API key → .env (appliance-side, gitignored, never echoed)."""
    replaced = await _write_env_key("ELEVENLABS_API_KEY", payload.api_key)
    logger.info("dashboard: ELEVENLABS_API_KEY written to .env (value never logged)")
    return {"ok": True, "api_key_set": True, "replaced": replaced}


@router.put("/tts/key/deepgram", dependencies=GUARD)
async def put_tts_key_deepgram(payload: TTSKeyPayload):
    """Deepgram API key → .env (appliance-side, gitignored, never echoed)."""
    replaced = await _write_env_key("DEEPGRAM_API_KEY", payload.api_key)
    logger.info("dashboard: DEEPGRAM_API_KEY written to .env (value never logged)")
    return {"ok": True, "api_key_set": True, "replaced": replaced}


@router.put("/localai/key", dependencies=GUARD)
async def put_localai_key(payload: TTSKeyPayload):
    """LocalAI API key → .env (LVI_LOCALAI_KEY; never echoed). Saved from the
    Keys card, like every other key."""
    replaced = await _write_env_key("LVI_LOCALAI_KEY", payload.api_key)
    logger.info("dashboard: LVI_LOCALAI_KEY written to .env (value never logged)")
    return {"ok": True, "api_key_set": True, "replaced": replaced, "var": "LVI_LOCALAI_KEY"}


# ── Keys card: every API key in one place ──
# Presence + masked hint only; values never leave the server. Saving goes
# through the existing kind-aware routes above (one write path, one set of
# laws); this section only lists and tests.


@router.get("/keys")
async def get_keys():
    import keys_catalog

    return {"keys": keys_catalog.rows(_saved_env(), dict(os.environ), _keys_cfg(), file_env=_file_env())}


def _keys_cfg() -> dict:
    """The config the bot will run with — config.yaml, then the same env
    fallbacks and normalisation bot.py applies."""
    def pick(sec, key, env, default):
        v = (config_store.read_section(sec) or {}).get(key)
        return str(v if v not in (None, "") else os.getenv(env, default)).strip().lower()
    icfg = dict(config_store.read_section("intelligence") or {})
    icfg["kind"] = pick("intelligence", "kind", "LVI_INTELLIGENCE_KIND", intelligence.DEFAULT_KIND)
    return {"tts": {"provider": tts_catalog.resolve_provider(pick("tts", "provider", "LVI_TTS_PROVIDER", ""))},
            "stt": {"provider": pick("stt", "provider", "LVI_STT_PROVIDER", "whisper")},
            "intelligence": icfg,
            "room_agents": dict(config_store.read_section("room_agents") or {})}


class KeyTestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    var: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")


def _keys_http(method: str, url: str, headers: dict) -> tuple:
    """(status, body) — outbound call for a key test. Never raises on an HTTP
    status; network failures raise OSError. Tests replace this."""
    import urllib.error as _ue
    import urllib.request as _rq

    try:
        with _rq.urlopen(_rq.Request(url, method=method, headers=headers), timeout=5) as resp:
            return resp.status, resp.read(2000).decode("utf-8", "replace")
    except _ue.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "replace")
    except _ue.URLError as e:
        raise OSError(str(e.reason)) from e


def _key_test(row: dict, key: str) -> dict:
    """Synchronous (worker thread). → {ok, status, detail}; never the key."""
    try:
        return _key_test_inner(row, key)
    except Exception as e:  # noqa: BLE001 — the contract is a result, never a 500
        return {"ok": False, "status": "unreachable", "detail": type(e).__name__}


def _key_test_inner(row: dict, key: str) -> dict:
    def scrub(text):
        return str(text).replace(key, "***")[:160] if key else str(text)[:160]

    lane = intelligence.get(row["kind"])
    if lane:
        # Each lane knows how to check its own key against its SAVED URL.
        icfg = dict(config_store.read_section("intelligence") or {})
        return lane.key_test(row, key, icfg, probe=_intelligence_probe, http=_keys_http, scrub=scrub)
    if row["kind"] == "localai":
        import localai
        url = _localai_saved()["url"]
        if not url:
            return {"ok": False, "status": "unreachable", "detail": "Set the LocalAI URL on the LocalAI card first"}
        ok, _ms, why = localai.probe(url=url, key=key)
        if ok:
            return {"ok": True, "status": "ok", "detail": "LocalAI accepted the key"}
        status = "rejected" if "refused the key" in (why or "") else "unreachable"
        return {"ok": False, "status": status, "detail": scrub(why or "LocalAI did not answer")}
    if row["kind"] == "deepgram":
        req = ("GET", "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {key}"})
    else:
        req = ("GET", "https://api.elevenlabs.io/v1/user", {"xi-api-key": key})
    try:
        code, body = _keys_http(*req)
    except OSError as e:
        return {"ok": False, "status": "unreachable", "detail": scrub(e)}
    if 200 <= code < 300:
        return {"ok": True, "status": "ok", "detail": f"{row['label']} accepted the key"}
    if row["kind"] == "elevenlabs" and code == 401 and "missing_permissions" in body:
        # A restricted key without user_read: authenticated, just scoped.
        return {"ok": True, "status": "ok", "detail": "ElevenLabs accepted the key (restricted permissions)"}
    if code in (401, 403):
        return {"ok": False, "status": "rejected", "detail": f"{row['label']} rejected the key (HTTP {code})"}
    return {"ok": False, "status": "unreachable", "detail": f"{row['label']} answered HTTP {code}"}


@router.post("/keys/test", dependencies=GUARD)
async def post_key_test(payload: KeyTestPayload):
    """Check the SAVED key (what the next restart loads) against its
    provider, server-side. POST + GUARD: it makes outbound requests."""
    import keys_catalog

    row = keys_catalog.target_for(payload.var, keys_catalog.known_agents(_keys_cfg()))
    lane = intelligence.get(row["kind"]) if row is not None else None
    # A per-agent key is testable only once it's in .env (never a stray
    # process variable).
    if row is None or (lane is not None and lane.per_agent_keys and row["var"] != lane.base_key_var
                       and payload.var not in _file_env()):
        raise HTTPException(status_code=422, detail=f"{payload.var} is not an API key this appliance uses")
    key = _key_saved_value(payload.var)
    if not key:
        return {"ok": False, "status": "missing", "detail": f"{payload.var} is not set"}
    result = await asyncio.to_thread(_key_test, row, key)
    logger.info(f"dashboard: key test {payload.var} -> {result['status']}")
    return result


def _key_saved_value(var: str) -> str:
    """The key the NEXT restart loads: .env is the store (systemd loads it
    as EnvironmentFile), so a removed line means removed even while the
    running process still holds the old value."""
    if _env_path().is_file():
        return _file_env().get(var) or ""
    return os.environ.get(var) or ""


async def _remove_env_key(var: str) -> bool:
    """Drop `var=` from .env (backup first, atomic, 0600). False if absent."""
    env_path = _env_path()
    if not env_path.is_file():
        return False
    lines = env_path.read_text().splitlines()
    keep = [ln for ln in lines
            if not (ln.startswith(var + "=") or ln.lstrip().startswith(f"export {var}="))]
    if len(keep) == len(lines):
        return False
    _backup_env(env_path)
    _write_private(env_path, "\n".join(keep) + ("\n" if keep else ""))
    return True


@router.delete("/keys/{var}", dependencies=GUARD)
async def delete_key(var: str):
    """Remove an API key from .env — for security (a leaked or unused key).
    A private backup of .env is written first; the running bot keeps the
    old value until the next restart."""
    import keys_catalog

    if not re.fullmatch(r"[A-Z0-9_]{1,64}", var or ""):
        raise HTTPException(status_code=422, detail="not a key variable")
    row = keys_catalog.target_for(var, keys_catalog.known_agents(_keys_cfg()))
    if row is None:
        raise HTTPException(status_code=422, detail=f"{var} is not an API key this appliance uses")
    if not await _remove_env_key(var):
        raise HTTPException(status_code=404, detail=f"{var} is not in .env")
    logger.info(f"dashboard: {var} removed from .env (backup kept; value never logged)")
    return {"ok": True, "var": var, "removed": True}


# ── LocalAI (System — speech models on a LAN server) ──
# The URL and model picks live in config.yaml (localai:), the key in .env
# (LVI_LOCALAI_KEY, Keys card only). The client module is imported lazily so
# this file stays light; nothing here installs or edits models on LocalAI.

_LOCALAI_URL_RE = re.compile(r"^https?://[A-Za-z0-9._-]+(:\d{1,5})?$")
_LOCALAI_NAME = r"^[A-Za-z0-9._:/@+-]*$"


def _localai_saved() -> dict:
    """The LocalAI settings the bot will boot with (config.yaml, then env)."""
    sec = config_store.read_section("localai") or {}

    def pick(key, env, default=""):
        v = sec.get(key)
        return v if v not in (None, "") else os.getenv(env, default)

    return {"url": pick("url", "LVI_LOCALAI_URL") or None,
            "model_stt": str(pick("model_stt", "LVI_LOCALAI_MODEL_STT") or ""),
            "model_tts": str(pick("model_tts", "LVI_LOCALAI_MODEL_TTS") or ""),
            "voice": str(pick("voice", "LVI_LOCALAI_VOICE") or ""),
            "speed": pick("speed", "LVI_LOCALAI_SPEED", None)}


def _localai_problem() -> str | None:
    """Why LocalAI can't be picked yet (None = it can)."""
    if not _localai_saved()["url"]:
        return "Set the LocalAI URL on the LocalAI card first"
    if not _key_saved_value("LVI_LOCALAI_KEY"):
        return "Add the LocalAI key on System · Keys first"
    return None


class LocalAIPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    url: str | None = Field(default=None, max_length=200)
    model_stt: str | None = Field(default=None, max_length=64, pattern=_LOCALAI_NAME)
    model_tts: str | None = Field(default=None, max_length=64, pattern=_LOCALAI_NAME)
    voice: str | None = Field(default=None, max_length=64, pattern=_LOCALAI_NAME)
    speed: float | None = Field(default=None, ge=0.5, le=1.5)

    @model_validator(mode="after")
    def _url_ok(self):
        if self.url is not None and self.url.strip():
            import localai
            url = localai.normalize_url(self.url)
            if not url or not _LOCALAI_URL_RE.match(url):
                raise ValueError("url must be host:port or http(s)://host:port")
            try:
                urllib.parse.urlsplit(url).port
            except ValueError:
                raise ValueError("url has an invalid port") from None
            self.url = url
        return self


@router.get("/localai")
async def get_localai():
    """Saved LocalAI URL + speech-to-text model, and whether its key is
    saved. Never the key."""
    s = _localai_saved()
    return {"url": s["url"], "model_stt": s["model_stt"], "key_set": bool(_key_saved_value("LVI_LOCALAI_KEY"))}


@router.put("/localai", dependencies=GUARD)
async def put_localai(payload: LocalAIPayload):
    """Save the URL / model picks (None = leave as-is, "" = clear).
    Applies on the next bot restart, like every provider setting."""
    current = dict(config_store.read_section("localai") or {})
    for field in ("url", "model_stt", "model_tts", "voice", "speed"):
        value = getattr(payload, field)
        if value is None:
            continue
        if value == "":
            current.pop(field, None)
        else:
            current[field] = value
    try:
        config_store.write_section("localai", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("dashboard: localai settings saved — applies on next restart")
    return {"ok": True, "restart": True, **_localai_saved()}


@router.post("/localai/test", dependencies=GUARD)
async def post_localai_test():
    """Reach the SAVED LocalAI with the SAVED key: answer time + the
    speech-to-text and voice models it serves. A result, never a 500."""
    import localai
    s = _localai_saved()
    key = _key_saved_value("LVI_LOCALAI_KEY")
    out = {"ok": False, "ms": None, "reason": None, "stt": [], "tts": []}
    if not s["url"]:
        return {**out, "reason": "Set the LocalAI URL first"}
    if not key:
        return {**out, "reason": localai.NO_KEY}
    try:
        ok, ms, why = await asyncio.to_thread(localai.probe, url=s["url"], key=key)
        if not ok:
            return {**out, "reason": localai.redact(why or "LocalAI did not answer", key)}
        found = await asyncio.to_thread(localai.discover, url=s["url"], key=key)
    except Exception as e:  # noqa: BLE001 — the contract is a result, never a 500
        return {**out, "reason": localai.redact(f"LocalAI test failed ({type(e).__name__})", key)}
    return {"ok": True, "ms": round(ms, 1) if ms is not None else None, "reason": None,
            "stt": found["stt"], "tts": found["tts"]}


# ── Intelligence (Settings — provider picker) ──


_INTELLIGENCE_URL_RE = r"^https?://[A-Za-z0-9._:-]+(:\d+)?(/[\w./-]*)?$"


def _normalize_intelligence_url(data):
    """Host:port is enough: the lane fills in http:// and /v1 (Lane.normalize_url)
    BEFORE the URL pattern runs, so what is saved and probed is always a full URL."""
    if isinstance(data, dict) and isinstance(data.get("url"), str) and isinstance(data.get("kind"), str):
        lane = intelligence.LANES.get(data["kind"])
        if lane is not None:
            data = {**data, "url": lane.normalize_url(data["url"])}
    return data


def _check_intelligence_url_port(url) -> None:
    # The pattern lets a port like 70000 (or a second colon) through, and
    # urlsplit().port raises on those — refuse them here so the caller gets
    # a 422 instead of a 500, and such a URL is never saved.
    if url is not None:
        try:
            urllib.parse.urlsplit(url).port
        except ValueError:
            raise ValueError("url has an invalid port") from None


class IntelligencePayload(BaseModel):
    # hermes = today's api_server contract; openclaw = OpenClaw Gateway
    # (model "openclaw/<agent>", x-openclaw-session-key sticky, no
    # conversation field); nanobot = one user message + session_id, agent
    # optional. Any other kind 422s.
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    kind: str = Field(pattern=f"^({'|'.join(intelligence.KINDS)})$")
    # The URL on screen, filed under `kind` only (intelligence.urls[kind]
    # is the per-kind recall map). There is deliberately no way to save the
    # OTHER kind's URL from here: the stored key goes to a kind's saved URL,
    # so only a URL the user saved while looking at that kind may land there.
    url: str | None = Field(default=None, max_length=200, pattern=_INTELLIGENCE_URL_RE)
    # The agent for THIS kind: an OpenClaw agent id, or a Hermes profile
    # ("default" = the main agent). Filed under intelligence.agents[kind].
    # "" (nanobot only) removes the saved one: nanobot then answers with its
    # own model, so a model change on the server can't 400 every turn.
    agent: str | None = Field(default=None, max_length=64,
                              pattern=r"^[A-Za-z0-9_.-]*$")

    @model_validator(mode="before")
    @classmethod
    def _short_url(cls, data):
        return _normalize_intelligence_url(data)

    @model_validator(mode="after")
    def _url_port(self):
        _check_intelligence_url_port(self.url)
        return self


# One lane, one variable, named for its intelligence (naming law
# 2026-10-08). Single source for the dashboard routes — GET status, key
# save, and the Test probe all read this map. Built from the lane registry.
INTELLIGENCE_KEY_VARS = {k: lane.base_key_var for k, lane in intelligence.LANES.items()}


class IntelligenceKeyPayload(BaseModel):
    # The key is saved for the kind the user is
    # LOOKING AT, not the kind config.yaml holds. The old route derived the
    # variable from saved config, so picking OpenClaw in the dropdown and
    # saving its token on a hermes-configured install wrote the OpenClaw
    # token into LVI_HERMES_KEY — the wrong-token 401 failure mode, rebuilt
    # by the UI. kind is REQUIRED (no default = no silent guess) and
    # extra="forbid" so a misspelled field 422s instead of no-opping.
    model_config = ConfigDict(extra="forbid")
    kind: _LaneKind
    # Hermes profiles each have their own key (LVI_HERMES_KEY_<AGENT>);
    # omitted or "default" = the kind's base variable. OpenClaw agents share
    # one Gateway token and nanobot has one server key, so agent is ignored
    # there.
    agent: str | None = Field(default=None, min_length=1, max_length=64,
                              pattern=r"^[A-Za-z0-9_.-]+$")
    # Same character-class law as TTSKeyPayload: no newlines/quotes/backslashes — a `\n` in a key PUT
    # must never smuggle a second env var into .env.
    api_key: str = Field(min_length=8, max_length=256,
                         pattern=r"^[A-Za-z0-9_.\-]+$")


@router.get("/intelligence")
async def get_intelligence():
    """Current Intelligence config + key status. Never returns the key."""
    import os as _os
    _env = _saved_env()
    intelligence_cfg = config_store.read_section("intelligence") or {}
    kind = intelligence_cfg.get("kind") or intelligence.DEFAULT_KIND
    # Key law (strict, 2026-10-08): each kind reads exactly ONE variable —
    # hermes: LVI_HERMES_KEY; openclaw: LVI_OPENCLAW_KEY; nanobot:
    # LVI_NANOBOT_KEY. No cross-kind fallback anywhere (a fallback turned a
    # missing key into a silent wrong-token outage once).
    # keys_set: status for EVERY kind, so the UI chip shows
    # the key of the kind on screen, not the kind in config.
    _kv = INTELLIGENCE_KEY_VARS.get(kind, intelligence.LANES[intelligence.DEFAULT_KIND].base_key_var)
    return {
        "kind": kind,
        "url": intelligence_cfg.get("url") or _env.get("LVI_HERMES_URL"),
        "urls": intelligence_cfg.get("urls") or {},
        # Live agent per kind (hermes "default" = the main agent).
        "agents": {k: (intelligence_target.agent_for(intelligence_cfg, k) or lane.default_agent)
                   for k, lane in intelligence.LANES.items()},
        "key_set": bool(_env.get(_kv)),
        "keys_set": {k: bool(_env.get(v)) for k, v in INTELLIGENCE_KEY_VARS.items()},
    }


@router.get("/intelligence/lanes")
async def get_intelligence_lanes():
    """The UI renders lane pickers/labels from this; static, no secrets."""
    return {"lanes": [lane.ui_meta() for lane in intelligence.LANES.values()]}


@router.put("/intelligence", dependencies=GUARD)
async def put_intelligence(payload: IntelligencePayload):
    """Write kind (+ url/agent when provided) to config.yaml (intelligence
    section) — read-merge-write, so fillers/max_tokens/lull always
    survive. Applies on the next bot restart."""
    lane = intelligence.LANES[payload.kind]  # the pattern admits registry kinds only
    if payload.agent == "" and lane.agent_required:
        optional = " / ".join(k for k, ln in intelligence.LANES.items() if not ln.agent_required)
        raise HTTPException(status_code=422,
                            detail=f"{payload.kind} needs an agent; only {optional} can run without one")
    current = dict(config_store.read_section("intelligence") or {})
    saved_kind = current.get("kind") or intelligence.DEFAULT_KIND
    urls = dict(current.get("urls") or {})
    if (lane.url_first_setup and payload.agent is None and payload.url is not None
            and not lane.agent_for(current)):
        # New setup of a URL-first lane (OpenClaw): its agents can't be
        # listed until the URL is saved (discovery only sends the key to a
        # saved URL), and the lane
        # can't run without an agent. Save just the URL and leave the live
        # lane as it is; the next Save, with an agent picked, switches it.
        urls[payload.kind] = payload.url
        current["urls"] = urls
        if saved_kind == payload.kind:
            current["url"] = payload.url
        try:
            config_store.write_section("intelligence", current)
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
        logger.info(f"dashboard: intelligence {payload.kind} URL saved (no agent yet; live lane unchanged)")
        return {"ok": True, "kind": saved_kind, "url_only": True}
    current["kind"] = payload.kind
    if payload.url is not None:
        current["url"] = payload.url
        # File the URL under the kind being saved (per-kind recall map).
        urls[payload.kind] = payload.url
        current["urls"] = urls
    elif urls.get(payload.kind):
        # Bare kind flip: restore THAT kind's remembered URL.
        current["url"] = urls[payload.kind]
    elif payload.kind != saved_kind:
        # No URL for the new kind. Keeping the old kind's URL would point
        # the new lane (and its key) at the wrong intelligence, and clearing
        # it would fall back to LVI_HERMES_URL — refuse instead.
        raise HTTPException(status_code=422, detail=f"set a URL for {payload.kind} first")
    if payload.agent is not None:
        agents = dict(current.get("agents") or {})
        if payload.agent:
            agents[payload.kind] = payload.agent
        else:
            agents.pop(payload.kind, None)  # nanobot: back to its own model
        current["agents"] = agents
        if lane.legacy_agent_key:
            current["agent"] = payload.agent  # legacy key, read by older builds
    if lane.agent_required and lane.default_agent is None and not lane.agent_for(current):
        raise HTTPException(status_code=422, detail=f"{payload.kind} needs an agent id (intelligence.agent)")
    try:
        config_store.write_section("intelligence", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"dashboard: intelligence config saved (kind={payload.kind}) — applies on next restart")
    return {"ok": True, "kind": payload.kind}


@router.put("/intelligence/key", dependencies=GUARD)
async def put_intelligence_key(payload: IntelligenceKeyPayload):
    """Intelligence API key → .env, kind-aware (naming law: each intelligence's key names what it is):
    hermes → LVI_HERMES_KEY, openclaw → LVI_OPENCLAW_KEY, nanobot →
    LVI_NANOBOT_KEY — the ONE variable
    that lane reads (strict law: no fallbacks, no cross-kind). The kind
    comes from the PAYLOAD — the kind the user is saving a
    key for — never from saved config. Never echoed; the response names
    the variable so the UI can say where the key went."""
    kind = payload.kind
    var = intelligence_target.key_var(kind, payload.agent or "")
    replaced = await _write_env_key(var, payload.api_key)
    logger.info(f"dashboard: {var} written to .env for kind={kind} (value never logged)")
    return {"ok": True, "key_set": True, "replaced": replaced, "var": var, "kind": kind,
            "key_hint": intelligence_target.key_hint(payload.api_key)}


def _intelligence_probe(intelligence_cfg: dict) -> dict:
    """Synchronous connectivity probe run in a worker thread (event loop
    must never block on urllib). Probes the SAVED config: health endpoint,
    then one minimal chat completion through the exact request shape the
    agent sends. No secrets in the returned error text."""
    import os as _os
    import urllib.request as _rq
    import urllib.error as _ue
    kind = intelligence_cfg.get("kind") or intelligence.DEFAULT_KIND
    # An unknown kind probes the default lane's way (health, body, error).
    default_lane = intelligence.LANES[intelligence.DEFAULT_KIND]
    lane = _lane(kind) or default_lane
    agent = intelligence_target.agent_for(intelligence_cfg, kind)
    url = intelligence_target.request_url(
        kind, intelligence_cfg.get("url") or _os.getenv("LVI_HERMES_URL") or "", agent)
    # Key law (strict, 2026-10-08): each kind reads EXACTLY ONE variable —
    # hermes: LVI_HERMES_KEY; openclaw: LVI_OPENCLAW_KEY. No cross-kind
    # fallback anywhere (this probe previously read the now-DELETED
    # LVI_BRAIN_KEY — the openclaw Test button would 401 with no key found;
    # caught in the 2026-10-08 naming sweep).
    _kv = intelligence_target.key_var(kind, agent) or default_lane.base_key_var
    key = _saved_env().get(_kv) or ""  # the saved key: Test checks what will run
    if not url:
        return {"ok": False, "error": "no url configured"}
    if not key:
        return {"ok": False, "error": "no key set (save the key first)"}
    t0 = time.monotonic()
    health = lane.health_url(url)
    try:
        # health: no-auth liveness (OpenClaw /health; hermes api_server
        # has none — a plain GET on the base URL 404s but proves reach)
        _rq.urlopen(_rq.Request(health, method="GET"), timeout=5).read()
    except _ue.HTTPError:
        pass  # 404/401 on health still proves the endpoint is listening
    except Exception as e:
        return {"ok": False, "error": f"unreachable: {type(e).__name__}"}
    model, body = lane.probe_body(agent, intelligence_cfg)
    req = _rq.Request(url + "/chat/completions",
                      data=json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    try:
        with _rq.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        latency = round((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "model": model,
                "reply": (content or "")[:80]}
    except _ue.HTTPError as e:
        return {"ok": False, "error": lane.probe_error(e, key)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}


class IntelligenceAgentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: _LaneKind
    # The URL on screen (may be unsaved); omitted = that kind's saved URL.
    url: str | None = Field(default=None, max_length=200,
                             pattern=r"^https?://[A-Za-z0-9._:-]+(:\d+)?(/[\w./-]*)?$")

    @model_validator(mode="before")
    @classmethod
    def _short_url(cls, data):
        return _normalize_intelligence_url(data)

    @model_validator(mode="after")
    def _url_port(self):
        _check_intelligence_url_port(self.url)
        return self


@router.post("/intelligence/agents", dependencies=GUARD)
async def post_intelligence_agents(payload: IntelligenceAgentsPayload):
    """List the agents the provider offers, each with key status.
    OpenClaw: its /v1/models. Nanobot: its /v1/models (its one configured
    model). Hermes: the profiles on its gateway (the Hermes dashboard's
    public status route; falls back to the default agent). Never returns a
    key. POST + GUARD: it makes outbound requests.
    The stored key is attached ONLY when the probe targets this kind's
    SAVED URL (an omitted url means the saved URL); any other URL — typed,
    not yet saved — is probed unauthenticated, so a mistyped or third-party
    host can never receive the OpenClaw or Nanobot key."""
    icfg = dict(config_store.read_section("intelligence") or {})
    lane = intelligence.LANES[payload.kind]  # the Literal admits registry kinds only
    saved_url = lane.saved_url(icfg)
    url = payload.url or saved_url
    attach_key = not payload.url or intelligence_target.same_target(payload.url, saved_url)
    result = await asyncio.to_thread(
        intelligence_target.discover_agents, payload.kind, url, _saved_env(), 4.0,
        icfg.get(lane.status_url_key) if lane.status_url_key else None,
        attach_key)
    return {"agents": [{"id": a["id"], "key_set": a["key_set"]} for a in result["agents"]],
            "note": result.get("note"),
            "selected": intelligence_target.agent_for(icfg, payload.kind) or lane.default_agent}


@router.post("/intelligence/test", dependencies=GUARD)
async def post_intelligence_test():
    """Test the SAVED Intelligence config end-to-end (health + one
    minimal chat completion). Runs the sync probe in a thread."""
    intelligence_cfg = dict(config_store.read_section("intelligence") or {})
    result = await asyncio.to_thread(_intelligence_probe, intelligence_cfg)
    if result.get("ok"):
        logger.info(f"dashboard: intelligence test ok "
                    f"(latency {result.get('latency_ms')}ms)")
    else:
        logger.warning(f"dashboard: intelligence test failed: {result.get('error')}")
    return result


# ── bot control (Settings) ─────────────────────────────────────────────

# Restart-from-the-dashboard problem: the dashboard LIVES in the bot
# process, so a plain restart would kill the HTTP response mid-flight.
# Resolution: hand the restart to a DETACHED systemd transient unit —
# the user manager owns it, not us, so it fires 2s after the response
# completes.
#
# LVI_RESTART_COMMAND is GONE from prod:
# it was a generic "run this command" hook readable from .env, and the
# newline-injection bug (since fixed at TTSKeyPayload) could have armed
# it. The test suite now monkeypatches subprocess.run for this route
# instead — see tests/test_dashboard.py.


@router.post("/restart", dependencies=GUARD)
async def restart_bot():
    """Restart lenoxvi-bot from the dashboard (Settings → Bot control).

    The response returns BEFORE the restart fires (2s window) — the
    transient unit survives this process's death by design. Client polls
    /status until uptime resets, then reloads panels.
    """
    unit = f"lenoxvi-dash-restart-{int(time.time() * 1000)}"
    cmd = [
        "systemd-run", "--user", f"--unit={unit}",
        "bash", "-c", "sleep 2 && systemctl --user restart lenoxvi-bot",
    ]
    # Off the event loop: this process also runs every room's voice pipeline.
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        logger.error(f"dashboard: restart scheduling failed: {proc.stderr.strip()}")
        raise HTTPException(status_code=500, detail="restart scheduling failed (see bot log)")
    logger.info(f"dashboard: bot restart scheduled via transient unit {unit} — fires in 2s")
    return {"ok": True, "restarting": True}


# ── Voice Tuning (Voice tab — wake/VAD/turn knobs, all slider-set) ──────

# Six ear-tuning knobs, each config-gated and read at pipeline
# construction. Ranges are honest per knob's real behavior history:
# wake threshold 0.30-0.60 (TV exploits fired 0.40-0.51; real wakes
# 0.84-1.00), awake timeout, VAD confidence/min_volume, turn lull, echo
# dedup. Writes are read-merge-write PER SECTION — never a blind section
# replace (the 2026-10-04 filler-clobber lesson).


class VoiceTuningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    wake_threshold: float | None = Field(default=None, ge=0.30, le=0.60)
    wake_awake_timeout: float | None = Field(default=None, ge=5.0, le=60.0)
    vad_confidence: float | None = Field(default=None, ge=0.30, le=0.90)
    vad_min_volume: float | None = Field(default=None, ge=0.10, le=0.90)
    lull_secs: float | None = Field(default=None, ge=0.2, le=2.0)
    dedup_window: float | None = Field(default=None, ge=2.0, le=30.0)


_TUNING_TARGETS = {  # payload field -> (config section, key)
    "wake_threshold": ("wake", "threshold"),
    "wake_awake_timeout": ("wake", "awake_timeout"),
    "vad_confidence": ("vad", "confidence"),
    "vad_min_volume": ("vad", "min_volume"),
    "lull_secs": ("intelligence", "lull_secs"),
    "dedup_window": ("intelligence", "dedup_window"),
}


# ── speech-to-text card ──────────────────────────────────────────────
# A provider from one of the three speech tiers (stt_catalog) and a
# language. Embedded is one fixed Whisper model on this machine, so the
# card has nothing to download, delete or size up.


def _stt_status() -> dict:
    import stt_fallback
    import stt_local

    return {**stt_local.status(), **stt_fallback.status()}


def _stt_deepgram_sdk() -> bool:
    """pipecat's Deepgram STT is built on Deepgram's SDK (an optional extra)."""
    import importlib.util

    return importlib.util.find_spec("deepgram") is not None


def _stt_key_set(provider: str) -> bool:
    return _key_set(stt_catalog.KEY_VARS.get(provider))


def _stt_providers() -> list:
    """Every STT provider in card order with its tier (stt_catalog). One
    that can't be picked yet says why: missing setup first, then its key."""
    localai_problem = _localai_problem()
    out = []
    for p in stt_catalog.PROVIDERS:
        item = dict(p, available=True)
        if p["name"] == "deepgram" and not _stt_deepgram_sdk():
            item.update(available=False, reason="not installed")
        elif p["name"] == "localai" and localai_problem:
            item.update(available=False, reason=_LOCALAI_REASON)
        elif stt_catalog.is_cloud(p["name"]) and not _stt_key_set(p["name"]):
            item.update(available=False, reason=_ADD_KEY_REASON)
        out.append(item)
    return out


def _stt_current() -> dict:
    # Fresh from config.yaml (config > env > default, the same chain the bot
    # boots with) — the process config cache misses hand edits. An unset or
    # unknown provider reads as Embedded, the way the bot resolves it.
    sec = config_store.read_section("stt") or {}

    def pick(key, env, default=None):
        return sec.get(key) or os.getenv(env) or default

    language = pick("language", "LVI_STT_LANGUAGE") or "auto"
    return {
        "provider": stt_catalog.resolve_provider(pick("provider", "LVI_STT_PROVIDER")),
        "language": language,
        "custom_language": language not in stt_catalog.LANGUAGE_CODES,
    }


def _stt_write(language: str | None = None, provider: str | None = None) -> None:
    # Read-modify-write under the store's lock. Only provider and language
    # are written; anything else the section holds stays as it is (the
    # product never rewrites a user's config).
    with config_store.LOCK:
        section = dict(config_store.read_section("stt") or {})
        if provider is not None:
            section["provider"] = provider
        if language is not None:
            # Written explicitly, "auto" included: removing the key would let
            # an LVI_STT_LANGUAGE env value override Auto-detect.
            section["language"] = language
        config_store.write_section("stt", section)


class SttPayload(BaseModel):
    # extra='forbid': a field the card no longer has (a tab left open
    # across an upgrade) 422s naming it, and nothing is written.
    model_config = ConfigDict(extra="forbid")
    language: str | None = Field(default=None, max_length=16)
    provider: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _something(self):
        if self.language is None and self.provider is None:
            raise ValueError("send language and/or provider")
        return self


@router.get("/stt")
async def get_stt():
    """The STT card: the saved pick, the three tiers, every provider (its
    tier, and whether it can be picked now), the languages and the running
    state."""
    return {
        "current": _stt_current(),
        "tiers": stt_catalog.TIERS,
        "providers": _stt_providers(),
        "languages": stt_catalog.LANGUAGES,
        "deepgram_key_set": _stt_key_set("deepgram"),
        "elevenlabs_key_set": _stt_key_set("elevenlabs"),
        "status": _stt_status(),
    }


@router.put("/stt", dependencies=GUARD)
async def put_stt(payload: SttPayload):
    """Save the STT provider and/or language. A provider change waits for a
    restart; the language is live on Whisper (a cloud provider fixes it when
    the session is built)."""
    err = stt_catalog.validate_language(payload.language)
    if payload.provider is not None:
        err = err or stt_catalog.validate_provider(payload.provider)
        if not err and payload.provider == "deepgram" and not _stt_deepgram_sdk():
            err = "deepgram-sdk is not installed on this machine (pip install deepgram-sdk)"
        if not err and payload.provider == "localai":
            err = _localai_problem()
    if err:
        raise HTTPException(status_code=422, detail=err)
    import stt_fallback

    running_provider = stt_fallback._STATE["provider"]
    before = _stt_current()
    restart_needed = False
    # Whisper reads the language live; a cloud provider's is fixed when the
    # session is built, so on (or headed to) one it's a restart.
    if (payload.language is not None and payload.language != before["language"]
            and any(map(stt_catalog.is_cloud, (before["provider"], running_provider, payload.provider)))):
        restart_needed = True
    provider = None
    if payload.provider is not None and payload.provider != before["provider"]:
        provider = payload.provider
        # A restart only if the bot isn't already running what was picked.
        restart_needed = restart_needed or payload.provider != running_provider
    if payload.language is not None or provider is not None:
        try:
            _stt_write(language=payload.language, provider=provider)
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
    cur = _stt_current()
    key_needed = stt_catalog.is_cloud(cur["provider"]) and not _stt_key_set(cur["provider"])
    return {"ok": True, "restart_needed": restart_needed, "key_needed": key_needed, "current": cur}


@router.get("/voice-tuning")
async def get_voice_tuning():
    """Current wake/VAD/turn knob values for the Voice Tuning panel."""
    wake = config_store.read_section("wake") or {}
    vad = config_store.read_section("vad") or {}
    intelligence_cfg = config_store.read_section("intelligence") or {}
    return {
        "wake_threshold": wake.get("threshold"),
        "wake_awake_timeout": wake.get("awake_timeout"),
        "vad_confidence": vad.get("confidence"),
        "vad_min_volume": vad.get("min_volume"),
        "lull_secs": intelligence_cfg.get("lull_secs"),
        "dedup_window": intelligence_cfg.get("dedup_window"),
    }


@router.put("/voice-tuning", dependencies=GUARD)
async def put_voice_tuning(payload: VoiceTuningPayload):
    """Write the provided knobs; None fields are left untouched. Merges
    into each target section (wake/vad/intelligence) — never replaces a section
    wholesale, so intelligence.fillers/max_tokens etc. always survive."""
    by_section: dict[str, dict] = {}
    for field, (section, key) in _TUNING_TARGETS.items():
        val = getattr(payload, field)
        if val is not None:
            by_section.setdefault(section, {})[key] = val
    if not by_section:
        raise HTTPException(status_code=422, detail="no knobs provided")
    written: dict[str, float] = {}
    for section, updates in by_section.items():
        current = dict(config_store.read_section(section) or {})
        current.update(updates)
        try:
            config_store.write_section(section, current)
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
        written.update({f"{section}.{k}": v for k, v in updates.items()})
    logger.info(f"dashboard: voice-tuning saved {written} — applies on next restart")
    return {"ok": True, "written": written}


# ── OWW models (Voice tab — one active wake model, process-wide) ─────


class OWWModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    model: str = Field(min_length=1, max_length=64)
    # The source disambiguates a same-basename root/models/
    # collision — without it the route resolved models/-first and a
    # click on the ROOT row silently activated the models/ copy.
    # None = legacy resolution (models first, root fallback).
    # "stock" = a pre-trained model bundled with openwakeword, by key.
    source: Literal["root", "models", "stock"] | None = None

    @model_validator(mode="after")
    def _check_model(self):
        if self.source == "stock":
            if self.model not in wake_name.STOCK_MODELS:
                raise ValueError(f"unknown stock model (one of {', '.join(wake_name.STOCK_MODELS)})")
        elif not re.match(r"^[A-Za-z0-9_.-]+\.onnx$", self.model):
            raise ValueError("model must be a .onnx file name")
        return self


def _oww_dir() -> Path:
    # Repo-root .onnx files stay where they are (legacy root models keep
    # working — the loader resolves wake.model by path); models/ is the
    # dashboard-managed home. Both gitignored; neither ships in the repo.
    d = models_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _oww_active_path() -> Path:
    """The model file the loader would resolve RIGHT NOW (config > env >
    code default) as a full resolved path — path-exact, so two files
    with the same basename (root vs models/) are distinct."""
    # The FILE, not cfg(): the boot cache only refreshes on dashboard
    # writes, so a hand-edited wake.model left the active marker — and
    # the delete guard — pointing at a stale model.
    wake = config_store.read_section("wake") or {}
    value = str(wake.get("model") or _getenv("LVI_WAKE_MODEL") or wake_name.DEFAULT_MODEL)
    try:
        return Path(wake_name.resolve_model(value)).resolve()
    except Exception:                   # stock key but openwakeword broken: never 500 the panel
        return Path(value).resolve()


def _oww_validate(data: bytes) -> None:
    """Load-and-one-test-inference against the SAME library and shape the
    pipeline feeds (loaded-gun rule: a model that can't run
    never lands on disk, so a restart can never wake-brick the bot)."""
    import io

    import onnxruntime as ort

    try:
        sess = ort.InferenceSession(data, providers=["CPUExecutionProvider"])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"model failed to load: {e}")
    inp = sess.get_inputs()[0]
    # Shape comes FROM THE MODEL (production model: [1, 16, 96] float32 —
    # 16 frames x 96 mel bands). Derive each dim: -1/dynamic/None probes
    # as 1; concrete dims probe at face value. No shape is hard-coded.
    shape = []
    for d in inp.shape:
        if isinstance(d, int) and d > 0:
            shape.append(d)
        else:
            shape.append(1)
    probe = np.zeros(tuple(shape), dtype=np.float32)
    try:
        sess.run(None, {inp.name: probe})
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"test inference failed (input shape {tuple(inp.shape)}): {e}")


@router.get("/oww/models")
async def oww_list_models():
    """All wake-word models available to the bot: models/ dir + legacy
    repo-root .onnx files, with size, source and the path-exact active
    marker (a root file with the same basename as the active models/
    file still shows as a distinct row — display all options)."""
    active = _oww_active_path()
    out = []
    for d in (_oww_dir(), wake_root()):
        try:
            files = sorted(d.glob("*.onnx")) if d.exists() else []
        except OSError:
            continue
        for f in files:
            out.append({"name": f.name, "size": f.stat().st_size,
                        # resolved: an env-spelled dir equal to the root
                        # (e.g. LVI_MODELS_DIR=".") must compare equal
                        "source": "models" if f.parent.resolve() != wake_root().resolve() else "root",
                        "path": str(f.resolve()),
                        "selected": f.resolve() == active})
    # Stock pre-trained models ship inside the openwakeword package:
    # selectable by key, never deletable, licence shown (non-commercial).
    for key in wake_name.STOCK_MODELS:
        try:
            p = Path(wake_name.resolve_model(key))
        except Exception as e:          # openwakeword missing/broken: keep custom rows
            logger.warning(f"dashboard: stock wake model '{key}' unavailable ({e})")
            continue
        out.append({"name": key, "size": p.stat().st_size if p.is_file() else 0,
                    "source": "stock", "license": wake_name.STOCK_LICENSE,
                    "path": str(p.resolve()), "selected": p.resolve() == active})
    return {"models": out}


# One swap lock per model name: same-name uploads each stage to their
# OWN temp file, and the swap into `dest` serializes so a slower upload
# can't un-seat a finished one mid-swap.
_OWW_UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}


@router.post("/oww/models", dependencies=GUARD)
async def oww_upload_model(file: UploadFile = File(...)):
    """Upload a wake-word .onnx. Validates by loading + one test
    inference BEFORE accepting (rejects corrupt/foreign models with
    422). Same-name upload = intentional replace (retrain-and-push).
    Atomic staged write — a rejected upload never clobbers a good model."""
    name = file.filename or ""
    if not re.match(r"^[A-Za-z0-9_.-]+\.onnx$", name):
        raise HTTPException(status_code=400, detail="filename must be letters/digits/_-. and end .onnx")
    data = await file.read()
    if len(data) < 1024:
        raise HTTPException(status_code=400, detail="file too small to be a wake-word model")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="model too large (50MB cap)")
    # Model load + test inference (up to 50 MB) and the write run off the
    # event loop: this process also runs every room's voice pipeline.
    await asyncio.to_thread(_oww_validate, data)
    dest = _oww_dir() / name
    # One temp file per upload (mkstemp), never the shared
    # "<name>.uploading": two same-name uploads wrote that one file and
    # the first swap moved what the second was still writing (the same
    # race the firmware uploader stages around).
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f"{name}.",
                                    suffix=".uploading")
    tmp = Path(tmp_name)
    lock = _OWW_UPLOAD_LOCKS.setdefault(name, asyncio.Lock())
    try:
        def _write():
            with os.fdopen(fd, "wb") as f:
                f.write(data)

        await asyncio.to_thread(_write)
        async with lock:
            await asyncio.to_thread(tmp.replace, dest)  # validated: swap in atomically
    finally:
        # gone after the swap; a refused or dropped upload must not
        # leave its partial behind for the next upload to swap in
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    logger.info(f"dashboard: OWW model '{name}' uploaded ({len(data)} bytes) — select it to use")
    return {"ok": True, "name": name, "size": len(data)}


@router.put("/oww/select", dependencies=GUARD)
async def oww_select_model(payload: OWWModelPayload):
    """Point wake.model at an uploaded model (applies on next restart).
    Merges into the wake section — threshold/awake_timeout survive.

    `source` keys the EXACT file the panel row named — with the
    same basename in both root and models/, a name-only select always
    resolved to the models/ copy and a click on the root row silently
    activated the other file. None keeps the legacy resolution."""
    name = payload.model
    if payload.source == "stock":
        # The KEY goes into config (resolved at load time), so the
        # config stays portable across installs and venv paths.
        path = name
    elif payload.source is not None:
        path = (_oww_dir() if payload.source == "models" else wake_root()) / name
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"model '{name}' not found in {payload.source}")
    else:
        path = _oww_dir() / name
        if not path.is_file():
            # legacy root model? (pre-models-dir uploads / hand-copied files)
            if (wake_root() / name).is_file():
                path = wake_root() / name
            else:
                raise HTTPException(status_code=404, detail=f"model '{name}' not found")
    # Stored relative to the state root when the file lives under it
    # ("models/x.onnx", "x.onnx" — as before the paths anchor): the config
    # stays portable across installs. Stock keys are bare names already.
    if isinstance(path, Path):
        try:
            path = path.relative_to(state_root())
        except ValueError:
            pass
    current = dict(config_store.read_section("wake") or {})
    current["model"] = str(path)
    try:
        config_store.write_section("wake", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"dashboard: wake model set to '{path}' — applies on next restart")
    return {"ok": True, "model": str(path), "note": "applies on next bot restart"}


class WakePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    name: str | None = Field(default=None, max_length=30)   # "" = clear (no name)

    @model_validator(mode="after")
    def _check_terms(self):
        if self.name and self.name.strip() and not wake_name.valid_term(self.name.strip()):
            raise ValueError("name: a letter, then letters, spaces, ' or - (max 30)")
        return self


@router.get("/wake")
async def get_wake():
    """The wake name card: the name (wake.name, "" if unset)."""
    return {"name": wake_name.name()}


@router.put("/wake", dependencies=GUARD)
async def put_wake(payload: WakePayload):
    """Save the name. Applies LIVE — the drop-filter and log name
    read config per call (the write refreshes the cache). Edits the wake
    section in place (comments survive)."""
    wake = config_store.read_section("wake") or {}
    touched = {}
    if payload.name is not None:
        if payload.name.strip():
            wake["name"] = " ".join(payload.name.split())
        else:
            wake.pop("name", None)
        touched["wake"] = wake
    if touched:
        try:
            config_store.write_sections(touched)
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
        logger.info(f"dashboard: wake {'/'.join(sorted(touched))} saved — live, no restart")
    return {"ok": True, "name": wake_name.name()}


@router.delete("/oww/models/{name}", dependencies=GUARD)
async def oww_delete_model(name: str, source: str = ""):
    """Delete a stored model. HARD RULE: the file the loader would
    resolve RIGHT NOW (path-exact) can never be deleted — a same-named
    spare (root vs models/) stays deletable. `source` disambiguates
    same-basename copies when present (?source=root|models)."""
    if source == "stock" or name in wake_name.STOCK_MODELS:
        raise HTTPException(status_code=409,
                            detail="stock models ship with openwakeword and can't be deleted")
    if not re.match(r"^[A-Za-z0-9_.-]+\.onnx$", name):
        raise HTTPException(status_code=400, detail="bad model name")
    active = _oww_active_path()
    candidates = []
    if source in ("", "models"):
        candidates.append(_oww_dir() / name)
    if source in ("", "root"):
        candidates.append(wake_root() / name)
    for base in candidates:
        if base.is_file():
            if base.resolve() == active:
                raise HTTPException(status_code=409,
                                     detail="cannot delete the active model — select another first")
            base.unlink()
            logger.info(f"dashboard: OWW model '{name}' ({base}) deleted")
            return {"ok": True, "deleted": name, "path": str(base)}
    raise HTTPException(status_code=404, detail=f"model '{name}' not found")


# ── firmware push (operator side of firmware_server.py) ─────────────

_FIRMWARE_SWAP_LOCKS = {t: asyncio.Lock() for t in firmware_image.TRACKS}

# Node setup's boot files (deploy_firmware.sh publishes them): a Firmware-card
# upload replaces the app only and must never delete them.
FLASH_SET_FILES = ("bootloader.bin", "partition-table.bin", "ota_data_initial.bin")


def _check_firmware_image(tmp: Path, size: int, track: str) -> str:
    """Validate a staged upload for `track`; returns its version or raises
    a 400 naming what's wrong. Blocking file reads — call in a thread."""
    if size < 1024 * 200:
        raise HTTPException(status_code=400, detail="file too small to be a firmware image")
    # Version from the embedded descriptor: the .bin carries a 32-byte
    # SPI image header, then esp_app_desc_t — magic 0xABCD5432 (LE) at
    # byte 32, version[32] at byte 48 (measured against the real image
    # 2026-09-29; an offset of 24 was wrong and rejected a good bin).
    with open(tmp, "rb") as f:
        head = f.read(256)
    if head[32:36] != b"\x32\x54\xcd\xab":
        raise HTTPException(
            status_code=400,
            detail="not an ESP32 app image (no app-descriptor magic) — is this a LenoxVI build?",
        )
    version = head[48:80].split(b"\x00")[0].decode(errors="replace").strip()
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        raise HTTPException(
            status_code=400,
            detail=f"no valid version in image header (got '{version or 'nothing'}') — is this a LenoxVI app image?",
        )
    # Board identity: the
    # image proves its line — compiled-in OTA URL format, plus the project
    # name on 1.2.9+ images (core/firmware_image.py). Refuse a mismatch
    # BEFORE the swap — the served image and manifest stay untouched.
    img_track = firmware_image.image_track(tmp.read_bytes())
    if img_track is None:
        raise HTTPException(
            status_code=400,
            detail="can't tell which board this image is for (no single OTA-track "
                   "marker) — not uploading it to any track",
        )
    if img_track != track:
        raise HTTPException(
            status_code=400,
            detail=f"this is {firmware_image.TRACK_LABEL[img_track]} firmware — it can't "
                   f"go on the {firmware_image.TRACK_LABEL[track]} track (it would "
                   f"cross-flash that node). Pick track '{img_track}'.",
        )
    return version


def _swap_firmware_image(tmp: Path, dest: Path, version: str) -> None:
    """Serve a validated staged image as `dest` and point the track's
    manifest at it. Blocking file I/O — call in a thread, under the
    track's swap lock."""
    os.chmod(tmp, 0o644)  # mkstemp creates 0600; served images were always world-readable
    tmp.replace(dest)  # validated: swap in atomically
    manifest = {"version": version, "filename": dest.name}
    mpath = dest.parent / "version.json"
    mtmp = mpath.with_suffix(".json.tmp")
    mtmp.write_text(json.dumps(manifest, indent=2))
    mtmp.replace(mpath)
    # The manifest now names only dest: any other .bin in the track dir
    # (e.g. a pre-rename node_esp32.bin) is unreachable — remove it so the
    # directory holds exactly what's served. Exception: Node setup's boot
    # files (FLASH_SET_FILES) are served too, via the flash-set route —
    # they stay.
    for stale in dest.parent.glob("*.bin"):
        if stale.name != dest.name and stale.name not in FLASH_SET_FILES:
            stale.unlink(missing_ok=True)


@router.post("/firmware/upload", dependencies=GUARD)
async def upload_firmware(
    file: UploadFile = File(...),
    # REQUIRED — no default. The old Form("node")
    # silently staged a track-less upload onto the ARRAY line; with no
    # board identity in the image yet, a Flex .bin
    # landing there gets OTA'd by an array node and kills it. A missing
    # track is a 422, never a guess. (Reverses the earlier "legacy
    # callers stay array" default — its only caller was the dead
    # page.html, which never sent a track.)
    track: str = Form(...),
):
    """Upload a firmware .bin; version comes from the image's embedded
    ESP app descriptor (magic 0xABCD5432 at byte 24; version string at
    byte 48, NUL-padded — measured against the real image 2026-09-29,
    same string the node self-reports via get_info). Manifest written
    atomically in the same step: no typed-version mismatch is
    possible (manual version entry removed 2026-09-29).

    Track-aware: the uploader declares which hardware line
    the image is for (array | flex | waveshare — one track vocabulary);
    the image and manifest land in that track's directory ONLY, under the
    track's own file name (<track>_esp32.bin). A Flex .bin
    on the array track gets OTA'd by an array node and kills it
    (cross-flash, AGENTS.md law). A wrong track is an honest 400, not a
    silent dead node."""
    if track not in firmware_image.TRACKS:
        raise HTTPException(status_code=400,
                            detail=f"unknown track '{track}' ({' | '.join(firmware_image.TRACKS)})")
    if not (file.filename or "").endswith(".bin"):
        raise HTTPException(status_code=400, detail="must be a .bin")
    # paths-anchored (repo/LVI_STATE_DIR), like the OTA server's reads — cwd
    # anchoring only lined up because systemd starts the bot in the repo.
    dest_dir = firmware_dir() / track
    dest = dest_dir / f"{track}_esp32.bin"  # fixed served name (manifest points here)

    # Stage to a temp file FIRST: a rejected upload must never clobber
    # the currently-served good image (loaded-gun rule). One temp file per
    # upload: with a shared name, two uploads to the same track interleaved
    # their chunks, and the first swap moved the file the second was still
    # writing into the served image.
    def _open_staging():
        dest_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=dest_dir, prefix=f"{dest.name}.", suffix=".uploading")
        return os.fdopen(fd, "wb"), Path(name)

    # Every disk step runs in a thread: all rooms' audio shares this loop.
    # Only the network read of each chunk stays on it.
    f, tmp = await asyncio.to_thread(_open_staging)
    size = 0
    try:
        try:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)
        version = await asyncio.to_thread(_check_firmware_image, tmp, size, track)
        # Signed by this bot before anything serves it: nodes on 1.3+ accept
        # only images signed with the key of the image they run.
        try:
            await asyncio.to_thread(firmware_signing.sign_file, tmp)
        except Exception as e:  # a refused upload, never a half-served one
            logger.warning(f"dashboard: firmware upload not signed: {e}")
            raise HTTPException(status_code=500, detail=f"couldn't sign the image: {e}") from e
        # Swap + manifest as one step per track, so the served image and
        # the version that names it always come from the same upload.
        async with _FIRMWARE_SWAP_LOCKS[track]:
            await asyncio.to_thread(_swap_firmware_image, tmp, dest, version)
    finally:
        # Already gone after a swap; a refused or dropped upload leaves it.
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    logger.info(f"dashboard: firmware {version} served on track '{track}' "
                f"({size} bytes) — nodes on this track pick it up on next check")
    return {"ok": True, "filename": dest.name, "size": size, "version": version,
            "track": track, "signing_key": firmware_signing.fingerprint()[:16]}


# The legacy POST /firmware/version route is
# REMOVED. It wrote the array manifest with any version string and no
# binary — a hand-crafted manifest could point the track at a version
# that was never validated (or never uploaded). The upload route above is
# the only manifest writer now: version AND track come from the image
# itself, guarded by the board-identity check.


# ── node setup: the full USB flash set (System tab) ──────────────────

_SETTINGS_REGION = (0x9000, 0x10000)  # nvs + phy_init: never part of a flash set


def _flash_set(track: str) -> dict:
    """The full flash set for one line (bootloader, partition table, OTA data,
    app). Blocking file I/O — call in a thread."""
    d = firmware_dir() / track
    root = d.resolve()
    missing = HTTPException(
        status_code=404,
        detail=f"no full flash set for '{track}' — publish one with deploy_firmware.sh")
    try:
        fm = json.loads((d / "flash.json").read_text())
        vm = json.loads((d / "version.json").read_text())
        parts = [{"name": str(p["name"]), "file": str(p["file"]), "offset": int(p["offset"])}
                 for p in fm["parts"]]
        parts.append({"name": "app", "file": str(vm["filename"]), "offset": int(fm["app_offset"])})
        flash = {k: str(fm["flash"][k]) for k in ("mode", "freq", "size")}
    except (OSError, ValueError, KeyError, TypeError):
        raise missing
    lo, hi = _SETTINGS_REGION
    out = []
    for p in parts:
        f = (d / p["file"]).resolve()
        if f.parent != root or not f.is_file():
            raise missing
        data = f.read_bytes()
        if p["offset"] < hi and p["offset"] + len(data) > lo:
            raise HTTPException(status_code=404,
                                detail=f"flash set for '{track}' would write the node's settings region — refusing")
        if p["name"] == "app" and not firmware_signing.has_signature(data):
            # A node flashed over USB with an unsigned 1.3+ image would refuse
            # every later update; the bot signs at upload/deploy/startup.
            raise HTTPException(status_code=409,
                                detail=f"the {track} app image isn't signed yet — re-deploy it or restart the bot to sign it")
        out.append({**p, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return {"track": track, "version": str(vm.get("version", "")), "flash": flash, "parts": out}


@router.get("/firmware/{track}/flash")
async def firmware_flash_set(track: str):
    """Full flash set for the System tab's Node setup page. Parts download
    from /firmware/{track}/{file}. The settings region is never part of a
    set, so a reflash keeps the node's Wi-Fi, name and host address."""
    if track not in firmware_image.TRACKS:
        raise HTTPException(status_code=404, detail=f"unknown track '{track}'")
    return await asyncio.to_thread(_flash_set, track)