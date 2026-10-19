"""routes — the dashboard's JSON API (the stable contract under the skin).

All endpoints live under /api/dashboard/ so they can never collide with
pipecat's own /api/* routes. The HTML page (page.html) is a thin skin
over these; a restyle or a host-side integration consumes the same
endpoints.

No auth by design: LAN-only page. Anyone on
the LAN can read status and edit mappings — accepted risk.
"""

import asyncio
import json
import os
import re
import subprocess
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
import intelligence_target
import node_speaker
import tts_pocket
import wake_name

import wake_word  # module-level import is light (pipecat frames only;
                  # the OWW/torch load happens at filter construction)

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


class MappingPayload(BaseModel):
    # extra='forbid': an unwrapped dict (pre-2026-10-07 React bug) must
    # 422 LOUDLY, never silently become room_zones={} via the default
    # factory — that default was a silent-wipe landmine (three real
    # wipes shipped to config.yaml before this guard existed).
    model_config = ConfigDict(extra="forbid")
    room_zones: dict[str, str] = Field(default_factory=dict)


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
    kind = str(cfg_env("intelligence", "kind", "LVI_INTELLIGENCE_KIND", intelligence_cfg.get("kind") or "hermes") or "hermes")
    if kind == "openclaw":
        agent = intelligence_target.agent_for(intelligence_cfg, "openclaw") \
            or str(_getenv("LVI_INTELLIGENCE_AGENT") or "")
        return f"OpenClaw · {agent}" if agent else "OpenClaw"
    agent = intelligence_target.agent_for(intelligence_cfg, "hermes")
    return f"Hermes · {agent}" if agent else "Hermes"


@router.get("/status")
async def status():
    boot = registry.boot_time()
    wake_cfg = config_store.read_section("wake") or {}
    stt_cfg = config_store.read_section("stt") or {}
    tts_cfg = config_store.read_section("tts") or {}
    return {
        "uptime_secs": round(time.time() - boot, 1) if boot else None,
        "version": registry.version(),
        # Rig facts (Settings tab header, read-only) — resolved through
        # the SAME chains the consumers use, so the dashboard always
        # shows what's actually constructed, never static text:
        # config > env > code default.
        "wake_model": os.path.basename(str(
            cfg_env("wake", "model", "LVI_WAKE_MODEL",
                    wake_cfg.get("model") or wake_word.WAKE_MODEL))),
        "stt_model": cfg_env("stt", "model", "LVI_STT_MODEL",
                             stt_cfg.get("model") or "large-v3-turbo"),
        # STT card chips: load state + per-command transcription time (median
        # of the last 10), straight from the shared model's owner.
        "stt": __import__("stt_local").status(),
        "tts_provider": cfg_env("tts", "provider", "LVI_TTS_PROVIDER",
                                tts_cfg.get("provider") or "elevenlabs"),
        # Intelligence: rig-facts label — same cfg_env chain
        # the agent reads, so the dashboard shows what will actually be
        # constructed. Config section, env vars, and UI all say
        # "intelligence" — one name, everywhere (naming rule).
        "intelligence": _intelligence_label(),
        "active_room": registry.active_room(),
        "last_wake": registry.last_wake(),
        "rooms": registry.known_rooms(),
        # Session modes (fresh | sticky per node) — the dashboard renders
        # these as the per-node checkbox; sticky rooms keep conversation
        # memory across reconnects/restarts.
        "session_modes": dict(config_store.read_section("session_modes") or {}),
        "note": "config edits apply on the next bot restart",
    }


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


def _wiim_cfg() -> dict:
    return dict(cfg("wiim") or {})


def _sonos_names(zones: dict) -> dict:
    """Live player_name per zone key (soco, per-IP). Missing/unreachable
    players just don't get a name — the config key still displays.
    Display-only: the Sonos-side
    name shows verbatim for mapping UX; routing stays keyed on the
    config key + UID so a Sonos-side rename can't orphan a mapping.
    SYNC soco network calls — caller MUST run this in a thread (this
    is the voice pipeline's event loop)."""
    out = {}
    for key, z in zones.items():
        ip = (z or {}).get("ip")
        if not ip:
            continue
        try:
            import soco

            player = soco.SoCo(ip)
            uid = player.uid
            if (z or {}).get("player_uid") and uid != z.get("player_uid"):
                logger.warning(f"zones: '{key}' UID mismatch — sonos says {uid}, config says {z.get('player_uid')}")
            out[key] = player.player_name
        except ImportError:
            break  # soco absent: skip the whole enrichment, keys still display
        except Exception as e:
            logger.debug(f"zones: sonos name fetch failed for '{key}' ({ip}): {e}")
    return out


