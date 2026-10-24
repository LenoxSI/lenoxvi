"""Cloud STT provider + Whisper fallback pins (host venv; pipecat + deepgram-sdk).

Run: cd tests && ~/venvs/lenoxvi/bin/python stt_fallback_test.py <path-to-core>
The failover proof uses a REAL DeepgramSTTService inside a REAL PipelineTask
(pointed at a dead local port, and at Deepgram with a junk key) — the TTS
fallback taught that reading the code isn't proof. ElevenLabs Scribe gets the
same proof against dead local ports and a local fake server only.
"""
import asyncio
import os
import socket
import sys
import time

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import stt_catalog as cat

# ── catalog ──────────────────────────────────────────────────────────
check("provider catalog: Local — Whisper first, then Deepgram Nova-3 (cloud), then ElevenLabs Scribe v2 (cloud)",
      [(p["name"], p["label"]) for p in getattr(cat, "PROVIDERS", [])]
      == [("whisper", "Local — Whisper"), ("deepgram", "Deepgram Nova-3 (cloud)"),
          ("elevenlabs", "ElevenLabs Scribe v2 (cloud)")],
      str(getattr(cat, "PROVIDERS", None)))
check("deepgram language: auto -> multi, a code passes through, empty -> multi",
      getattr(cat, "deepgram_language", lambda c: None)("auto") == "multi"
      and cat.deepgram_language("en") == "en" and cat.deepgram_language("ko") == "ko"
      and cat.deepgram_language(None) == "multi")
check("validate_provider: known ok, unknown refused",
      getattr(cat, "validate_provider", lambda p: "x")("deepgram") is None
      and "provider" in (cat.validate_provider("grok") or ""))

import stt_fallback as sf
import stt_local as sl

# ── make_deepgram ────────────────────────────────────────────────────
_saved_key = os.environ.pop("DEEPGRAM_API_KEY", None)
svc, why = sf.make_deepgram("auto")
check("make_deepgram without a key: no service, reason names the key",
      svc is None and "DEEPGRAM_API_KEY" in (why or ""), str(why))
os.environ["DEEPGRAM_API_KEY"] = "junk-test-key"
svc, why = sf.make_deepgram("fr")
check("make_deepgram with a key: a DeepgramSTTService on nova-3, language mapped",
      svc is not None and type(svc).__name__ == "DeepgramSTTService"
      and str(svc._settings.model) == "nova-3" and "fr" in str(svc._settings.language).lower(), f"{svc} {why}")

bad = [lang["code"] for lang in cat.LANGUAGES if sf.make_deepgram(lang["code"])[0] is None]
check("make_deepgram: every card language builds a service (auto -> multi)", not bad, str(bad))
import pipecat.services.deepgram.stt as _dgmod

_real_cls = _dgmod.DeepgramSTTService


class _Boom(_real_cls):
    def __init__(self, *a, **k):
        raise TypeError("client API changed")


_dgmod.DeepgramSTTService = _Boom
try:
    svc, why = sf.make_deepgram("en")
finally:
    _dgmod.DeepgramSTTService = _real_cls
check("make_deepgram never raises: a constructor failure -> (None, reason)",
      svc is None and "could not be built" in (why or "") and "TypeError" in why, str(why))

# ── redaction ────────────────────────────────────────────────────────
red = sf.redact("Deepgram rejected (status 401): bad key junk-test-key; Authorization: Token abc123SECRET; Bearer xyz.789")
check("redact: the key by value and any Token/Bearer value never survive",
      "junk-test-key" not in red and "abc123SECRET" not in red and "xyz.789" not in red, red)
check("redact: headers tail dropped, length capped",
      sf.redact("x: headers: {'Authorization': 'Token q'}") == "x" and len(sf.redact("y" * 999)) == 160)

# ── stt_local latency modes ──────────────────────────────────────────
sl._reset_status_for_tests()
sl.set_latency_mode("e2e")
sl.note_latency(420.0)
sl.note_latency(380.0)
st = sl.status()
check("e2e latency mode: note_latency feeds the median", st.get("samples") == 2 and st.get("latency_ms_median") == 400, str(st))
check("latency mode is reported", sl.latency_mode() == "e2e")
sl.set_latency_mode("compute")
sl._reset_status_for_tests()

# ── SttTimer: VAD stop -> final transcript ───────────────────────────
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)

timer = sf.SttTimer()
check("timer: a final transcript with no VAD stop records nothing",
      timer.observe(TranscriptionFrame("hi", "", "0"), now=1.0) is None)
timer.observe(VADUserStoppedSpeakingFrame(), now=10.0)
check("timer: interim transcripts don't stop the clock",
      timer.observe(InterimTranscriptionFrame("hel", "", "0"), now=10.1) is None)
ms = timer.observe(TranscriptionFrame("hello", "", "0"), now=10.35)
check("timer: VAD stop -> final = 350 ms", ms is not None and abs(ms - 350) < 1, str(ms))
check("timer: one measurement per stop", timer.observe(TranscriptionFrame("again", "", "0"), now=11.0) is None)
from pipecat.frames.frames import VADUserStartedSpeakingFrame

timer.observe(VADUserStoppedSpeakingFrame(), now=20.0)
timer.observe(VADUserStartedSpeakingFrame(), now=21.0)
check("timer: a stop that produced no transcript doesn't time the next turn",
      timer.observe(TranscriptionFrame("mid-speech final", "", "0"), now=80.0) is None)
timer.observe(VADUserStoppedSpeakingFrame(), now=100.0)
check("timer: over 10 s is a missed turn, not a sample", timer.observe(TranscriptionFrame("late", "", "0"), now=112.0) is None)

# ── status aggregation ───────────────────────────────────────────────
sf._reset_for_tests()
sf.set_provider("deepgram")
sf.note_active("kitchen", "deepgram")
sf.note_active("den", "deepgram")
s1 = sf.status()
sf.note_active("den", "whisper", reason="connection failed to stay up 3 times in a row")
s2 = sf.status()
sf.forget("den")
s3 = sf.status()
_old, _new = object(), object()
sf.note_active("hall", "deepgram", owner=_old)
sf.note_active("hall", "whisper", reason="down", owner=_new)
sf.forget("hall", owner=_old)
check("status: an old session's close doesn't wipe the new session's entry (node reconnect race)",
      sf.status().get("fallback_rooms") == ["hall"], str(sf.status()))
