"""routes — the dashboard's JSON API (the stable contract under the skin).

All endpoints live under /api/dashboard/ so they can never collide with
pipecat's own /api/* routes. The HTML page (page.html) is a thin skin
over these; a restyle or a host-side integration consumes the same
endpoints.

No auth by ratified design (design note): LAN-only page. Anyone on
the LAN can read status and edit mappings — accepted risk.
"""

import asyncio
import json
import re
import time
import urllib.parse
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from config import cfg

from . import config_store, node_command, registry
from .guard import mutation_guard

router = APIRouter(prefix="/api/dashboard")
# H5: every mutating route gets the Origin/Host guard (+optional
# LVI_DASHBOARD_TOKEN check) — see guard.py.
GUARD = [Depends(mutation_guard)]


# ── models ───────────────────────────────────────────────────────────


class MappingPayload(BaseModel):
    room_zones: dict[str, str] = Field(default_factory=dict)


class SessionModePayload(BaseModel):
    mode: str = Field(pattern="^(fresh|sticky)$")


class ZonePayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=7, max_length=64)
    player_uid: str = Field(min_length=8, max_length=64)
    backend: str = Field(default="sonos", pattern="^(sonos|wiim)$")


# ── status ───────────────────────────────────────────────────────────


@router.get("/status")
async def status():
    boot = registry.boot_time()
    return {
        "uptime_secs": round(time.time() - boot, 1) if boot else None,
        "version": registry.version(),
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
    Display-only (design note amendment 2026-09-30): the Sonos-side
    name shows verbatim for mapping UX; routing stays keyed on the
    config key + UID so a Sonos-side rename can't orphan a mapping.
    SYNC soco network calls — caller MUST run this in a thread (M5,
    Opus review: this is the voice pipeline's event loop)."""
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
async def get_mapping():
    zones = _all_zones()
    return {
        "room_zones": dict(config_store.read_section("room_zones") or {}),
        "zones": zones,
        "default_zone": cfg("sonos", "default_zone"),
        # Per-zone clip volumes (slider lane): config copy + live layer,
        # merged across both output backends.
        "zone_volumes": {**(cfg("sonos", "zone_volumes") or {}),
                         **(cfg("wiim", "zone_volumes") or {})},
        "clip_volume": cfg("sonos", "clip_volume"),
        # M5: soco network calls go to a thread — the dashboard rides the
        # voice pipeline's event loop; a slow/offline player must not
        # stall audio ingest (Opus review).
        "sonos_names": await asyncio.to_thread(_sonos_names, zones),
    }


@router.put("/mapping", dependencies=GUARD)
async def put_mapping(payload: MappingPayload):
    zones = _all_zones()
    # Validate BEFORE writing: every mapped zone must exist (either backend).
    bad = {r: z for r, z in payload.room_zones.items() if z not in zones}
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
    return {"ok": True, "room_zones": clean, "changed": diff["before"] != diff["after"]}


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
        # M5: soco UPnP discovery blocks 5s+ — off the event loop.
        for z in await asyncio.to_thread(soco.discover, timeout=5) or []:
            try:
                # soco API (0.31.4): SoCo has NO .model_name attribute —
                # model lives in get_speaker_info()["model_name"] (a
                # per-device UPnP call, so it rides the SAME thread as
                # discovery; M5). player_name IS a direct attribute
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
                        # The proven audioClip path is S2 Port/Amp only
                        # (design note); everything else is at the
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
    if payload.volume == int(section.get("clip_volume", 35)):
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
    # Collision check across BOTH backends: one merged namespace (the
    # mapping routes and the room_zones config key see one zone list).
    if name in _all_zones():
        raise HTTPException(status_code=400, detail=f"zone '{name}' already exists")
    if payload.backend == "wiim":
        # WiiM zones need no player_uid (HTTP API is UID-less) — but the
        # form still sends one; accept and ignore it. Adding a zone to a
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
        config_store.write_section("wiim", {**_wiim_cfg(), "zones": zones})
    else:
        if name not in _zones():
            raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
        zones = _zones()
        del zones[name]
        config_store.write_section("sonos", {**dict(cfg("sonos") or {}), "zones": zones})
    # rooms mapped to a deleted zone become dangling — drop them too
    mapping = dict(config_store.read_section("room_zones") or {})
    dangling = [r for r, z in mapping.items() if z == name]
    if dangling:
        for r in dangling:
            del mapping[r]
        config_store.write_section("room_zones", mapping)
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
    """Reuse the pipeline's shared clip server; start one only if the
    pipeline hasn't (sonos disabled). Same port, same config — never a
    second bind."""
    from sonos_output import ClipServer, SonosOutput

    shared = SonosOutput._shared_clip_server
    if shared is not None and shared._runner is not None:
        return shared
    cs = cfg("clip_server") or {}
    host_ip = cs.get("host_ip")
    if not host_ip:
        raise HTTPException(
            status_code=400,
            detail="clip_server.host_ip not set in config.yaml",
        )
    server = ClipServer(Path("clips"), host_ip, int(cs.get("port", 8765)))
    await server.start()
    return server


@router.post("/zones/{name}/test", dependencies=GUARD)
async def test_zone(name: str):
    zones = _all_zones()
    if name not in zones:
        raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
    backend = _backend_of(name)
    import lameenc

    server = await _clip_server()
    pcm = _test_tone_pcm()
    # M4: lameenc encode blocks — off the event loop (Opus review).
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


def _zone_test_volume(name: str, backend: str) -> int:
    """Slider-aware test-tone volume (live override > config per-zone >
    global), resolved through the right backend's live layer."""
    if backend == "wiim":
        from wiim_output import live_wiim_volume
        vol = live_wiim_volume(name)
        if vol is None:
            zv = (cfg("wiim", "zone_volumes") or {}).get(name)
            vol = int(zv) if zv is not None else int(cfg("wiim", "clip_volume") or 35)
    else:
        from sonos_output import live_zone_volume
        vol = live_zone_volume(name)
        if vol is None:
            zv = (cfg("sonos", "zone_volumes") or {}).get(name)
            vol = int(zv) if zv is not None else int(cfg("sonos", "clip_volume") or 35)
    return int(vol)


# ── node commands (firmware 1.1.0: restart / scan / set_wifi / set_name) ──


class WifiPayload(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=63)


class NamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=31, pattern=r"^[A-Za-z0-9_-]+$")


def _room_online(room: str) -> None:
    if not any(r["room"] == room and r["online"] for r in registry.known_rooms()):
        raise HTTPException(status_code=409, detail=f"node '{room}' is not online")


@router.post("/nodes/{room}/restart", dependencies=GUARD)
async def restart_node(room: str):
    _room_online(room)
    try:
        reply = await node_command.request(room, "restart", timeout=8.0)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    return reply


@router.post("/nodes/{room}/scan", dependencies=GUARD)
async def scan_node(room: str):
    _room_online(room)
    try:
        reply = await node_command.request(room, "scan", timeout=20.0)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"scan failed: {reply.get('data')}")
    return reply  # {"ok": true, "data": {"ssid": rssi, ...}}


