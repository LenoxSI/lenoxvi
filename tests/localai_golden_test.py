"""Golden of the LOCAL speech path — the LocalAI work must not move it.

Run on the host (pipecat + faster-whisper + kokoro-onnx installed):
  cd tests && ~/venvs/lenoxvi/bin/python localai_golden_test.py <path-to-core> [record]

`record` writes fixtures/golden/golden_local.json from the CURRENT tree (do
it from main); without it the test compares. What it pins:
- Whisper: the transcripts LocalWhisperSTTService._transcribe returns for
  three fixed clips, on the Embedded model (base, CPU int8, deterministic).
- Kokoro: the PCM TrackedKokoroTTSService.run_tts yields for three fixed
  sentences, pinned as signal statistics — sample count, RMS, peak —
  compared with a relative tolerance: neural TTS float output drifts
  across CPUs and onnxruntime builds, so exact bytes are not portable
  (issue #49). The sha256 in each golden entry stays an informational
  fingerprint, printed when it differs, never a failure.
The clips were made once with Kokoro (am_adam, 1.0) and resampled to 16 kHz
mono with soxr; see make_golden_clips.py next to this file.
"""
import asyncio
import hashlib
import json
import math
import os
import sys
from array import array

# Relative tolerances for the kokoro golden stats compare (issue #49).
KOKORO_SAMPLES_TOL = 0.02  # sample count: catches speed and sample-rate regressions
KOKORO_RMS_TOL = 0.20      # catches voice and level regressions
KOKORO_PEAK_TOL = 0.25     # catches voice change and clipping; noisier than RMS, so looser

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
WHISPER_MODELS = ["base"]  # the Embedded model: LocalWhisperSTTService can run nothing else
SENTENCES = [
    "The kitchen lights are now off.",
    "Your timer for 10 minutes has started.",
    "It is 9:15 in the morning, and the high today is 72 degrees.",
]

import stt_local  # noqa: E402


async def whisper_text(wav: bytes) -> str:
    stt_local.LocalWhisperSTTService._shared_model = None
    stt_local._language_hook = lambda: "en"
    svc = stt_local.LocalWhisperSTTService(language="en")
    try:
        return (await svc._transcribe(wav)).text
    finally:
        await svc.cleanup()


def pcm_stats(pcm: bytes) -> dict:
    """Signal statistics of int16 mono PCM — sample count, RMS in int16
    units, peak — with the byte sha256 alongside as an informational
    fingerprint. Stats only, never per-sample: cross-machine float drift
    decorrelates individual samples, so only the statistics are portable
    (issue #49)."""
    assert array("h").itemsize == 2, "int16 samples expected on this platform"
    s = array("h", pcm)
    n = len(s)
    assert n, "no PCM to measure"
    return {
        "sha256": hashlib.sha256(pcm).hexdigest(),
        "samples": n,
        "rms": math.sqrt(sum(x * x for x in s) / n),
        "peak": max(abs(x) for x in s),
    }


async def kokoro_stats(text: str) -> dict:
    import tts_kokoro

    class Mixin:
        pass

    svc = tts_kokoro.make_service(mixin=Mixin, voice="af_heart", speed=1.0)
    assert svc is not None, "Kokoro service did not build (model files?)"
    svc._sample_rate = 24000  # what StartFrame sets in a pipeline (same idiom as stt_fallback_test)
    pcm = bytearray()
    async for f in svc.run_tts(text, "golden"):
        audio = getattr(f, "audio", None)
        if audio:
            pcm += audio
    assert pcm, f"no audio frames for {text!r}"
    return pcm_stats(bytes(pcm))


def kokoro_stat_failures(got: dict, want: dict) -> list[str]:
    """Which golden stats sit outside their relative tolerance, as printable
    lines. The sha256 is deliberately absent: a byte mismatch alone never
    fails the test."""
    out = []
    for key, tol in (("samples", KOKORO_SAMPLES_TOL), ("rms", KOKORO_RMS_TOL), ("peak", KOKORO_PEAK_TOL)):
        if abs(got[key] - want[key]) > tol * abs(want[key]):
            out.append(f"{key} {got[key]} vs {want[key]} (over {tol:.0%})")
    return out


async def main():
    import tts_kokoro
    assert tts_kokoro.preload(), "Kokoro failed to preload"
    out = {"whisper": {}, "kokoro": {}}
    for c in CLIPS:
        with open(os.path.join(GOLD_DIR, f"{c}.wav"), "rb") as f:
            wav = f.read()
        out["whisper"][c] = {}
        assert WHISPER_MODELS == [stt_local.stt_catalog.EMBEDDED_MODEL], "the golden pins the Embedded model"
        for m in WHISPER_MODELS:
            out["whisper"][c][m] = await whisper_text(wav)
    for s in SENTENCES:
        out["kokoro"][s] = await kokoro_stats(s)
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
        got, want = out["kokoro"][s], gold["kokoro"][s]
        if not isinstance(want, dict) or "samples" not in want:
            check(f"golden: kokoro stats within tolerance [{s[:28]}]", False,
                  "the golden entry predates the stats pin (sha256-only) — re-record: "
                  "cd tests && ~/venvs/lenoxvi/bin/python localai_golden_test.py ../core record")
            continue
        bad = kokoro_stat_failures(got, want)
        check(f"golden: kokoro stats within tolerance [{s[:28]}]", not bad, "; ".join(bad))
        if got["sha256"] != want["sha256"]:
            print(f"[note] golden: kokoro bytes differ [{s[:28]}] — sha256 {got['sha256']} vs {want['sha256']}")


asyncio.run(main())
if not RECORD:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
