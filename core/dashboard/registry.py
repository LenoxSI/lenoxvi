"""registry — runtime node/room state for the dashboard (process memory).

Identity model (measured 2026-09-29 against the live stack):
- The mic node's firmware bakes a `room` string into every /api/offer
  JSON body (its zone-routing key, set at firmware build time). It sends
  NO per-device id and NO pc_id — pipecat generates pc_id SERVER-SIDE
  per connection (uuid), so pc_id changes on every reconnect and is
  useless as a stable key on its own.
- Resolution: the offer tee (offer_tee.py) captures {room, pc_id} from
  the offer/response exchange. ORDERING RACE (real, measured against the
  request handler's flow): on_client_connected can fire during
  initialize(), i.e. BEFORE the response body reaches the tee — so
  mark_connected may run before the (pc_id, room) pair exists. Handled
  with a provisional room + reconcile in note_offer, keyed by pc_id —
  exact under concurrent offers, no FIFO guessing.
- Browser clients POST /api/offer with no `room` field; they surface
  as room "browser".

Persistence split:
- In-memory here: rooms seen since boot, online status, last wake.
- config.yaml `room_zones` (via config_store): the durable
  room -> Sonos zone mapping a human edits on the dashboard.
"""

import time
from pathlib import Path

from loguru import logger

_version = "unknown"

_boot_time = None
_active_room = None
_room_by_pc = {}  # pc_id -> room ("unknown" = provisional, awaiting offer tee)
_known_rooms = {}  # room -> {"online": bool, "first_seen": ts, "last_seen": ts}
_out_by_pc = {}  # pc_id -> output transport (DIRECT data-channel wire)
_fw_by_pc = {}  # pc_id -> running fw version (held through the provisional window)
_muted_by_pc = {}  # pc_id -> mic mute state (held the same way as fw)
_line_by_pc = {}  # pc_id -> board line "array"|"flex"|"waveshare" (firmware 1.2.19+; held the same way)
_boot_flags_by_pc = {}  # pc_id -> (fw_rejected, wifi_rollback) from fw_push (held the same way)
NODE_LINES = ("array", "flex", "waveshare")
_last_wake = None  # {"time": ts, "room": str|None}
_gate = None  # echo gate ref (set by bot.py; used to block test clips)
_gate_by_pc = {}  # pc_id -> that session's echo gate (multi-node: one
                  # global gate blocked the wrong room's test clips)

PROVISIONAL = "unknown"
BROWSER = "browser"  # the label of a session whose offer names no room


def _evict_other_sessions(pc_id, room):
    """Single live session per room: when pc_id claims `room`, drop any
    OLDER pc_id still mapped to it (and its sender). The node re-offers
    before pipecat reaps its dead session (~22s zombie window) —
    measured: rename/scan queued into the dead
    session's task and 504'd while the live session sat idle."""
    if not pc_id or not room or room == PROVISIONAL:
        return
    stale = [p for p, r in _room_by_pc.items() if r == room and p != pc_id]
    for p in stale:
        _room_by_pc.pop(p, None)
        _out_by_pc.pop(p, None)
        _gate_by_pc.pop(p, None)
        if stale:
            logger.info(f"registry: evicted stale session {p[:24]}… from room '{room}'")
    # Two DIFFERENT nodes claiming the same room name
    # is a config problem (both built with the default PIPECAT_ROOM, or
    # a rename raced) — they'd evict each other into a reconnect loop.
    # A stale-zombie eviction is normal churn; a collision between two
    # LIVE senders is not. Warn loudly so it gets fixed (rename one node
    # via the dashboard) instead of silently reboot-looping.
    if stale and len(_out_by_pc) >= 2 and len({p for p, r in _room_by_pc.items() if r == room}) >= 1:
        remaining = [p for p, r in _room_by_pc.items() if r == room]
        if len(remaining) == 1 and _out_by_pc.get(remaining[0]) is not None:
            logger.warning(
                f"registry: room '{room}' churn — session {pc_id[:24]}… evicted "
                f"{len(stale)} other session(s) that still had live senders. If "
                f"two physical nodes share the room name '{room}', they will "
                f"reconnect-loop: rename one of them (dashboard → Nodes)."
            )


