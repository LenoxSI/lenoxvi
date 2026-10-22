"""Pocket TTS lane module pins (host venv; fake torch/pocket_tts).

Never loads real weights: fake `torch` and `pocket_tts` modules are put in
sys.modules BEFORE anything imports pipecat's pocket service.

Run: cd tests && ~/venvs/lenoxvi/bin/python pocket_test.py <path-to-core>
"""
import asyncio
import inspect
import sys
import types

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ── fakes: installed BEFORE anything imports pipecat's pocket module ──
LOADS = {"n": 0, "fail": False}


class _FakeTensor:
    def __init__(self, n):
        self.n = n
        self.shape = (n,)

    def clamp(self, *a):
        return self

    def __mul__(self, k):
        return self

    def to(self, *a):
        return self

    def numpy(self):
        return self

    def tobytes(self):
        return b"\x00\x00" * self.n


class FakeModel:
    sample_rate = 24000

    def get_state_for_audio_prompt(self, voice):
        return {"voice": voice}

    def generate_audio_stream(self, state, text, copy_state=True):
        yield _FakeTensor(240)


class FakeTTSModel:
    @staticmethod
    def load_model(**kw):
        LOADS["n"] += 1
        if LOADS["fail"]:
            raise OSError("no weights (WAN dead)")
        return FakeModel()


fake_torch = types.ModuleType("torch")
fake_torch.int16 = "int16"
fake_pt = types.ModuleType("pocket_tts")
fake_pt.TTSModel = FakeTTSModel
sys.modules["torch"] = fake_torch
sys.modules["pocket_tts"] = fake_pt

import tts_pocket as tp  # noqa: E402


class Mixin:  # stand-in for bot.TTSTracked (same attribute surface)
    _tts_provider = "x"
    _tts_room = None


check("pocket catalog: 21 English presets, alba first",
      len(tp.POCKET_VOICES) == 21 and tp.POCKET_VOICES[0] == "alba")
check("pocket: pocket_available() true with modules importable", tp.pocket_available() is True)
check("pocket: resolve_voice keeps a catalog voice", tp.resolve_voice("vera") == "vera")
check("pocket: resolve_voice unknown -> alba", tp.resolve_voice("not-a-voice") == "alba")
check("pocket: resolve_voice empty -> alba", tp.resolve_voice(None) == "alba")
check("pocket: resolve_voice passes a .wav path through",
      tp.resolve_voice("/data/voice.wav") == "/data/voice.wav")

# load failure: returns None/False, never raises (Review Focus 1)
LOADS["fail"] = True
check("pocket: preload failure returns False (never raises)", tp.preload("alba") is False)
_n_after_fail = LOADS["n"]
check("pocket: make_service returns None when the model can't load",
      tp.make_service(mixin=Mixin, voice="alba") is None)
# Gate 0 ruling: a WAN-dead load costs ~24 s — never retried per session
check("pocket: failed load is not retried per session",
      LOADS["n"] == _n_after_fail, f"loads {_n_after_fail} -> {LOADS['n']}")
LOADS["fail"] = False
tp._reset_for_tests()
LOADS["n"] = 0

# shared model: loaded once across two sessions
s1 = tp.make_service(mixin=Mixin, voice="alba")
s2 = tp.make_service(mixin=Mixin, voice="vera")
check("pocket: model loaded ONCE across two sessions", LOADS["n"] == 1, f"loads={LOADS['n']}")
check("pocket: both sessions share the model object",
      s1 is not None and s2 is not None and s1._model is s2._model)
check("pocket: service carries mixin + provider label",
      isinstance(s1, Mixin) and s1._tts_provider == "pocket")
check("pocket: voice state seeded from cache (no per-session derive)",
      s2 is not None and s2._voice_state == {"voice": "vera"})


# generation lock serializes (Review Focus 5)
async def _lock_probe():
    active = {"now": 0, "max": 0}
    from pipecat.services.pocket_tts.tts import PocketTTSService
    orig = PocketTTSService.run_tts

    async def slow(self, text, context_id):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        yield None

    PocketTTSService.run_tts = slow
    try:
        async def drain(s):
            async for _ in s.run_tts("hi", "c"):
                pass
        await asyncio.gather(drain(s1), drain(s2))
    finally:
        PocketTTSService.run_tts = orig
    return active["max"]