@router.get("/mapping")
async def get_mapping(names: bool = False):
    zones = _all_zones()
    out = {
        "room_zones": dict(config_store.read_section("room_zones") or {}),
        "zones": zones,
        "default_zone": cfg("sonos", "default_zone"),
        # Per-zone clip volumes (slider lane): config copy + live layer,
        # merged across both output backends.
        "zone_volumes": {**(cfg("sonos", "zone_volumes") or {}),
                         **(cfg("wiim", "zone_volumes") or {})},
        "clip_volume": cfg("sonos", "clip_volume"),
        # What each zone ACTUALLY plays at — the slider's only
        # source (the UI's own `?? 70` fallback disagreed with the backends).
        "effective_volumes": {z: effective_zone_volume(z) for z in zones},
    }
    # Sonos display names are OPT-IN (?names=1). The
    # React UI polls /mapping every 5s and never reads them, yet every poll
    # ran soco network calls against EVERY zone — including WiiM IPs,
    # which a Sonos client can't answer (an offline player = a timeout per
    # poll). Sonos zones only. Still threaded when asked (this
    # is the voice pipeline's event loop).
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
    if names:
        out["sonos_names"] = await asyncio.to_thread(_sonos_names, _zones())
    return out


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
    try:
        diff = config_store.write_section("room_zones", clean)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # exactly one output sounds a reply — tell every speaker node
    # whether it's now the one (a full-dict PUT can map OR unmap it).
    node_speaker.sync_all(registry.capable_rooms())
    return {"ok": True, "room_zones": clean, "changed": diff["before"] != diff["after"]}


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
    gate = registry.gate()
    if gate is not None:
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
                # soco API (0.31.4): SoCo has NO .model_name attribute —
                # model lives in get_speaker_info()["model_name"] (a
                # per-device UPnP call, so it rides the SAME thread as
                # discovery). player_name IS a direct attribute
                # (cached from get_speaker_info). The first cut read
                # z.model_name, which raised AttributeError per device —
                # the swallow-all handler turned "found the Port" into
                # {"found": []} silently (2026-10-03, dashboard button test).
                info = await asyncio.to_thread(z.get_speaker_info) or {}
                model = (info.get("model_name") or "").strip()
                # soco reports "Sonos Port"/"Sonos Amp" with the prefix —
                # normalize before the supported check, else the flag lies
                # false for the exact device the audioClip path is proven on.
                model_norm = model.removeprefix("Sonos").strip()
                found.append(
                    {
                        "ip": z.ip_address,
                        "name": z.player_name,
                        "uid": z.uid,
                        "model": model,
                        # The proven audioClip path is S2 Port/Amp only;
                        # everything else is at the
                        # user's own risk and labeled as such.
                        "supported": model_norm in ("Port", "Amp"),
                    }
                )
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
    if name not in _all_zones():
        raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
    backend = _backend_of(name)
    from sonos_output import zone_volume_overrides
    from wiim_output import wiim_volume_overrides
    if backend == "wiim":
        section = dict(cfg("wiim") or {})
    else:
        section = dict(cfg("sonos") or {})
    vols = {k: int(v) for k, v in (section.get("zone_volumes") or {}).items()}
    before = vols.get(name)
    if payload.volume == int(section.get("clip_volume", DEFAULT_CLIP_VOLUME)):
        # Setting a zone back to the global value = no override needed;
        # keep the config minimal (same pattern as session modes).
        vols.pop(name, None)
    else:
        vols[name] = payload.volume
    try:
        config_store.write_section("sonos" if backend == "sonos" else "wiim",
                                   {**section, "zone_volumes": vols})
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
    if not name or "." in name or ":" in name:
        raise HTTPException(status_code=400, detail="bad zone name")
    if node_speaker.is_reserved_zone_name(name):
        raise HTTPException(status_code=400, detail=f"'{name}' is reserved (a node's built-in speaker)")
    # Collision check across BOTH backends: one merged namespace (the
    # mapping routes and the room_zones config key see one zone list).
    if name in _all_zones():
        raise HTTPException(status_code=400, detail=f"zone '{name}' already exists")
    if payload.backend == "wiim":
        # WiiM zones need no player_uid (HTTP API is UID-less); the
        # payload validator already dropped any sent value. Adding a zone to a
        # backend IMPLIES enabling it: the 2026-10-04 bedroom test went
        # silent because the wiim section was written without
        # enabled: true and WiiMOutput constructed disabled (replies
        # dropped before ever reaching the network).
        zones = _wiim_zones()
        zones[name] = {"ip": payload.ip.strip()}
        merged = {**_wiim_cfg(), "enabled": True, "zones": zones}
        if not merged.get("default_zone"):
            merged["default_zone"] = name
        try:
            config_store.write_section("wiim", merged)
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
    else:
        zones = _zones()
        zones[name] = {"ip": payload.ip.strip(), "player_uid": payload.player_uid.strip()}
        try:
            config_store.write_section("sonos", {**dict(cfg("sonos") or {}), "zones": zones})
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "zones": zones, "backend": payload.backend}