@router.post("/nodes/{room}/wifi", dependencies=GUARD)
async def set_node_wifi(room: str, payload: WifiPayload):
    _room_online(room)
    # Fire-and-report: the node disconnects to apply creds; the final
    # verify/rollback result lands over the NEW session (firmware
    # reports on reconnect). The dashboard shows "applying…".
    try:
        reply = await node_command.request(room, "set_wifi",
                                           {"ssid": payload.ssid, "pass": payload.password},
                                           timeout=8.0)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    return reply


@router.post("/nodes/{room}/name", dependencies=GUARD)
async def set_node_name(room: str, payload: NamePayload):
    _room_online(room)
    try:
        reply = await node_command.request(room, "set_name", {"name": payload.name})
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"rename failed: {reply.get('data')}")
    return reply


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

# The ElevenLabs API key NEVER rides config.yaml (it ships in the public
# repo) and NEVER appears in any API response — .env only, on the appliance
# only. The dashboard writes it via the key route and shows set/unset.


class TTSPayload(BaseModel):
    voice_id: str = Field(min_length=8, max_length=64)
    model: str = Field(pattern="^eleven_(flash_v2_5|v2)$")


class TTSKeyPayload(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)


@router.get("/tts")
async def get_tts():
    """Current TTS config + API-key status. Never returns the key."""
    import os as _os
    tts_cfg = config_store.read_section("tts") or {}
    return {
        "voice_id": tts_cfg.get("voice_id") or _os.getenv("LVI_TTS_VOICE_ID"),
        "model": tts_cfg.get("model") or "eleven_flash_v2_5",
        "voice_source": "config" if tts_cfg.get("voice_id") else
                        ("env" if _os.getenv("LVI_TTS_VOICE_ID") else "unset"),
        "api_key_set": bool(_os.getenv("ELEVENLABS_API_KEY")),
        "note": "voice_id/model apply on next bot restart; api_key applies "
                "on next bot restart (read at pipeline construction)",
    }


