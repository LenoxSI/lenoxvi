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
from collections import deque
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
NODE_LINES = ("array", "flex", "waveshare")
_last_wake = None  # {"time": ts, "room": str|None}
_gate = None  # echo gate ref (set by bot.py; used to block test clips)

PROVISIONAL = "unknown"


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


def set_gate(gate):
    """bot.py hands over the EchoGate so test clips can block echo."""
    global _gate
    _gate = gate


def gate():
    return _gate


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


def has_speaker(room) -> bool:
    """The node in `room` has a built-in speaker (its board line is one
    of node_speaker.SPEAKER_LINES). Unknown room / unknown line -> False."""
    from node_speaker import SPEAKER_LINES
    rec = _known_rooms.get(room) if room else None
    return bool(rec) and rec.get("line") in SPEAKER_LINES


def speaker_rooms() -> list:
    return sorted(r for r in _known_rooms if r != PROVISIONAL and has_speaker(r))


def note_speaker_ok(pc_id: str, ok) -> None:
    """The node's set_speaker reply says whether its speaker came up
    this boot (None until it answers)."""
    room = _room_by_pc.get(pc_id)
    if not room or room == PROVISIONAL or not isinstance(ok, bool):
        return
    _known_rooms.setdefault(room, {"online": False, "first_seen": time.time()})["speaker_ok"] = ok


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
    if held_fw is None and held_muted is None and held_line is None:
        return
    rec = _known_rooms.setdefault(label, {"online": False, "first_seen": time.time()})
    if held_fw:
        rec["fw"] = held_fw
    if held_line:
        rec["line"] = held_line
    if held_muted is not None:
        rec["muted"] = held_muted


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
    label = room or "browser"
    if not pc_id:
        return
    prev = _room_by_pc.get(pc_id)
    if prev == PROVISIONAL and prev != label:
        # promote the provisional connect: move its online state under
        # the real room name
        _room_by_pc[pc_id] = label
        prov = _known_rooms.pop(PROVISIONAL, None)
        if prov and prov.get("online"):
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


def note_wake(room: str | None = None, heard_key: str | None = None):
    """Called by the wake gate on IDLE -> AWAKE (sync, cheap). Room is
    THIS pipeline's room (passed by bot.py from room_for_pc — the
    old global-active-room read routed replies to the last-connected
    room, wrong the moment a second node exists). None/unknown keeps
    the wake record but marks the room unknown."""
    global _last_wake
    _last_wake = {"time": time.time(), "room": room}
    # heard_key: the room key the pipeline's intelligence records transcripts
    # under (captured at build time) — so wake and transcript always pair.
    _WAKE_PENDING[(heard_key if heard_key is not None else room) or "unknown"] = time.time()


# Heard-after-wake: the FIRST final transcript within HEARD_WINDOW_S of a wake
# in the same room — what speech-to-text made of the wake phrase. Shown on the
# dashboard's Wake name card so a new mis-spelling becomes a one-click alias.
# In memory only (never written to disk), last 2, cleared on restart.
HEARD_WINDOW_S = 30.0
_RECENT_HEARD: deque = deque(maxlen=2)
_WAKE_PENDING: dict = {}


def note_heard(room: str | None, text: str, dropped: bool) -> None:
    """Record `text` if it's the first transcript since this room's wake."""
    t = _WAKE_PENDING.pop(room or "unknown", None)
    if t is None or time.time() - t > HEARD_WINDOW_S:
        return
    _RECENT_HEARD.appendleft({"time": time.time(), "room": room,
                              "text": str(text)[:200], "dropped": bool(dropped)})


def recent_heard() -> list:
    return list(_RECENT_HEARD)


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
                "speaker": has_speaker(room),
                "speaker_ok": rec.get("speaker_ok"),
            }
        )
    return sorted(out, key=lambda r: r["room"])