@router.delete("/zones/{name}", dependencies=GUARD)
async def delete_zone(name: str):
    backend = _backend_of(name)
    if backend == "wiim":
        if name not in _wiim_zones():
            raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
        zones = _wiim_zones()
        del zones[name]
        # ONE atomic multi-section write — the old back-to-back
        # write_section pair was non-atomic (crash between them left a
        # deleted zone with dangling room mappings) and churned two
        # backups. Sections dict preserves every OTHER key untouched.
        sections = {"wiim": {**_wiim_cfg(), "zones": zones}}
    else:
        if name not in _zones():
            raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
        zones = _zones()
        del zones[name]
        sections = {"sonos": {**dict(cfg("sonos") or {}), "zones": zones}}
    # rooms mapped to a deleted zone become dangling — drop them in the
    # SAME write (they're a separate section, same atomic pass)
    mapping = dict(config_store.read_section("room_zones") or {})
    dangling = [r for r, z in mapping.items() if z == name]
    if dangling:
        for r in dangling:
            del mapping[r]
        sections["room_zones"] = mapping
    try:
        config_store.write_sections(sections)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # A speaker room mapped to the deleted zone is back on its own speaker
    # (the default) — tell its node, or it stays silent.
    node_speaker.sync_all(registry.capable_rooms())
    return {"ok": True, "dropped_room_mappings": dangling, "backend": backend}


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
    path = Path("clips") / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mp3)
    url = server.url_for(filename)

    duration = len(pcm) / (2 * 24000)
    gate = registry.gate()
    if gate is not None:
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
    password: str = Field(min_length=1, max_length=63)


class NamePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    name: str = Field(min_length=1, max_length=31, pattern=r"^[A-Za-z0-9_-]+$")


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


@router.post("/nodes/{room}/name", dependencies=GUARD)
async def set_node_name(room: str, payload: NamePayload):
    _room_online(room)
    reply = await _node_cmd(room, "set_name", {"name": payload.name})
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

# The dashboard's pick-lists come FROM HERE via
# GET /tts — the UI used to hardcode its own copies (12 of 40 Aura, 16 of
# 36 Flux), so most accepted voices were unpickable and a configured
# aura-2-zeus-en couldn't even display. One catalog, one owner.
TTS_CATALOG = {
    "elevenlabs": list(_ELEVENLABS_MODELS),
    "deepgram": list(_DEEPGRAM_VOICES),
    "deepgram-flux": list(_DEEPGRAM_FLUX_VOICES),
    "pocket": list(tts_pocket.POCKET_VOICES),
}


class TTSPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    provider: str = Field(default="elevenlabs",
                          pattern="^(elevenlabs|deepgram|deepgram-flux|pocket)$")
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
    # Pocket (local): a preset voice name only from the dashboard; a .wav
    # clone path is config-file-only (it needs gated weights + an HF login).
    pocket_voice: str | None = Field(default=None)
    # Local hot standby behind a cloud provider. "off" clears it; None =
    # leave as-is (partial update).
    fallback: str | None = Field(default=None, pattern="^(pocket|off)$")


def _validate_tts_payload(p: TTSPayload, saved_fallback: str | None = None) -> None:
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
    elif p.provider == "pocket":
        if not p.pocket_voice or p.pocket_voice not in tts_pocket.POCKET_VOICES:
            raise HTTPException(status_code=422, detail="pocket_voice must be one of the Pocket presets (alba …)")
        if not tts_pocket.pocket_available():
            raise HTTPException(status_code=422, detail=f"Pocket TTS is not installed — {tts_pocket.INSTALL_HINT}")
    # A Pocket voice is validated wherever it rides (primary voice or the
    # fallback's voice): only the presets are dashboard-settable.
    if p.pocket_voice is not None and p.pocket_voice not in tts_pocket.POCKET_VOICES:
        raise HTTPException(status_code=422, detail="pocket_voice must be one of the Pocket presets (alba …)")
    if p.fallback == "pocket":
        if p.provider == "pocket":
            raise HTTPException(status_code=422, detail="the fallback can't be the primary (Pocket is already the provider)")
        # Install check only when TURNING IT ON: re-sending an already-saved
        # fallback must never lock the panel if Pocket went missing later.
        if saved_fallback != "pocket" and not tts_pocket.pocket_available():
            raise HTTPException(status_code=422, detail=f"Pocket TTS is not installed — {tts_pocket.INSTALL_HINT}")


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


@router.get("/tts")
async def get_tts():
    """Current TTS config + key status per provider. Never returns keys."""
    import os as _os
    tts_cfg = config_store.read_section("tts") or {}
    return {
        "provider": tts_cfg.get("provider") or "elevenlabs",
        "voice_id": tts_cfg.get("voice_id") or _os.getenv("LVI_TTS_VOICE_ID"),
        "model": tts_cfg.get("model") or "eleven_flash_v2_5",
        "deepgram_voice": tts_cfg.get("deepgram_voice") or "aura-2-thalia-en",
        "deepgram_flux_voice": tts_cfg.get("deepgram_flux_voice") or "flux-hannah-en",
        "deepgram_flux_speed": tts_cfg.get("deepgram_flux_speed"),
        "deepgram_flux_expressivity": tts_cfg.get("deepgram_flux_expressivity"),
        "elevenlabs_speed": tts_cfg.get("elevenlabs_speed"),
        "elevenlabs_stability": tts_cfg.get("elevenlabs_stability"),
        "pocket_voice": tts_cfg.get("pocket_voice") or tts_pocket.DEFAULT_VOICE,
        "fallback": tts_cfg.get("fallback") or None,
        "pocket_installed": tts_pocket.pocket_available(),
        "elevenlabs_key_set": bool(_os.getenv("ELEVENLABS_API_KEY")),
        "deepgram_key_set": bool(_os.getenv("DEEPGRAM_API_KEY")),
        # First 4 characters of a set key, so a wrong key is recognisable.
        "elevenlabs_key_hint": intelligence_target.key_hint(_saved_env().get("ELEVENLABS_API_KEY")),
        "deepgram_key_hint": intelligence_target.key_hint(_saved_env().get("DEEPGRAM_API_KEY")),
        # The accepted values per provider — the UI's only source
        # for its pick-lists (elevenlabs = model ids; deepgram* = voices).
        "catalog": TTS_CATALOG,
        "note": "provider/voice/model apply on next bot restart; keys apply "
                "on next bot restart (read at pipeline construction)",
    }