@router.put("/tts", dependencies=GUARD)
async def put_tts(payload: TTSPayload):
    """Write voice_id + model to config.yaml (tts section). Applies on the
    next bot restart — same contract as every config edit."""
    current = dict(config_store.read_section("tts") or {})
    current["voice_id"] = payload.voice_id
    current["model"] = payload.model
    try:
        config_store.write_section("tts", current)
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("dashboard: tts config saved (voice_id + model) — applies on next restart")
    return {"ok": True, "voice_id": payload.voice_id, "model": payload.model}


@router.put("/tts/key", dependencies=GUARD)
async def put_tts_key(payload: TTSKeyPayload):
    """Write the ElevenLabs API key to .env (appliance-side, gitignored).
    Never echoes the key back; returns set/unset only. Applies on next bot
    restart — the bot reads .env at pipeline construction."""
    import os as _os
    from pathlib import Path as _Path
    env_path = _Path(_os.getenv("LVI_ENV_FILE",
                                _Path(__file__).resolve().parent.parent.parent / ".env"))
    lines = env_path.read_text().splitlines() if env_path.is_file() else []
    out, replaced = [], False
    for ln in lines:
        if ln.startswith("ELEVENLABS_API_KEY="):
            out.append("ELEVENLABS_API_KEY=" + payload.api_key)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append("ELEVENLABS_API_KEY=" + payload.api_key)
    env_path.write_text("\n".join(out) + "\n")
    logger.info("dashboard: ELEVENLABS_API_KEY written to .env (value never logged)")
    return {"ok": True, "api_key_set": True, "replaced": replaced}


# ── firmware push (operator side of firmware_server.py) ─────────────

@router.post("/firmware/upload", dependencies=GUARD)
async def upload_firmware(file: UploadFile = File(...)):
    """Upload a firmware .bin; version comes from the image's embedded
    ESP app descriptor (magic 0xABCD5432 at byte 24; version string at
    byte 48, NUL-padded — measured against the real image 2026-09-29,
    same string the node self-reports via get_info). Manifest written
    atomically in the same step: no typed-version mismatch is
    possible (manual version entry removed 2026-09-29)."""
    if not (file.filename or "").endswith(".bin"):
        raise HTTPException(status_code=400, detail="must be a .bin")
    dest_dir = Path("firmware/node")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "node_esp32.bin"  # fixed served name (manifest points here)
    size = 0
    # Stage to a temp file FIRST: a rejected upload must never clobber
    # the currently-served good image (loaded-gun rule, design note).
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
    tmp.replace(dest)  # validated: swap in atomically
    manifest = {"version": version, "filename": "node_esp32.bin"}
    mpath = Path("firmware/node/version.json")
    mtmp = mpath.with_suffix(".json.tmp")
    mtmp.write_text(json.dumps(manifest, indent=2))
    mtmp.replace(mpath)
    return {"ok": True, "filename": dest.name, "size": size, "version": version}


class FirmwareVersionPayload(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


@router.post("/firmware/version", dependencies=GUARD)
async def set_firmware_version(payload: FirmwareVersionPayload):
    """Write version.json (manifest) so nodes pick the uploaded bin up."""
    manifest = {"version": payload.version, "filename": "node_esp32.bin"}
    p = Path("firmware/node/version.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(__import__("json").dumps(manifest, indent=2))
    return {"ok": True, "manifest": manifest}