sf.forget("hall", owner=_new)
check("status: all rooms on Deepgram -> active deepgram, model nova-3",
      s1.get("provider") == "deepgram" and s1.get("active") == "deepgram" and s1.get("cloud_model") == "nova-3", str(s1))
check("status: any room on the fallback -> active whisper + reason + rooms",
      s2.get("active") == "whisper" and "3 times" in (s2.get("fallback_reason") or "") and s2.get("fallback_rooms") == ["den"],
      str(s2))
check("status: a closed session is forgotten", s3.get("active") == "deepgram", str(s3))
sf.set_provider("whisper")
check("status: provider whisper -> no cloud fields", "active" not in sf.status() and sf.status().get("provider") == "whisper",
      str(sf.status()))
sf.set_provider("deepgram")
sf.note_unavailable("DEEPGRAM_API_KEY is not set")
s4 = sf.status()
check("status: provider deepgram but unusable at build -> active whisper + reason",
      s4.get("active") == "whisper" and "DEEPGRAM_API_KEY" in (s4.get("fallback_reason") or ""), str(s4))
sf._reset_for_tests()


# ── recover_stt with fakes ───────────────────────────────────────────
class _FakeDG:
    def __init__(self, ok_after):
        self.calls, self.ok_after, self._usable = 0, ok_after, False

    async def _do_reconnect(self):
        self.calls += 1
        if self.calls < self.ok_after:
            raise ConnectionError("still down")

    async def set_usable(self, v):
        self._usable = v

    @property
    def is_usable(self):
        return self._usable


async def _rec():
    dg = _FakeDG(ok_after=2)
    switched, reach = [], iter([False, True, True, True])
    active = {"v": False}

    async def req(s):
        switched.append(s)
        active["v"] = True

    async def reachable():
        return next(reach)
    ok = await sf.recover_stt(dg, req, 0.01, reachable=reachable, is_active=lambda: active["v"])
    return ok, dg, switched

ok, dg, switched = asyncio.run(_rec())
check("recover: waits for reachability, retries the reconnect, marks usable, switches back once",
      ok is True and dg.calls == 2 and dg.is_usable and switched == [dg], f"{ok} {dg.calls} {switched}")


async def _rec_closed():
    dg = _FakeDG(ok_after=99)
    closed = {"v": False}

    async def req(s):
        pass

    async def reachable():
        closed["v"] = True
        return False
    return await sf.recover_stt(dg, req, 0.01, reachable=reachable, closed=lambda: closed["v"])

check("recover: stops when the session closes", asyncio.run(_rec_closed()) is False)

# ── REAL failover proof ──────────────────────────────────────────────
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.stt_service import STTService


class _FakeWhisper(STTService):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.audio_chunks = 0

    async def run_stt(self, audio):
        self.audio_chunks += 1
        yield TranscriptionFrame("fallback", "", "0")


def _dns_ok(host):
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


async def _failover(dg, budget, provider="deepgram", feed=False):
    """feed: audio every 0.1 s while waiting (a room that is awake) — a
    service that connects on demand can't fail before it hears something."""
    whisper = _FakeWhisper()
    task_ref = {}

    async def queue(frame):
        await task_ref["task"].queue_frame(frame)
    sf._reset_for_tests()
    sf.set_provider(provider)
    kw = {} if provider == "deepgram" else {"provider": provider}
    switcher = sf.wrap_stt_with_fallback(dg, whisper, room="proof", queue_frame=queue, cooldown=3600, **kw)
    task = PipelineTask(Pipeline([switcher]), params=PipelineParams(), cancel_on_idle_timeout=False)
    task_ref["task"] = task
    runner = PipelineRunner(handle_sigint=False)
    run = asyncio.create_task(runner.run(task))
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget and switcher.strategy.active_service is not whisper:
        if feed:
            await task.queue_frame(_audio())
        await asyncio.sleep(0.1)
    took = time.monotonic() - t0
    landed = switcher.strategy.active_service is whisper
    st = sf.status()
    sf.close_stt_fallback(switcher)
    await task.queue_frame(EndFrame())
    try:
        await asyncio.wait_for(run, 10)
    except Exception:  # noqa: BLE001 — teardown: cancel whatever is left
        await task.cancel()
    return landed, took, st

from pipecat.services.deepgram.stt import DeepgramSTTService

dead = DeepgramSTTService(api_key="junk", base_url="ws://127.0.0.1:9",
                          settings=DeepgramSTTService.Settings(model="nova-3", language="en"))
landed, took, st = asyncio.run(_failover(dead, 40))
check("REAL: Deepgram unreachable (dead port) -> the switcher lands on Whisper",
      landed, f"after {took:.1f}s")
check("REAL: the fallback is visible in status (active whisper + reason, room listed)",
      st.get("active") == "whisper" and st.get("fallback_rooms") == ["proof"] and st.get("fallback_reason"), str(st))
print(f"    (dead-port failover took {took:.1f}s)")

if _dns_ok("api.deepgram.com"):
    badkey = DeepgramSTTService(api_key="definitely-not-a-key",
                                settings=DeepgramSTTService.Settings(model="nova-3", language="en"))
    landed, took, st = asyncio.run(_failover(badkey, 20))
    check("REAL: a rejected key (4xx from Deepgram) -> Whisper, fast", landed and took < 10, f"{landed} after {took:.1f}s")
    print(f"    (bad-key failover took {took:.1f}s)")
else:
    print("[SKIP] bad-key failover: no DNS for api.deepgram.com")

# ── REAL: a key that stays bad -> recovery keeps Whisper (no flapping) ──
from pipecat.frames.frames import InputAudioRawFrame


def _audio():
    return InputAudioRawFrame(audio=b"\x00\x00" * 320, sample_rate=16000, num_channels=1)


async def _run(dg, cooldown, steps, provider="deepgram"):
    whisper = _FakeWhisper()
    task_ref = {}

    async def queue(frame):
        await task_ref["task"].queue_frame(frame)
    sf._reset_for_tests()
    sf.set_provider(provider)
    kw = {} if provider == "deepgram" else {"provider": provider}
    switcher = sf.wrap_stt_with_fallback(dg, whisper, room="proof", queue_frame=queue, cooldown=cooldown, **kw)
    task = PipelineTask(Pipeline([switcher]), params=PipelineParams(), cancel_on_idle_timeout=False)
    task_ref["task"] = task
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))

    async def until(cond, budget):
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget and not cond():
            await asyncio.sleep(0.1)
        return cond()
    try:
        return await steps(switcher, whisper, task, until)
    finally:
        sf.close_stt_fallback(switcher)
        await task.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(run, 10)
        except Exception:  # noqa: BLE001 — teardown
            await task.cancel()