@router.put("/tts", dependencies=GUARD)
async def put_tts(payload: TTSPayload):
    """Write provider + knobs to config.yaml (tts section). Applies on the
    next bot restart — same contract as every config edit."""
    current = dict(config_store.read_section("tts") or {})
    _validate_tts_payload(payload, saved_fallback=current.get("fallback"))
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
    elif payload.provider == "deepgram":
        current["deepgram_voice"] = payload.deepgram_voice
    elif payload.provider == "pocket":
        current["pocket_voice"] = payload.pocket_voice
        current.pop("fallback", None)   # Pocket as primary never carries a fallback
    else:
        current["deepgram_flux_voice"] = payload.deepgram_flux_voice
        # Flux knobs ride along — None means "leave as-is" (partial update
        # semantics: the panel always sends current values, but a bare
        # API PUT of just provider+voice must not wipe tuned knobs).
        if payload.deepgram_flux_speed is not None:
            current["deepgram_flux_speed"] = payload.deepgram_flux_speed
        if payload.deepgram_flux_expressivity is not None:
            current["deepgram_flux_expressivity"] = payload.deepgram_flux_expressivity
    if payload.fallback == "pocket" and payload.provider != "pocket":
        current["fallback"] = "pocket"
        # The fallback's voice (same tts.pocket_voice key Pocket-as-primary
        # uses) rides with the fallback; ignored when the fallback is off.
        if payload.pocket_voice:
            current["pocket_voice"] = payload.pocket_voice
    elif payload.fallback == "off":
        current.pop("fallback", None)
    try:
        config_store.write_section("tts", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"dashboard: tts config saved (provider={payload.provider}) — applies on next restart")
    return {"ok": True, "provider": payload.provider}


def _env_path():
    from pathlib import Path as _Path
    return _Path(os.getenv("LVI_ENV_FILE",
                           _Path(__file__).resolve().parent.parent.parent / ".env"))


def _saved_env() -> dict:
    """Process env overlaid with what .env holds NOW, so key status and
    hints show a key the moment it is saved (the voice agent itself still
    reads the process env, at the next restart). Read-only."""
    view = dict(os.environ)
    try:
        for ln in _env_path().read_text().splitlines():
            k, sep, v = ln.partition("=")
            if sep and k and not k.lstrip().startswith("#"):
                view[k.strip()] = v.strip()
    except OSError:
        pass
    return view


async def _write_env_key(var: str, value: str) -> bool:
    """Replace-or-append `var=value` in .env. Returns True if replaced.

    Atomic write + 0600: write_text created a
    missing .env as 0644, and a crash mid-write could truncate it.
    TTSKeyPayload's character-class pattern is the upstream defense
    against newline smuggling — this is the belt to that suspenders.
    """
    import os as _os
    from pathlib import Path as _Path
    env_path = _Path(_os.getenv("LVI_ENV_FILE",
                                _Path(__file__).resolve().parent.parent.parent / ".env"))
    lines = env_path.read_text().splitlines() if env_path.is_file() else []
    out, replaced = [], False
    for ln in lines:
        if ln.startswith(var + "="):
            out.append(var + "=" + value)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(var + "=" + value)
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n")
    _os.chmod(tmp, 0o600)
    _os.replace(tmp, env_path)  # atomic on the same filesystem
    return replaced


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


# ── Intelligence (Settings — provider picker) ──


class IntelligencePayload(BaseModel):
    # hermes = today's api_server contract; openclaw = OpenClaw Gateway
    # (model "openclaw/<agent>", x-openclaw-session-key sticky, no
    # conversation field). Any other kind 422s.
    model_config = ConfigDict(extra="forbid")  # misspelled keys 422, never no-op
    kind: str = Field(pattern="^(hermes|openclaw)$")
    url: str | None = Field(default=None, max_length=200,
                             pattern=r"^https?://[A-Za-z0-9._:-]+(:\d+)?(/[\w./-]*)?$")
    # Per-kind URL memory (2026-10-08): the two lanes live at different
    # addresses (api_server 8642 vs OpenClaw Gateway 18789). kind and url
    # arrive together; the route files url under the kind being saved so
    # the UI can offer the right one back on switch. `url` itself stays
    # the live value the agent reads — this map is recall, not truth.
    urls: dict[str, str] | None = Field(default=None)
    # The agent for THIS kind: an OpenClaw agent id, or a Hermes profile
    # ("default" = the main agent). Filed under intelligence.agents[kind].
    agent: str | None = Field(default=None, min_length=1, max_length=64,
                              pattern=r"^[A-Za-z0-9_.-]+$")


