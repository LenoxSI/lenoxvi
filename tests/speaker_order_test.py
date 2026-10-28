"""Speaker output order and timing (host venv: pipecat + lameenc).

- a slow older clip (a filler) never loads after a newer reply (Sonos, WiiM)
- the Sonos duck window covers her speech, not the 8 s silence tail
- Sonos setup never fails a session over the clip server (Sonos off, or a
  bad host_ip)

Run: cd tests && ~/venvs/lenoxvi/bin/python speaker_order_test.py <path-to-core>
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


STATE = tempfile.mkdtemp(prefix="lvi-spk-")
os.environ["LVI_STATE_DIR"] = STATE
Path(STATE, "clips").mkdir()
REPO = Path(CORE).resolve().parent
os.environ.setdefault("LVI_TEST_CONFIG", str(REPO / "tests" / "fixtures" / "config.yaml"))

import sonos_output
import wiim_output
from pipecat.processors.frame_processor import FrameProcessor

PLAYS = []


class FakeSw:
    """Sonos websocket stand-in: the FIRST connection of a run is slow."""
    made = 0

    def __init__(self, ip, player_id=None):
        self.n = FakeSw.made
        FakeSw.made += 1

    async def connect(self):
        await asyncio.sleep(0.4 if self.n == 0 else 0.01)

    async def play_clip(self, url, volume=None):
        PLAYS.append(url.rsplit("/", 1)[-1])
        return {"clipId": f"clip-{self.n}"}

    async def cancel_clip(self, cid):
        return None

    async def close(self):
        return None


sys.modules["sonos_websocket"] = SimpleNamespace(SonosWebsocket=FakeSw)
SRV = SimpleNamespace(url_for=lambda n: f"http://127.0.0.1:9/clips/{n}")
sonos_output.SonosOutput._zones_audited = True


def pcm_secs(s):
    return b"\x01\x00" * int(24000 * s)


def sonos():
    so = sonos_output.SonosOutput(echo_gate=None, zone_selector=lambda: "test", led=None)
    so._enabled = True
    so._zones = {"test": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
    so._clip_server = SRV
    so._duck_while_awake = False
    return so


def size_of(name):
    return (Path(STATE) / "clips" / name).stat().st_size


# ── Sonos: an older clip whose connect is slow never replaces the reply ──
async def sonos_order():
    so = sonos()
    so._speech_epoch = 1
    filler = asyncio.create_task(so._speak_to_sonos(pcm_secs(0.5), epoch=1))
    await asyncio.sleep(0.15)     # the filler is connecting (slow)
    so._speech_epoch = 2          # the reply is generated
    reply = asyncio.create_task(so._speak_to_sonos(pcm_secs(3.0), epoch=2))
    await asyncio.gather(filler, reply)


FakeSw.made = 0
asyncio.run(sonos_order())
sizes = [size_of(n) for n in PLAYS]
check("speaker: Sonos loads only the newest reply when an older filler connects slowly",
      len(PLAYS) == 1 and sizes[0] > 20000, f"plays={PLAYS} sizes={sizes}")

# ── Sonos: both play, in order, when the older one is not stale ─────
PLAYS.clear()


async def sonos_in_order():
    so = sonos()
    so._speech_epoch = 1
    await so._speak_to_sonos(pcm_secs(0.5), epoch=1)
    so._speech_epoch = 2
    await so._speak_to_sonos(pcm_secs(3.0), epoch=2)


FakeSw.made = 1  # no slow connection
asyncio.run(sonos_in_order())
check("speaker: a filler that loaded before the reply still played (no over-dropping)",
      len(PLAYS) == 2 and size_of(PLAYS[0]) < size_of(PLAYS[1]), str(PLAYS))

# ── Sonos: the duck window covers her speech, not the silence tail ──
PLAYS.clear()


async def duck_window():
    so = sonos()
    so._duck_while_awake = True
    so._wake_gate = SimpleNamespace(state="awake", note_speech=lambda d: None)
    t0 = time.monotonic()
    await so._speak_to_sonos(pcm_secs(1.0))
    return so._clip_active_until - t0


FakeSw.made = 1
win = asyncio.run(duck_window())
check("speaker: the Sonos duck window is speech + ~1 s (the holder refreshes inside the 8 s tail)",
      1.5 <= win <= 3.0 and len(PLAYS) == 1, f"window={win:.2f}s")

# ── Sonos setup never fails a session over the clip server ─────────
calls = []


async def boom():
    calls.append(1)
    raise RuntimeError("clip_server.host_ip not set in config.yaml")


async def noop_setup(self, setup):
    return None


async def setup_probe(enabled):
    so = sonos()
    so._enabled = enabled
    so._clip_server = None
    try:
        await so.setup(None)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"raised {e}"
    finally:
        task = getattr(so, "_duck_task", None)
        if task is not None:
            task.cancel()


real_setup, real_scs = FrameProcessor.setup, sonos_output.shared_clip_server
FrameProcessor.setup, sonos_output.shared_clip_server = noop_setup, boom
try:
    off = asyncio.run(setup_probe(False))
    off_calls = len(calls)
    on = asyncio.run(setup_probe(True))
finally:
    FrameProcessor.setup, sonos_output.shared_clip_server = real_setup, real_scs
check("speaker: Sonos off -> setup never touches the clip server", off == "ok" and off_calls == 0, f"{off} {calls}")
check("speaker: Sonos on with a bad host_ip -> setup still succeeds (bind retried when speaking)",
      on == "ok", on)

# ── WiiM: same rule, the stale clip is dropped inside the play lock ─
CMDS = []
real_api, real_status = wiim_output.wiim_api, wiim_output.wiim_get_status


def fake_api(ip, cmd, timeout=4.0):
    if cmd.startswith("setPlayerCmd:play:"):
        time.sleep(0.4 if not CMDS else 0.01)   # the first play call is slow
        CMDS.append(cmd)
    return "OK"


wiim_output.wiim_api = fake_api
wiim_output.wiim_get_status = lambda ip, timeout=4.0: {"vol": "20"}


async def wiim_order():
    wo = wiim_output.WiiMOutput(echo_gate=None, zone_selector=lambda: "test", led=None)
    wo._enabled = True
    wo._zones = {"test": {"ip": "127.0.0.2"}}
    wo._clip_server = SRV
    wo._speech_epoch = 1
    first = asyncio.create_task(wo._speak_to_wiim(pcm_secs(0.3), epoch=1))
    await asyncio.sleep(0.15)   # the first play call holds the lock
    wo._speech_epoch = 2
    third_epoch_clip = asyncio.create_task(wo._speak_to_wiim(pcm_secs(0.3), epoch=2))
    await asyncio.sleep(0.05)
    wo._speech_epoch = 3        # a newer reply exists before epoch 2 gets the lock
    newest = asyncio.create_task(wo._speak_to_wiim(pcm_secs(0.3), epoch=3))
    await asyncio.wait_for(asyncio.gather(first, third_epoch_clip, newest, return_exceptions=True), 10)


try:
    asyncio.run(wiim_order())
finally:
    wiim_output.wiim_api, wiim_output.wiim_get_status = real_api, real_status
check("speaker: WiiM drops a clip whose generation went stale while it waited for the player",
      len(CMDS) == 2, f"plays={len(CMDS)}")

# ── WiiM: a bot stop during the play command still restores the music volume ─
VOLS = []


def slow_play_api(ip, cmd, timeout=4.0):
    if cmd.startswith("setPlayerCmd:vol:"):
        VOLS.append(int(cmd.rsplit(":", 1)[1]))
    elif cmd.startswith("setPlayerCmd:play:"):
        time.sleep(0.3)   # the stop lands inside this window
    return "OK"


real_live_vol = wiim_output.live_wiim_volume
wiim_output.wiim_api = slow_play_api
wiim_output.wiim_get_status = lambda ip, timeout=4.0: {"vol": "20"}
wiim_output.live_wiim_volume = lambda zone: None


async def wiim_stop_mid_play():
    wiim_output._VOL_SAVED.pop("127.0.0.3", None)
    wo = wiim_output.WiiMOutput(echo_gate=None, zone_selector=lambda: "test", led=None)
    wo._enabled = True
    wo._zones = {"test": {"ip": "127.0.0.3"}}
    wo._zone_volumes = {"test": 45}
    wo._clip_server = SRV
    wo._speech_epoch = 1
    task = asyncio.create_task(wo._speak_to_wiim(pcm_secs(0.3), epoch=1))
    while not VOLS:              # the voice volume is set: the play call is now in flight
        await asyncio.sleep(0.01)
    task.cancel()                # the bot stops mid play command
    try:
        await asyncio.wait_for(task, 10)
    except asyncio.CancelledError:
        pass
    return "127.0.0.3" in wiim_output._VOL_SAVED


try:
    _left_saved = asyncio.run(wiim_stop_mid_play())
finally:
    wiim_output.wiim_api, wiim_output.wiim_get_status = real_api, real_status
    wiim_output.live_wiim_volume = real_live_vol
check("speaker: WiiM put back the music volume when the bot stopped during the play command",
      VOLS == [45, 20] and not _left_saved, f"vols={VOLS} saved_left={_left_saved}")

print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
