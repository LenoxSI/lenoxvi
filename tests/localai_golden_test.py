"""Golden of the LOCAL speech path — the LocalAI work must not move it.

Run on the host (pipecat + faster-whisper + kokoro-onnx installed):
  cd tests && ~/venvs/lenoxvi/bin/python localai_golden_test.py <path-to-core> [record]

`record` writes fixtures/golden/golden_local.json from the CURRENT tree (do
it from main); without it the test compares. What it pins:
- Whisper: the transcripts LocalWhisperSTTService._transcribe returns for
  three fixed clips, on CPU int8 (deterministic), models base + medium.
- Kokoro: sha256 of the PCM TrackedKokoroTTSService.run_tts yields for
  three fixed sentences (CPU ONNX, deterministic).
The clips were made once with Kokoro (am_adam, 1.0) and resampled to 16 kHz
mono with soxr; see make_golden_clips.py next to this file.
"""
import asyncio
import hashlib
import json
import os
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
RECORD = len(sys.argv) > 2 and sys.argv[2] == "record"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


HERE = os.path.dirname(os.path.abspath(__file__))
GOLD_DIR = os.path.join(HERE, "fixtures", "golden")
GOLD = os.path.join(GOLD_DIR, "golden_local.json")
CLIPS = ["clip1", "clip2", "clip3"]
WHISPER_MODELS = ["base", "medium"]
SENTENCES = [
    "The kitchen lights are now off.",
    "Your timer for 10 minutes has started.",
    "It is 9:15 in the morning, and the high today is 72 degrees.",
]

import stt_local  # noqa: E402


async def whisper_text(model: str, wav: bytes) -> str:
    stt_local.LocalWhisperSTTService._shared_model = None
    stt_local._language_hook = lambda: "en"
    svc = stt_local.LocalWhisperSTTService(model=model, device="cpu", compute_type="int8", language="en")
    try:
        return (await svc._transcribe(wav)).text
    finally:
        await svc.cleanup()


async def kokoro_hash(text: str) -> str:
    import tts_kokoro

    class Mixin:
        pass

    svc = tts_kokoro.make_service(mixin=Mixin, voice="af_heart", speed=1.0)
    assert svc is not None, "Kokoro service did not build (model files?)"
    svc._sample_rate = 24000  # what StartFrame sets in a pipeline (same idiom as stt_fallback_test)
    h = hashlib.sha256()
    n = 0
    async for f in svc.run_tts(text, "golden"):
        audio = getattr(f, "audio", None)
        if audio:
            h.update(audio)
            n += 1
    assert n, f"no audio frames for {text!r}"
    return h.hexdigest()


async def main():
    import tts_kokoro
    assert tts_kokoro.preload(), "Kokoro failed to preload"
    out = {"whisper": {}, "kokoro": {}}
    for c in CLIPS:
        with open(os.path.join(GOLD_DIR, f"{c}.wav"), "rb") as f:
            wav = f.read()
        out["whisper"][c] = {}
        for m in WHISPER_MODELS:
            out["whisper"][c][m] = await whisper_text(m, wav)
    for s in SENTENCES:
        out["kokoro"][s] = await kokoro_hash(s)
    stt_local.LocalWhisperSTTService._shared_model = None
    if RECORD:
        with open(GOLD, "w") as f:
            json.dump(out, f, indent=1, sort_keys=True)
            f.write("\n")
        print(json.dumps(out, indent=1))
        print("recorded", GOLD)
        return
    with open(GOLD) as f:
        gold = json.load(f)
    for c in CLIPS:
        for m in WHISPER_MODELS:
            got, want = out["whisper"][c][m], gold["whisper"][c][m]
            check(f"golden: whisper {m} {c} transcript unchanged", got == want, f"{got!r} != {want!r}")
    for s in SENTENCES:
        check(f"golden: kokoro pcm unchanged [{s[:28]}]", out["kokoro"][s] == gold["kokoro"][s])


asyncio.run(main())
if not RECORD:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
