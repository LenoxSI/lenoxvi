"""A node's built-in speaker as a node-private reply zone.

Rules:
- `builtin` is a reserved zone value in `room_zones`. Only a node whose
  board line has a speaker (SPEAKER_LINES) may use it — enforced by the
  mapping route, not just hidden in the UI.
- An UNMAPPED speaker node replies on its own speaker. That default is
  resolved at runtime here — never written into config.
- Exactly one output ever sounds a reply. Reply audio streams to EVERY
  node over its WebRTC track (it drives the node-side turn lock), so the
  node is told explicitly whether to play it: `set_speaker {enabled,
  volume}` after its fw_push, after any mapping PUT, after a volume PUT.
  Sonos/WiiM skip a `builtin` reply (no default-zone fallback).
- Volume lives in bot config (`node_speaker.volumes`, default 40); the
  node stores nothing and boots disabled.
"""
from __future__ import annotations

import asyncio

from loguru import logger

BUILTIN = "builtin"
SPEAKER_LINES = ("waveshare",)
DEFAULT_VOLUME = 40


def resolve_zone(room, room_zones, has_speaker: bool):
    """The reply zone for `room`: its mapping, else `builtin` for a speaker
    node, else None (the output backend's default zone)."""
    if not room or room == "unknown":
        return None
    mapped = (room_zones or {}).get(room)
    if mapped:
        return mapped
    return BUILTIN if has_speaker else None


def is_reserved_zone_name(name) -> bool:
    return str(name or "").strip().lower() == BUILTIN


def volume_for(room, volumes) -> int:
    v = (volumes or {}).get(room, DEFAULT_VOLUME)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return DEFAULT_VOLUME
    return max(0, min(100, int(v)))


# ── live hooks (patchable in tests, like wake_name._wake) ────────────
def _room_zones():
    from config import cfg
    return cfg("room_zones") or {}


def _volumes():
    from config import cfg
    return cfg("node_speaker", "volumes") or {}


def _has_speaker(room) -> bool:
    from dashboard import registry
    return registry.has_speaker(room)


def zone_for(room):
    """Runtime resolution against live config + the registry."""
    return resolve_zone(room, _room_zones(), _has_speaker(room))


def payload(room) -> dict:
    return {"cmd": "set_speaker", "enabled": zone_for(room) == BUILTIN,
            "volume": volume_for(room, _volumes())}


def sync(room) -> bool:
    """Tell `room`'s node whether to play replies (and at what volume).
    No-op (False) for nodes without a speaker. Best-effort: an offline
    node gets synced again after its next fw_push."""
    if not _has_speaker(room):
        return False
    from dashboard import node_command
    return bool(node_command.send_to_room(room, payload(room)))


def sync_all(rooms) -> None:
    for room in dict.fromkeys(rooms or ()):
        sync(room)


async def sync_pc(pc_id: str, wait_s: float = 10.0, poll_s: float = 0.25) -> bool:
    """After a speaker node's fw_push: its room may still be provisional
    (fw_push can beat the offer tee). Wait for the room to bind, then sync
    THAT session directly. Gives up quietly — the next connect retries."""
    from dashboard import node_command, registry
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        room = registry.room_for_pc(pc_id)
        if room and room not in (registry.PROVISIONAL, "unknown"):
            break
        if loop.time() >= deadline:
            logger.info(f"node_speaker: {pc_id[:24]}… never bound to a room; not synced")
            return False
        await asyncio.sleep(poll_s)
    out, _ = registry.sender_for_pc(pc_id)
    if out is None:
        return False
    p = payload(room)
    ok = bool(node_command._send(out, p))
    if ok:
        logger.info(f"node_speaker: -> '{room}': {p}")
    return ok
