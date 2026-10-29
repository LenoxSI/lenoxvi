"""tts failover — a cloud voice falls back to the Kokoro standby (core/tts_fallback.py), host venv.

The standby is the real Kokoro service class (tts_kokoro.make_service) on a
fake shared model: audio comes out of it without loading the 325 MB ONNX
model, and the timing doesn't depend on this host's CPU (the real model's
audio is pinned by localai_golden_test). The WAN-dead boot checks drive a
REAL ElevenLabs service pointed at a refused port, inside a real switcher,
inside a real pipeline.

Run: cd tests && ~/venvs/lenoxvi/bin/python tts_failover_test.py <path-to-core>
"""
import asyncio
import importlib.util
import inspect
import sys
import types

import numpy as np

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _run(coro_fn, default):
    """Run one async probe; an exception (e.g. API not there yet) = default."""
    try:
        return asyncio.run(coro_fn())
    except Exception as e:  # noqa: BLE001
        print(f"   (probe {coro_fn.__name__} raised {type(e).__name__}: {str(e)[:90]})")
        return default


# pipecat's Kokoro module imports kokoro_onnx: stand one in on a box without
# it (it is in the base install; the fake shared model below does the talking).
if importlib.util.find_spec("kokoro_onnx") is None:
    _kk_mod = types.ModuleType("kokoro_onnx")
    _kk_mod.Kokoro = object
    sys.modules["kokoro_onnx"] = _kk_mod

import tts_fallback as tf
import tts_kokoro as tk
from pipecat.frames.frames import ErrorFrame, ManuallySwitchServiceFrame
from pipecat.pipeline.service_switcher import (
    ServiceSwitcherStrategyFailover,
)


class Mixin:  # stand-in for bot.TTSTracked (same attribute surface)
    _tts_provider = "x"
    _tts_room = None


class _FakeKokoro:
    """The shared kokoro-onnx model's surface: every catalog voice, and 10 ms
    of silence per sentence (enough for audio frames to flow)."""

    def __init__(self):
        self.voices = {v: object() for v in tk.KOKORO_VOICES}

    async def create_stream(self, text, voice, speed=1.0, lang="en-us", **kw):
        yield np.zeros(240, dtype=np.float32), 24000


tk._reset_for_tests()
tk._kokoro = _FakeKokoro()


def _kokoro(provider=None):
    """A real Kokoro service on the fake shared model; `provider` relabels it
    (a stand-in primary for the wrap checks)."""
    svc = tk.make_service(mixin=Mixin, voice=tk.DEFAULT_VOICE, speed=tk.DEFAULT_SPEED)
    if svc is not None and provider:
        svc._tts_provider = provider
    return svc


class FakeSvc:
    def __init__(self, name, provider):
        self.name = name
        self._tts_provider = provider
        self.is_usable = True
        self.revives = 0
        self.revive_ok = False
        self.queued = []

    async def queue_frame(self, f, *a, **k):
        self.queued.append(f)

    async def _try_revive(self):
        self.revives += 1
        if self.revive_ok:
            self.is_usable = True


# ── the contract: names and defaults ─────────────────────────────────
_wsig = inspect.signature(tf.wrap_with_fallback).parameters
_rsig = inspect.signature(tf.recover).parameters
check("tts failover: wrap_with_fallback(primary, standby, *, room, queue_frame, cooldown, standby_name='kokoro')",
      list(_wsig) == ["primary", "standby", "room", "queue_frame", "cooldown", "standby_name"]
      and _wsig["standby_name"].default == "kokoro", str(list(_wsig)))
check("tts failover: one standby, Kokoro — recover() takes standby_idle and the only log label is Kokoro's",
      [p for p in _rsig if p.endswith("_idle")] == ["standby_idle"] and getattr(tf, "_LABELS", None) == {"kokoro": "Kokoro"},
      f"recover={list(_rsig)} labels={getattr(tf, '_LABELS', None)}")