def mark_boot():
    global _boot_time
    _boot_time = time.time()
    # Resolve the running install's version ONCE at boot: `git describe`
    # from the repo the bot runs from (the host checkout is the install
    # — sync_host.sh rsyncs the repo to ~/projects/lenoxvi on the
    # appliance). Falls back to "unknown" if git is missing or this is
    # not a checkout (stranger clones still get a dashboard, per the
    # public-flip rule). Not a static string on purpose: the statusbar shows the ACTUAL installed version,
    # including -N-g<hash> drift past a tag.
    global _version
    try:
        import subprocess
        _version = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:
        _version = "unknown"


def version():
    return _version


def boot_time():
    return _boot_time


def set_gate(gate, pc_id=None):
    """bot.py hands over the EchoGate so test clips can block echo.
    With pc_id it is THAT session's gate (each node gets its own); the
    process-wide gate stays for single-node/older callers."""
    global _gate
    _gate = gate
    if pc_id:
        _gate_by_pc[pc_id] = gate


def gate():
    return _gate


def gates_for_rooms(rooms) -> list:
    """The echo gates of the live sessions sitting in `rooms` — a
    dashboard test tone must block the room it plays in, and with
    several nodes the one global gate belongs to whichever session
    connected last. Falls back to the global gate (the pre-multi-node
    handoff) when no named session matches; empty when nothing can be
    blocked."""
    wanted = set(rooms)
    gates = []
    for pc_id, room in _room_by_pc.items():
        if room == PROVISIONAL or room not in wanted:
            continue
        g = _gate_by_pc.get(pc_id)
        if g is not None and g not in gates:
            gates.append(g)
    if gates:
        return gates
    g = gate()
    return [g] if g is not None else []


def note_firmware(pc_id: str, version: str) -> None:
    """fw_push (1.1.2 connect-push): the node announced its RUNNING
    firmware version on data-channel open. Stored on the room record so
    the Nodes table shows what's actually flashing on the metal — the
    OTA manifest only knows what's AVAILABLE. Survives rename via
    relabel_room (the record moves, the fw field rides along)."""
    if not version:
        return
    room = _room_by_pc.get(pc_id)
    if not room or room == PROVISIONAL:
        # Connect raced the offer tee (provisional window): hold the
        # version keyed by pc_id; note_offer promotes it with the room.
        _fw_by_pc[pc_id] = version
        return
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})
    rec["fw"] = version


def note_line(pc_id: str, line: str) -> None:
    """fw_push (firmware 1.2.19+) names the board line the image was built
    for — the same compiled-in track the OTA board guard uses — so the
    Nodes table can label each node's hardware. Only the real lines
    (NODE_LINES) are accepted (anything else is dropped, not displayed). Same
    provisional-window handling as note_firmware."""
    if line not in NODE_LINES:
        return
    room = _room_by_pc.get(pc_id)
    if not room or room == PROVISIONAL:
        _line_by_pc[pc_id] = line
        return
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})
    rec["line"] = line
    rec["speaker_ok"] = None  # new session: last boot's speaker proof is void


def speaker_capable(room) -> bool:
    """The node in `room` is a board line with a built-in speaker
    (node_speaker.SPEAKER_LINES). Unknown room / unknown line -> False."""
    from node_speaker import SPEAKER_LINES
    rec = _known_rooms.get(room) if room else None
    return bool(rec) and rec.get("line") in SPEAKER_LINES


def has_speaker(room) -> bool:
    """A PROVEN speaker: capable AND its set_speaker reply said
    speaker_ok=true this session (older firmware never acks; a speaker that
    failed to start acks false)."""
    return speaker_capable(room) and _known_rooms[room].get("speaker_ok") is True


