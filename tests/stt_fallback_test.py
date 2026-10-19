"""Cloud STT provider + Whisper fallback pins (host venv; pipecat + deepgram-sdk).

Run: cd tests && ~/venvs/lenoxvi/bin/python stt_fallback_test.py <path-to-core>
The failover proof uses a REAL DeepgramSTTService inside a REAL PipelineTask
(pointed at a dead local port, and at Deepgram with a junk key) — the TTS
fallback taught that reading the code isn't proof.
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
check("provider catalog: Local — Whisper first, then Deepgram Nova-3 (cloud)",
      [(p["name"], p["label"]) for p in getattr(cat, "PROVIDERS", [])]
      == [("whisper", "Local — Whisper"), ("deepgram", "Deepgram Nova-3 (cloud)")],
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


async def _failover(dg, budget):
    whisper = _FakeWhisper()
    task_ref = {}

    async def queue(frame):
        await task_ref["task"].queue_frame(frame)
    sf._reset_for_tests()
    sf.set_provider("deepgram")
    switcher = sf.wrap_stt_with_fallback(dg, whisper, room="proof", queue_frame=queue, cooldown=3600)
    task = PipelineTask(Pipeline([switcher]), params=PipelineParams(), cancel_on_idle_timeout=False)
    task_ref["task"] = task
    runner = PipelineRunner(handle_sigint=False)
    run = asyncio.create_task(runner.run(task))
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget and switcher.strategy.active_service is not whisper:
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


async def _run(dg, cooldown, steps):
    whisper = _FakeWhisper()
    task_ref = {}

    async def queue(frame):
        await task_ref["task"].queue_frame(frame)
    sf._reset_for_tests()
    sf.set_provider("deepgram")
    switcher = sf.wrap_stt_with_fallback(dg, whisper, room="proof", queue_frame=queue, cooldown=cooldown)
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

if _saved_key is None:
    os.environ.pop("DEEPGRAM_API_KEY", None)
else:
    os.environ["DEEPGRAM_API_KEY"] = _saved_key

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
