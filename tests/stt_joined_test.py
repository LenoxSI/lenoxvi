"""stt — joint re-transcription of multi-stretch turns: the stretch store
and transcribe_joined on the local service (host venv).

The transcribe backend is faked through stt_local's hooks (the shared
model and a model_override stand-in): no model loads, nothing downloads,
no network.

Run: cd tests && ~/venvs/lenoxvi/bin/python stt_joined_test.py <path-to-core>
"""
import asyncio
import io
import sys
import time
import wave

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import stt_joined as sj  # noqa: E402


def wav(seconds=0.5, rate=16000, ch=1, width=2):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * int(rate * seconds))
    return b.getvalue()


def payload(clip):
    with wave.open(io.BytesIO(clip)) as w:
        return w.readframes(w.getnframes())


# ── the store: text-matched lookup, concatenation order ──────────────
st = sj.StretchStore()
one, two = wav(0.25), wav(0.5)
st.keep("What about", one)
st.keep("Yeah.", two)
check("store: join looks each text up and concatenates the matched audio in order",
      payload(st.join(["What about", "Yeah."])) == payload(one) + payload(two)
      and payload(st.join(["Yeah.", "What about"])) == payload(two) + payload(one))
check("store: a text never kept, an empty text or an empty list -> None",
      st.join(["What about", "never kept"]) is None and st.join([""]) is None and st.join([]) is None)
check("store: one text alone -> that stretch's own clip",
      payload(st.join(["Yeah."])) == payload(two))

# ── the store: the 8/30/60 bounds ────────────────────────────────────
st8 = sj.StretchStore()
st8.keep("short", wav(2.0))
st8.keep("over", wav(sj.STRETCH_KEEP_SECS + 1.0))
check("store: a stretch past the keep bound (8 s) is never kept; a shorter one is",
      st8.join(["short"]) is not None and st8.join(["over"]) is None)

stj = sj.StretchStore()
for i in range(4):
    stj.keep(f"s{i}", wav(sj.STRETCH_KEEP_SECS))
check("store: a join past the bound (30 s) -> None; under it -> one clip",
      stj.join(["s0", "s1", "s2", "s3"]) is None      # 32 s of audio
      and stj.join(["s0", "s1", "s2"]) is not None)   # 24 s

ste = sj.StretchStore()
for i in range(9):
    ste.keep(f"e{i}", wav(sj.STRETCH_KEEP_SECS))
check("store: past the total bound (60 s) the OLDEST stretches are evicted, the newest stay",
      ste.join(["e0"]) is None and ste.join(["e1"]) is None
      and ste.join(["e2"]) is not None and ste.join(["e8"]) is not None)

stx = sj.StretchStore()
stx.keep("odd", b"RIFF....WAVE")
stx.keep("", wav(0.5))
check("store: audio that does not parse as WAV, or an empty text, is never kept",
      stx.join(["odd"]) is None and stx.join([""]) is None)

st_r = sj.StretchStore()
st_r.keep("yeah", wav(0.25))
st_r.keep("yeah", wav(0.5))
check("store: a re-heard text keeps the newest audio",
      payload(st_r.join(["yeah"])) == payload(wav(0.5)))
st_m = sj.StretchStore()
st_m.keep("mono", wav(0.25))
st_m.keep("wide", wav(0.25, ch=2))
check("store: clips with mismatched formats are never joined",
      st_m.join(["mono", "wide"]) is None)

# ── transcribe_joined: the service over a faked backend ─────────────
import stt_local as sl  # noqa: E402


class _Seg:
    def __init__(self, t):
        self.text = t


class _Info:
    def __init__(self, lang):
        self.language = lang


class _FakeModel:
    """faster-whisper's transcribe() shape (also LocalAIWhisper's): texts
    served in order, every call's clip recorded."""

    def __init__(self, texts, delay=0.0, fail_after=None):
        self.texts = list(texts)
        self.delay = delay
        self.calls = []
        self.fail_after = fail_after

    def transcribe(self, audio, language=None, beam_size=1, vad_filter=False):
        data = audio.read() if hasattr(audio, "read") else bytes(audio)
        time.sleep(self.delay)
        self.calls.append(data)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("backend gone")
        return [_Seg(self.texts.pop(0))], _Info(language or "en")


