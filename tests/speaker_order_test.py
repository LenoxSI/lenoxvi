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
from typing import ClassVar

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

# ── Sonos reply failures, bad config values, duck-holder recovery ───
import uuid

import lameenc  # noqa: F401 — the real encoder, put back after the broken-encoder probe
import yaml
from config import DEFAULT_CLIP_VOLUME
from echo_gate import EchoGate
from loguru import logger as _logger

LOGS = []
_logger.add(lambda m: LOGS.append((m.record["level"].name, m.record["message"])), level="DEBUG")
FIXTURE_SONOS = yaml.safe_load(Path(os.environ["LVI_TEST_CONFIG"]).read_text())["sonos"]
ZONES = {"Kitchen": {"ip": "192.0.2.21", "player_uid": "RINCON_TEST_KITCHEN"},
         "Hall": {"ip": "192.0.2.22", "player_uid": "RINCON_TEST_HALL"}}


def logged(level, needle):
    return [m for lv, m in LOGS if lv == level and needle in m]


class GateProbeSw(FakeSw):
    """Records how long the echo gate still has to run when play is called."""
    gate = None
    seen: ClassVar[list] = []

    async def play_clip(self, url, volume=None):
        if GateProbeSw.gate is not None:
            GateProbeSw.seen.append(GateProbeSw.gate._blocked_until - time.monotonic())
        return await super().play_clip(url, volume)


sys.modules["sonos_websocket"] = SimpleNamespace(SonosWebsocket=GateProbeSw)


def kitchen(gate):
    so = sonos_output.SonosOutput(echo_gate=gate, zone_selector=lambda: "Kitchen", led=None)
    so._enabled = True
    so._zones = dict(ZONES)
    so._clip_server = SRV
    so._duck_while_awake = False
    return so


class BrokenEncoder:
    def __init__(self):
        raise RuntimeError("encoder broke")


def _replace_fails(src, dst):
    raise OSError(28, "No space left on device")


async def _bind_fails():
    raise OSError(98, "Address already in use")


async def reply_fails(step, prearm):
    """One 1 s reply whose `step` fails; returns what the gate and log saw."""
    gate = EchoGate()
    GateProbeSw.gate = gate
    if prearm:
        gate.block(5.0)  # another clip (a filler) is still sounding
    before = gate._blocked_until
    so = kitchen(gate)
    real_lame, real_os, real_scs = sys.modules.get("lameenc"), sonos_output.os, sonos_output.shared_clip_server
    if step == "encode":
        sys.modules["lameenc"] = SimpleNamespace(Encoder=BrokenEncoder)
    elif step == "clip write":
        sonos_output.os = SimpleNamespace(replace=_replace_fails)
    elif step == "clip server bind":
        so._clip_server = None
        sonos_output.shared_clip_server = _bind_fails
    n_plays, n_logs = len(PLAYS), len(LOGS)
    escaped = None
    try:
        await so._speak_to_sonos(pcm_secs(1.0))
    except Exception as e:  # noqa: BLE001 — the probe reports it
        escaped = e
    finally:
        sys.modules["lameenc"], sonos_output.os = real_lame, real_os
        sonos_output.shared_clip_server = real_scs
    errors = [m for lv, m in LOGS[n_logs:] if lv == "ERROR"]
    return SimpleNamespace(escaped=escaped, before=before, after=gate._blocked_until,
                           blocked=gate.blocked(), played=len(PLAYS) - n_plays, errors=errors)


FakeSw.made = 1
for _step in ("encode", "clip write", "clip server bind"):
    r = asyncio.run(reply_fails(_step, prearm=True))
    check(f"speaker: a Sonos reply whose {_step} fails leaves another clip's echo window untouched",
          r.after == r.before and r.blocked and r.played == 0, f"before={r.before} after={r.after}")
    check(f"speaker: a Sonos reply whose {_step} fails logs one error naming the step, nothing escapes",
          r.escaped is None and len(r.errors) == 1 and _step in r.errors[0], f"escaped={r.escaped!r} errors={r.errors}")
    r = asyncio.run(reply_fails(_step, prearm=False))
    check(f"speaker: a Sonos reply whose {_step} fails never arms the echo gate",
          r.after == 0.0 and not r.blocked and r.escaped is None, f"after={r.after} escaped={r.escaped!r}")
