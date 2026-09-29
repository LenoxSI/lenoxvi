"""routes — the dashboard's JSON API (the stable contract under the skin).

All endpoints live under /api/dashboard/ so they can never collide with
pipecat's own /api/* routes. The HTML page (page.html) is a thin skin
over these; a restyle or a host-side integration consumes the same
endpoints.

No auth by ratified design (design note): LAN-only page. Anyone on
the LAN can read status and edit mappings — accepted risk.
"""

import json
import re
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from config import cfg

from . import config_store, node_command, registry

router = APIRouter(prefix="/api/dashboard")


# ── models ───────────────────────────────────────────────────────────


class MappingPayload(BaseModel):
    room_zones: dict[str, str] = Field(default_factory=dict)


class ZonePayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=7, max_length=64)
    player_uid: str = Field(min_length=8, max_length=64)


# ── status ───────────────────────────────────────────────────────────


@router.get("/status")
async def status():
    boot = registry.boot_time()
    return {
        "uptime_secs": round(time.time() - boot, 1) if boot else None,
        "active_room": registry.active_room(),
        "last_wake": registry.last_wake(),
        "rooms": registry.known_rooms(),
        "note": "config edits apply on the next bot restart",
    }


# ── mapping ───────────────────────────────────────────────────────────


def _zones() -> dict:
    return dict(cfg("sonos", "zones") or {})


@router.get("/mapping")
async def get_mapping():
    return {
        "room_zones": dict(config_store.read_section("room_zones") or {}),
        "zones": _zones(),
        "default_zone": cfg("sonos", "default_zone"),
    }


@router.put("/mapping")
async def put_mapping(payload: MappingPayload):
    zones = _zones()
    # Validate BEFORE writing: every mapped zone must exist.
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


@router.post("/zones/discover")
async def discover_zones():
    """Scan the LAN for Sonos players (soco UPnP discovery)."""
    try:
        import soco
    except ImportError:
        raise HTTPException(status_code=503, detail="soco not installed")
    found = []
    try:
        for z in soco.discover(timeout=5) or []:
            try:
                model = (z.model_name or "").strip()
                found.append(
                    {
                        "ip": z.ip_address,
                        "name": z.player_name,
                        "uid": z.uid,
                        "model": model,
                        # The proven audioClip path is S2 Port/Amp only
                        # (design note); everything else is at the
                        # user's own risk and labeled as such.
                        "supported": model in ("Port", "Sonos Amp", "Amp"),
                    }
                )
            except Exception:  # one flaky device must not kill the scan
                logger.debug("zones/discover: skipped one unresponsive player")
                continue
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"discovery failed: {e}")
    found.sort(key=lambda d: d["name"].lower())
    return {"found": found}


@router.post("/zones")
async def add_zone(payload: ZonePayload):
    name = payload.name.strip()
    if not name or "." in name or ":" in name:
        raise HTTPException(status_code=400, detail="bad zone name")
    zones = dict(_zones())
    zones[name] = {"ip": payload.ip.strip(), "player_uid": payload.player_uid.strip()}
    try:
        config_store.write_section("sonos", {**dict(cfg("sonos") or {}), "zones": zones})
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "zones": zones}


@router.delete("/zones/{name}")
async def delete_zone(name: str):
    zones = dict(_zones())
    if name not in zones:
        raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
    del zones[name]
    # rooms mapped to a deleted zone become dangling — drop them too
    mapping = dict(config_store.read_section("room_zones") or {})
    dangling = [r for r, z in mapping.items() if z == name]
    if dangling:
        for r in dangling:
            del mapping[r]
        config_store.write_section("room_zones", mapping)
    config_store.write_section("sonos", {**dict(cfg("sonos") or {}), "zones": zones})
    return {"ok": True, "dropped_room_mappings": dangling}


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


@router.post("/zones/{name}/test")
async def test_zone(name: str):
    zones = _zones()
    if name not in zones:
        raise HTTPException(status_code=404, detail=f"unknown zone '{name}'")
    import lameenc
    from sonos_websocket import SonosWebsocket

    server = await _clip_server()
    pcm = _test_tone_pcm()
    enc = lameenc.Encoder()
    enc.set_bit_rate(96)
    enc.set_in_sample_rate(24000)
    enc.set_channels(1)
    enc.set_quality(2)
    mp3 = bytes(enc.encode(pcm)) + bytes(enc.flush())

    filename = f"test-{name}-{int(time.time())}.mp3"
    path = Path("clips") / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mp3)
    url = server.url_for(filename)

    duration = len(pcm) / (2 * 24000)
    gate = registry.gate()
    if gate is not None:
        gate.block(duration + 1.5)  # same margin sonos_output uses

    zone = zones[name]
    sw = SonosWebsocket(zone["ip"], player_id=zone["player_uid"])
    try:
        await sw.connect()
        await sw.play_clip(url, volume=int(cfg("sonos", "clip_volume") or 70))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sonos refused the clip: {e}")
    finally:
        await sw.close()
    logger.info(f"dashboard: test clip on '{name}' ({url})")
    return {"ok": True, "zone": name, "url": url}


# ── node commands (firmware 1.1.0: restart / scan / set_wifi / set_name) ──


class WifiPayload(BaseModel):
    room: str = Field(min_length=1, max_length=32)
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=63)


class NamePayload(BaseModel):
    room: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=31, pattern=r"^[A-Za-z0-9_-]+$")


def _room_online(room: str) -> None:
    if not any(r["room"] == room and r["online"] for r in registry.known_rooms()):
        raise HTTPException(status_code=409, detail=f"node '{room}' is not online")


@router.post("/nodes/{room}/restart")
async def restart_node(room: str):
    _room_online(room)
    try:
        reply = await node_command.request(room, "restart", timeout=8.0)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    return reply


@router.post("/nodes/{room}/scan")
async def scan_node(room: str):
    _room_online(room)
    try:
        reply = await node_command.request(room, "scan", timeout=20.0)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"scan failed: {reply.get('data')}")
    return reply  # {"ok": true, "data": {"ssid": rssi, ...}}


@router.post("/nodes/{room}/wifi")
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


@router.post("/nodes/{room}/name")
async def set_node_name(room: str, payload: NamePayload):
    _room_online(room)
    try:
        reply = await node_command.request(room, "set_name", {"name": payload.name})
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    if not reply.get("ok"):
        raise HTTPException(status_code=502, detail=f"rename failed: {reply.get('data')}")
    return reply


# ── firmware push (operator side of firmware_server.py) ─────────────


@router.post("/firmware/upload")
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


@router.post("/firmware/version")
async def set_firmware_version(payload: FirmwareVersionPayload):
    """Write version.json (manifest) so nodes pick the uploaded bin up."""
    manifest = {"version": payload.version, "filename": "node_esp32.bin"}
    p = Path("firmware/node/version.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(__import__("json").dumps(manifest, indent=2))
    return {"ok": True, "manifest": manifest}