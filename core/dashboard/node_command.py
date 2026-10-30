"""node_command — bot→node dashboard commands over the WebRTC data channel.

Firmware 1.1.0 command schema: {"cmd": name, ...args}
out, {"evt":"cmd_result", "cmd": name, "ok": bool, "data": ...} back.

Transport model (pipecat smallwebrtc, measured live): every node
connection gets its own pipeline + transport; the OUTPUT TRANSPORT's
send_message delivers on THAT node's data channel directly — no
pipeline, no queues. The old queue_frame
path entered at the pipeline source and queued behind IntelligenceAgent:
a restart/scan during a 40s intelligence call 504'd. Same direct wire the LED
messages use (led_state.LedSender).

Liveness contract (zombie fix, ASYMMETRIC): the node probes every 5 min with
{"evt":"cmd_result","cmd":"probe","data":"probe"}; the bot MUST answer
{"cmd":"pong"} or the node reboots its session. The node consumes the
pong silently — NO reply-to-reply (symmetric ping/pong looped ~1/sec
for 12 min, measured 2026-09-29). This handler answers
automatically — a quiet house never false-reboots a node.
"""

import asyncio
import itertools
import json
import re
import time

from loguru import logger

from . import registry


class NodeCommandError(Exception):
    """Base for node-command transport failures (routes map them to HTTP)."""


class CommandInFlight(NodeCommandError):
    """A duplicate command is already awaiting this node's reply — a
    CONFLICT, not a timeout (this used to raise a bare
    TimeoutError, so a double-click read as a 'Gateway Timeout')."""


class NoLiveSession(NodeCommandError):
    """The registry knows the room but no live data channel exists —
    the half-open session shape (the node thinks it's connected, the
    bot can't reach it)."""


# pc_id -> asyncio.Future, keyed (pc_id, cmd) — awaited by API routes.
_pending = {}
_pending_lock = asyncio.Lock()
# (pc_id, cmd) -> monotonic deadline after a TIMED-OUT request. The protocol
# has no request id the node echoes back, so a late reply to the timed-out
# command would resolve a retry with the wrong answer (a stale mute state).
# A retry inside the window is refused (409); the late reply still applies
# its side effects and lifts the window.
_settling: dict = {}
SETTLE_SECS = 15.0
# Request ids (firmware 1.2.37+ echoes them in the reply): each request
# carries a unique id, and a reply answers the request only if its id
# matches. A node that has echoed an id once skips the settling window -
# a late reply just carries a stale id. Older firmware ignores the field.
_next_id = itertools.count(1)
_pending_ids: dict = {}   # (pc_id, cmd) -> id of the request awaiting a reply
_echoes_id: set = set()   # pc_ids whose node echoes request ids
ID_ECHO_FW = (1, 2, 37)   # first firmware that echoes them (fw_push says so up front)
_rejected_logged: set = set()  # pc_ids whose refused OTA version was already logged
# In-flight sends: the loop keeps only weak references to tasks, so an
# unreferenced one can be garbage-collected mid-send.
_inflight: set = set()


def _fw_tuple(fw):
    """'1.2.37' -> (1, 2, 37). Anything else (a suffix, a prefix, a
    number, None) -> None: an unreadable version proves nothing."""
    if not isinstance(fw, str):
        return None
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", fw.strip())
    return tuple(int(p) for p in m.groups()) if m else None


def _remember(seen: set, pc_id: str) -> None:
    if len(seen) > 1000:  # one entry per session; keep it bounded
        seen.clear()
    seen.add(pc_id)