sl.LocalWhisperSTTService._shared_model = None
sl._reset_status_for_tests()
sl._language_hook = lambda: None
JA, JB = "What about", "Yeah."
JOINT = "What about Wednesday?"
HEAD, TAIL = wav(1.0), wav(0.5)


async def joint_probe():
    model = _FakeModel([JA, JB, JOINT])
    sl.LocalWhisperSTTService._shared_model = model
    svc = sl.LocalWhisperSTTService(language=None)
    await svc._transcribe(HEAD)            # stretch 1
    await svc._transcribe(TAIL)            # stretch 2
    n_lat = sl.status()["samples"]
    out = await svc.transcribe_joined([JA, JB], timeout=5.0)
    return model, svc, out, n_lat, sl.status()["samples"]


model, svc, out, n_before, n_after = asyncio.run(joint_probe())
check("stt: transcribe_joined makes ONE request over the stretches' audio, in order, and returns its text",
      out == JOINT and len(model.calls) == 3
      and payload(model.calls[2]) == payload(HEAD) + payload(TAIL),
      f"out={out!r} calls={len(model.calls)}")
check("stt: the stretch clips stay in the store; the joint clip never enters it",
      svc._stretch_store.join([JA]) is not None and svc._stretch_store.join([JOINT]) is None)
check("stt: the joint call stays out of the per-command latency stat",
      n_before == 2 and n_after == n_before, f"{n_before} -> {n_after}")


async def single_probe():
    model = _FakeModel([JA, JOINT])
    sl.LocalWhisperSTTService._shared_model = model
    svc = sl.LocalWhisperSTTService(language=None)
    await svc._transcribe(HEAD)
    return model, await svc.transcribe_joined([JA], timeout=5.0)


model, out = asyncio.run(single_probe())
check("stt: a single stretch joined alone is transcribed as-is (one request, its own clip)",
      out == JOINT and len(model.calls) == 2 and payload(model.calls[1]) == payload(HEAD),
      f"out={out!r}")


async def timeout_probe():
    model = _FakeModel([JA, "too slow"], delay=0.25)
    sl.LocalWhisperSTTService._shared_model = model
    svc = sl.LocalWhisperSTTService(language=None)
    await svc._transcribe(HEAD)
    return await svc.transcribe_joined([JA], timeout=0.05)


out = asyncio.run(timeout_probe())
check("stt: a joint request slower than its bound -> None, never a raise", out is None, repr(out))


async def raise_probe():
    model = _FakeModel([JA], fail_after=1)
    sl.LocalWhisperSTTService._shared_model = model
    svc = sl.LocalWhisperSTTService(language=None)
    await svc._transcribe(HEAD)
    return await svc.transcribe_joined([JA], timeout=5.0)


out = asyncio.run(raise_probe())
check("stt: a joint request that raises -> None, never a raise to the caller", out is None, repr(out))


async def miss_probe():
    model = _FakeModel([JA])
    sl.LocalWhisperSTTService._shared_model = model
    svc = sl.LocalWhisperSTTService(language=None)
    await svc._transcribe(HEAD)
    missing = await svc.transcribe_joined([JA, "never said"], timeout=5.0)
    empty = await svc.transcribe_joined([], timeout=5.0)
    return model, missing, empty


model, missing, empty = asyncio.run(miss_probe())
check("stt: a text the store never kept, or an empty text list -> None, with NO request",
      missing is None and empty is None and len(model.calls) == 1, str(len(model.calls)))


async def standin_probe():
    model = _FakeModel([JA, JB, JOINT])
    svc = sl.LocalWhisperSTTService(language=None, model_override=model)
    await svc._transcribe(HEAD)
    await svc._transcribe(TAIL)
    out = await svc.transcribe_joined([JA, JB], timeout=5.0)
    return model, out


model, out = asyncio.run(standin_probe())
check("stt: a model_override stand-in (the LocalAI lane) serves the joint request the same way",
      out == JOINT and len(model.calls) == 3
      and payload(model.calls[2]) == payload(HEAD) + payload(TAIL),
      f"out={out!r} calls={len(model.calls)}")

sl.LocalWhisperSTTService._shared_model = None

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
