"""Built-in speaker zone pins (host venv).

Pure module + registry + output-stage + node_command wiring. Route pins
live in test_dashboard.py (built-in speaker block).
Run: cd tests && ~/venvs/lenoxvi/bin/python node_speaker_test.py <path-to-core>
"""
import asyncio
import json
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import node_speaker as ns
from dashboard import node_command, registry

# ── pure rules ───────────────────────────────────────────────────────
check("speaker: reserved value is 'builtin'", ns.BUILTIN == "builtin")
check("speaker: speaker lines = waveshare only", ns.SPEAKER_LINES == ("waveshare",))
check("speaker: default volume 40", ns.DEFAULT_VOLUME == 40)

rz = {"den": "Kitchen", "bench": "builtin"}
check("speaker resolve: mapped zone wins", ns.resolve_zone("den", rz, has_speaker=True) == "Kitchen")
check("speaker resolve: mapped builtin", ns.resolve_zone("bench", rz, has_speaker=True) == "builtin")
check("speaker resolve: unmapped speaker node -> builtin (default, no config write)",
      ns.resolve_zone("new", rz, has_speaker=True) == "builtin")
check("speaker resolve: unmapped mic-only node -> None (no speaker: silent until mapped)",
      ns.resolve_zone("new", rz, has_speaker=False) is None)
check("speaker resolve: None/unknown room -> None",
      ns.resolve_zone(None, rz, has_speaker=True) is None
      and ns.resolve_zone("unknown", rz, has_speaker=True) is None)
check("speaker resolve: a builtin mapping on a node without a (proven) speaker -> None (silent)",
      ns.resolve_zone("bench", rz, has_speaker=False) is None)
check("speaker resolve: tolerates a missing room_zones section",
      ns.resolve_zone("new", None, has_speaker=True) == "builtin")

for n in ("builtin", " Builtin ", "BUILTIN"):
    check(f"speaker: reserved zone name {n!r}", ns.is_reserved_zone_name(n))
for n in ("Kitchen", "built in", "builtins"):
    check(f"speaker: not reserved {n!r}", not ns.is_reserved_zone_name(n))

check("speaker volume: default when unset", ns.volume_for("x", {}) == 40)
check("speaker volume: configured", ns.volume_for("x", {"x": 55}) == 55)
check("speaker volume: clamped high/low", ns.volume_for("x", {"x": 150}) == 100 and ns.volume_for("x", {"x": -5}) == 0)
check("speaker volume: junk -> default", ns.volume_for("x", {"x": "loud"}) == 40 and ns.volume_for("x", None) == 40)

# ── payload + sync (patchable hooks, no config files) ───────────────
STATE = {"room_zones": {"bench": "builtin", "den": "Kitchen"}, "volumes": {"bench": 30}}
ns._room_zones = lambda: STATE["room_zones"]
ns._volumes = lambda: STATE["volumes"]
SPK = {"bench", "den", "fresh"}
ns._has_speaker = lambda room: room in SPK
CAPABLE = SPK | {"probing"}
ns._capable = lambda room: room in CAPABLE

check("speaker payload: builtin -> enabled + volume",
      ns.payload("bench") == {"cmd": "set_speaker", "enabled": True, "volume": 30}, str(ns.payload("bench")))
check("speaker payload: mapped to a shared zone -> disabled (never both outputs)",
      ns.payload("den") == {"cmd": "set_speaker", "enabled": False, "volume": 40}, str(ns.payload("den")))
check("speaker payload: unmapped speaker room -> enabled (default)", ns.payload("fresh")["enabled"] is True)
check("speaker payload: capable but not yet PROVEN (no speaker_ok ack) -> disabled probe",
      ns.payload("probing") == {"cmd": "set_speaker", "enabled": False, "volume": 40}, str(ns.payload("probing")))

SENT = []
_real_send = node_command.send_to_room
node_command.send_to_room = lambda room, p: SENT.append((room, p)) or True
try:
    check("speaker sync: speaker room sends set_speaker", ns.sync("bench") is True and SENT[-1] == ("bench", ns.payload("bench")))
    n = len(SENT)
    check("speaker sync: mic-only room sends nothing", ns.sync("kitchen") is False and len(SENT) == n)
    check("speaker sync: a capable-but-unproven room is probed (enabled false)",
          ns.sync("probing") is True and SENT[-1] == ("probing", {"cmd": "set_speaker", "enabled": False, "volume": 40}))
    SENT.clear()
    ns.sync_all(["bench", "kitchen", "den", "bench"])
    check("speaker sync_all: only speaker rooms, each once", sorted(r for r, _ in SENT) == ["bench", "den"], str(SENT))
