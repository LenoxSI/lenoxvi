"""Live LocalAI round trips — silent (no speaker, no node).

  LVI_TEST_LIVE=1 LVI_LOCALAI_URL=<host:port> LVI_LOCALAI_KEY=<key> \
      python tests/manual/localai_live_test.py core

Reads the URL and key from the environment only and never prints the key.
Uses the golden clips in tests/fixtures/golden/.
"""
import os
import sys
import time

CORE = sys.argv[1] if len(sys.argv) > 1 else "core"
sys.path.insert(0, CORE)
if os.environ.get("LVI_TEST_LIVE") != "1":
    print("SKIP live: set LVI_TEST_LIVE=1")
    sys.exit(0)

import localai as la  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


URL = la.normalize_url(os.environ.get("LVI_LOCALAI_URL"))
HERE = os.path.dirname(os.path.abspath(__file__))
ok, ms, why = la.probe(url=URL)
check("live: LocalAI answers with the key", ok, str(why))
d = la.discover(url=URL)
print(f"  discovered: {d}")
check("live: discovery finds a speech-to-text model and a voice model", bool(d["stt"]) and bool(d["tts"]), str(d))
stt_model = d["stt"][0] if d["stt"] else ""
wav = b""
for c in ("clip1", "clip2", "clip3"):
    with open(os.path.join(HERE, "..", "fixtures", "golden", f"{c}.wav"), "rb") as f:
        wav = f.read()
    t0 = time.monotonic()
    try:
        text = la.transcribe(wav, stt_model, "en", url=URL)
    except la.LocalAIError as e:
        text = ""
        print(f"  {c}: error {e.reason}")
    print(f"  {c}: {(time.monotonic() - t0) * 1000:.0f} ms [{text}]")
    check(f"live: {c} transcribed", bool(text))
tts_model = d["tts"][0]["id"] if d["tts"] else ""
t0 = time.monotonic()
pcm, rate = la.speech("The kitchen lights are now off.", tts_model, "af_heart", 1.0, url=URL)
print(f"  speech: {(time.monotonic() - t0) * 1000:.0f} ms, {len(pcm) // 2 / rate:.2f} s of audio at {rate} Hz")
check("live: speech returns mono 16-bit audio with a real rate", len(pcm) > 1000 and 8000 <= rate <= 48000, str(rate))
try:
    la.transcribe(wav, stt_model, "en", url=URL, key="wrong-key-0000000000")
    check("live: a wrong key is refused", False, "accepted")
except la.LocalAIError as e:
    check("live: a wrong key is refused with 401", e.status == 401, e.reason)
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
