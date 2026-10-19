"""stt — STT card pins: catalog + stt_local status / latency / live language (host venv).

Whisper is faked through stt_local's hooks: no model loads, nothing downloads.
Run: cd tests && ~/venvs/lenoxvi/bin/python stt_card_test.py <path-to-core>
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


import stt_catalog as cat  # noqa: E402

# ── catalog ──────────────────────────────────────────────────────────
names = [m["name"] for m in cat.MODELS]
check("stt catalog: exactly the four benchmarked models, Turbo first",
      names == ["large-v3-turbo", "medium", "small", "base"], str(names))
gpu_only = {m["name"]: m["gpu_only"] for m in cat.MODELS}
check("stt catalog: Turbo + Medium are GPU-only; Small + Base run on CPU",
      gpu_only == {"large-v3-turbo": True, "medium": True, "small": False, "base": False}, str(gpu_only))
check("stt catalog: every model has a label and a size",
      all(m.get("label") and isinstance(m.get("size_gb"), (int, float)) for m in cat.MODELS))
codes = [lang["code"] for lang in cat.LANGUAGES]
check("stt languages: Auto first, then the 16 common ones",
      codes[0] == "auto" and len(codes) == 17 and "en" in codes and len(set(codes)) == 17, str(codes))
check("stt runtime: GPU -> cuda/float16", cat.runtime_for(True) == ("cuda", "float16"))
check("stt runtime: no GPU -> cpu/int8", cat.runtime_for(False) == ("cpu", "int8"))
check("stt validate: good pick", cat.validate("medium", "en", has_gpu=True) is None)
check("stt validate: model outside the four", "model" in (cat.validate("large-v3", "en", has_gpu=True) or ""))
check("stt validate: GPU-only model without a GPU",
      "GPU" in (cat.validate("large-v3-turbo", "auto", has_gpu=False) or ""))
check("stt validate: CPU model without a GPU is fine", cat.validate("base", "auto", has_gpu=False) is None)
check("stt validate: unknown language", "language" in (cat.validate("small", "xx", has_gpu=True) or ""))
check("stt validate: language-only (model None) is allowed", cat.validate(None, "fr", has_gpu=False) is None)
check("stt language_value: auto/empty -> None, code -> code",
      cat.language_value("auto") is None and cat.language_value("") is None
      and cat.language_value(None) is None and cat.language_value("en") == "en")

# ── stt_local: status on preload ─────────────────────────────────────
import stt_local as sl  # noqa: E402


class _Seg:
    def __init__(self, t):
        self.text = t


class _Info:
    def __init__(self, lang):
        self.language = lang


class _FakeModel:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []

    def transcribe(self, audio, language=None, beam_size=1, vad_filter=False):
        self.calls.append(language)

        def gen():
            time.sleep(self.delay)  # faster-whisper decodes lazily, while segments are drained
            yield _Seg(" hello there ")
        return gen(), _Info(language or "en")


sl.LocalWhisperSTTService._shared_model = None
sl._reset_status_for_tests()
check("stt status before any load: not loaded, no latency",
      sl.status().get("state") == "not_loaded" and "latency_ms_median" not in sl.status(), str(sl.status()))

_real_new = sl._new_model


def _boom(model, device, compute):
    raise RuntimeError("CUDA driver missing")

sl._new_model = _boom
raised = False
try:
    sl.preload_shared_model("large-v3-turbo", "cuda", "float16")
except RuntimeError:
    raised = True
st = sl.status()
check("stt preload failure is recorded (state error + reason) and still raised (bot.py stays non-fatal)",
      raised and st.get("state") == "error" and "CUDA driver missing" in (st.get("error") or "")
      and st.get("model") == "large-v3-turbo", str(st))

class _WarmupBoom(_FakeModel):
    def transcribe(self, *a, **k):
        raise RuntimeError("cuBLAS failed to load")


sl._new_model = lambda model, device, compute: _WarmupBoom()
raised = False
try:
    sl.preload_shared_model("small", "cuda", "float16")
except RuntimeError:
    raised = True
st = sl.status()
check("stt warm-up failure (where cuBLAS loads) is recorded as error, not stuck on loading",
      raised and st.get("state") == "error" and "cuBLAS" in (st.get("error") or "")
      and sl.LocalWhisperSTTService._shared_model is None, str(st))

sl._new_model = lambda model, device, compute: _FakeModel()
sl.preload_shared_model("medium", "cuda", "float16")
st = sl.status()
check("stt preload success: ready with model/device/compute, error cleared",
      st.get("state") == "ready" and st.get("model") == "medium" and st.get("device") == "cuda"
      and st.get("compute") == "float16" and "error" not in st, str(st))
check("stt status never carries a load time (stays in the log only)", "ready_s" not in st and "load_s" not in st)
sl._new_model = _real_new

# ── stt_local: per-command latency ring + live language ──────────────


def _wav(seconds=0.5):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return b.getvalue()


fake = _FakeModel(delay=0.02)
sl.LocalWhisperSTTService._shared_model = fake
svc = sl.LocalWhisperSTTService(model="medium", device="cuda", compute_type="float16", language=None)
LANG = {"v": None}
sl._language_hook = lambda: LANG["v"]


async def _say(n):
    out = []
    for _ in range(n):
        out.append(await svc._transcribe(_wav()))
    return out

res = asyncio.run(_say(3))
st = sl.status()
check("stt transcription still returns the text", res[0].text == "hello there", repr(res[0].text))
check("stt latency: each command timed (incl. draining segments); median reported in ms",
      st.get("samples") == 3 and 15 <= (st.get("latency_ms_median") or 0) < 500, str(st))
asyncio.run(_say(12))
check("stt latency ring keeps the last 10 only", sl.status().get("samples") == 10, str(sl.status()))
check("stt language: unset -> auto-detect (None passed to Whisper)", fake.calls[-1] is None, str(fake.calls[-3:]))
LANG["v"] = "en"
asyncio.run(_say(1))
check("stt language applies LIVE: the next utterance uses the new code, same session",
      fake.calls[-1] == "en", str(fake.calls[-3:]))
LANG["v"] = "auto"
asyncio.run(_say(1))
check("stt language 'auto' -> None", fake.calls[-1] is None, str(fake.calls[-3:]))

def _hook_boom():
    raise RuntimeError("config hiccup")

sl._language_hook = _hook_boom
svc_auto = sl.LocalWhisperSTTService(model="medium", device="cuda", compute_type="float16", language="auto")
asyncio.run(svc_auto._transcribe(_wav()))
check("stt hook failure falls back to the constructor language, with 'auto' mapped to None",
      fake.calls[-1] is None, str(fake.calls[-2:]))

sl.LocalWhisperSTTService._shared_model = None

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