if _dns_ok("api.deepgram.com"):
    async def _stays(switcher, whisper, task, until):
        landed = await until(lambda: switcher.strategy.active_service is whisper, 15)
        await asyncio.sleep(4.5)          # >= 3 recovery rounds at the 1 s floor
        return landed, switcher.strategy.active_service is whisper, sf.status()
    badkey = DeepgramSTTService(api_key="definitely-not-a-key",
                                settings=DeepgramSTTService.Settings(model="nova-3", language="en"))
    landed, still, st = asyncio.run(_run(badkey, 1, _stays))
    check("REAL: a key that stays bad -> recovery rounds keep the session on Whisper (no flapping)",
          landed and still and st.get("active") == "whisper", f"{landed} {still} {st}")
    check("REAL: the status reason never contains the key", "definitely-not-a-key" not in str(st), str(st))
else:
    print("[SKIP] bad-key recovery: no DNS for api.deepgram.com")

# ── REAL: down -> Whisper hears; back -> switch back, ONLY Deepgram hears ──
import websockets

_srv_sock = socket.socket()
_srv_sock.bind(("127.0.0.1", 0))
PORT = _srv_sock.getsockname()[1]
_srv_sock.close()
RECV = {"binary": 0, "conns": 0}


async def _ws_handler(ws):
    RECV["conns"] += 1
    async for msg in ws:
        if isinstance(msg, (bytes, bytearray)):
            RECV["binary"] += 1


async def _cycle(switcher, whisper, task, until):
    dg = switcher._lenoxvi_primary
    r = {}
    r["failed_over"] = await until(lambda: switcher.strategy.active_service is whisper, 40)
    for _ in range(5):
        await task.queue_frame(_audio())
    await until(lambda: whisper.audio_chunks >= 5, 5)
    r["whisper_heard_while_down"] = whisper.audio_chunks
    server = await websockets.serve(_ws_handler, "127.0.0.1", PORT)
    try:
        r["switched_back"] = await until(lambda: switcher.strategy.active_service is dg, 20)
        await asyncio.sleep(0.5)
        r["status_after"] = sf.status()
        before_w, before_dg = whisper.audio_chunks, RECV["binary"]
        for _ in range(5):
            await task.queue_frame(_audio())
        await until(lambda: RECV["binary"] >= before_dg + 5, 5)
        await asyncio.sleep(0.3)
        r["whisper_heard_after"] = whisper.audio_chunks - before_w
        r["deepgram_heard_after"] = RECV["binary"] - before_dg
    finally:
        server.close()
    return r

local = DeepgramSTTService(api_key="junk", base_url=f"ws://127.0.0.1:{PORT}",
                           settings=DeepgramSTTService.Settings(model="nova-3", language="en"))
r = asyncio.run(_run(local, 1, _cycle))
check("REAL cycle: fake Deepgram down -> Whisper, and Whisper gets the audio",
      r.get("failed_over") and r.get("whisper_heard_while_down", 0) >= 5, str(r))
check("REAL cycle: fake Deepgram up -> recovery switches back, status says deepgram",
      r.get("switched_back") and (r.get("status_after") or {}).get("active") == "deepgram", str(r))
check("REAL cycle: after the switch back ONLY Deepgram gets audio (no double transcripts)",
      r.get("deepgram_heard_after", 0) >= 5 and r.get("whisper_heard_after") == 0, str(r))
print(f"    (cycle: {r})")

# ── ElevenLabs Scribe: same switcher, same recovery, the TTS key ─────
# Never the real ElevenLabs service: dead local ports and a local fake
# server only, and the recovery probe is replaced (no outbound TCP).
import json
import re
import types

from websockets.protocol import State