# One lane, one variable, named for its intelligence (naming law
# 2026-10-08). Single source for the dashboard routes — GET status, key
# save, and the Test probe all read this map.
INTELLIGENCE_KEY_VARS = {"hermes": "LVI_HERMES_KEY", "openclaw": "LVI_OPENCLAW_KEY"}


class IntelligenceKeyPayload(BaseModel):
    # The key is saved for the kind the user is
    # LOOKING AT, not the kind config.yaml holds. The old route derived the
    # variable from saved config, so picking OpenClaw in the dropdown and
    # saving its token on a hermes-configured install wrote the OpenClaw
    # token into LVI_HERMES_KEY — the wrong-token 401 failure mode, rebuilt
    # by the UI. kind is REQUIRED (no default = no silent guess) and
    # extra="forbid" so a misspelled field 422s instead of no-opping.
    model_config = ConfigDict(extra="forbid")
    kind: Literal["hermes", "openclaw"]
    # Hermes profiles each have their own key (LVI_HERMES_KEY_<AGENT>);
    # omitted or "default" = the kind's base variable. OpenClaw agents share
    # one Gateway token, so agent is ignored there.
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
    kind = intelligence_cfg.get("kind") or "hermes"
    # Key law (strict, 2026-10-08): each kind reads exactly ONE variable —
    # hermes: LVI_HERMES_KEY; openclaw: LVI_OPENCLAW_KEY. No cross-kind
    # fallback anywhere (a fallback turned a missing key into a silent
    # wrong-token outage once). key_kind names the variable.
    # keys_set: status for EVERY kind, so the UI chip shows
    # the key of the kind on screen, not the kind in config.
    _kv = INTELLIGENCE_KEY_VARS.get(kind, "LVI_HERMES_KEY")
    return {
        "kind": kind,
        "url": intelligence_cfg.get("url") or _env.get("LVI_HERMES_URL"),
        "urls": intelligence_cfg.get("urls") or {},
        "agent": intelligence_cfg.get("agent"),
        # Live agent per kind (hermes "default" = the main agent).
        "agents": {k: (intelligence_target.agent_for(intelligence_cfg, k)
                       or ("default" if k == "hermes" else None))
                   for k in INTELLIGENCE_KEY_VARS},
        "key_set": bool(_env.get(_kv)),
        "keys_set": {k: bool(_env.get(v)) for k, v in INTELLIGENCE_KEY_VARS.items()},
        "key_hints": {k: intelligence_target.key_hint(_env.get(v))
                      for k, v in INTELLIGENCE_KEY_VARS.items()},
        "key_kind": _kv.lower() if _env.get(_kv) else None,
        "note": "applies on the next bot restart (read at pipeline construction)",
    }


@router.put("/intelligence", dependencies=GUARD)
async def put_intelligence(payload: IntelligencePayload):
    """Write kind (+ url/agent when provided) to config.yaml (intelligence
    section) — read-merge-write, so fillers/max_tokens/lull always
    survive. Applies on the next bot restart."""
    current = dict(config_store.read_section("intelligence") or {})
    current["kind"] = payload.kind
    if payload.url is not None:
        current["url"] = payload.url
        # File the URL under the kind being saved (per-kind recall map).
        urls = dict(current.get("urls") or {})
        urls[payload.kind] = payload.url
        current["urls"] = urls
    else:
        # Bare kind flip: restore THAT kind's remembered URL
        # when one exists — keeping the old kind's URL would point the
        # new lane at the wrong intelligence (api_server vs Gateway).
        remembered = (current.get("urls") or {}).get(payload.kind)
        if remembered:
            current["url"] = remembered
    if payload.urls is not None:
        # UI may send the full recall map (it knows both kinds' URLs).
        merged = dict(current.get("urls") or {})
        for k, v in payload.urls.items():
            if k in ("hermes", "openclaw") and isinstance(v, str) and v:
                merged[k] = v
        current["urls"] = merged
    if payload.agent is not None:
        agents = dict(current.get("agents") or {})
        agents[payload.kind] = payload.agent
        current["agents"] = agents
        if payload.kind == "openclaw":
            current["agent"] = payload.agent  # legacy key, read by older builds
    if payload.kind == "openclaw" and not intelligence_target.agent_for(current, "openclaw"):
        raise HTTPException(status_code=422, detail="openclaw needs an agent id (intelligence.agent)")
    try:
        config_store.write_section("intelligence", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"dashboard: intelligence config saved (kind={payload.kind}) — applies on next restart")
    return {"ok": True, "kind": payload.kind}