def capable_rooms() -> list:
    return sorted(r for r in _known_rooms if r != PROVISIONAL and speaker_capable(r))


def speaker_rooms() -> list:
    return sorted(r for r in _known_rooms if r != PROVISIONAL and has_speaker(r))


def note_speaker_ok(pc_id: str, ok) -> bool:
    """The node's set_speaker reply says whether its speaker came up this
    boot (None until it answers). Returns True when this is the FIRST proof
    (-> the caller re-syncs, so the real state follows the probe)."""
    room = _room_by_pc.get(pc_id)
    if not room or room == PROVISIONAL or not isinstance(ok, bool):
        return False
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})
    became = ok is True and rec.get("speaker_ok") is not True
    rec["speaker_ok"] = ok
    return became


def note_muted(pc_id: str, muted: bool) -> None:
    """Firmware 1.2.11+: the node's mic-mute state, from
    fw_push (connect) or mute_push (an array node's PHYSICAL button was
    pressed). Same provisional-window handling as note_firmware: held by
    pc_id until note_offer binds the room."""
    room = _room_by_pc.get(pc_id)
    if not room or room == PROVISIONAL:
        _muted_by_pc[pc_id] = bool(muted)
        return
    note_muted_room(room, muted)


def note_boot_flags(pc_id: str, rejected, wifi_rollback) -> None:
    """Firmware 1.2.41+: fw_push says what this boot recovered from —
    `rejected` names an OTA image the node rolled back from and now
    refuses, `wifi_rollback` that it went back to its previous network
    after a failed Wi-Fi change. Every fw_push sets both: a push without
    the keys (a clean boot, or older firmware) clears them. Same
    provisional-window handling as note_firmware."""
    flags = (rejected if isinstance(rejected, str) and rejected else None,
             wifi_rollback is True)
    room = _room_by_pc.get(pc_id)
    if not room or room == PROVISIONAL:
        _boot_flags_by_pc[pc_id] = flags
        return
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})
    rec["fw_rejected"], rec["wifi_rollback"] = flags


def note_muted_room(room: str, muted: bool) -> None:
    """Mute state by room (the set_mute route's ack lands here)."""
    if not room or room == PROVISIONAL:
        return
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})
    rec["muted"] = bool(muted)


def _drain_held(pc_id, label) -> None:
    """Release everything held for pc_id through the connect window (fw
    version, mute state) onto the now-known room record. EVERY path that
    binds pc_id -> label must call this (the fw=None bug, 2026-10-09)."""
    held_fw = _fw_by_pc.pop(pc_id, None)
    held_muted = _muted_by_pc.pop(pc_id, None)
    held_line = _line_by_pc.pop(pc_id, None)
    held_flags = _boot_flags_by_pc.pop(pc_id, None)
    if held_fw is None and held_muted is None and held_line is None and held_flags is None:
        return
    rec = _known_rooms.setdefault(label, {"online": False, "first_seen": time.time()})
    if held_fw:
        rec["fw"] = held_fw
    if held_line:
        rec["line"] = held_line
        rec["speaker_ok"] = None  # new session: re-proven by its set_speaker ack
    if held_muted is not None:
        rec["muted"] = held_muted
    if held_flags is not None:
        rec["fw_rejected"], rec["wifi_rollback"] = held_flags


def note_tts(room: str | None, provider: str, usable: bool) -> None:
    """Tracked TTS services (bot.py TTSTracked) report pipecat's
    usability verdict for THIS pipeline's room (silent-TTS
    incident): a websocket death marks a service permanently
    unusable — the node keeps hearing while replies silently vanish,
    and nothing on the dashboard said so. Room None/PROVISIONAL is
    ignored (browser sessions have no room TTS worth reporting)."""
    if not room or room == PROVISIONAL:
        return
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})
    rec["tts_provider"] = provider
    rec["tts_usable"] = bool(usable)


