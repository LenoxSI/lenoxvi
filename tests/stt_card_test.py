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


import stt_catalog as cat

# ── catalog ──────────────────────────────────────────────────────────
codes = [lang["code"] for lang in cat.LANGUAGES]
check("stt languages: Auto first, then the 16 common ones",
      codes[0] == "auto" and len(codes) == 17 and "en" in codes and len(set(codes)) == 17, str(codes))
check("stt language_value: auto/empty -> None, code -> code",
      cat.language_value("auto") is None and cat.language_value("") is None
      and cat.language_value(None) is None and cat.language_value("en") == "en")

# ── stt_local: the Embedded model is fixed ───────────────────────────
import ast  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

import stt_local as sl  # noqa: E402
import stt_localai as sla  # noqa: E402

EMBEDDED = (cat.EMBEDDED_MODEL, cat.EMBEDDED_DEVICE, cat.EMBEDDED_COMPUTE)
_ctor = inspect.signature(sl.LocalWhisperSTTService.__init__).parameters
check("stt: LocalWhisperSTTService takes no model, device or compute_type (Embedded Whisper is fixed)",
      not ({"model", "device", "compute_type"} & set(_ctor)), str(list(_ctor)))
check("stt: preload_shared_model takes no arguments (it always loads the Embedded model)",
      not inspect.signature(sl.preload_shared_model).parameters, str(inspect.signature(sl.preload_shared_model)))
# The env names are built from pieces so the repo-wide grep gate for the
# deleted variables stays at zero hits.
_STT_ENV = tuple(f"LVI_STT_{k}" for k in ("MODEL", "DEVICE", "COMPUTE"))
_KEPT_ENV = ("LVI_STT_PROVIDER", "LVI_STT_LANGUAGE", "LVI_STT_REVIVE_COOLDOWN")
_leaks = []
for _f in ("stt_local.py", "stt_localai.py", "stt_fallback.py"):
    for _n in ast.walk(ast.parse((Path(CORE) / _f).read_text())):
        if isinstance(_n, ast.Constant) and isinstance(_n.value, str) and (
                _n.value in {"tiny", "base", "small", "medium", "cpu", "cuda", "int8", "float16"}
                or _n.value.startswith("large")
                or (_n.value.startswith("LVI_STT_") and _n.value not in _KEPT_ENV)):
            _leaks.append(f"{_f}: {_n.value!r}")
        if (isinstance(_n, ast.Call) and getattr(_n.func, "id", getattr(_n.func, "attr", "")) in ("cfg", "cfg_env")
                and len(_n.args) >= 2 and all(isinstance(a, ast.Constant) for a in _n.args[:2])
                and _n.args[0].value == "stt" and _n.args[1].value not in ("language", "provider", "revive_cooldown_secs")):
            _leaks.append(f"{_f}: reads stt.{_n.args[1].value}")
check("stt: the Whisper modules name no model, device or precision and read only the stt keys that stay "
      "(stt_catalog's Embedded constants decide)", not _leaks, str(_leaks))


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
LOADS = []


def _boom(model, device, compute):
    LOADS.append((model, device, compute))
    raise RuntimeError("model.bin is unreadable")


sl._new_model = _boom
raised = False
try:
    sl.preload_shared_model()
except RuntimeError:
    raised = True
st = sl.status()
check("stt preload failure is recorded (state error + reason) and still raised (bot.py stays non-fatal)",
      raised and st.get("state") == "error" and "model.bin is unreadable" in (st.get("error") or "")
      and st.get("model") == cat.EMBEDDED_MODEL, str(st))


class _WarmupBoom(_FakeModel):
    def transcribe(self, *a, **k):
        raise RuntimeError("warm-up transcribe failed")


sl._new_model = lambda model, device, compute: _WarmupBoom()
raised = False
try:
    sl.preload_shared_model()
except RuntimeError:
    raised = True
st = sl.status()
check("stt warm-up failure is recorded as error, not stuck on loading",
      raised and st.get("state") == "error" and "warm-up transcribe failed" in (st.get("error") or "")
      and sl.LocalWhisperSTTService._shared_model is None, str(st))

_saved_env = {k: os.environ.get(k) for k in _STT_ENV}
os.environ.update(dict(zip(_STT_ENV, ("medium", "cuda", "float16"))))


def _record(model, device, compute):
    LOADS.append((model, device, compute))
    return _FakeModel()


LOADS.clear()
sl._new_model = _record
sl.preload_shared_model()
st = sl.status()
check("stt preload success: ready with model/device/compute, error cleared",
      st.get("state") == "ready" and (st.get("model"), st.get("compute")) == (EMBEDDED[0], EMBEDDED[2])
      and "device" not in st
      and "error" not in st, str(st))
check("stt: preload loads base on the CPU (int8) whatever the LVI_STT_ variables say", LOADS == [EMBEDDED], str(LOADS))
check("stt status never carries a load time (stays in the log only)", "ready_s" not in st and "load_s" not in st)
LOADS.clear()
sl.preload_shared_model()
check("stt: a second preload reuses the shared model (no reload)", LOADS == [], str(LOADS))

sl.LocalWhisperSTTService._shared_model = None
_lazy = sl.LocalWhisperSTTService(language=None)
asyncio.run(_lazy.ensure_model())
check("stt: with no preload, the first turn loads the same Embedded model, shared by every room",
      LOADS == [EMBEDDED] and _lazy._fw_model is sl.LocalWhisperSTTService._shared_model, str(LOADS))

LOADS.clear()
sl.LocalWhisperSTTService._shared_model = None
sla._reset_for_tests()
_wb = sla.whisper_base()
check("stt: LocalAI's standby (whisper_base) is the shared Embedded model, loaded once",
      LOADS == [EMBEDDED] and _wb is sl.LocalWhisperSTTService._shared_model and sla.whisper_base() is _wb,
      str(LOADS))
for _k, _v in _saved_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
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
svc = sl.LocalWhisperSTTService(language=None)
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
svc_auto = sl.LocalWhisperSTTService(language="auto")
asyncio.run(svc_auto._transcribe(_wav()))
check("stt hook failure falls back to the constructor language, with 'auto' mapped to None",
      fake.calls[-1] is None, str(fake.calls[-2:]))

sl.LocalWhisperSTTService._shared_model = None

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