@router.put("/intelligence/key", dependencies=GUARD)
async def put_intelligence_key(payload: IntelligenceKeyPayload):
    """Intelligence API key → .env, kind-aware (naming law: each intelligence's key names what it is):
    hermes → LVI_HERMES_KEY, openclaw → LVI_OPENCLAW_KEY — the ONE variable
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
    kind = intelligence_cfg.get("kind") or "hermes"
    agent = intelligence_target.agent_for(intelligence_cfg, kind)
    url = intelligence_target.request_url(
        kind, intelligence_cfg.get("url") or _os.getenv("LVI_HERMES_URL") or "", agent)
    # Key law (strict, 2026-10-08): each kind reads EXACTLY ONE variable —
    # hermes: LVI_HERMES_KEY; openclaw: LVI_OPENCLAW_KEY. No cross-kind
    # fallback anywhere (this probe previously read the now-DELETED
    # LVI_BRAIN_KEY — the openclaw Test button would 401 with no key found;
    # caught in the 2026-10-08 brain→intelligence sweep).
    _kv = intelligence_target.key_var(kind, agent) or "LVI_HERMES_KEY"
    key = _saved_env().get(_kv) or ""  # the saved key: Test checks what will run
    if not url:
        return {"ok": False, "error": "no url configured"}
    if not key:
        return {"ok": False, "error": "no key set (save the key first)"}
    t0 = time.monotonic()
    try:
        # health: no-auth liveness (OpenClaw /health; hermes api_server
        # has none — a plain GET on the base URL 404s but proves reach)
        _rq.urlopen(_rq.Request(url + "/health", method="GET"), timeout=5).read()
    except _ue.HTTPError:
        pass  # 404/401 on health still proves the endpoint is listening
    except Exception as e:
        return {"ok": False, "error": f"unreachable: {type(e).__name__}"}
    if kind == "openclaw":
        model = "openclaw/" + agent
        body = {"model": model, "stream": False, "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
    else:
        model = intelligence_cfg.get("model") or "hermes-agent"
        body = {"model": model, "stream": False, "max_tokens": 16,
                "conversation": "dashboard-test",
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
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
        return {"ok": False, "error": f"gateway error: HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}


class IntelligenceAgentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["hermes", "openclaw"]
    # The URL on screen (may be unsaved); omitted = that kind's saved URL.
    url: str | None = Field(default=None, max_length=200,
                             pattern=r"^https?://[A-Za-z0-9._:-]+(:\d+)?(/[\w./-]*)?$")


@router.post("/intelligence/agents", dependencies=GUARD)
async def post_intelligence_agents(payload: IntelligenceAgentsPayload):
    """List the agents the provider offers, each with key status + hint.
    OpenClaw: its /v1/models. Hermes: the profiles on its gateway (the
    Hermes dashboard's public status route; falls back to the default
    agent). Never returns a key. POST + GUARD: it makes outbound requests."""
    icfg = dict(config_store.read_section("intelligence") or {})
    url = payload.url or (icfg.get("urls") or {}).get(payload.kind) \
        or (icfg.get("url") if (icfg.get("kind") or "hermes") == payload.kind else None) or ""
    result = await asyncio.to_thread(
        intelligence_target.discover_agents, payload.kind, url, _saved_env(), 4.0,
        icfg.get("hermes_status_url") if payload.kind == "hermes" else None)
    result["selected"] = intelligence_target.agent_for(icfg, payload.kind) \
        or ("default" if payload.kind == "hermes" else None)
    return result


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
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
# Four benchmarked faster-whisper models (stt_catalog), a language, and the
# GPU/CPU runtime written with each pick. A model that isn't on disk is
# downloaded FIRST; config switches only after the download succeeds, so a
# restart never downloads and config never names a missing model.

import stt_catalog  # noqa: E402

_STT_DL: dict = {}          # {"model", "state": downloading|ready|error, "error"?}
_STT_DL_LOCK = __import__("threading").Lock()
_GPU_NAME_CACHE: list = []


def _stt_cached(name: str) -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model(name, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 — not cached (or no hub): either way, not on disk
        return False


def _stt_download(name: str) -> None:
    from faster_whisper.utils import download_model
    download_model(name)


def _stt_gpu_name():
    """The CUDA GPU's name, or None without one. Looked up once."""
    if _GPU_NAME_CACHE:
        return _GPU_NAME_CACHE[0]
    name = None
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            name = "GPU"
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=3)
                first = out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 else ""
                name = first.replace("NVIDIA ", "") or name
            except Exception:  # noqa: BLE001 — a name is cosmetic; "GPU" will do
                pass
    except Exception:  # noqa: BLE001 — no ctranslate2 CUDA support -> no GPU
        name = None
    _GPU_NAME_CACHE.append(name)
    return name


def _stt_current() -> dict:
    model = cfg_env("stt", "model", "LVI_STT_MODEL", "large-v3-turbo")
    language = cfg_env("stt", "language", "LVI_STT_LANGUAGE") or "auto"
    return {
        "model": model,
        "language": language,
        "device": cfg_env("stt", "device", "LVI_STT_DEVICE", "cuda"),
        "compute": cfg_env("stt", "compute_type", "LVI_STT_COMPUTE", "float16"),
        "custom_model": model not in stt_catalog.MODEL_NAMES,
        "custom_language": language not in stt_catalog.LANGUAGE_CODES,
    }


def _stt_write(model: str | None = None, language: str | None = None, has_gpu: bool = True) -> None:
    section = dict(config_store.read_section("stt") or {})
    if model is not None:
        device, compute = stt_catalog.runtime_for(has_gpu)
        section.update(model=model, device=device, compute_type=compute)
    if language is not None:
        value = stt_catalog.language_value(language)
        if value is None:
            section.pop("language", None)
        else:
            section["language"] = value
    config_store.write_section("stt", section)