# ── failover, recovery, switch notes ─────────────────────────────────
async def _failover():
    p, k = FakeSvc("el", "elevenlabs"), FakeSvc("kk", "kokoro")
    strat = ServiceSwitcherStrategyFailover([p, k])
    p.is_usable = False                      # pipecat flips this BEFORE the ErrorFrame
    err = ErrorFrame(error="websocket dead")
    err.processor = p
    new = await strat.handle_error(err)
    return new is k and strat.active_service is k


check("tts failover: primary unusable + ErrorFrame -> the Kokoro standby is active", asyncio.run(_failover()))


async def _recovery_one_switch():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False
    switches, sleeps = [], []

    async def req(svc):
        switches.append(svc)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            p.revive_ok = True               # revives on the 3rd probe

    ok = await tf.recover(p, req, 30.0, sleep=fake_sleep)
    return ok, switches, sleeps, p.revives


ok, sw, sl, rv = asyncio.run(_recovery_one_switch())
check("tts failover: recovery probes every cooldown", sl == [30.0, 30.0, 30.0], f"sleeps={sl}")
check("tts failover: recovery queues exactly ONE switch then stops",
      ok and len(sw) == 1 and rv == 3, f"sw={sw} rv={rv}")


async def _cancel():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False
    sw = types.SimpleNamespace(_lenoxvi_recovery=None)

    async def req(svc):
        pass

    task = asyncio.create_task(tf.recover(p, req, 30.0))
    sw._lenoxvi_recovery = task
    await asyncio.sleep(0)
    tf.cancel_recovery(sw)
    await asyncio.sleep(0)
    return sw._lenoxvi_recovery is None and task.cancelled()


check("tts failover: session end cancels the recovery task", asyncio.run(_cancel()))


async def _switch_notes():
    import dashboard.registry as reg
    notes = []
    orig = reg.note_tts
    reg.note_tts = lambda room, prov, ok: notes.append((room, prov, ok))
    try:
        p, k = FakeSvc("el", "elevenlabs"), FakeSvc("kk", "kokoro")
        queued = []

        async def q(f):
            queued.append(f)

        sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_room="Kitchen",
                                   _lenoxvi_queue=q, _lenoxvi_cooldown=30.0,
                                   _lenoxvi_recovery=None)
        await tf.on_switched(sw, k)
        started = sw._lenoxvi_recovery is not None
        tf.cancel_recovery(sw)
        await tf.on_switched(sw, p)
        return notes, started
    finally:
        reg.note_tts = orig


notes, started = asyncio.run(_switch_notes())
check("tts failover: switch to the standby notes 'kokoro (fallback)' + starts recovery",
      len(notes) >= 1 and notes[0] == ("Kitchen", "kokoro (fallback)", True) and started, f"notes={notes}")
check("tts failover: switch back notes the primary provider",
      len(notes) >= 2 and notes[1] == ("Kitchen", "elevenlabs", True), f"notes={notes}")


async def _request_frame():
    p, k = FakeSvc("el", "elevenlabs"), FakeSvc("kk", "kokoro")
    queued = []

    async def q(f):
        queued.append(f)

    sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_room=None, _lenoxvi_queue=q,
                               _lenoxvi_cooldown=0.0, _lenoxvi_recovery=None)
    p.is_usable = False
    p.revive_ok = True
    await tf.on_switched(sw, k)
    await asyncio.wait_for(sw._lenoxvi_recovery, 1.0)
    return queued


q = asyncio.run(_request_frame())
check("tts failover: recovery queues a ManuallySwitchServiceFrame for the primary",
      len(q) == 1 and isinstance(q[0], ManuallySwitchServiceFrame)
      and q[0].service._tts_provider == "elevenlabs", f"queued={q}")