check("pocket: generation lock serializes two rooms", asyncio.run(_lock_probe()) == 1)


# a dead pocket must not try a websocket revive
async def _rev():
    await s1._try_revive()
    return True


check("pocket: pocket _try_revive is a no-op (no websocket)", asyncio.run(_rev()))

# drift pin: the pipecat internals this module relies on
from pipecat.services.pocket_tts.tts import PocketTTSService as _P  # noqa: E402
_src = inspect.getsource(_P)
check("pocket: pipecat internals: run_tts uses self._model + _get_voice_state",
      "self._model.generate_audio_stream" in _src and "_get_voice_state" in _src)

# ── Pocket fallback: switcher wrap + failover + recovery ──
import tts_fallback as tf  # noqa: E402
from pipecat.frames.frames import ErrorFrame, ManuallySwitchServiceFrame  # noqa: E402
from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover  # noqa: E402


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


async def _failover():
    p, k = FakeSvc("el", "elevenlabs"), FakeSvc("pk", "pocket")
    strat = ServiceSwitcherStrategyFailover([p, k])
    p.is_usable = False                      # pipecat flips this BEFORE the ErrorFrame
    err = ErrorFrame(error="websocket dead")
    err.processor = p
    new = await strat.handle_error(err)
    return new is k and strat.active_service is k