def _stt_download_then_switch(model: str, has_gpu: bool) -> None:
    try:
        _stt_download(model)
        _stt_write(model=model, has_gpu=has_gpu)
        with _STT_DL_LOCK:
            _STT_DL.update(model=model, state="ready")
            _STT_DL.pop("error", None)
        logger.info(f"stt: downloaded {model}; config switched (restart to load it)")
    except Exception as e:  # noqa: BLE001 — reported to the card; config untouched
        with _STT_DL_LOCK:
            _STT_DL.update(model=model, state="error", error=f"{type(e).__name__}: {e}"[:300])
        logger.warning(f"stt: download of {model} failed: {e}")


class SttPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _something(self):
        if self.model is None and self.language is None:
            raise ValueError("send model and/or language")
        return self


@router.get("/stt")
async def get_stt():
    gpu = await asyncio.to_thread(_stt_gpu_name)
    cached = await asyncio.gather(*(asyncio.to_thread(_stt_cached, m["name"]) for m in stt_catalog.MODELS))
    import stt_local
    with _STT_DL_LOCK:
        dl = dict(_STT_DL) or None
    return {
        "current": _stt_current(),
        "models": [{**m, "cached": c, "available": not (m["gpu_only"] and gpu is None)}
                   for m, c in zip(stt_catalog.MODELS, cached)],
        "languages": stt_catalog.LANGUAGES,
        "gpu": gpu,
        "status": stt_local.status(),
        "download": dl,
    }


@router.put("/stt", dependencies=GUARD)
async def put_stt(payload: SttPayload):
    has_gpu = (await asyncio.to_thread(_stt_gpu_name)) is not None
    err = stt_catalog.validate(payload.model, payload.language, has_gpu)
    if err:
        raise HTTPException(status_code=422, detail=err)
    if payload.model is not None:
        with _STT_DL_LOCK:
            if _STT_DL.get("state") == "downloading":
                raise HTTPException(status_code=409, detail=f"still downloading {_STT_DL.get('model')}")
    try:
        if payload.language is not None:
            _stt_write(language=payload.language)
        restart_needed, download = False, "not_needed"
        if payload.model is not None:
            cur = _stt_current()
            want = (payload.model, *stt_catalog.runtime_for(has_gpu))
            if want != (cur["model"], cur["device"], cur["compute"]):
                if await asyncio.to_thread(_stt_cached, payload.model):
                    _stt_write(model=payload.model, has_gpu=has_gpu)
                    restart_needed = True
                else:
                    with _STT_DL_LOCK:
                        _STT_DL.clear()
                        _STT_DL.update(model=payload.model, state="downloading")
                    __import__("threading").Thread(
                        target=_stt_download_then_switch, args=(payload.model, has_gpu),
                        name="stt-download", daemon=True).start()
                    download = "started"
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "restart_needed": restart_needed, "download": download, "current": _stt_current()}