_tmp_left = [p.name for p in Path(STATE, "clips").iterdir() if p.name.endswith(".tmp")]
check("speaker: a failed Sonos clip write leaves no temp file behind in clips/", not _tmp_left, str(_tmp_left))


async def reply_plays():
    gate = EchoGate()
    GateProbeSw.gate = gate
    GateProbeSw.seen.clear()
    await kitchen(gate)._speak_to_sonos(pcm_secs(2.0))
    return list(GateProbeSw.seen), gate.blocked()


_seen, _blocked = asyncio.run(reply_plays())
check("speaker: a Sonos reply arms the echo gate for at least its speech before play is called",
      len(_seen) == 1 and _seen[0] >= 2.0 and _blocked, f"remaining at play={_seen}")


async def barge_during_connect():
    gate = EchoGate()
    GateProbeSw.gate = gate
    so = kitchen(gate)
    FakeSw.made = 0  # this connect is slow: the barge-in lands inside it
    task = asyncio.create_task(so._speak_to_sonos(pcm_secs(1.0), barge=so._barge_epoch))
    await asyncio.sleep(0.25)
    await so.stop_current_clip()
    await task
    return gate._blocked_until, gate.blocked()


n_plays = len(PLAYS)
_until, _blocked = asyncio.run(barge_during_connect())
check("speaker: a barge-in while the Sonos reply connects drops it and leaves the echo gate un-armed",
      len(PLAYS) == n_plays and _until == 0.0 and not _blocked, f"until={_until} plays={PLAYS[n_plays:]}")
FakeSw.made = 1


# Config values: a hand-edited config.yaml can hold anything.
def sonos_from(**overrides):
    path = Path(STATE, f"cfg-{uuid.uuid4().hex[:8]}.yaml")
    path.write_text(yaml.safe_dump({"sonos": {**FIXTURE_SONOS, **overrides}}))

    async def build():
        return sonos_output.SonosOutput(config_path=str(path), echo_gate=None, led=None)

    try:
        return asyncio.run(build()), None
    except Exception as e:  # noqa: BLE001 — the probe reports it
        return None, e


so, err = sonos_from()
check("speaker: Sonos volumes from valid config are unchanged (global and per zone)",
      err is None and so._volume == FIXTURE_SONOS["clip_volume"]
      and so._zone_volumes == {k: int(v) for k, v in FIXTURE_SONOS["zone_volumes"].items()}
      and so._volume_for("Hall") == FIXTURE_SONOS["zone_volumes"]["Hall"]
      and so._retention_days == FIXTURE_SONOS["clip_retention_days"], f"err={err!r}")

so, err = sonos_from(clip_volume="loud")
try:
    _vols = [so._volume, so._volume, so._volume_for("Kitchen")]
except Exception as e:  # noqa: BLE001 — the probe reports it
    _vols = [repr(e)]
check("speaker: a non-numeric sonos.clip_volume falls back to the default with one warning",
      _vols == [DEFAULT_CLIP_VOLUME] * 3 and len(logged("WARNING", "'loud'")) == 1, f"vols={_vols}")

so, err = sonos_from(zone_volumes={"Hall": "quiet", "Kitchen": float("nan")})
try:
    _vols = [so._volume_for("Hall"), so._volume_for("Kitchen"), so._volume_for("Hall")]
except Exception as e:  # noqa: BLE001 — the probe reports it
    _vols = [repr(e)]
_g = FIXTURE_SONOS["clip_volume"]
check("speaker: a bad per-zone Sonos volume is skipped so the global volume applies",
      _vols == [_g, _g, _g], f"vols={_vols}")