def note_offer(pc_id, room):
    """The offer tee recorded a completed offer/response exchange.

    Fixes up any provisional room assigned at connect time (connect can
    fire before the response carries the pc_id — see module docstring),
    and stores the pair so later connects on the same pc_id resolve
    instantly (renegotiation re-offers).
    """
    label = room or BROWSER
    if not pc_id:
        return
    prev = _room_by_pc.get(pc_id)
    if prev == PROVISIONAL and prev != label:
        # promote the provisional connect: this pc_id is connected (only
        # mark_connected binds it provisional, and a disconnect unbinds
        # it), so its real room is online. The placeholder record is
        # shared by every pc_id still provisional (all nodes re-offer at
        # once after a restart): drop it only when the last one resolves.
        _room_by_pc[pc_id] = label
        if PROVISIONAL not in _room_by_pc.values():
            _known_rooms.pop(PROVISIONAL, None)
        rec = _known_rooms.setdefault(
            label, {"online": False, "first_seen": time.time()}
        )
        rec["online"] = True
        rec["last_seen"] = time.time()
        # fw_push raced the tee (connect window): carry the held version
        # (and mute state) onto the now-known room record.
        _drain_held(pc_id, label)
    else:
        _room_by_pc[pc_id] = label
        # The OTHER race order (fw=None bug, 2026-10-09): fw_push landed
        # while pc_id had NO entry at all (room=None — note_firmware held
        # it in _fw_by_pc). This branch never drained the hold, so the
        # version stranded in _fw_by_pc forever and the Nodes table showed
        # fw=None until some LATER reconnect got luckier ordering. Drain
        # here too — every path that binds pc_id→label must release the
        # held version, not just the provisional promotion.
        _drain_held(pc_id, label)
    _evict_other_sessions(pc_id, label)


def mark_connected(pc_id):
    """on_client_connected fired. May run BEFORE note_offer (ordering
    race): assign a provisional room then; note_offer promotes it."""
    room = _room_by_pc.get(pc_id) if pc_id else None
    if room is None:
        room = PROVISIONAL
        if pc_id:
            _room_by_pc[pc_id] = room
    global _active_room
    _active_room = room
    now = time.time()
    rec = _known_rooms.setdefault(room, {"online": False, "first_seen": now})
    rec["online"] = True
    rec["last_seen"] = now


def mark_disconnected(pc_id):
    room = _room_by_pc.pop(pc_id, None) if pc_id else None
    if not room:
        # Unknown pc_id: a stale session from before this boot, or a
        # duplicate disconnect. Don't touch room state — a newer session
        # may own the room right now (measured: new session connected,
        # the zombie was reaped after it, dashboard lied).
        return
    # Only mark offline if NO other live pc_id still claims this room —
    # pipecat reaps zombie sessions after the replacement connected.
    still_live = any(r == room for other, r in _room_by_pc.items() if other != pc_id)
    if still_live:
        return
    rec = _known_rooms.get(room)
    if rec:
        rec["online"] = False
        rec["last_seen"] = time.time()


def note_wake(room: str | None = None):
    """Called by the wake gate on IDLE -> AWAKE (sync, cheap). Room is
    THIS pipeline's room (passed by bot.py from room_for_pc — the
    old global-active-room read routed replies to the last-connected
    room, wrong the moment a second node exists). None/unknown keeps
    the wake record but marks the room unknown."""
    global _last_wake
    _last_wake = {"time": time.time(), "room": room}


def sender_for_room(room: str):
    """(output_transport, pc_id) for the node currently connected in
    `room`, or (None, None). The output transport sends on that node's
    data channel DIRECTLY (send_message — no pipeline):
    commands must not queue behind a blocked intelligence (same stall class
    as the LED fix). LAST match wins: with eviction
    there's at most one, but if a stale mapping ever survives, the
    newest session is the one that can actually hear us."""
    found = (None, None)
    for pc_id, r in _room_by_pc.items():
        if r == room and _out_by_pc.get(pc_id) is not None:
            found = (_out_by_pc[pc_id], pc_id)
    return found