@router.get("/voice-tuning")
async def get_voice_tuning():
    """Current wake/VAD/turn knob values for the Voice Tuning panel."""
    wake = config_store.read_section("wake") or {}
    vad = config_store.read_section("vad") or {}
    intelligence = config_store.read_section("intelligence") or {}
    return {
        "wake_threshold": wake.get("threshold"),
        "wake_awake_timeout": wake.get("awake_timeout"),
        "vad_confidence": vad.get("confidence"),
        "vad_min_volume": vad.get("min_volume"),
        "lull_secs": intelligence.get("lull_secs"),
        "dedup_window": intelligence.get("dedup_window"),
        "note": "applies on the next bot restart (read at pipeline construction)",
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
    d = Path(os.getenv("LVI_MODELS_DIR", "models"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _oww_active_path() -> Path:
    """The model file the loader would resolve RIGHT NOW (config > env >
    code default) as a full resolved path — path-exact, so two files
    with the same basename (root vs models/) are distinct."""
    wake = config_store.read_section("wake") or {}
    value = str(cfg_env("wake", "model", "LVI_WAKE_MODEL", wake.get("model") or wake_name.DEFAULT_MODEL))
    try:
        return Path(wake_name.resolve_model(value)).resolve()
    except Exception:                   # stock key but openwakeword broken: never 500 the panel
        return Path(value).resolve()


def _oww_current() -> str:
    return _oww_active_path().name


def _oww_validate(data: bytes) -> None:
    """Load-and-one-test-inference against the SAME library and shape the
    pipeline feeds (loaded-gun rule: a model that can't run
    never lands on disk, so a restart can never wake-brick the bot)."""
    import onnxruntime as ort
    import io

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
    for d in (_oww_dir(), Path(".")):
        try:
            files = sorted(d.glob("*.onnx")) if d.exists() else []
        except OSError:
            continue
        for f in files:
            out.append({"name": f.name, "size": f.stat().st_size,
                        "source": "models" if f.parent != Path(".") else "root",
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
    return {"models": out, "current": active.name}


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
    _oww_validate(data)
    dest = _oww_dir() / name
    tmp = _oww_dir() / (name + ".uploading")
    with open(tmp, "wb") as f:
        f.write(data)
    tmp.replace(dest)  # validated: swap in atomically
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
        path = (_oww_dir() if payload.source == "models" else Path(".")) / name
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"model '{name}' not found in {payload.source}")
    else:
        path = _oww_dir() / name
        if not path.is_file():
            # legacy root model? (pre-models-dir uploads / hand-copied files)
            if (Path(".") / name).is_file():
                path = Path(".") / name
            else:
                raise HTTPException(status_code=404, detail=f"model '{name}' not found")
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
    heard_as: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _check_terms(self):
        if self.name and self.name.strip() and not wake_name.valid_term(self.name.strip()):
            raise ValueError("name: a letter, then letters, spaces, ' or - (max 30)")
        if self.heard_as is not None:
            if not all(wake_name.valid_term(a) for a in self.heard_as):
                raise ValueError("heard_as: each a letter, then letters, spaces, ' or - (max 30)")
            bad = [a for a in self.heard_as if wake_name.reserved_term(a)]
            if bad:
                raise ValueError(f"heard_as: '{bad[0]}' is a filler word — it would swallow real commands")
        return self


@router.get("/wake")
async def get_wake():
    """The wake name card: the name (wake.name, "" if unset), the heard-as
    aliases, and what speech-to-text heard right after recent wakes."""
    return {"name": wake_name.name(), "heard_as": wake_name.aliases(),
            "recent": registry.recent_heard(),
            "model": wake_name.model_value()}


@router.put("/wake", dependencies=GUARD)
async def put_wake(payload: WakePayload):
    """Save the name / aliases. Applies LIVE — the drop-filter and log name
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
    if payload.heard_as is not None:
        wake["heard_as"] = wake_name.normalise_aliases(payload.heard_as)
        touched["wake"] = wake
    if touched:
        try:
            config_store.write_sections(touched)
        except FileNotFoundError as e:
            raise HTTPException(status_code=409, detail=str(e))
        logger.info(f"dashboard: wake {'/'.join(sorted(touched))} saved — live, no restart")
    return {"ok": True, "name": wake_name.name(), "heard_as": wake_name.aliases()}


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
        candidates.append(Path(".") / name)
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
    dest_dir = Path(f"firmware/{track}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{track}_esp32.bin"  # fixed served name (manifest points here)
    size = 0
    # Stage to a temp file FIRST: a rejected upload must never clobber
    # the currently-served good image (loaded-gun rule).
    tmp = dest.with_suffix(".bin.uploading")
    with open(tmp, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            f.write(chunk)
    if size < 1024 * 200:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="file too small to be a firmware image")
    # Version from the embedded descriptor: the .bin carries a 32-byte
    # SPI image header, then esp_app_desc_t — magic 0xABCD5432 (LE) at
    # byte 32, version[32] at byte 48 (measured against the real image
    # 2026-09-29; an offset of 24 was wrong and rejected a good bin).
    with open(tmp, "rb") as f:
        head = f.read(256)
    if head[32:36] != b"\x32\x54\xcd\xab":
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="not an ESP32 app image (no app-descriptor magic) — is this a lenoxvi build?",
        )
    version = head[48:80].split(b"\x00")[0].decode(errors="replace").strip()
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"no valid version in image header (got '{version or 'nothing'}') — is this a lenoxvi app image?",
        )
    # Board identity: the
    # image proves its line — compiled-in OTA URL format, plus the project
    # name on 1.2.9+ images (core/firmware_image.py). Refuse a mismatch
    # BEFORE the swap — the served image and manifest stay untouched.
    img_track = firmware_image.image_track(tmp.read_bytes())
    if img_track is None:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="can't tell which board this image is for (no single OTA-track "
                   "marker) — not uploading it to any track",
        )
    if img_track != track:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"this is {firmware_image.TRACK_LABEL[img_track]} firmware — it can't "
                   f"go on the {firmware_image.TRACK_LABEL[track]} track (it would "
                   f"cross-flash that node). Pick track '{img_track}'.",
        )
    tmp.replace(dest)  # validated: swap in atomically
    manifest = {"version": version, "filename": dest.name}
    mpath = Path(f"firmware/{track}/version.json")
    mtmp = mpath.with_suffix(".json.tmp")
    mtmp.write_text(json.dumps(manifest, indent=2))
    mtmp.replace(mpath)
    # The manifest now names only dest: any other .bin in the track dir
    # (e.g. a pre-rename node_esp32.bin) is unreachable — remove it so the
    # directory holds exactly what's served.
    for stale in dest_dir.glob("*.bin"):
        if stale.name != dest.name:
            stale.unlink(missing_ok=True)
    logger.info(f"dashboard: firmware {version} served on track '{track}' "
                f"({size} bytes) — nodes on this track pick it up on next check")
    return {"ok": True, "filename": dest.name, "size": size, "version": version,
            "track": track}


# The legacy POST /firmware/version route is
# REMOVED. It wrote the array manifest with any version string and no
# binary — a hand-crafted manifest could point the track at a version
# that was never validated (or never uploaded). The upload route above is
# the only manifest writer now: version AND track come from the image
# itself, guarded by the board-identity check.