check("pocket: primary unusable + ErrorFrame -> Pocket active", asyncio.run(_failover()))


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
check("pocket: recovery probes every cooldown", sl == [30.0, 30.0, 30.0], f"sleeps={sl}")
check("pocket: recovery queues exactly ONE switch then stops",
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


check("pocket: session end cancels the recovery task", asyncio.run(_cancel()))


async def _switch_notes():
    import dashboard.registry as reg
    notes = []
    orig = reg.note_tts
    reg.note_tts = lambda room, prov, ok: notes.append((room, prov, ok))
    try:
        p, k = FakeSvc("el", "elevenlabs"), FakeSvc("pk", "pocket")
        queued = []

        async def q(f):
            queued.append(f)

        sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_room="kitchen",
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
check("pocket: switch to Pocket notes 'pocket (fallback)' + starts recovery",
      len(notes) >= 1 and notes[0] == ("kitchen", "pocket (fallback)", True) and started,
      f"notes={notes}")
check("pocket: switch back notes the primary provider",
      len(notes) >= 2 and notes[1] == ("kitchen", "elevenlabs", True), f"notes={notes}")


async def _request_frame():
    p, k = FakeSvc("el", "elevenlabs"), FakeSvc("pk", "pocket")
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
check("pocket: recovery queues a ManuallySwitchServiceFrame for the primary",
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
        prim = tp.make_service(mixin=Mixin, voice="alba", provider_label="elevenlabs")
        stand = tp.make_service(mixin=Mixin, voice="alba")

        async def q(f):
            pass

        w = tf.wrap_with_fallback(prim, stand, room="kitchen", queue_frame=q, cooldown=30.0)
        shape = (isinstance(w, ServiceSwitcher)
                 and isinstance(w.strategy, ServiceSwitcherStrategyFailover)
                 and w.services == [prim, stand] and w.strategy.active_service is prim)
        await w.strategy._call_event_handler("on_service_switched", stand)
        await asyncio.sleep(0.05)
        wired = ("kitchen", "pocket (fallback)", True) in notes
        tf.cancel_recovery(w)
        return shape, wired
    finally:
        reg.note_tts = orig


shape, wired = asyncio.run(_wrap_real())
check("pocket wrap_with_fallback: real switcher, Failover, [primary, pocket], primary active", shape)
check("pocket wrap_with_fallback: switch event reaches on_switched", wired)


# ── Pocket review fixes ──────────────────────────────────────────────────


def _run(coro_fn, default):
    """Run one async probe; an exception (e.g. API not there yet) = default."""
    try:
        return asyncio.run(coro_fn())
    except Exception as e:  # noqa: BLE001
        print(f"   (probe {coro_fn.__name__} raised {type(e).__name__}: {str(e)[:90]})")
        return default


# R1 (critical): WAN-dead boot. A REAL ElevenLabs service pointed at a refused
# port, inside a real switcher, inside a real pipeline: the failed first
# connect must fail over to Pocket and the next speak must come out of Pocket.
async def _wan_dead_boot(dialogue=False):
    from pipecat.frames.frames import EndFrame, TTSAudioRawFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.worker import PipelineTask, PipelineParams
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
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
        prim = Prim(api_key="suite-key-not-real", voice_id="suite-voice",
                    url="ws://127.0.0.1:9")
    stand = tp.make_service(mixin=Mixin, voice="alba")

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
    except Exception:
        run.cancel()
    getattr(tf, "close_fallback", tf.cancel_recovery)(sw)
    return active_after_boot is stand, sink.audio, prim.is_usable


_r1_active, _r1_audio, _r1_prim_usable = _run(_wan_dead_boot, (False, 0, True))
check("pocket: R1 WAN-dead boot: failed first connect fails over to Pocket", _r1_active)
check("pocket: R1 WAN-dead boot: the next speak comes out of Pocket", _r1_audio > 0, f"audio frames={_r1_audio}")
check("pocket: R1 WAN-dead boot: primary marked unusable", _r1_prim_usable is False)


# The same WAN-dead boot on the v4 Turbo lane (Text-to-Dialogue websocket).
async def _wan_dead_boot_v4():
    return await _wan_dead_boot(dialogue=True)


_r1d_active, _r1d_audio, _r1d_prim_usable = _run(_wan_dead_boot_v4, (False, 0, True))
check("pocket: R1 v4 lane WAN-dead boot: fails over to Pocket", _r1d_active)
check("pocket: R1 v4 lane WAN-dead boot: the next speak comes out of Pocket",
      _r1d_audio > 0, f"audio frames={_r1d_audio}")
check("pocket: R1 v4 lane WAN-dead boot: primary marked unusable", _r1d_prim_usable is False)


async def _r1_guard():
    calls = []

    class P(FakeSvc):
        async def push_error(self, msg, *a, force_treat_as_permanent=False, **k):
            calls.append(force_treat_as_permanent)

    p, k = P("el", "elevenlabs"), FakeSvc("pk", "pocket")
    sw = types.SimpleNamespace(_lenoxvi_primary=p, _lenoxvi_closed=False,
                               strategy=types.SimpleNamespace(active_service=k))
    await tf.on_primary_connection_error(sw, "refused")    # Pocket active: a probe failing
    sw.strategy.active_service = p
    p.is_usable = False
    await tf.on_primary_connection_error(sw, "refused")    # already unusable
    p.is_usable = True
    await tf.on_primary_connection_error(sw, "refused")    # active + usable: escalate
    p._websocket = object()
    await tf.on_primary_connection_error(sw, "blip")       # live socket: pipecat's own reconnects handle it
    return calls


check("pocket: R1 connection error escalates ONLY when primary is active + usable",
      _run(_r1_guard, None) == [True], "")


# R3 (minor): no switch-back while Pocket is still generating.
async def _r3():
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
                     pocket_idle=idle, is_active=lambda: True)
    return req


check("pocket: R3 switch-back waits until Pocket is idle", _run(_r3, None) == [3])


# R4 (minor): a refused switch keeps recovery going.
async def _r4():
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
                          pocket_idle=lambda: True, is_active=lambda: active["v"],
                          confirm_secs=0.05)
    return ok, len(req)


check("pocket: R4 refused switch-back retries until the primary is really active",
      _run(_r4, None) == (True, 2))


# R5 (minor): a closed session never probes.
async def _r5():
    p = FakeSvc("el", "elevenlabs")
    p.is_usable = False

    async def request(svc):
        pass

    async def fsleep(s):
        pass

    ok = await tf.recover(p, request, 30.0, sleep=fsleep, reachable=None,
                          closed=lambda: True)
    sw = types.SimpleNamespace(_lenoxvi_recovery=None, _lenoxvi_closed=False)
    tf.close_fallback(sw)
    return ok, p.revives, sw._lenoxvi_closed


check("pocket: R5 closed session: recovery exits without probing + close flag set",
      _run(_r5, None) == (False, 0, True))


# R7 (minor): while the primary's host is unreachable, no websocket probes.
async def _r7():
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