def sender_for_pc(pc_id: str):
    return _out_by_pc.get(pc_id), pc_id


def room_for_pc(pc_id: str):
    """Current room label for a session (None if unknown)."""
    return _room_by_pc.get(pc_id)


def register_sender(pc_id: str, out) -> None:
    """bot.py hands the OUTPUT TRANSPORT over at connect so node_command
    can send on that node's data channel without touching the pipeline
    (queue_frame enters at the pipeline source and queues behind a
    blocked IntelligenceAgent — restart/scan 504'd during 40s intelligence calls)."""
    _out_by_pc[pc_id] = out


def drop_sender(pc_id: str) -> None:
    _out_by_pc.pop(pc_id, None)
    _gate_by_pc.pop(pc_id, None)


def room_claimed(room: str, pc_id: str) -> bool:
    """Another session than pc_id is bound to `room` (case-insensitive,
    the same rule as the dashboard's rename check)."""
    low = str(room).lower()
    return any(p != pc_id and str(r).lower() == low for p, r in _room_by_pc.items())


def relabel_room(pc_id: str, info: dict | None) -> None:
    """Node acked set_name: move its room record to the new name and
    keep the pc→room binding consistent (in-memory only — the offer
    carries the new name after the node reconnects)."""
    old = _room_by_pc.get(pc_id)
    new = None
    if isinstance(info, dict):
        new = info.get("name")
    if not new or new == old:
        return
    if room_claimed(new, pc_id):
        # Taking the name would leave two sessions in one room and drop the
        # other room's record; the offer settles it when the node reconnects.
        logger.warning(f"registry: '{old}' -> '{new}' not relabeled: another session holds '{new}'")
        return
    _room_by_pc[pc_id] = new
    rec = _known_rooms.pop(old, None)
    if rec:
        _known_rooms[new] = rec
    global _active_room
    if _active_room == old:
        _active_room = new
    logger.info(f"registry: room '{old}' relabeled '{new}' (set_name ack)")


def active_room():
    return _active_room


def last_wake():
    return _last_wake


def known_rooms():
    """[{room, online, first_seen, last_seen}] sorted by name."""
    out = []
    for room, rec in _known_rooms.items():
        out.append(
            {
                "room": room,
                "online": bool(rec.get("online")),
                "first_seen": rec.get("first_seen"),
                "last_seen": rec.get("last_seen"),
                "fw": rec.get("fw"),  # running version (fw_push, 1.1.2)
                # fw_push, 1.2.41+: the OTA version this node rolled back
                # from and now refuses (None = none), and whether this boot
                # went back to the previous Wi-Fi (False on older firmware).
                "fw_rejected": rec.get("fw_rejected"),
                "wifi_rollback": bool(rec.get("wifi_rollback")),
                # board line, one of NODE_LINES (fw_push, 1.2.19+); None =
                # older firmware that doesn't say.
                "line": rec.get("line"),
                # TTS health (2026-10-16): None until this room's
                # pipeline reports — False = the node hears but can't
                # speak (dead TTS service; pipecat never revives it
                # in-session).
                "tts_provider": rec.get("tts_provider"),
                "tts_usable": rec.get("tts_usable"),
                # Firmware 1.2.11+: mic mute — None = this node never
                # reported it (older firmware); the dashboard then asks
                # get_info instead.
                "muted": rec.get("muted"),
                # built-in speaker (board line) + whether it came up
                # this boot (set_speaker reply; None = not answered yet).
                "speaker": speaker_capable(room),
                "speaker_ok": rec.get("speaker_ok"),
            }
        )
    return sorted(out, key=lambda r: r["room"])