async def _wrap_real():
    # Real ServiceSwitcher over two real TTSService instances (fake model):
    # order, strategy, and that the switch event reaches on_switched.
    import dashboard.registry as reg
    from pipecat.pipeline.service_switcher import ServiceSwitcher
    notes = []
    orig = reg.note_tts
    reg.note_tts = lambda room, prov, ok: notes.append((room, prov, ok))
    try:
        prim, stand = _kokoro("elevenlabs"), _kokoro()

        async def q(f):
            pass

        w = tf.wrap_with_fallback(prim, stand, room="Kitchen", queue_frame=q, cooldown=30.0)
        shape = (isinstance(w, ServiceSwitcher)
                 and isinstance(w.strategy, ServiceSwitcherStrategyFailover)
                 and w.services == [prim, stand] and w.strategy.active_service is prim)
        await w.strategy._call_event_handler("on_service_switched", stand)
        await asyncio.sleep(0.05)
        wired = ("Kitchen", "kokoro (fallback)", True) in notes
        tf.cancel_recovery(w)
        return shape, wired
    finally:
        reg.note_tts = orig


shape, wired = _run(_wrap_real, (False, False))
check("tts failover: wrap_with_fallback: real switcher, Failover, [primary, standby], primary active", shape)
check("tts failover: wrap_with_fallback: switch event reaches on_switched", wired)


# ── Kokoro as the standby ────────────────────────────────────────────
async def _kokoro_standby():
    import dashboard.registry as reg
    notes = []
    orig_note = reg.note_tts
    reg.note_tts = lambda room, prov, ok: notes.append((room, prov, ok))
    try:
        p, k = FakeSvc("el", "elevenlabs"), FakeSvc("kk", "kokoro")

        async def q(f):
            pass

        sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_room="Den", _lenoxvi_queue=q,
                                   _lenoxvi_cooldown=30.0, _lenoxvi_recovery=None,
                                   _lenoxvi_standby_name="kokoro")
        await tf.on_switched(sw, k)
        tf.cancel_recovery(sw)
        await tf.on_switched(sw, p)
        sw_n = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_room="Den", _lenoxvi_queue=q,
                                     _lenoxvi_cooldown=30.0, _lenoxvi_recovery=None)
        await tf.on_switched(sw_n, k)
        tf.cancel_recovery(sw_n)
        return notes
    finally:
        reg.note_tts = orig_note


notes = asyncio.run(_kokoro_standby())
check("tts failover: a Kokoro standby notes 'kokoro (fallback)', then the primary on switch back",
      notes[:2] == [("Den", "kokoro (fallback)", True), ("Den", "elevenlabs", True)], f"notes={notes}")
check("tts failover: no standby name = Kokoro (the one standby)",
      notes[2:3] == [("Den", "kokoro (fallback)", True)], f"notes={notes}")


async def _default_idle():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False
    p.revive_ok = True
    asked = []

    async def request(svc):
        asked.append(tk.GEN_LOCK.locked())

    async def tick(s):
        await asyncio.sleep(0.01)

    async with tk.GEN_LOCK:                  # Kokoro is speaking
        task = asyncio.create_task(tf.recover(p, request, 30.0, sleep=tick, reachable=None))
        await asyncio.sleep(0.3)
        during = list(asked)
    ok = await asyncio.wait_for(task, 2.0)
    return during, asked, ok


_di = _run(_default_idle, None)
check("tts failover: recover() waits on Kokoro's generation lock by default — no switch-back while Kokoro speaks",
      _di is not None and _di[0] == [] and _di[1] == [False] and _di[2] is True, f"{_di}")


async def _kokoro_idle_lock():
    free = tf._kokoro_idle()
    async with tk.GEN_LOCK:
        busy = tf._kokoro_idle()
    return free, busy


free, busy = asyncio.run(_kokoro_idle_lock())
check("tts failover: Kokoro counts as busy while it generates", free is True and busy is False)


async def _wrap_names():
    prim, stand = _kokoro("elevenlabs"), _kokoro()

    async def q(f):
        pass

    w = tf.wrap_with_fallback(prim, stand, room="Kitchen", queue_frame=q, cooldown=30.0, standby_name="kokoro")
    w2 = tf.wrap_with_fallback(prim, stand, room="Kitchen", queue_frame=q, cooldown=30.0)
    return w._lenoxvi_standby_name, w2._lenoxvi_standby_name


check("tts failover: wrap_with_fallback records the standby name (default kokoro)",
      _run(_wrap_names, None) == ("kokoro", "kokoro"))


