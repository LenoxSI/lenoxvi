"""Kill test: STT + TTS against the real LocalAI, LocalAI stopped and started
mid-run. Silent. Same env as localai_live_test.py, plus LVI_KILL_CMD /
LVI_START_CMD (shell commands that stop/start LocalAI).
"""
import asyncio
import io
import os
import subprocess
import sys
import time

CORE = sys.argv[1] if len(sys.argv) > 1 else "core"
sys.path.insert(0, CORE)
if os.environ.get("LVI_TEST_LIVE") != "1":
    print("SKIP live: set LVI_TEST_LIVE=1")
    sys.exit(0)
import localai as la  # noqa: E402
import stt_localai  # noqa: E402
import stt_local  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


HERE = os.path.dirname(os.path.abspath(__file__))
wav = open(os.path.join(HERE, "..", "fixtures", "golden", "clip1.wav"), "rb").read()
stt_local.preload_shared_model()
base = stt_local.LocalWhisperSTTService._shared_model
la.set_cooldown(5)
FELL_BACK = []  # Whisper base was handed the clip
m = stt_localai.LocalAIWhisper(la.discover()["stt"][0], lambda: FELL_BACK.append(1) or base)


def hear():
    FELL_BACK.clear()
    t0 = time.monotonic()
    segs, _ = m.transcribe(io.BytesIO(wav), language="en")
    eng = "whisper" if FELL_BACK else "localai"
    return " ".join(s.text.strip() for s in segs), eng, (time.monotonic() - t0) * 1000


text, eng, ms = hear()
print(f"  up: {eng} {ms:.0f} ms [{text}]")
check("kill: LocalAI up -> LocalAI hears", eng == "localai" and "kitchen" in text.lower())
subprocess.run(os.environ["LVI_KILL_CMD"], shell=True, check=True)
time.sleep(2)
text, eng, ms = hear()
print(f"  killed: {eng} {ms:.0f} ms [{text}] reason={la.down_reason()}")
check("kill: LocalAI stopped -> the same clip is heard by Whisper base, fast", eng == "whisper"
      and "kitchen" in text.lower() and ms < 3000)
text, eng, ms = hear()
check("kill: next turn skips LocalAI at once", eng == "whisper" and ms < 3000, f"{ms:.0f} ms")
subprocess.run(os.environ["LVI_START_CMD"], shell=True, check=True)
t0 = time.monotonic()
while la.down_reason() and time.monotonic() - t0 < 90:
    la.should_try()
    time.sleep(1)
print(f"  back after {time.monotonic() - t0:.0f} s")
check("kill: the background probe brings LocalAI back", la.down_reason() is None)
text, eng, ms = hear()
print(f"  back: {eng} {ms:.0f} ms [{text}]")
check("kill: LocalAI hears again", eng == "localai" and "kitchen" in text.lower())
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