finally:
    node_command.send_to_room = _real_send

# sync_pc: fw_push can beat the offer tee — wait for the room, then send to the PC
PC_SENT = []
_real_sender_for_pc = registry.sender_for_pc
_real_send_raw = node_command._send
registry.sender_for_pc = lambda pc: ("OUT", pc)
node_command._send = lambda out, p: PC_SENT.append((out, p)) or True


async def _bind_later():
    registry._room_by_pc["pc-spa"] = registry.PROVISIONAL
    task = asyncio.create_task(ns.sync_pc("pc-spa", wait_s=3.0, poll_s=0.05))
    await asyncio.sleep(0.2)
    check("speaker sync_pc: nothing sent while the room is provisional", PC_SENT == [], str(PC_SENT))
    registry._room_by_pc["pc-spa"] = "den"
    return await task

try:
    ok = asyncio.run(_bind_later())
    check("speaker sync_pc: sends once the room binds, enabled from THAT room's zone",
          ok is True and PC_SENT == [("OUT", ns.payload("den"))], str(PC_SENT))
    PC_SENT.clear()
    registry._room_by_pc["pc-spb"] = registry.PROVISIONAL
    ok = asyncio.run(ns.sync_pc("pc-spb", wait_s=0.2, poll_s=0.05))
    check("speaker sync_pc: gives up quietly if the room never binds", ok is False and PC_SENT == [])
finally:
    registry.sender_for_pc = _real_sender_for_pc
    node_command._send = _real_send_raw
    registry._room_by_pc.pop("pc-spa", None)
    registry._room_by_pc.pop("pc-spb", None)

# ── registry ─────────────────────────────────────────────────────────
registry._room_by_pc["pc-spr"] = "wssp"
registry.note_line("pc-spr", "waveshare")
registry._room_by_pc["pc-spx"] = "xvfsp"
registry.note_line("pc-spx", "array")
check("speaker registry: a waveshare line alone is CAPABLE, not yet a speaker",
      registry.speaker_capable("wssp") is True and registry.has_speaker("wssp") is False)
registry.note_speaker_ok("pc-spr", True)
check("speaker registry.has_speaker: waveshare line + speaker_ok ack", registry.has_speaker("wssp") is True)
check("speaker registry.has_speaker: array line / unknown room", registry.has_speaker("xvfsp") is False and registry.has_speaker("nopesp") is False)
row = {r["room"]: r for r in registry.known_rooms()}
check("speaker: known_rooms carries speaker flag", row["wssp"].get("speaker") is True and row["xvfsp"].get("speaker") is False)
check("speaker: speaker_rooms lists speaker rooms only", "wssp" in registry.speaker_rooms() and "xvfsp" not in registry.speaker_rooms())
registry.note_speaker_ok("pc-spr", False)
check("speaker: note_speaker_ok recorded (a failed speaker is no speaker)",
      {r["room"]: r for r in registry.known_rooms()}["wssp"]["speaker_ok"] is False and registry.has_speaker("wssp") is False)
check("speaker: capable_rooms still lists it (so it gets probed again)", "wssp" in registry.capable_rooms())

# ── node_command wiring ──────────────────────────────────────────────
RESYNC = []
_real_sync = ns.sync
ns.sync = lambda room: RESYNC.append(room) or True


def _ack(ok, pc="pc-spr"):
    node_command.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_speaker", "ok": True,
                                        "data": {"enabled": False, "volume": 40, "speaker_ok": ok}}), pc)

try:
    _ack(True)
    check("speaker: set_speaker reply updates speaker_ok", {r["room"]: r for r in registry.known_rooms()}["wssp"]["speaker_ok"] is True)
    check("speaker: first proof (ack speaker_ok true) re-syncs so the real state follows the probe", RESYNC == ["wssp"], str(RESYNC))
    _ack(True)
    check("speaker: a repeat ack does not re-sync (no ping-pong)", RESYNC == ["wssp"], str(RESYNC))
    node_command.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_speaker", "ok": False,
                                        "note": "unknown cmd"}), "pc-spx")
    check("speaker: older firmware answering 'unknown cmd' never becomes a speaker", registry.has_speaker("xvfsp") is False)
finally:
    ns.sync = _real_sync

CALLED = []
_real_sync_pc = ns.sync_pc


