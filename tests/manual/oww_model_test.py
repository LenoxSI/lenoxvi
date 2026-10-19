"""Headless test: does a wake-word OWW model load and behave?
Usage: python tests/manual/oww_model_test.py <path/to/model.onnx>

Feed 10s of synthetic noise (no wake word) — scores must stay LOW.
We cannot synthesize a positive without real wake-phrase audio, so the
positive case is validated by a live test.
"""

import sys
import time

import numpy as np

MODEL = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)


def main():
    from openwakeword.model import Model

    t0 = time.time()
    m = Model(wakeword_model_paths=[MODEL])
    print(f"model loaded in {time.time() - t0:.1f}s")

    names = list(m.models.keys())
    print("prediction keys:", names)

    # 10s of pink-ish noise at 16kHz, chunked at OWW's 80ms frame
    sr = 16000
    chunk = int(sr * 0.08)
    t = np.linspace(0, 10, sr * 10, endpoint=False)
    noise = (np.random.default_rng(42).standard_normal(t.shape) * 3000).astype(np.int16)

    scores = []
    for i in range(0, len(noise), chunk):
        scores.append(m.predict(noise[i : i + chunk])[names[0]])
    peak = max(scores)
    print(f"noise chunks: {len(scores)}, peak score: {peak:.3f}")
    assert peak < 0.5, f"model fired on noise (peak {peak:.3f}) — false positives must stay under 0.5"
    print("OWW-MODEL-OK (no false fire on noise; positive case awaits live test)")


if __name__ == "__main__":
    main()