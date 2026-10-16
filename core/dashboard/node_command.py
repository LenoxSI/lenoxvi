"""node_command — bot→node dashboard commands over the WebRTC data channel.

Firmware 1.1.0 command schema (design note): {"cmd": name, ...args}
out, {"evt":"cmd_result", "cmd": name, "ok": bool, "data": ...} back.

Transport model (pipecat smallwebrtc, measured live): every node
connection gets its own pipeline + transport; the OUTPUT TRANSPORT's
send_message delivers on THAT node's data channel directly — no
pipeline, no queues (M6, Opus review 2026-10-01). The old queue_frame
path entered at the pipeline source and queued behind HermesAgent:
a restart/scan during a 40s intelligence call 504'd. Same direct wire the LED
messages use (led_state.LedSender).

Liveness contract (design note zombie fix, ASYMMETRIC — docstring
corrected 2026-10-01, was M7): the node probes every 5 min with
{"evt":"cmd_result","cmd":"probe","data":"probe"}; the bot MUST answer
{"cmd":"pong"} or the node reboots its session. The node consumes the
pong silently — NO reply-to-reply (symmetric ping/pong looped ~1/sec
for 12 min, measured 2026-09-29 19:35–19:47). This handler answers
automatically — a quiet house never false-reboots a node.
"""

import asyncio
import json

from loguru import logger

from . import registry


class NodeCommandError(Exception):
    """Base for node-command transport failures (routes map them to HTTP)."""


class CommandInFlight(NodeCommandError):
    """A duplicate command is already awaiting this node's reply — a
    CONFLICT, not a timeout (041 L9: this used to raise a bare
    TimeoutError, so a double-click read as a 'Gateway Timeout')."""


class NoLiveSession(NodeCommandError):
    """The registry knows the room but no live data channel exists —
    the half-open session shape (the node thinks it's connected, the
    bot can't reach it)."""


# pc_id -> asyncio.Future, keyed (pc_id, cmd) — awaited by API routes.
_pending = {}
_pending_lock = asyncio.Lock()


def _send(out, payload: dict) -> bool:
    """Direct data-channel send via the output transport (M6): no
    pipeline, no queue_frame — a blocked intelligence can't stall commands.
    Schedules the async send on the running loop; never raises."""
    import asyncio

    from pipecat.frames.frames import OutputTransportMessageFrame

    async def _go():
        try:
            await out.send_message(
                OutputTransportMessageFrame(message=payload)
            )
        except Exception as e:  # command delivery is best-effort
            logger.debug(f"node_command: send failed: {e}")

    try:
        asyncio.get_running_loop().create_task(_go())
        return True
    except RuntimeError:
        return False


def send_to_room(room: str, payload: dict) -> bool:
    """Send a data-channel message to the node currently in `room`.
    Returns False if no live session."""
    out, pc_id = registry.sender_for_room(room)
    if out is None:
        logger.warning(f"node_command: no live session for room '{room}'")
        return False
    ok = _send(out, payload)
    if ok:
        logger.info(f"node_command: -> '{room}' ({pc_id[:24]}…): {payload}")
    return ok


def send_pong(pc_id: str) -> None:
    """Answer the node's liveness probe (keeps a quiet house's session
    alive — the node reboots if probes go unanswered)."""
    out, _ = registry.sender_for_pc(pc_id)
    if out is None:
        return
    _send(out, {"cmd": "pong"})


async def request(room: str, cmd: str, args: dict = None, timeout: float = 12.0):
    """Send a command and await its cmd_result. Returns the reply dict
    or raises TimeoutError. One in-flight request per cmd per node
    (single-node rooms by design)."""
    payload = {"cmd": cmd, **(args or {})}
    out, pc_id = registry.sender_for_room(room)
    if out is None:
        raise NoLiveSession(f"no live session for room '{room}'")

    key = (pc_id, cmd)
    async with _pending_lock:
        if key in _pending and not _pending[key].done():
            raise CommandInFlight(f"'{cmd}' already in flight for '{room}'")
        fut = asyncio.get_event_loop().create_future()
        _pending[key] = fut

    if not _send(out, payload):
        async with _pending_lock:
            _pending.pop(key, None)
        raise TimeoutError(f"send failed for '{cmd}' to '{room}'")
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        async with _pending_lock:
            _pending.pop(key, None)


def on_message(message, pc_id: str) -> None:
    """bot.py's on_app_message feeds every data-channel payload here."""
    try:
        data = json.loads(message) if isinstance(message, str) else message
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return

    # Liveness probe: answer immediately, unconditionally. The protocol
    # is ASYMMETRIC (node sends evt cmd_result "probe" every 5 min; bot
    # answers {"cmd":"pong"}; node consumes pong silently — no reply).
    # Symmetric ping/pong looped ~1/sec for 12 min (measured 19:35–19:47).
    if data.get("evt") == "cmd_result" and data.get("cmd") == "probe":
        send_pong(pc_id)
        return

    # fw_push (firmware 1.1.2 connect-push): the node announced its
    # RUNNING version on data-channel open — store it for the Nodes
    # table. No future to resolve (nothing requested it); no reply
    # (the push is fire-and-forget by design).
    if data.get("evt") == "cmd_result" and data.get("cmd") == "fw_push":
        info = data.get("data") if isinstance(data.get("data"), dict) else {}
        fw = info.get("fw") if isinstance(info, dict) else None
        if fw:
            registry.note_firmware(pc_id, str(fw))
            logger.info(f"node_command: <- {pc_id[:24]}… fw_push: running {fw}")
        return

    if data.get("evt") != "cmd_result":
        return  # legacy RTVI messages (LED state) — not ours

    cmd = data.get("cmd")
    key = (pc_id, cmd)
    fut = _pending.get(key)
    logger.info(f"node_command: <- {pc_id[:24]}… cmd_result '{cmd}' ok={data.get('ok')} data={str(data.get('data'))[:80]}")

    # Room rename: the registry relabels, and the room_zones mapping key
    # must follow — otherwise the rename silently orphans the node's
    # zone mapping (the node's zone was lost when it
    # renamed to bedroom-mic1, measured 2026-09-29 21:49) and the
    # mapping table keeps a ghost row for the old name.
    if cmd == "set_name" and data.get("ok"):
        info = data.get("data") if isinstance(data.get("data"), dict) else None
        old = registry.room_for_pc(pc_id)
        new = info.get("name") if info else None
        if old and new and old != new:
            from . import config_store

            mapping = dict(config_store.read_section("room_zones") or {})
            if old in mapping:
                mapping[new] = mapping.pop(old)
                config_store.write_section("room_zones", mapping)
                logger.info(
                    f"node_command: rename carried mapping '{old}' -> '{new}' "
                    f"(zone '{mapping[new]}')"
                )
        registry.relabel_room(pc_id, info)

    if fut and not fut.done():
        fut.set_result(data)