async def _fake_sync_pc(pc_id, **kw):
    CALLED.append(pc_id)
    return True

ns.sync_pc = _fake_sync_pc


async def _push(line, pc):
    node_command.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push", "ok": True,
                                        "data": {"fw": "1.2.32", "line": line}}), pc)
    await asyncio.sleep(0.05)

try:
    asyncio.run(_push("waveshare", "pc-spr"))
    check("speaker: fw_push from a speaker line schedules sync_pc", CALLED == ["pc-spr"], str(CALLED))
    check("speaker: a new session forgets last boot's proof (re-probed; a downgrade can't stay 'speaker')",
          registry.has_speaker("wssp") is False and {r["room"]: r for r in registry.known_rooms()}["wssp"]["speaker_ok"] is None)
    CALLED.clear()
    asyncio.run(_push("array", "pc-spx"))
    check("speaker: fw_push from a mic-only line does not", CALLED == [], str(CALLED))
finally:
    ns.sync_pc = _real_sync_pc

# ── output stage: echo gate for builtin replies ──────────────────────
import node_speaker_output as nso
from pipecat.frames.frames import (
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class _Gate:
    def __init__(self):
        self.blocks = []

    def block(self, s):
        self.blocks.append(s)


def _run_reply(zone):
    gate = _Gate()
    proc = nso.NodeSpeakerOutput(echo_gate=gate, zone_selector=lambda: zone)
    pushed = []

    async def _push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)
    proc.push_frame = _push_frame

    async def go():
        for f in (TTSStartedFrame(), TTSAudioRawFrame(audio=b"\x00\x00" * 24000, sample_rate=24000, num_channels=1),
                  TTSStoppedFrame()):
            await proc.process_frame(f, FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    return gate, pushed

gate, pushed = _run_reply("builtin")
check("speaker: builtin reply arms the echo gate for duration + 1.5 s",
      len(gate.blocks) == 1 and abs(gate.blocks[0] - 2.5) < 0.01, str(gate.blocks))
check("speaker: output stage passes every frame downstream (the audio IS the node's reply)", len(pushed) == 3, str(len(pushed)))
def _run_slow_reply(zone, gen_s):
    gate = _Gate()
    proc = nso.NodeSpeakerOutput(echo_gate=gate, zone_selector=lambda: zone)

    async def _push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    proc.push_frame = _push_frame

    async def go():
        await proc.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
        await proc.process_frame(TTSAudioRawFrame(audio=b"\x00\x00" * 24000, sample_rate=24000, num_channels=1),
                                 FrameDirection.DOWNSTREAM)
        await asyncio.sleep(gen_s)  # streaming TTS: generation ends while the node is already playing
        await proc.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    return gate

g = _run_slow_reply("builtin", 0.4)
check("speaker: echo gate anchored at the FIRST audio frame (playback start), not generation end",
      len(g.blocks) == 1 and abs(g.blocks[0] - 2.1) < 0.08, str(g.blocks))
gate, pushed = _run_reply("Kitchen")
check("speaker: shared-zone reply: stage leaves the gate to the Sonos/WiiM backend", gate.blocks == [] and len(pushed) == 3)
gate, _ = _run_reply(None)
check("speaker: no zone (unmapped room): stage stays out of it", gate.blocks == [])

# ── R-I5 speaker-only install (Sonos disabled): LEDs + wake gate follow the reply ──
import sonos_output as _so


class _Led:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


class _WakeGate:
    def __init__(self):
        self.speech = []

    def note_speech(self, d):
        self.speech.append(d)


def _sonos_disabled_reply(zone):
    led, wg = _Led(), _WakeGate()
    so = _so.SonosOutput(echo_gate=_Gate(), zone_selector=lambda: zone, wake_gate=wg, led=led)
    so._enabled = False
    ends = []
    so._schedule_end_of_speech = lambda s: ends.append(s)
    so._cancel_end_of_speech = lambda: None

    async def _pf(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    so.push_frame = _pf

    async def go():
        for f in (TTSStartedFrame(), TTSAudioRawFrame(audio=b"\x00\x00" * 24000, sample_rate=24000, num_channels=1),
                  TTSStoppedFrame()):
            await so.process_frame(f, FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    return led, wg, ends

led, wg, ends = _sonos_disabled_reply("builtin")
check("speaker: Sonos disabled + builtin reply: LED end at duration + 0.5 (not instantly)",
      ends == [1.5] and "bot-started-speaking" in led.sent, f"ends={ends} led={led.sent}")
check("speaker: Sonos disabled + builtin reply: wake gate gets the real speech window", wg.speech == [1.0], str(wg.speech))
led, wg, ends = _sonos_disabled_reply(None)
check("speaker: Sonos disabled, no builtin: unchanged (LED ends at once, no speech window)", ends == [0.0] and wg.speech == [])

# ── speakers: a room with no speaker mapping stays silent until it is mapped ──
import inspect

import wiim_output as _wo_un


def _unmapped_reply(zone, room="probe-room"):
    """Both backends enabled, each with a default_zone configured: a None
    reply zone must still play nowhere."""
    led, wg = _Led(), _WakeGate()
    gate = _Gate()
    wo = _wo_un.WiiMOutput(echo_gate=gate, zone_selector=lambda: zone, wake_gate=wg, led=led)
    wo._enabled, wo._zones, wo._zone = True, {"wz": {"ip": "127.0.0.1"}}, "wz"
    kw = ({"room_selector": lambda: room}
          if "room_selector" in inspect.signature(_so.SonosOutput.__init__).parameters else {})
    so = _so.SonosOutput(echo_gate=gate, zone_selector=lambda: zone, wake_gate=wg, led=led,
                         reply_elsewhere=lambda: wo.owns_reply(), **kw)
    so._enabled, so._zones, so._zone = True, {"sz": {"ip": "127.0.0.1", "player_uid": "RINCON_X"}}, "sz"
    so._duck_while_awake = False
    tasks, ends = [], []
    for o in (so, wo):
        o._schedule_end_of_speech = lambda s: ends.append(s)
        o._cancel_end_of_speech = lambda: None
        o.create_task = lambda coro, name=None, context=None: tasks.append(coro) or coro.close()

        async def _pf(frame, direction=FrameDirection.DOWNSTREAM):
            pass
        o.push_frame = _pf

    async def go():
        for f in (TTSStartedFrame(), TTSAudioRawFrame(audio=b"\x00\x00" * 24000, sample_rate=24000, num_channels=1),
                  TTSStoppedFrame()):
            await so.process_frame(f, FrameDirection.DOWNSTREAM)
            await wo.process_frame(f, FrameDirection.DOWNSTREAM)
    asyncio.run(go())
    return wo, tasks, ends, wg, gate


_wo, _tasks, _ends, _wg, _g = _unmapped_reply(None)
check("speakers: unmapped room: neither backend claims or plays the reply (default_zone unused)",
      _wo.owns_reply() is False and _tasks == [], f"owns={_wo.owns_reply()} tasks={len(_tasks)}")
check("speakers: unmapped room: LED ends at once, no speech window, echo gate never armed",
      _ends == [0.0] and _wg.speech == [] and _g.blocks == [], f"ends={_ends} speech={_wg.speech} blocks={_g.blocks}")
# An unproven builtin resolves to None (silent), and to builtin once speaker_ok arrives.
_rz_b = {"bench": "builtin"}
_wo, _tasks, _ends, _wg, _g = _unmapped_reply(ns.resolve_zone("bench", _rz_b, has_speaker=False))
check("speakers: an unproven builtin room is silent (no shared-zone fallback) until speaker_ok",
      _tasks == [] and _ends == [0.0] and ns.resolve_zone("bench", _rz_b, has_speaker=True) == ns.BUILTIN,
      f"tasks={len(_tasks)} ends={_ends}")
_wo, _tasks, _ends, _wg, _g = _unmapped_reply("sz")
check("speakers: a mapped Sonos room still plays (one speak task, speech window set)",
      len(_tasks) == 1 and _wg.speech == [1.0], f"tasks={len(_tasks)} speech={_wg.speech}")

# ── bot wiring (source pins) ─────────────────────────────────────────
from pathlib import Path

_bot = (Path(CORE).parent / "bot.py").read_text()
check("speaker: bot._reply_zone resolves through node_speaker", "node_speaker.resolve_zone(" in _bot)
import re as _re

check("speaker: NodeSpeakerOutput sits in the pipeline after the backends, right before the transport output",
      _re.search(r"\n\s+wiim_out,\n\s+node_spk_out,\n\s+out,\n", _bot) is not None)
import sonos_output
import wiim_output

check("speaker Sonos/WiiM: builtin is not theirs and isn't logged as a warning",
      "BUILTIN" in inspect.getsource(sonos_output) and "BUILTIN" in inspect.getsource(wiim_output))

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
