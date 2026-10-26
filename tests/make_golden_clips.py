"""One-off: make the three golden speech clips with Kokoro (CPU).

Run on the host from the repo root:
  ~/venvs/lenoxvi/bin/python tests/make_golden_clips.py core
Writes tests/fixtures/golden/clip{1,2,3}.wav — 16 kHz, mono, 16-bit.
Kept in the repo so the clips can be remade the same way; the WAVs
themselves are the fixture (re-running is not required).
"""
import os
import sys
import wave

import numpy as np
import soxr

CORE = sys.argv[1] if len(sys.argv) > 1 else "core"
sys.path.insert(0, CORE)
import tts_kokoro  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "golden")
CLIPS = {
    "clip1": "Turn on the kitchen lights.",
    "clip2": "Set a timer for ten minutes.",
    "clip3": "What is the weather tomorrow in the morning?",
}

assert tts_kokoro.preload(), "Kokoro failed to preload"
k = tts_kokoro.get_shared_model()
os.makedirs(OUT, exist_ok=True)
for name, text in CLIPS.items():
    samples, sr = k.create(text, voice="am_adam", speed=1.0, lang="en-us")
    pcm = np.clip(soxr.resample(samples, sr, 16000) * 32767, -32768, 32767).astype(np.int16)
    with wave.open(os.path.join(OUT, f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    print(name, len(pcm) / 16000, "s")
