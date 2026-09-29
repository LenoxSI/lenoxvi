"""registry — runtime node/room state for the dashboard (process memory).

Identity model (measured 2026-09-29 against the live stack; design note):
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

_boot_time = None
_active_room = None
_room_by_pc = {}  # pc_id -> room ("unknown" = provisional, awaiting offer tee)
_known_rooms = {}  # room -> {"online": bool, "first_seen": ts, "last_seen": ts}
_last_wake = None  # {"time": ts, "room": str|None}
_gate = None  # echo gate ref (set by bot.py; used to block test clips)

PROVISIONAL = "unknown"


def mark_boot():
    global _boot_time
    _boot_time = time.time()


def boot_time():
    return _boot_time


def set_gate(gate):
    """bot.py hands over the EchoGate so test clips can block echo."""
    global _gate
    _gate = gate


def gate():
    return _gate


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
    else:
        _room_by_pc[pc_id] = label


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
    if room:
        rec = _known_rooms.get(room)
        if rec:
            rec["online"] = False
            rec["last_seen"] = time.time()


def note_wake():
    """Called by the wake gate on IDLE -> AWAKE (sync, cheap)."""
    global _last_wake
    _last_wake = {"time": time.time(), "room": _active_room}


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
            }
        )
    return sorted(out, key=lambda r: r["room"])