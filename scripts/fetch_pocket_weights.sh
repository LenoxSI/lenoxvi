#!/usr/bin/env bash
# fetch_pocket_weights.sh — pre-download the Pocket TTS model and every
# preset voice the dashboard offers, so a box with no internet at boot can
# still speak (the local fallback's whole point).
#
# Run once after `pip install -r requirements-pocket.txt`, from the repo
# root, with the bot's Python:
#   PYTHON=/path/to/venv/bin/python bash scripts/fetch_pocket_weights.sh
#
# No revision list lives here: pocket-tts pins every hf:// path to an exact
# commit in its own config, so the package version (requirements-pocket.txt)
# decides the weights. Files land in the standard Hugging Face cache
# (~/.cache/huggingface), about 350 MB.
#
# Loading from a warm cache with no network works, but huggingface_hub
# retries the network first (~25 s) before using the cache.
set -euo pipefail

PY="${PYTHON:-python}"
cd "$(dirname "$0")/.."

"$PY" - <<'EOF'
import ast
import time
from pathlib import Path

# The voice list the dashboard offers, read from core/tts_pocket.py without
# importing it (so this runs in any venv that has pocket-tts).
tree = ast.parse(Path("core/tts_pocket.py").read_text())
voices = next(ast.literal_eval(n.value) for n in ast.walk(tree)
              if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "POCKET_VOICES")

from pocket_tts import TTSModel

t0 = time.monotonic()
model = TTSModel.load_model(language="english")
print(f"ok  model loaded ({time.monotonic() - t0:.1f}s, sample rate {model.sample_rate})")
bad = []
for v in voices:
    try:
        model.get_state_for_audio_prompt(v)
    except Exception as e:
        bad.append(f"{v}: {e}")
if bad:
    raise SystemExit("FAILED voices:\n  " + "\n  ".join(bad))
print(f"ok  {len(voices)} preset voices cached")
EOF