_saved_el = os.environ.pop("ELEVENLABS_API_KEY", None)
_saved_probes = (getattr(sf, "_probe_elevenlabs", None), sf._probe_deepgram)
_elmod = None
try:
    check("elevenlabs catalog: Scribe model scribe_v2_realtime; elevenlabs is a valid cloud provider",
          getattr(cat, "ELEVENLABS_MODEL", None) == "scribe_v2_realtime" and cat.validate_provider("elevenlabs") is None
          and getattr(cat, "is_cloud", lambda p: None)("elevenlabs") is True and cat.is_cloud("deepgram") is True
          and cat.is_cloud("whisper") is False and cat.is_cloud("grok") is False, str(getattr(cat, "PROVIDERS", None)))
    check("elevenlabs language: auto or empty -> None (Scribe detects it), a code passes through to pipecat's mapping",
          getattr(cat, "elevenlabs_language", lambda c: "x")("auto") is None and cat.elevenlabs_language(None) is None
          and cat.elevenlabs_language("") is None and cat.elevenlabs_language("de") == "de")

    svc, why = getattr(sf, "make_elevenlabs", lambda lang: ("missing", None))("auto")
    check("make_elevenlabs without a key: no service, reason names the key",
          svc is None and "ELEVENLABS_API_KEY" in (why or ""), f"{svc} {why}")
    os.environ["ELEVENLABS_API_KEY"] = "el-junk-test-key"
    import pipecat.services.elevenlabs.stt as _elmod

    svc, why = sf.make_elevenlabs("fr")
    check("make_elevenlabs with a key: the on-demand realtime service on scribe_v2_realtime, French mapped to fra",
          isinstance(svc, _elmod.ElevenLabsRealtimeSTTService) and type(svc).__name__ == "OnDemandElevenLabsSTTService"
          and str(svc._settings.model) == "scribe_v2_realtime" and str(svc._settings.language) == "fra", f"{svc} {why}")
    check("make_elevenlabs: pipecat's keepalive is off (Scribe bills its silence as audio)",
          svc is not None and svc._keepalive_timeout is None, str(getattr(svc, "_keepalive_timeout", None)))
    # The class make_elevenlabs builds, and where it looks it up: the fakes
    # below swap it there. stt_elevenlabs holds the on-demand tunables.
    _od_cls = type(svc)
    _od_mod = sys.modules[_od_cls.__module__]
    _sel = sys.modules.get("stt_elevenlabs")
    _sel_saved = {k: getattr(_sel, k) for k in ("IDLE_CLOSE_SECS", "HOLD_SECS", "CONNECT_TIMEOUT_SECS",
                                                  "SERVER_IDLE_SECS") if hasattr(_sel, k)} if _sel else {}

    def _tune(**kw):
        for k, v in kw.items():
            if _sel is not None:
                setattr(_sel, k, v)
    check("make_elevenlabs: MANUAL commits (our VAD ends the turn) and logging never requested",
          svc is not None and svc._commit_strategy == _elmod.CommitStrategy.MANUAL and svc._enable_logging is False,
          f"{getattr(svc, '_commit_strategy', None)} {getattr(svc, '_enable_logging', None)}")
    svc, why = sf.make_elevenlabs("auto")
    check("make_elevenlabs: Auto-detect sends no language", svc is not None and svc._settings.language is None,
          f"{getattr(getattr(svc, '_settings', None), 'language', None)} {why}")
    _el_langs = {lang["code"]: sf.make_elevenlabs(lang["code"])[0] for lang in cat.LANGUAGES if lang["code"] != "auto"}
    check("make_elevenlabs: every card language builds and maps to ElevenLabs' 3-letter code",
          all(s is not None and len(str(s._settings.language)) == 3 for s in _el_langs.values()),
          str({c: getattr(getattr(s, "_settings", None), "language", None) for c, s in _el_langs.items()}))
    check("make_cloud: elevenlabs builds Scribe on demand; deepgram still builds Nova-3 (still holds)",
          type(getattr(sf, "make_cloud", lambda p, lang: (None, None))("elevenlabs", "en")[0]).__name__
          == "OnDemandElevenLabsSTTService"
          and type(sf.make_cloud("deepgram", "en")[0]).__name__ == "DeepgramSTTService")

    class _ElBoom(_od_cls):
        def __init__(self, *a, **k):
            raise TypeError("client API changed")

    setattr(_od_mod, _od_cls.__name__, _ElBoom)
    try:
        svc, why = sf.make_elevenlabs("en")
    finally:
        setattr(_od_mod, _od_cls.__name__, _od_cls)
    check("make_elevenlabs never raises: a constructor failure -> (None, reason)",
          svc is None and "could not be built" in (why or "") and "TypeError" in why, str(why))
    _prev_mod = sys.modules.get("pipecat.services.elevenlabs.stt")
    sys.modules["pipecat.services.elevenlabs.stt"] = None   # what a missing module looks like to import
    try:
        svc, why = sf.make_elevenlabs("en")
    finally:
        sys.modules["pipecat.services.elevenlabs.stt"] = _prev_mod
    check("make_elevenlabs: the service missing from this pipecat -> (None, reason), never a crash",
          svc is None and "not available" in (why or ""), str(why))

    red = sf.redact("Error: invalid xi-api-key el-junk-test-key; deepgram junk-test-key")
    check("redact: the ElevenLabs key by value never survives (and the Deepgram key still doesn't)",
          "el-junk-test-key" not in red and "junk-test-key" not in red, red)

    sf._reset_for_tests()
    sf.set_provider("elevenlabs")
    sf.note_active("kitchen", "elevenlabs")
    e1 = sf.status()
    sf.note_active("kitchen", "whisper")
    e2 = sf.status()
    sf.forget("kitchen")
    sf.note_unavailable("ELEVENLABS_API_KEY is not set")
    e3 = sf.status()
    sf._reset_for_tests()
    check("status: on Scribe -> active elevenlabs, model scribe_v2_realtime",
          e1.get("provider") == "elevenlabs" and e1.get("active") == "elevenlabs"
          and e1.get("cloud_model") == "scribe_v2_realtime", str(e1))
    check("status: a room on the fallback -> active whisper, default reason names ElevenLabs",
          e2.get("active") == "whisper" and e2.get("fallback_reason") == "ElevenLabs unreachable"
          and e2.get("fallback_rooms") == ["kitchen"], str(e2))
    check("status: provider elevenlabs but unusable at build -> active whisper + reason",
          e3.get("active") == "whisper" and "ELEVENLABS_API_KEY" in (e3.get("fallback_reason") or ""), str(e3))

    # A websocket service can accept the handshake and hang up at once (a
    # rejected key answered with an error message): never switch back to that.
    class _FakeWS:
        def __init__(self, hangs_up):
            self.calls, self.hangs_up, self._usable, self._websocket = 0, hangs_up, False, None

        async def _do_reconnect(self):
            self.calls += 1
            self._websocket = types.SimpleNamespace(state=State.OPEN)
            if self.calls <= self.hangs_up:
                ws = self._websocket
                asyncio.get_running_loop().call_later(0.01, lambda: setattr(ws, "state", State.CLOSED))

        async def set_usable(self, v):
            self._usable = v

    async def _rec_ws():
        ws = _FakeWS(hangs_up=1)
        switched = []

        async def req(s):
            switched.append(s)

        async def reachable():
            return True
        ok = await sf.recover_stt(ws, req, 0.01, reachable=reachable, settle_secs=0.1)
        return ok, ws, switched

    ok, ws, switched = asyncio.run(_rec_ws())
    check("recover: a websocket that hangs up right after connecting is not switched to; the next one that stays is",
          ok is True and ws.calls == 2 and ws._usable and switched == [ws], f"{ok} {ws.calls} {switched}")

    async def _rec_nows():
        ws = _FakeWS(hangs_up=0)
        ws._do_reconnect = lambda: _nows(ws)
        switched, rounds = [], {"n": 0}

        async def req(s):
            switched.append(s)

        async def reachable():
            rounds["n"] += 1
            return True
        return await sf.recover_stt(ws, req, 0.01, reachable=reachable, settle_secs=0.01,
                                    closed=lambda: rounds["n"] >= 3), switched

    async def _nows(ws):
        ws.calls += 1                       # connect failed quietly: no socket
        ws._websocket = None

    ok, switched = asyncio.run(_rec_nows())
    check("recover: a reconnect that left no websocket never switches back", ok is False and switched == [], str(switched))

    # ── REAL ElevenLabsRealtimeSTTService in a REAL PipelineTask ──
    PROBES = {"elevenlabs": 0, "deepgram": 0}

    async def _fake_el_probe():
        PROBES["elevenlabs"] += 1
        return True

    async def _fake_dg_probe():
        PROBES["deepgram"] += 1
        return False
    sf._probe_elevenlabs = _fake_el_probe
    sf._probe_deepgram = _fake_dg_probe

    class _LocalScribe(_od_cls):
        """The real service, built by make_elevenlabs, over plain ws:// to a
        local port (no TLS, and never ElevenLabs' host)."""
        port = 9

        async def _websocket_connect(self, uri, **kw):
            local = re.sub(r"^wss://[^/]+", f"ws://127.0.0.1:{self.port}", uri)
            return await super()._websocket_connect(local, **kw)

    def _scribe(port):
        setattr(_od_mod, _od_cls.__name__, type("_LocalScribeAt", (_LocalScribe,), {"port": port}))
        try:
            svc, why = sf.make_elevenlabs("auto")
        finally:
            setattr(_od_mod, _od_cls.__name__, _od_cls)
        if svc is None:
            raise RuntimeError(f"make_elevenlabs built nothing: {why}")
        return svc

    async def _feed_until(task, cond, budget):
        """Audio every 0.1 s (a room that is awake) until cond() or the budget runs out."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget and not cond():
            await task.queue_frame(_audio())
            await asyncio.sleep(0.1)
        return cond()

    async def _with_server(handler, run):
        server = await websockets.serve(handler, "127.0.0.1", EL_PORT)
        try:
            return await run
        finally:
            server.close()

    landed, took, st = asyncio.run(_failover(_scribe(9), 60, provider="elevenlabs", feed=True))
    check("REAL: ElevenLabs unreachable (dead port) at the first audio -> the switcher lands on Whisper",
          landed, f"after {took:.1f}s")
    check("REAL: the ElevenLabs fallback is visible in status (active whisper + reason, room listed)",
          st.get("provider") == "elevenlabs" and st.get("active") == "whisper" and st.get("fallback_rooms") == ["proof"]
          and st.get("fallback_reason"), str(st))
    print(f"    (elevenlabs dead-port failover took {took:.1f}s)")

    _s = socket.socket()
    _s.bind(("127.0.0.1", 0))
    EL_PORT = _s.getsockname()[1]
    _s.close()
    EL = {"conns": 0, "audio": 0, "commits": 0, "paths": []}

    def _path(ws):
        return getattr(getattr(ws, "request", None), "path", None) or getattr(ws, "path", "")

    async def _el_reject(ws):
        # Handshake accepted, then a rejected key: an error message and a hang-up.
        EL["conns"] += 1
        await ws.send(json.dumps({"message_type": "auth_error", "error": "invalid key el-bad-SECRET-9999"}))
        await ws.close()

    async def _el_ok(ws):
        EL["conns"] += 1
        EL["paths"].append(_path(ws))
        await ws.send(json.dumps({"message_type": "session_started"}))
        unc = 0
        async for msg in ws:
            d = json.loads(msg)
            if d.get("commit"):
                if unc < 9600:                # real Scribe: under 0.3 s -> refused, socket dropped
                    EL["refused"] = EL.get("refused", 0) + 1
                    await ws.close()
                    return
                EL["commits"] += 1
                unc = 0
            elif d.get("audio_base_64"):
                EL["audio"] += 1
                unc += len(d["audio_base_64"]) * 3 // 4   # base64 -> bytes

    async def _el_stays(switcher, whisper, task, until):
        await asyncio.sleep(1.0)
        idle = (EL["conns"], switcher.strategy.active_service is switcher._lenoxvi_primary)
        landed = await _feed_until(task, lambda: switcher.strategy.active_service is whisper, 60)
        await asyncio.sleep(4.5)          # >= 3 recovery rounds at the 1 s floor
        return idle, landed, switcher.strategy.active_service is whisper, sf.status()

    os.environ["ELEVENLABS_API_KEY"] = "el-bad-SECRET-9999"
    try:
        idle, landed, still, st = asyncio.run(_with_server(
            _el_reject, _run(_scribe(EL_PORT), 1, _el_stays, provider="elevenlabs")))
    finally:
        os.environ["ELEVENLABS_API_KEY"] = "el-junk-test-key"
    check("on-demand: with a key ElevenLabs rejects, an idle room opens no connection and stays on ElevenLabs",
          idle == (0, True), str(idle))
    check("REAL: a key ElevenLabs keeps rejecting -> Whisper at the first audio, and recovery rounds keep it there "
          "(no flapping)", landed and still and st.get("active") == "whisper", f"{landed} {still} {st}")
    check("REAL: the ElevenLabs status reason never contains the key", "el-bad-SECRET-9999" not in str(st), str(st))

    async def _el_cycle(switcher, whisper, task, until):
        el = switcher._lenoxvi_primary
        r = {}
        r["failed_over"] = await _feed_until(task, lambda: switcher.strategy.active_service is whisper, 60)
        before_w = whisper.audio_chunks
        for _ in range(5):
            await task.queue_frame(_audio())
        await until(lambda: whisper.audio_chunks >= before_w + 5, 5)
        r["whisper_heard_while_down"] = whisper.audio_chunks - before_w
        server = await websockets.serve(_el_ok, "127.0.0.1", EL_PORT)
        try:
            r["switched_back"] = await until(lambda: switcher.strategy.active_service is el, 20)
            await asyncio.sleep(0.5)
            r["status_after"] = sf.status()
            before_w, before_el = whisper.audio_chunks, EL["audio"]
            for _ in range(20):               # 0.4 s: a real utterance (Scribe refuses < 0.3 s)
                await task.queue_frame(_audio())
            await until(lambda: EL["audio"] >= before_el + 20, 5)
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            await until(lambda: EL["commits"] >= 1, 5)
            await asyncio.sleep(0.3)
            r["whisper_heard_after"] = whisper.audio_chunks - before_w
            r["elevenlabs_heard_after"] = EL["audio"] - before_el
            r["commits"] = EL["commits"]
            # A recovered socket idles out like any other; the next audio reopens it.
            conns, before_el = EL["conns"], EL["audio"]
            r["idle_closed_after_recovery"] = await until(lambda: el._websocket is None, 8)
            r["on_elevenlabs_while_closed"] = switcher.strategy.active_service is el
            for _ in range(3):
                await task.queue_frame(_audio())
            await until(lambda: EL["audio"] >= before_el + 3, 5)
            r["reopened"] = EL["conns"] - conns
            r["heard_after_reopen"] = EL["audio"] - before_el
        finally:
            server.close()
        return r

    EL.update(conns=0, audio=0, commits=0, paths=[])
    PROBES.update(elevenlabs=0, deepgram=0)
    _tune(IDLE_CLOSE_SECS=3.0)            # > recovery's 1 s settle, short enough to wait for
    try:
        r = asyncio.run(_run(_scribe(EL_PORT), 1, _el_cycle, provider="elevenlabs"))
    finally:
        _tune(**_sel_saved)
    check("REAL ElevenLabs cycle: down -> Whisper, and Whisper gets the audio",
          r.get("failed_over") and r.get("whisper_heard_while_down", 0) >= 5, str(r))
    check("REAL ElevenLabs cycle: up -> recovery switches back, status says elevenlabs",
          r.get("switched_back") and (r.get("status_after") or {}).get("active") == "elevenlabs", str(r))
    check("REAL ElevenLabs cycle: after the switch back ONLY ElevenLabs gets audio, and our VAD stop commits the turn",
          r.get("elevenlabs_heard_after", 0) >= 5 and r.get("whisper_heard_after") == 0 and r.get("commits", 0) >= 1, str(r))
    check("REAL ElevenLabs cycle: after recovery the idle socket closes, the room stays on ElevenLabs, and the "
          "next audio reopens exactly one connection",
          r.get("idle_closed_after_recovery") and r.get("on_elevenlabs_while_closed") and r.get("reopened") == 1
          and r.get("heard_after_reopen", 0) >= 3, str(r))
    check("REAL ElevenLabs cycle: recovery probed ElevenLabs' host, never Deepgram's",
          PROBES["elevenlabs"] >= 1 and PROBES["deepgram"] == 0, str(PROBES))
    _q = EL["paths"][-1] if EL["paths"] else ""
    check("REAL ElevenLabs cycle: the session asks for scribe_v2_realtime, 16 kHz PCM, manual commits, no logging",
          "model_id=scribe_v2_realtime" in _q and "audio_format=pcm_16000" in _q and "commit_strategy=manual" in _q
          and "enable_logging=true" not in _q and "language_code" not in _q, _q)
    print(f"    (elevenlabs cycle: {r})")

    # ── on demand: Scribe bills a held socket's silence, so it opens on the
    # first (post-wake) audio and closes after a quiet spell ──
    import base64
    import struct

    from loguru import logger as _logger

    SEQ = {"conns": 0, "closed": 0, "events": [], "delay": 0.0, "start": True, "err": None, "hangup": None}

    def _seq(n):
        return InputAudioRawFrame(audio=struct.pack("<H", n) * 320, sample_rate=16000, num_channels=1)

    async def _el_seq(ws):
        # Numbered audio in: order, commits, silence and the connection each
        # arrived on are all visible. session_started after SEQ["delay"].
        SEQ["conns"] += 1
        conn = SEQ["conns"]
        try:
            await asyncio.sleep(SEQ["delay"])
            if SEQ["err"]:
                await ws.send(json.dumps({"message_type": SEQ["err"], "error": "refused: acct el-SECRET-4242"}))
            elif SEQ["start"]:
                await ws.send(json.dumps({"message_type": "session_started"}))
            if SEQ["hangup"] is not None:
                # Scribe's own quiet-socket hang-up: close after this long without a message.
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), SEQ["hangup"])
                    except TimeoutError:
                        await ws.close()
                        return
                    d = json.loads(msg)
                    pcm = base64.b64decode(d.get("audio_base_64") or "")
                    SEQ["events"].append((conn, "commit" if d.get("commit") else
                                          struct.unpack("<H", pcm[:2])[0] if pcm.strip(b"\0") else "silence"))
            unc = 0                           # audio bytes since the last commit
            async for msg in ws:
                d = json.loads(msg)
                pcm = base64.b64decode(d.get("audio_base_64") or "")
                unc += len(pcm)
                if d.get("commit"):
                    if unc < 9600:            # real Scribe: under 0.3 s -> refused, socket dropped
                        SEQ["events"].append((conn, "commit-refused"))
                        await ws.close()
                        return
                    SEQ["events"].append((conn, "commit"))
                    unc = 0
                elif pcm.strip(b"\0"):
                    SEQ["events"].append((conn, struct.unpack("<H", pcm[:2])[0]))
                else:
                    SEQ["events"].append((conn, "silence"))
        except websockets.ConnectionClosed:
            pass
        finally:
            SEQ["closed"] += 1

    def _seq_run(steps, delay=0.0, start=True, err=None, hangup=None, svc=None, cooldown=3600):
        SEQ.update(conns=0, closed=0, events=[], delay=delay, start=start, err=err, hangup=hangup)
        LOGS.clear()
        return asyncio.run(_with_server(_el_seq, _run(svc or _scribe(EL_PORT), cooldown, steps,
                                                      provider="elevenlabs")))

    LOGS = []
    _sink = _logger.add(lambda m: LOGS.append(m.record["message"]), level="INFO")
    try:
        async def _el_wake(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            r = {}
            await asyncio.sleep(1.0)
            r["conns_idle"] = SEQ["conns"]
            r["on_el_idle"] = switcher.strategy.active_service is el and sf.status().get("active") == "elevenlabs"
            for n in range(1, 21):            # 0.4 s: a real utterance (Scribe refuses < 0.3 s)
                await task.queue_frame(_seq(n))
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            for n in range(21, 24):           # the next turn's first audio
                await task.queue_frame(_seq(n))
            await until(lambda: len(SEQ["events"]) >= 24, 5)
            await asyncio.sleep(0.3)
            r["conns"] = SEQ["conns"]
            r["events"] = list(SEQ["events"])
            return r

        r = _seq_run(_el_wake, delay=0.3)     # all of it arrives while the session starts
        _audio_in = [e for c, e in r.get("events", []) if e != "commit"]
        _conn_log = [m for m in LOGS if m.startswith("stt: elevenlabs connected in ")]
        _held_ms = re.search(r"\((\d+) ms of audio buffered\)", _conn_log[0]) if _conn_log else None
        check("on-demand: no connection at pipeline start; the idle room counts as on ElevenLabs, not down",
              r.get("conns_idle") == 0 and r.get("on_el_idle"), str(r))
        check("on-demand: the first audio opens exactly one connection",
              r.get("conns_idle") == 0 and r.get("conns") == 1, str(r))
        check("on-demand: audio sent while the session starts arrives complete and in order",
              r.get("conns_idle") == 0 and _audio_in == list(range(1, 24))
              and all(c == 1 for c, _e in r.get("events", [])), str(r))
        check("on-demand: a VAD stop during the connect is sent after the audio it ends, before the next turn's",
              r.get("conns_idle") == 0
              and r.get("events") == [(1, n) for n in range(1, 21)] + [(1, "commit")] + [(1, n) for n in range(21, 24)],
              str(r))
        check("on-demand: one INFO line per connect with the held audio: 'connected in N ms (M ms of audio buffered)'",
              len(_conn_log) == 1 and _held_ms is not None and int(_held_ms.group(1)) >= 200, str(_conn_log))

        async def _el_quiet(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            for n in range(1, 4):
                await task.queue_frame(_seq(n))
            await until(lambda: len(SEQ["events"]) >= 3, 5)
            await asyncio.sleep(17)           # pipecat's keepalive sends silence from 10-15 s of quiet
            ws = el._websocket
            return {"events": list(SEQ["events"]), "conns": SEQ["conns"],
                    "open": ws is not None and ws.state is State.OPEN}

        _tune(IDLE_CLOSE_SECS=30.0)           # hold it open past pipecat's 10-15 s keepalive point
        try:
            r = _seq_run(_el_quiet)
        finally:
            _tune(**_sel_saved)
        check("on-demand: an idle open socket gets no keepalive silence (17 s, inside IDLE_CLOSE_SECS)",
              r.get("open") and r.get("conns") == 1 and r.get("events") == [(1, 1), (1, 2), (1, 3)], str(r))

        async def _el_idle(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            r = {}
            for n in range(1, 4):
                await task.queue_frame(_seq(n))
            await until(lambda: len(SEQ["events"]) >= 3, 5)
            r["closed"] = await until(lambda: SEQ["closed"] >= 1, 5)
            await asyncio.sleep(1.0)          # closed by us: nobody reconnects it
            r["conns_closed"] = SEQ["conns"]
            r["on_el"] = switcher.strategy.active_service is el and sf.status().get("active") == "elevenlabs"
            for n in range(4, 7):
                await task.queue_frame(_seq(n))
            await until(lambda: len(SEQ["events"]) >= 6, 5)
            r["conns"] = SEQ["conns"]
            r["events"] = list(SEQ["events"])
            return r

        _tune(IDLE_CLOSE_SECS=1.5)
        try:
            r = _seq_run(_el_idle)
        finally:
            _tune(**_sel_saved)
        check("on-demand: IDLE_CLOSE_SECS without audio closes the socket, and nothing reopens it while quiet",
              r.get("closed") and r.get("conns_closed") == 1 and any("closed until the next audio" in m for m in LOGS),
              str(r))
        check("on-demand: a socket we closed for idling is healthy — the room stays on ElevenLabs",
              r.get("closed") and r.get("on_el"), str(r))
        check("on-demand: the next audio opens a fresh connection and arrives there in order",
              r.get("conns") == 2 and r.get("events") == [(1, 1), (1, 2), (1, 3), (2, 4), (2, 5), (2, 6)], str(r))

        async def _el_no_session(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            for n in range(1, 4):
                await task.queue_frame(_seq(n))
            gave_up = await until(lambda: any(m.startswith("stt: elevenlabs no session after") for m in LOGS), 4)
            return {"gave_up": gave_up, "opening": getattr(el, "_opening", None),
                    "held": getattr(el, "_held_bytes", None)}

        _tune(CONNECT_TIMEOUT_SECS=0.5)
        try:
            r = _seq_run(_el_no_session, start=False)
        finally:
            _tune(**_sel_saved)
        check("on-demand: a server that never starts a session -> the connect gives up after CONNECT_TIMEOUT_SECS "
              "(no hang), its held audio dropped with a warning",
              r.get("gave_up") and r.get("opening") is False and r.get("held") == 0, str(r))

        # The hold is bounded: overflow drops the oldest audio, with one warning.
        svc = _scribe(9)
        svc._sample_rate = 16000
        _tune(HOLD_SECS=0.1)                  # 3200 bytes = five 20 ms chunks
        LOGS.clear()
        _commit = getattr(_sel, "_COMMIT", None)
        _hold = getattr(svc, "_hold", lambda item: None)
        try:
            for n in range(1, 4):
                _hold(struct.pack("<H", n) * 320)
            _hold(_commit)
            for n in range(4, 9):
                _hold(struct.pack("<H", n) * 320)
        finally:
            _tune(**_sel_saved)
        _kept = [struct.unpack("<H", i[:2])[0] for i in getattr(svc, "_held", []) if i is not _commit]
        check("on-demand: the hold is bounded — overflow keeps the newest audio in order and warns once",
              _kept == [4, 5, 6, 7, 8] and getattr(svc, "_held_bytes", None) == 3200
              and sum("dropping the oldest" in m for m in LOGS) == 1, f"{_kept} {LOGS}")

        # ── hardening: live traces and a read-only bug hunt ──
        # Scribe hangs up a quiet socket by itself (~15 s): it stays closed
        # (no reconnect nobody is talking to); the next audio reopens it.
        async def _el_server_quiet(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            r = {}
            for n in range(1, 4):
                await task.queue_frame(_seq(n))
            r["hung_up"] = await until(lambda: SEQ["closed"] >= 1, 5)
            await asyncio.sleep(2.5)          # pipecat would reconnect inside this
            r["conns_quiet"] = SEQ["conns"]
            r["on_el"] = switcher.strategy.active_service is el
            for n in range(4, 7):
                await task.queue_frame(_seq(n))
            await until(lambda: len(SEQ["events"]) >= 6, 5)
            r["conns"] = SEQ["conns"]
            r["events"] = list(SEQ["events"])
            return r

        _tune(IDLE_CLOSE_SECS=30.0, SERVER_IDLE_SECS=0.3)
        try:
            r = _seq_run(_el_server_quiet, hangup=1.0)
        finally:
            _tune(**_sel_saved)
        check("hardening: Scribe hanging up a quiet socket is not reconnected while quiet (no idle sessions)",
              r.get("hung_up") and r.get("conns_quiet") == 1 and r.get("on_el")
              and any("closed by the server while quiet" in m for m in LOGS), str(r))
        check("hardening: after Scribe's quiet hang-up the next audio opens one fresh connection, in order",
              r.get("conns") == 2 and r.get("events") == [(1, 1), (1, 2), (1, 3), (2, 4), (2, 5), (2, 6)], str(r))

        # A wake that lands while the idle close is still tearing the socket
        # down: it waits its turn and opens a fresh socket — nothing lost,
        # nothing orphaned, no false failure.
        slow = _scribe(EL_PORT)
        _orig_close = slow._disconnect_websocket

        async def _slow_close():
            await asyncio.sleep(0.6)          # a slow close handshake
            await _orig_close()
        slow._disconnect_websocket = _slow_close

        async def _el_race(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            r = {}
            for n in range(1, 4):
                await task.queue_frame(_seq(n))
            r["closing"] = await until(lambda: any("closed until the next audio" in m for m in LOGS), 5)
            for n in range(4, 7):             # mid-teardown
                await task.queue_frame(_seq(n))
            await until(lambda: len(SEQ["events"]) >= 6, 5)
            await asyncio.sleep(1.5)
            r["conns"], r["closed"] = SEQ["conns"], SEQ["closed"]
            r["events"] = list(SEQ["events"])
            ws = el._websocket
            r["open"] = ws is not None and ws.state is State.OPEN
            r["on_el"] = switcher.strategy.active_service is el
            r["no_session"] = any(m.startswith("stt: elevenlabs no session") for m in LOGS)
            return r

        _tune(IDLE_CLOSE_SECS=1.0, CONNECT_TIMEOUT_SECS=1.0)
        try:
            r = _seq_run(_el_race, svc=slow)
        finally:
            _tune(**_sel_saved)
        check("hardening: audio during the idle close's teardown reaches a fresh connection, complete and in order",
              r.get("closing") and r.get("conns") == 2
              and r.get("events") == [(1, 1), (1, 2), (1, 3), (2, 4), (2, 5), (2, 6)], str(r))
        check("hardening: after that race exactly one socket is open, it is ours, and the room stays on ElevenLabs",
              r.get("open") and r.get("closed") == 1 and r.get("on_el") and not r.get("no_session"), str(r))

        # Out of credits / bad key / terms: an account error moves the room
        # to Whisper at once, and recovery won't switch back while it lasts.
        async def _el_account(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            r = {}
            t0 = time.monotonic()
            r["landed"] = await _feed_until(task, lambda: switcher.strategy.active_service is whisper, 8)
            r["took"] = round(time.monotonic() - t0, 1)
            await asyncio.sleep(4.5)          # >= 3 recovery rounds at the 1 s floor
            r["still"] = switcher.strategy.active_service is whisper and el is not None
            r["st"] = sf.status()
            return r

        PROBES.update(elevenlabs=0, deepgram=0)
        r = _seq_run(_el_account, err="quota_exceeded", cooldown=1)
        check("hardening: Scribe quota_exceeded moves the room to Whisper at once (not deaf on a refusing account)",
              r.get("landed") and r.get("took", 99) < 4, str(r))
        check("hardening: recovery keeps Whisper while Scribe still refuses the account (no switch back, no flapping)",
              r.get("still") and (r.get("st") or {}).get("active") == "whisper" and PROBES["elevenlabs"] >= 1, str(r))
        check("hardening: the account error's server text never reaches status or logs",
              "SECRET" not in str(r.get("st")) and not any("SECRET" in m for m in LOGS), str(r.get("st")))

        # No session at wake (dead network, no answer): Whisper takes the
        # room right away instead of after pipecat's ~25 s reconnect ladder.
        async def _el_dead_wake(switcher, whisper, task, until):
            t0 = time.monotonic()
            landed = await _feed_until(task, lambda: switcher.strategy.active_service is whisper, 8)
            return {"landed": landed, "took": round(time.monotonic() - t0, 1)}

        _tune(CONNECT_TIMEOUT_SECS=0.5)
        try:
            r = _seq_run(_el_dead_wake, start=False)
        finally:
            _tune(**_sel_saved)
        check("hardening: a wake that gets no session fails over to Whisper within the connect timeout",
              r.get("landed") and r.get("took", 99) < 3, str(r))

        # Scribe refuses a commit under ~0.3 s and drops the socket (live: a
        # noise blip right after the wake word). Short turns aren't committed;
        # their audio rides along with the next words' commit.
        async def _el_blip(switcher, whisper, task, until):
            el = switcher._lenoxvi_primary
            for n in range(1, 21):            # a real turn: 0.4 s
                await task.queue_frame(_seq(n))
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            await until(lambda: (1, "commit") in SEQ["events"], 5)
            for n in range(21, 26):           # a 0.1 s blip
                await task.queue_frame(_seq(n))
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            await asyncio.sleep(0.5)
            r = {"conns_blip": SEQ["conns"]}
            for n in range(26, 41):           # the next words
                await task.queue_frame(_seq(n))
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            await until(lambda: sum(e == "commit" for _c, e in SEQ["events"]) >= 2, 5)
            await asyncio.sleep(0.3)
            r["events"] = list(SEQ["events"])
            r["conns"] = SEQ["conns"]
            r["on_el"] = switcher.strategy.active_service is el
            return r

        r = _seq_run(_el_blip)
        check("hardening: a blip under Scribe's minimum is not committed; the socket stays up (no refusal, no reconnect)",
              r.get("conns_blip") == 1 and r.get("conns") == 1 and r.get("on_el")
              and not any(e == "commit-refused" for _c, e in r.get("events", [])), str(r)[:300])
        check("hardening: the blip's audio rides with the next words' commit, in order",
              r.get("events") == [(1, n) for n in range(1, 21)] + [(1, "commit")]
              + [(1, n) for n in range(21, 41)] + [(1, "commit")], str(r.get("events"))[:300])

        async def _el_blip_held(switcher, whisper, task, until):
            for n in range(1, 6):             # a 0.1 s blip while the session starts
                await task.queue_frame(_seq(n))
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            for n in range(6, 26):
                await task.queue_frame(_seq(n))
            await task.queue_frame(VADUserStoppedSpeakingFrame())
            await until(lambda: any(e in ("commit", "commit-refused") for _c, e in SEQ["events"]), 5)
            await asyncio.sleep(0.5)
            return {"events": list(SEQ["events"]), "conns": SEQ["conns"]}

        r = _seq_run(_el_blip_held, delay=0.3)
        check("hardening: a blip held during the connect is not committed either; one commit after the real words",
              r.get("conns") == 1 and r.get("events") == [(1, n) for n in range(1, 26)] + [(1, "commit")],
              str(r)[:300])
    finally:
        _logger.remove(_sink)
finally:
    sf._probe_deepgram = _saved_probes[1]
    if _saved_probes[0] is not None:
        sf._probe_elevenlabs = _saved_probes[0]
    sf._reset_for_tests()
    if _saved_el is None:
        os.environ.pop("ELEVENLABS_API_KEY", None)
    else:
        os.environ["ELEVENLABS_API_KEY"] = _saved_el

if _saved_key is None:
    os.environ.pop("DEEPGRAM_API_KEY", None)
else:
    os.environ["DEEPGRAM_API_KEY"] = _saved_key

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
