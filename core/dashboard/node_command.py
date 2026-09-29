"""node_command — bot→node dashboard commands over the WebRTC data channel.

Firmware 1.1.0 command schema (design note): {"cmd": name, ...args}
out, {"evt":"cmd_result", "cmd": name, "ok": bool, "data": ...} back.

Transport model (pipecat smallwebrtc, measured live): every node
connection gets its own pipeline + transport; pushing an
OutputTransportMessageFrame into that pipeline's task sends it on THAT
node's data channel — the exact path the LED ring messages already
use (wake_word._send_led), so it is proven on this stack.

Liveness contract (design note zombie fix): the node probes every 5 min
with {"evt":"cmd_result","cmd":"ping","data":"probe"}; the bot MUST
answer {"cmd":"ping"} or the node reboots its session. This handler
answers automatically — a quiet house never false-reboots a node.
"""

import asyncio
import json

from loguru import logger

from . import registry

# pc_id -> asyncio.Future, keyed (pc_id, cmd) — awaited by API routes.
_pending = {}
_pending_lock = asyncio.Lock()


def send_to_room(room: str, payload: dict) -> bool:
    """Queue a data-channel message to the node currently in `room`.
    Returns False if no live session. Schedules the async queue on the
    running loop — safe to call from sync or async context."""
    task, pc_id = registry.task_for_room(room)
    if task is None:
        logger.warning(f"node_command: no live session for room '{room}'")
        return False
    from pipecat.frames.frames import OutputTransportMessageFrame

    import asyncio

    fut = task.queue_frame(OutputTransportMessageFrame(message=payload))
    # PipelineWorker.queue_frame is async; fire it on the loop
    asyncio.get_event_loop().create_task(fut) if asyncio.iscoroutine(fut) else None
    logger.info(f"node_command: -> '{room}' ({pc_id[:24]}…): {payload}")
    return True


def send_pong(pc_id: str) -> None:
    """Answer the node's liveness probe (keeps a quiet house's session
    alive — the node reboots if probes go unanswered)."""
    task, _ = registry.task_for_pc(pc_id)
    if task is None:
        return
    from pipecat.frames.frames import OutputTransportMessageFrame

    fut = task.queue_frame(OutputTransportMessageFrame(message={"cmd": "pong"}))
    import asyncio

    asyncio.get_event_loop().create_task(fut) if asyncio.iscoroutine(fut) else None


async def request(room: str, cmd: str, args: dict = None, timeout: float = 12.0):
    """Send a command and await its cmd_result. Returns the reply dict
    or raises TimeoutError. One in-flight request per cmd per node
    (single-node rooms by design)."""
    payload = {"cmd": cmd, **(args or {})}
    task, pc_id = registry.task_for_room(room)
    if task is None:
        raise TimeoutError(f"no live session for room '{room}'")

    key = (pc_id, cmd)
    async with _pending_lock:
        if key in _pending and not _pending[key].done():
            raise TimeoutError(f"'{cmd}' already in flight for '{room}'")
        fut = asyncio.get_event_loop().create_future()
        _pending[key] = fut

    from pipecat.frames.frames import OutputTransportMessageFrame

    # PipelineWorker.queue_frame is async — await it (a sync call leaves
    # the coroutine unawaited and the frame NEVER leaves the bot; the
    # RuntimeWarning at 18:10:07 was exactly this).
    await task.queue_frame(OutputTransportMessageFrame(message=payload))
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        async with _pending_lock:
            _pending.pop(key, None)


def on_message(message, pc_id: str) -> None:
    """bot.py's on_client_message feeds every data-channel payload here."""
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

    if data.get("evt") != "cmd_result":
        return  # legacy RTVI messages (LED state) — not ours

    cmd = data.get("cmd")
    key = (pc_id, cmd)
    fut = _pending.get(key)
    logger.info(f"node_command: <- {pc_id[:24]}… cmd_result '{cmd}' ok={data.get('ok')} data={str(data.get('data'))[:80]}")

    # Room rename: the registry relabels when the node acks set_name.
    if cmd == "set_name" and data.get("ok"):
        registry.relabel_room(pc_id, data.get("data") if isinstance(data.get("data"), dict) else None)

    if fut and not fut.done():
        fut.set_result(data)