check("speaker: each bad per-zone Sonos volume warns once, not on every reply",
      len(logged("WARNING", "zone_volumes.Hall 'quiet'")) == 1
      and len(logged("WARNING", "zone_volumes.Kitchen nan")) == 1,
      str(logged("WARNING", "zone_volumes")))

for _bad in ("abc", -1):
    so, err = sonos_from(clip_retention_days=_bad)
    check(f"speaker: sonos.clip_retention_days {_bad!r} -> the Sonos output still builds with 7 days, warned",
          err is None and so._retention_days == 7 and len(logged("WARNING", f"clip_retention_days {_bad!r}")) == 1,
          f"err={err!r} days={getattr(so, '_retention_days', None)}")


# Duck holder: a failed duck-clip create is retried, the holder never gives up.
async def duck_recovers(fail_first, awake_from_start, retry_s, run_s, wake_after=None):
    calls = []

    async def flaky_server():
        calls.append(1)
        if len(calls) <= fail_first:
            raise OSError(98, "Address already in use")
        return SRV

    real_scs, real_retry = sonos_output.shared_clip_server, getattr(sonos_output, "_DUCK_CLIP_RETRY_S", 30.0)
    sonos_output.shared_clip_server, sonos_output._DUCK_CLIP_RETRY_S = flaky_server, retry_s
    so = kitchen(None)
    so._clip_server = None
    so._duck_while_awake = True
    so._wake_gate = SimpleNamespace(state="awake" if awake_from_start else "idle")
    n_plays, n_logs = len(PLAYS), len(LOGS)
    task = asyncio.create_task(so._duck_holder_loop())
    try:
        if wake_after is not None:
            await asyncio.sleep(wake_after)
            so._wake_gate.state = "awake"
        await asyncio.sleep(run_s)
        alive = not task.done()
        task.cancel()
        try:
            await asyncio.wait_for(task, 3.0)
        except asyncio.CancelledError:
            pass
    finally:
        sonos_output.shared_clip_server, sonos_output._DUCK_CLIP_RETRY_S = real_scs, real_retry
    logs = LOGS[n_logs:]
    return SimpleNamespace(calls=len(calls), alive=alive, exited=task.done(),
                           ducked="duck-silence.mp3" in PLAYS[n_plays:], holding=so._duck_holding,
                           warnings=[m for lv, m in logs if lv == "WARNING" and "duck clip" in m],
                           recovered=[m for lv, m in logs if lv == "INFO" and "recovered" in m])


r = asyncio.run(duck_recovers(fail_first=1, awake_from_start=False, retry_s=30.0, run_s=1.5, wake_after=0.8))
check("speaker: Sonos duck holder retries a failed duck clip on the next wake and ducks",
      r.ducked and r.alive, f"calls={r.calls} alive={r.alive}")
check("speaker: Sonos duck holder still exits on cancel after recovering", r.exited and not r.holding)

r = asyncio.run(duck_recovers(fail_first=2, awake_from_start=True, retry_s=0.6, run_s=2.5))
check("speaker: Sonos duck holder retries a failed duck clip on its backoff while awake and ducks",
      r.ducked and r.calls == 3, f"calls={r.calls}")
check("speaker: Sonos duck holder warns once for repeated duck-clip failures, then logs the recovery",
      len(r.warnings) == 1 and len(r.recovered) == 1, f"warnings={r.warnings} recovered={r.recovered}")

r = asyncio.run(duck_recovers(fail_first=99, awake_from_start=True, retry_s=30.0, run_s=2.0))
check("speaker: a failing Sonos duck clip is retried on the backoff, not every tick",
      r.calls == 2 and not r.ducked, f"calls={r.calls}")
check("speaker: Sonos duck holder with no duck clip still exits on cancel", r.alive and r.exited and not r.holding,
      f"alive={r.alive} exited={r.exited}")

print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