# ── WAN-dead boot: a REAL ElevenLabs service pointed at a refused port, inside
# a real switcher, inside a real pipeline. The failed first connect must fail
# over to Kokoro, and the next speak must come out of Kokoro.
async def _wan_dead_boot(dialogue=False):
    from pipecat.frames.frames import EndFrame, TTSAudioRawFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.worker import PipelineParams, PipelineTask
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

    class Prim(Mixin, ElevenLabsTTSService):
        _tts_provider = "elevenlabs"

    class Sink(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.audio = 0

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSAudioRawFrame):
                self.audio += 1
            await self.push_frame(frame, direction)

    if dialogue:
        from pipecat.services.elevenlabs.dialogue.tts import (
            ElevenLabsDialogueTTSService,
        )

        class PrimDlg(Mixin, ElevenLabsDialogueTTSService):
            _tts_provider = "elevenlabs"

        prim = PrimDlg(api_key="suite-key-not-real", url="ws://127.0.0.1:9",
                       settings=ElevenLabsDialogueTTSService.Settings(
                           model="eleven_v4_turbo", voice="suite-voice"))
    else:
        prim = Prim(api_key="suite-key-not-real", voice_id="suite-voice", url="ws://127.0.0.1:9")
    stand = _kokoro()

    async def q(f):
        pass

    sw = tf.wrap_with_fallback(prim, stand, room=None, queue_frame=q, cooldown=30.0)
    sink = Sink()
    task = PipelineTask(Pipeline([sw, sink]), params=PipelineParams(), idle_timeout_secs=None)
    runner = PipelineRunner(handle_sigint=False)
    run = asyncio.create_task(runner.run(task))
    await asyncio.sleep(2.0)
    active_after_boot = sw.strategy.active_service
    await task.queue_frame(TTSSpeakFrame("hello from the standby"))
    await asyncio.sleep(1.5)
    await task.queue_frame(EndFrame())
    try:
        await asyncio.wait_for(run, 10)
    # Teardown: cancel whatever is left.
    except Exception:  # noqa: BLE001
        run.cancel()
    tf.close_fallback(sw)
    return active_after_boot is stand, sink.audio, prim.is_usable


_wd = _run(_wan_dead_boot, (False, 0, True))
check("tts failover: WAN-dead boot: a failed first connect fails over to Kokoro", _wd[0])
check("tts failover: WAN-dead boot: the next speak comes out of Kokoro", _wd[1] > 0, f"audio frames={_wd[1]}")
check("tts failover: WAN-dead boot: the primary is marked unusable", _wd[2] is False)


async def _wan_dead_boot_v4():
    return await _wan_dead_boot(dialogue=True)


_wd4 = _run(_wan_dead_boot_v4, (False, 0, True))
check("tts failover: v4 lane WAN-dead boot: fails over to Kokoro", _wd4[0])
check("tts failover: v4 lane WAN-dead boot: the next speak comes out of Kokoro", _wd4[1] > 0,
      f"audio frames={_wd4[1]}")
check("tts failover: v4 lane WAN-dead boot: the primary is marked unusable", _wd4[2] is False)


async def _escalation_guard():
    calls = []

    class P(FakeSvc):
        async def push_error(self, msg, *a, force_treat_as_permanent=False, **k):
            calls.append(force_treat_as_permanent)

    p, k = P("el", "elevenlabs"), FakeSvc("kk", "kokoro")
    sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_closed=False,
                               strategy=types.SimpleNamespace(active_service=k))
    await tf.on_primary_connection_error(sw, "refused")    # standby active: a recovery probe failing
    sw.strategy.active_service = p
    p.is_usable = False
    await tf.on_primary_connection_error(sw, "refused")    # already unusable
    p.is_usable = True
    await tf.on_primary_connection_error(sw, "refused")    # active + usable: escalate
    p._websocket = object()
    await tf.on_primary_connection_error(sw, "blip")       # live socket: pipecat's own reconnects handle it
    return calls