def _send(out, payload: dict) -> bool:
    """Direct data-channel send via the output transport: no
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
        task = asyncio.get_running_loop().create_task(_go())
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
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
    rid = next(_next_id)
    payload = {"cmd": cmd, **(args or {}), "id": rid}
    out, pc_id = registry.sender_for_room(room)
    if out is None:
        raise NoLiveSession(f"no live session for room '{room}'")

    key = (pc_id, cmd)
    async with _pending_lock:
        if key in _pending and not _pending[key].done():
            raise CommandInFlight(f"'{cmd}' already in flight for '{room}'")
        if pc_id not in _echoes_id and _settling.get(key, 0.0) > time.monotonic():
            raise CommandInFlight(f"the last '{cmd}' to '{room}' timed out and may still "
                                  "land - try again in a few seconds")
        _settling.pop(key, None)
        fut = asyncio.get_event_loop().create_future()
        _pending[key] = fut
        _pending_ids[key] = rid

    if not _send(out, payload):
        async with _pending_lock:
            _pending.pop(key, None)
            _pending_ids.pop(key, None)
        raise TimeoutError(f"send failed for '{cmd}' to '{room}'")
    try:
        return await asyncio.wait_for(fut, timeout)
    except TimeoutError:
        if pc_id not in _echoes_id:  # no id to tell a late reply apart
            _settling[key] = time.monotonic() + SETTLE_SECS
        raise
    finally:
        async with _pending_lock:
            _pending.pop(key, None)
            _pending_ids.pop(key, None)


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
    # Symmetric ping/pong looped ~1/sec for 12 min (measured).
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
        # Firmware 1.2.37+ echoes request ids: trust it from the first
        # command on, not only after the first id-bearing reply.
        ver = _fw_tuple(fw)
        if ver and ver >= ID_ECHO_FW:
            _remember(_echoes_id, pc_id)
        # Firmware 1.2.41+: an OTA image this node rolled back from and now
        # refuses, and whether this boot went back to the previous Wi-Fi.
        # Stored on every push: a push without them clears last boot's.
        rejected = info.get("rejected")
        registry.note_boot_flags(pc_id, rejected, info.get("wifi_rollback"))
        if isinstance(rejected, str) and rejected and pc_id not in _rejected_logged:
            _remember(_rejected_logged, pc_id)
            room = registry.room_for_pc(pc_id) or pc_id[:24]
            logger.warning(f"node_command: {room} refuses firmware {rejected} (it failed its "
                           "health check and rolled back) - upload a fixed build")
        # firmware 1.2.19+: fw_push names the board line (registry.NODE_LINES).
        if isinstance(info, dict) and isinstance(info.get("line"), str):
            registry.note_line(pc_id, info["line"])
            # a speaker node boots with its speaker disabled — tell it
            # whether to play replies once its room is bound.
            import node_speaker

            if info["line"] in node_speaker.SPEAKER_LINES:
                try:
                    task = asyncio.get_running_loop().create_task(node_speaker.sync_pc(pc_id))
                    _inflight.add(task)
                    task.add_done_callback(_inflight.discard)
                except RuntimeError:
                    pass
        # Firmware 1.2.35+: this boot rolled back to the previous network
        # after a failed Wi-Fi change (the trial never reached the bot).
        if isinstance(info, dict) and info.get("wifi_rollback") is True:
            room = registry.room_for_pc(pc_id) or pc_id[:24]
            logger.warning(f"node_command: {room} rolled back to its previous network "
                           "(the Wi-Fi change never reached the bot)")
        # Firmware 1.2.11+: fw_push also carries the node's mute state.
        if isinstance(info, dict) and isinstance(info.get("muted"), bool):
            registry.note_muted(pc_id, info["muted"])
        return

    # mute_push (firmware 1.2.11+): an array node's PHYSICAL Mute button
    # was pressed — the node tells us unprompted so the dashboard follows.
    # Fire-and-forget like fw_push: nothing requested it, nothing replies.
    if data.get("evt") == "cmd_result" and data.get("cmd") == "mute_push":
        info = data.get("data") if isinstance(data.get("data"), dict) else {}
        if isinstance(info.get("muted"), bool):
            registry.note_muted(pc_id, info["muted"])
            logger.info(f"node_command: <- {pc_id[:24]}… mute_push: muted={info['muted']} (physical button)")
        return

    if data.get("evt") != "cmd_result":
        return  # legacy RTVI messages (LED state) — not ours

    cmd = data.get("cmd")
    key = (pc_id, cmd)
    fut = _pending.get(key)
    # A reply to a request that already timed out: its side effects below
    # still apply (they're the node's truth), but it answers no request.
    rid = data.get("id")
    if isinstance(rid, int) and not isinstance(rid, bool):
        _remember(_echoes_id, pc_id)
        late = rid != _pending_ids.get(key)
        _settling.pop(key, None)
    else:  # firmware without id echo: the settling window decides
        late = _settling.pop(key, None) is not None
    logger.info(f"node_command: <- {pc_id[:24]}… cmd_result '{cmd}' id={data.get('id')} "
                f"ok={data.get('ok')} data={str(data.get('data'))[:80]}")

    # Room rename: the registry relabels, and the room_zones mapping key
    # must follow — otherwise the rename silently orphans the node's
    # zone mapping and the mapping table keeps a ghost row for the old
    # name.
    if cmd == "set_name" and data.get("ok"):
        info = data.get("data") if isinstance(data.get("data"), dict) else None
        old = registry.room_for_pc(pc_id)
        new = info.get("name") if info else None
        # Another live session already answers to the new name (a rename
        # that raced past the dashboard's check, or done some other way):
        # carry nothing and relabel nothing — the node's next offer settles it.
        clash = bool(old and new and old != new and registry.room_claimed(new, pc_id))
        if clash:
            logger.warning(f"node_command: rename '{old}' -> '{new}': another live session "
                           f"holds '{new}'; config and registry left as-is")
        relabeled = False
        if old and new and old != new and not clash:
            from . import config_store

            mapping = dict(config_store.read_section("room_zones") or {})
            # the built-in speaker's volume is keyed by room too.
            spk = dict(config_store.read_section("node_speaker") or {})
            vols = dict(spk.get("volumes") or {})
            writes = {}

            def _carry(table: dict, what: str) -> bool:
                # Never overwrite another room's entry: the dashboard refuses a
                # rename onto an existing name, but a node renamed some other
                # way can still collide. The old entry then stays where it is.
                if old not in table:
                    return False
                if new in table:
                    logger.warning(f"node_command: rename '{old}' -> '{new}': '{new}' already "
                                   f"has a {what}; kept it, '{old}' left as-is")
                    return False
                table[new] = table.pop(old)
                logger.info(f"node_command: rename carried {what} '{old}' -> '{new}'")
                return True

            if _carry(mapping, "zone mapping"):
                writes["room_zones"] = mapping
            # per-room agent overrides (every intelligence system) follow too.
            ragents = dict(config_store.read_section("room_agents") or {})
            moved = False
            for kind, rooms in list(ragents.items()):
                if isinstance(rooms, dict):
                    rooms = dict(rooms)
                    if _carry(rooms, f"{kind} agent override"):
                        ragents[kind] = rooms
                        moved = True
            if moved:
                writes["room_agents"] = ragents
            if _carry(vols, "speaker volume"):
                writes["node_speaker"] = {**spk, "volumes": vols}
            # Sticky/fresh conversation mode (bot.py reads session_modes[room]).
            modes = dict(config_store.read_section("session_modes") or {})
            if _carry(modes, "session mode"):
                writes["session_modes"] = modes
            if writes:
                config_store.write_sections(writes)  # one atomic pass
            # A sticky room's saved conversation follows its name too. The
            # registry relabels under the stash's lock: the live session
            # resolves its name there at every save, so a save in flight
            # lands wholly under the old name (and is moved) or the new one.
            try:
                from intelligence import agent as intelligence_agent

                intelligence_agent.rename_session_history(
                    old, new, then=lambda: registry.relabel_room(pc_id, info))
                relabeled = True
            except Exception as e:  # noqa: BLE001 — history is best-effort; the rename stands
                logger.warning(f"node_command: session history not carried: {e}")
        if not clash and not relabeled:
            registry.relabel_room(pc_id, info)  # a no-op if it already ran
        if old and new and old != new and not clash:
            # The new name may carry a different mapping: re-tell the node
            # whether it plays (never both outputs, never neither).
            import node_speaker

            node_speaker.sync(new)

    # the speaker's per-boot health rides every set_speaker reply.
    if (cmd == "set_speaker" and isinstance(data.get("data"), dict)
            and registry.note_speaker_ok(pc_id, data["data"].get("speaker_ok"))):
            # First proof this session: the probe went out disabled — now
            # send the real state (its mapping may be the built-in speaker).
            import node_speaker

            node_speaker.sync(registry.room_for_pc(pc_id))

    if fut and not fut.done() and not late:
        fut.set_result(data)