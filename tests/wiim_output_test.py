"""WiiM output failure paths (host venv: pipecat + lameenc).

- setup never fails a session over the clip server; speaking retries the bind
- a clip that fails before it plays never arms the echo gate and never cuts
  another clip's window; the failed step is logged, nothing escapes the task
- the happy path arms the gate for the clip before the play command
- a failed music-volume restore is logged with the zone, on a bot stop too

Run: cd tests && ~/venvs/lenoxvi/bin/python wiim_output_test.py <path-to-core>
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


STATE = tempfile.mkdtemp(prefix="lvi-wiim-")
os.environ["LVI_STATE_DIR"] = STATE
Path(STATE, "clips").mkdir()
REPO = Path(CORE).resolve().parent
os.environ.setdefault("LVI_TEST_CONFIG", str(REPO / "tests" / "fixtures" / "config.yaml"))

import sonos_output
import wiim_output
from echo_gate import EchoGate
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor

SRV = SimpleNamespace(url_for=lambda n: f"http://192.0.2.1:9/clips/{n}")
LOGS = []
_sink = logger.add(lambda m: LOGS.append(m.record), level="DEBUG",
                   filter=lambda r: r["name"] == "wiim_output")


def logged(level, *needles):
    return any(r["level"].name == level and all(n in r["message"] for n in needles) for r in LOGS)


def pcm_secs(s):
    return b"\x01\x00" * int(16000 * s)


CMDS = []
PLAY_HOOK = []  # called in the API thread when the play command goes out


def fake_api(ip, cmd, timeout=4.0):
    CMDS.append(cmd)
    if cmd.startswith("setPlayerCmd:play:"):
        for hook in PLAY_HOOK:
            hook()
    return "OK"


real_api, real_status, real_live_vol = (wiim_output.wiim_api, wiim_output.wiim_get_status,
                                        wiim_output.live_wiim_volume)
real_scs, real_write = sonos_output.shared_clip_server, sonos_output._write_clip
wiim_output.wiim_api = fake_api
wiim_output.wiim_get_status = lambda ip, timeout=3.0: {"vol": "20"}
wiim_output.live_wiim_volume = lambda zone: None


def wiim(ip, gate, zone="Kitchen"):
    wiim_output._VOL_SAVED.pop(ip, None)
    wo = wiim_output.WiiMOutput(echo_gate=gate, zone_selector=lambda: zone, led=None)
    wo._enabled = True
    wo._zones = {zone: {"ip": ip}}
    wo._zone_volumes = {zone: 45}
    wo._clip_server = SRV
    return wo


async def speak(wo, pcm):
    """The speak task as process_frame runs it; returns what escaped it."""
    try:
        await asyncio.wait_for(wo._speak_to_wiim(pcm, epoch=wo._speech_epoch,
                                                 barge=wo._barge_epoch), 10)
        return None
    except Exception as e:  # noqa: BLE001 — the probe reports what escaped
        return e


def run_failing(label, ip, setup_fault, undo_fault, level_needles, bound=True):
    """One pre-play failure, twice: with another clip's window open, and
    with an idle gate."""
    for other_clip in (True, False):
        gate = EchoGate()
        if other_clip:
            gate.block(5)
        before = gate._blocked_until
        LOGS.clear()
        CMDS.clear()
        wo = wiim(ip, gate)
        if not bound:
            wo._clip_server = None
        setup_fault()
        try:
            escaped = asyncio.run(speak(wo, pcm_secs(1.0)))
        finally:
            undo_fault()
        played = any(c.startswith("setPlayerCmd:play:") for c in CMDS)
        if other_clip:
            check(f"wiim output: {label} leaves another clip's echo-gate window untouched",
                  gate._blocked_until == before and gate.blocked(),
                  f"before={before:.3f} after={gate._blocked_until:.3f}")
        else:
            check(f"wiim output: {label} never arms an idle echo gate",
                  gate._blocked_until == 0.0 and not gate.blocked(),
                  f"armed for {gate._blocked_until - time.monotonic():.2f}s")
            check(f"wiim output: {label} is logged and nothing escapes the speak task",
                  escaped is None and not played and logged(*level_needles),
                  f"escaped={escaped!r} played={played} logs={[r['message'] for r in LOGS]}")


# ── setup: a failed clip-server bind warns, and speaking retries it ─
BINDS = []


async def bind_fails():
    BINDS.append("fail")
    raise RuntimeError("clip_server.host_ip not set in config.yaml")


async def bind_ok():
    BINDS.append("ok")
    return SRV


async def noop_setup(self, setup):
    return None


async def setup_then_speak():
    wo = wiim("192.0.2.10", EchoGate())
    wo._clip_server = None
    sonos_output.shared_clip_server = bind_fails
    try:
        await wo.setup(None)
        raised = None
    except Exception as e:  # noqa: BLE001 — the probe reports it
        raised = e
    sonos_output.shared_clip_server = bind_ok
    escaped = await speak(wo, pcm_secs(0.1))
    return raised, escaped


real_fp_setup = FrameProcessor.setup
FrameProcessor.setup = noop_setup
LOGS.clear()
CMDS.clear()
try:
    _raised, _escaped = asyncio.run(setup_then_speak())
finally:
    FrameProcessor.setup = real_fp_setup
    sonos_output.shared_clip_server = real_scs
check("wiim output: setup survives a failed clip-server bind and warns",
      _raised is None and logged("WARNING", "wiim_output:", "clip server"),
      f"raised={_raised!r} logs={[r['message'] for r in LOGS]}")
check("wiim output: setup does not report the backend ready when the bind failed",
      not logged("INFO", "backend ready"), str([r["message"] for r in LOGS]))
check("wiim output: a speak after a failed setup bind retries the bind and plays",
      _escaped is None and BINDS == ["fail", "ok"]
      and any(c.startswith("setPlayerCmd:play:") for c in CMDS),
      f"escaped={_escaped!r} binds={BINDS}")

# ── pre-play failures: the gate is never armed ──────────────────────
_real_lameenc = sys.modules.get("lameenc")


def _broken_encoder():
    raise RuntimeError("encoder exploded")


def break_encoder():
    sys.modules["lameenc"] = SimpleNamespace(Encoder=_broken_encoder)


def restore_encoder():
    if _real_lameenc is None:
        sys.modules.pop("lameenc", None)
    else:
        sys.modules["lameenc"] = _real_lameenc


run_failing("an encode failure", "192.0.2.11", break_encoder, restore_encoder,
            ("ERROR", "wiim_output:", "encode failed"))


def write_fails(path, data):
    raise OSError("disk full")


def break_write():
    sonos_output._write_clip = write_fails


def restore_write():
    sonos_output._write_clip = real_write


run_failing("a clip write failure", "192.0.2.12", break_write, restore_write,
            ("ERROR", "wiim_output:", "clip write failed"))


def break_bind():
    sonos_output.shared_clip_server = bind_fails


def restore_bind():
    sonos_output.shared_clip_server = real_scs


run_failing("a failed clip-server bind at speak time", "192.0.2.13", break_bind, restore_bind,
            ("ERROR", "wiim_output:", "clip server"), bound=False)


def vol_set_fails(ip, cmd, timeout=4.0):
    CMDS.append(cmd)
    if cmd.startswith("setPlayerCmd:vol:"):
        raise OSError("unit unreachable")
    return "OK"


def break_vol():
    wiim_output.wiim_api = vol_set_fails


def restore_vol():
    wiim_output.wiim_api = fake_api


run_failing("a voice-volume failure before the play command", "192.0.2.14", break_vol, restore_vol,
            ("ERROR", "wiim_output:", "play failed"))


# ── play failure: the play command went out, the gate stays armed ───
def play_fails(ip, cmd, timeout=4.0):
    CMDS.append(cmd)
    if cmd.startswith("setPlayerCmd:play:"):
        raise OSError("read timed out")
    return "OK"


gate = EchoGate()
gate.block(5)
_before = gate._blocked_until
LOGS.clear()
CMDS.clear()
wiim_output.wiim_api = play_fails
try:
    _escaped = asyncio.run(speak(wiim("192.0.2.15", gate), pcm_secs(1.0)))
finally:
    wiim_output.wiim_api = fake_api
check("wiim output: a play failure never cuts another clip's echo-gate window",
      gate._blocked_until >= _before, f"before={_before:.3f} after={gate._blocked_until:.3f}")
check("wiim output: a play failure is logged and nothing escapes the speak task",
      _escaped is None and logged("ERROR", "wiim_output:", "play failed"),
      f"escaped={_escaped!r} logs={[r['message'] for r in LOGS]}")
check("wiim output: a play failure puts the music volume back",
      CMDS[-1] == "setPlayerCmd:vol:20", str(CMDS))

# ── guard: the happy path arms the gate for the clip before play ────
gate = EchoGate()
_at_play = []
PLAY_HOOK.append(lambda: _at_play.append(gate._blocked_until - time.monotonic()))
LOGS.clear()
CMDS.clear()
try:
    _escaped = asyncio.run(speak(wiim("192.0.2.16", gate), pcm_secs(1.0)))
finally:
    PLAY_HOOK.clear()
check("wiim output: the happy path arms the echo gate for at least the clip before play",
      _escaped is None and len(_at_play) == 1 and _at_play[0] >= 1.0,
      f"escaped={_escaped!r} armed_at_play={_at_play}")
check("wiim output: the happy path still plays and restores the music volume",
      [c.split(":")[1] for c in CMDS] == ["vol", "play", "vol"] and CMDS[-1] == "setPlayerCmd:vol:20",
      str(CMDS))


# ── guard: a barge-in before play keeps the relaxed gate relaxed ────
def vol_then_barge(ip, cmd, timeout=4.0):
    CMDS.append(cmd)
    if cmd == "setPlayerCmd:vol:45":
        time.sleep(0.3)   # the barge-in lands while the voice volume is set
    return "OK"


async def barge_before_play(wo):
    task = asyncio.create_task(wo._speak_to_wiim(pcm_secs(1.0), epoch=wo._speech_epoch,
                                                 barge=wo._barge_epoch))
    while "setPlayerCmd:vol:45" not in CMDS:
        await asyncio.sleep(0.01)
    await wo.stop_current_clip()
    await asyncio.wait_for(task, 10)


gate = EchoGate()
CMDS.clear()
wiim_output.wiim_api = vol_then_barge
try:
    asyncio.run(barge_before_play(wiim("192.0.2.17", gate)))
finally:
    wiim_output.wiim_api = fake_api
check("wiim output: a barge-in before the play command leaves the echo gate relaxed",
      gate._blocked_until <= time.monotonic() + 0.5, f"armed for {gate._blocked_until - time.monotonic():.2f}s")


# ── a failed music-volume restore is logged with the zone ───────────
def restore_fails(ip, cmd, timeout=4.0):
    CMDS.append(cmd)
    if cmd == "setPlayerCmd:vol:20":
        raise OSError("unit napping")
    return "OK"


LOGS.clear()
CMDS.clear()
wiim_output.wiim_api = restore_fails
try:
    _escaped = asyncio.run(speak(wiim("192.0.2.18", EchoGate(), zone="Den"), pcm_secs(0.1)))
    _kept = "192.0.2.18" in wiim_output._VOL_SAVED
finally:
    wiim_output.wiim_api = fake_api
check("wiim output: a failed music-volume restore logs a warning naming the zone",
      _escaped is None and logged("WARNING", "wiim_output:", "Den", "unit napping"),
      f"escaped={_escaped!r} logs={[r['message'] for r in LOGS]}")
check("wiim output: a failed music-volume restore keeps the snapshot for the next clip",
      _kept, str(wiim_output._VOL_SAVED))


# ── a bot stop while the clip sounds: a failed restore is logged too ─
async def stop_while_sounding():
    wo = wiim("192.0.2.19", EchoGate(), zone="Hall")
    task = asyncio.create_task(wo._speak_to_wiim(pcm_secs(2.0), epoch=wo._speech_epoch,
                                                 barge=wo._barge_epoch))
    while not any(c.startswith("setPlayerCmd:play:") for c in CMDS):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)     # past the play command: the restore timer is running
    task.cancel()                # the bot stops mid clip
    try:
        await asyncio.wait_for(task, 10)
    except asyncio.CancelledError:
        return "cancelled"
    except Exception as e:  # noqa: BLE001 — the probe reports what escaped
        return e
    return None


LOGS.clear()
CMDS.clear()
wiim_output.wiim_api = restore_fails
try:
    _stop = asyncio.run(stop_while_sounding())
finally:
    wiim_output.wiim_api = fake_api
check("wiim output: a bot stop while the clip sounds still propagates the cancel",
      _stop == "cancelled", repr(_stop))
check("wiim output: a failed music-volume restore on a bot stop logs a warning naming the zone",
      logged("WARNING", "wiim_output:", "Hall", "unit napping"),
      f"logs={[r['message'] for r in LOGS]}")


# ── an explicit config_path is read, not the live config ────────────
import yaml
from config import cfg

_scratch = Path(STATE, "wiim-config.yaml")
_scratch.write_text(yaml.safe_dump({"wiim": {
    "enabled": False, "clip_volume": 61, "zone_volumes": {"Den": 23},
    "zones": {"Kitchen": {"ip": "192.0.2.41"}, "Den": {"ip": "192.0.2.42"}}}}))
_wo = wiim_output.WiiMOutput(config_path=str(_scratch), echo_gate=None, led=None)
_got = (_wo._enabled, _wo._zones, _wo._volume, _wo._volume_for("Den"), _wo._volume_for("Kitchen"))
check("wiim output: a config_path file supplies enabled, zones and volumes (not the live config)",
      _got == (False, {"Kitchen": {"ip": "192.0.2.41"}, "Den": {"ip": "192.0.2.42"}}, 61, 23, 61), str(_got))

_live = cfg("wiim") or {}
_wo = wiim_output.WiiMOutput(echo_gate=None, led=None)
check("wiim output: without config_path the zones still come from the live config",
      _wo._zones == (_live.get("zones") or {}) and _wo._enabled == bool(_live.get("enabled")),
      f"zones={_wo._zones} live={_live.get('zones')}")

wiim_output.wiim_api, wiim_output.wiim_get_status = real_api, real_status
wiim_output.live_wiim_volume = real_live_vol
logger.remove(_sink)

print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