check("tts failover: a connection error escalates ONLY when the primary is active + usable",
      _run(_escalation_guard, None) == [True], "")


async def _waits_for_idle():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False
    p.revive_ok = True
    idle_checks, req = [], []

    def idle():
        idle_checks.append(1)
        return len(idle_checks) >= 3

    async def request(svc):
        req.append(len(idle_checks))

    async def fsleep(s):
        pass

    await tf.recover(p, request, 30.0, sleep=fsleep, reachable=None,
                     standby_idle=idle, is_active=lambda: True)
    return req


check("tts failover: switch-back waits until the standby is idle", _run(_waits_for_idle, None) == [3])


async def _refused_retries():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False
    p.revive_ok = True
    req = []
    active = {"v": False}

    async def request(svc):
        req.append(1)
        if len(req) == 2:
            active["v"] = True

    async def fsleep(s):
        pass

    ok = await tf.recover(p, request, 30.0, sleep=fsleep, reachable=None,
                          standby_idle=lambda: True, is_active=lambda: active["v"],
                          confirm_secs=0.05)
    return ok, len(req)


check("tts failover: a refused switch-back retries until the primary is really active",
      _run(_refused_retries, None) == (True, 2))


async def _closed_session():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False

    async def request(svc):
        pass

    async def fsleep(s):
        pass

    ok = await tf.recover(p, request, 30.0, sleep=fsleep, reachable=None, closed=lambda: True)
    sw = types.SimpleNamespace(_lenoxvi_recovery=None, _lenoxvi_closed=False)
    tf.close_fallback(sw)
    return ok, p.revives, sw._lenoxvi_closed


check("tts failover: a closed session: recovery exits without probing + close flag set",
      _run(_closed_session, None) == (False, 0, True))


async def _unreachable_host():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False
    n = {"i": 0}

    async def fsleep(s):
        n["i"] += 1
        if n["i"] > 5:
            raise asyncio.CancelledError

    async def unreachable():
        return False

    async def request(svc):
        pass

    try:
        await tf.recover(p, request, 30.0, sleep=fsleep, reachable=unreachable)
    except asyncio.CancelledError:
        pass
    return p.revives


check("tts failover: unreachable primary host: zero websocket probes (no log spam)", _run(_unreachable_host, None) == 0)
_pe = getattr(tf, "_primary_endpoint", lambda o: "missing")
check("tts failover: the probed endpoint comes from the service URL",
      _pe(types.SimpleNamespace(_url="wss://api.example.test")) == ("api.example.test", 443)
      and _pe(types.SimpleNamespace(_url="ws://127.0.0.1:9")) == ("127.0.0.1", 9)
      and _pe(types.SimpleNamespace()) is None)


async def _cooldown_clamp():
    prim, stand = _kokoro("elevenlabs"), _kokoro()

    async def q(f):
        pass

    return tf.wrap_with_fallback(prim, stand, room=None, queue_frame=q, cooldown=-5)._lenoxvi_cooldown


check("tts failover: a negative revive cooldown is clamped to >= 1 s", (_run(_cooldown_clamp, -1) or -1) >= 1.0)


async def _notes_suppressed():
    import dashboard.registry as reg
    orig = reg.note_tts
    reg.note_tts = lambda *a: None
    try:
        p, k = FakeSvc("el", "elevenlabs"), FakeSvc("kk", "kokoro")

        async def q(f):
            pass

        sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_room=None, _lenoxvi_queue=q,
                                   _lenoxvi_cooldown=30.0, _lenoxvi_recovery=None,
                                   _lenoxvi_closed=False,
                                   strategy=types.SimpleNamespace(active_service=k))
        await tf.on_switched(sw, k)
        during = getattr(p, "_tts_note_suppressed", None)
        tf.cancel_recovery(sw)
        await tf.on_switched(sw, p)
        after = getattr(p, "_tts_note_suppressed", None)
        return during, after
    finally:
        reg.note_tts = orig


check("tts failover: primary health notes suppressed while the standby speaks, restored after",
      _run(_notes_suppressed, None) == (True, False))

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
