"""050 — Pocket TTS lane module pins (host venv; fake torch/pocket_tts).

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


check("050 catalog: 21 English presets, alba first",
      len(tp.POCKET_VOICES) == 21 and tp.POCKET_VOICES[0] == "alba")
check("050 pocket_available() true with modules importable", tp.pocket_available() is True)
check("050 resolve_voice keeps a catalog voice", tp.resolve_voice("vera") == "vera")
check("050 resolve_voice unknown -> alba", tp.resolve_voice("not-a-voice") == "alba")
check("050 resolve_voice empty -> alba", tp.resolve_voice(None) == "alba")
check("050 resolve_voice passes a .wav path through",
      tp.resolve_voice("/data/voice.wav") == "/data/voice.wav")

# load failure: returns None/False, never raises (Review Focus 1)
LOADS["fail"] = True
check("050 preload failure returns False (never raises)", tp.preload("alba") is False)
_n_after_fail = LOADS["n"]
check("050 make_service returns None when the model can't load",
      tp.make_service(mixin=Mixin, voice="alba") is None)
# Gate 0 ruling: a WAN-dead load costs ~24 s — never retried per session
check("050 failed load is not retried per session",
      LOADS["n"] == _n_after_fail, f"loads {_n_after_fail} -> {LOADS['n']}")
LOADS["fail"] = False
tp._reset_for_tests()
LOADS["n"] = 0

# shared model: loaded once across two sessions
s1 = tp.make_service(mixin=Mixin, voice="alba")
s2 = tp.make_service(mixin=Mixin, voice="vera")
check("050 model loaded ONCE across two sessions", LOADS["n"] == 1, f"loads={LOADS['n']}")
check("050 both sessions share the model object",
      s1 is not None and s2 is not None and s1._model is s2._model)
check("050 service carries mixin + provider label",
      isinstance(s1, Mixin) and s1._tts_provider == "pocket")
check("050 voice state seeded from cache (no per-session derive)",
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


check("050 generation lock serializes two rooms", asyncio.run(_lock_probe()) == 1)


# a dead pocket must not try a websocket revive
async def _rev():
    await s1._try_revive()
    return True


check("050 pocket _try_revive is a no-op (no websocket)", asyncio.run(_rev()))

# drift pin: the pipecat internals this module relies on
from pipecat.services.pocket_tts.tts import PocketTTSService as _P  # noqa: E402
_src = inspect.getsource(_P)
check("050 pipecat internals: run_tts uses self._model + _get_voice_state",
      "self._model.generate_audio_stream" in _src and "_get_voice_state" in _src)

# ── 050 fallback: switcher wrap + failover + recovery ──
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


check("050 primary unusable + ErrorFrame -> Pocket active", asyncio.run(_failover()))


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
check("050 recovery probes every cooldown", sl == [30.0, 30.0, 30.0], f"sleeps={sl}")
check("050 recovery queues exactly ONE switch then stops",
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


check("050 session end cancels the recovery task", asyncio.run(_cancel()))


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
check("050 switch to Pocket notes 'pocket (fallback)' + starts recovery",
      len(notes) >= 1 and notes[0] == ("kitchen", "pocket (fallback)", True) and started,
      f"notes={notes}")
check("050 switch back notes the primary provider",
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
check("050 recovery queues a ManuallySwitchServiceFrame for the primary",
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
check("050 wrap_with_fallback: real switcher, Failover, [primary, pocket], primary active", shape)
check("050 wrap_with_fallback: switch event reaches on_switched", wired)

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