check("pocket: R7 unreachable primary host: zero websocket probes (no log spam)", _run(_r7, None) == 0)
_pe = getattr(tf, "_primary_endpoint", lambda o: "missing")
check("pocket: R7 endpoint derived from the service URL",
      _pe(types.SimpleNamespace(_url="wss://api.example.test")) == ("api.example.test", 443)
      and _pe(types.SimpleNamespace(_url="ws://127.0.0.1:9")) == ("127.0.0.1", 9)
      and _pe(types.SimpleNamespace()) is None)


# R9 (minor): cooldown clamped.
async def _r9_real():
    prim = tp.make_service(mixin=Mixin, voice="alba", provider_label="elevenlabs")
    stand = tp.make_service(mixin=Mixin, voice="alba")

    async def q(f):
        pass

    return tf.wrap_with_fallback(prim, stand, room=None, queue_frame=q, cooldown=-5)._lenoxvi_cooldown


check("pocket: R9 negative revive cooldown clamped to >= 1 s", (_run(_r9_real, -1) or -1) >= 1.0)


# R10 (minor): primary health notes suppressed while Pocket is active.
async def _r10():
    import dashboard.registry as reg
    orig = reg.note_tts
    reg.note_tts = lambda *a: None
    try:
        p, k = FakeSvc("el", "elevenlabs"), FakeSvc("pk", "pocket")

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


check("pocket: R10 primary health notes suppressed during fallback, restored after",
      _run(_r10, None) == (True, False))


# R6 (minor): a bad clone voice falls back to the default, Pocket stays up.
class _BadVoiceModel(FakeModel):
    def get_state_for_audio_prompt(self, voice):
        if str(voice).endswith(".wav"):
            raise FileNotFoundError(voice)
        return {"voice": voice}


_orig_load = FakeTTSModel.load_model
FakeTTSModel.load_model = staticmethod(lambda **kw: _BadVoiceModel())
tp._reset_for_tests()
try:
    _r6 = tp.make_service(mixin=Mixin, voice="/missing/clone.wav")
except Exception:
    _r6 = None
check("pocket: R6 bad .wav voice: Pocket still loads, falls back to alba",
      _r6 is not None and _r6._voice_state == {"voice": "alba"} and tp._load_failed is False,
      f"svc={_r6} failed={tp._load_failed}")
FakeTTSModel.load_model = _orig_load
tp._reset_for_tests()


# warm(): the model loads in a thread (session setup runs on the event loop;
# a load there stalls every room), and two rooms warming at once load ONCE.
import asyncio as _aio_w
import time as _t_w

tp._reset_for_tests()
LOADS["n"] = 0
_orig_load_w = FakeTTSModel.load_model


def _slow_load_w(**kw):
    _t_w.sleep(0.4)
    return _orig_load_w(**kw)


FakeTTSModel.load_model = staticmethod(_slow_load_w)


async def _warm_probe():
    gaps, state = [], {"stop": False}

    async def ticker():
        last = _t_w.monotonic()
        while not state["stop"]:
            await _aio_w.sleep(0.02)
            now = _t_w.monotonic()
            gaps.append(now - last)
            last = now

    t = _aio_w.create_task(ticker())
    await _aio_w.sleep(0.05)
    warm = getattr(tp, "warm", None)
    if warm is None:  # pre-fix shape: the load ran inline on the loop

        async def warm(v):
            return tp.preload(v)
    res = await _aio_w.gather(warm("alba"), warm("alba"))
    state["stop"] = True
    await t
    return res, max(gaps) if gaps else None


_w_res, _w_gap = _aio_w.run(_warm_probe())
FakeTTSModel.load_model = staticmethod(_orig_load_w)
check("pocket: warm() keeps the event loop responsive during a model load",
      _w_gap is not None and _w_gap < 0.15, f"max loop gap={_w_gap}")
check("pocket: two concurrent warms load the model once",
      _w_res == [True, True] and LOADS["n"] == 1, f"res={_w_res} loads={LOADS['n']}")

# R8 (minor): repeated warnings log once per process.
_r8_lines = []
from loguru import logger as _lg  # noqa: E402
_sink = _lg.add(lambda m: _r8_lines.append(str(m)), level="WARNING")
for _ in range(2):
    getattr(tp, "warn_once", lambda k, m: None)("suite-key", "suite warning")
_lg.remove(_sink)
check("pocket: R8 warn_once logs a repeated warning once", sum("suite warning" in s for s in _r8_lines) == 1)

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
