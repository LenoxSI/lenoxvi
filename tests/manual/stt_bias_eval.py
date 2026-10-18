"""Measure faster-whisper's `hotwords` bias for a wake name on a synthetic test set.

Usage (bot venv, from the repo root; needs the optional Pocket TTS install):
    python tests/manual/stt_bias_eval.py --name <Name> --trap <similar real word>
On GPU, export the same LD_LIBRARY_PATH as the bot's service unit (the pip
nvidia cublas/cudnn/cuda_nvrtc lib dirs) or CTranslate2 can't find libcublas.

Generates clips locally with Pocket TTS (several voices), transcribes each
twice with the bot's Whisper settings — bias off, then hotwords=<name> — and
prints a JSON summary. Exit 0 = PASS, 1 = FAIL. The pass bar:
  * the name is spelled right MORE often with the bias on,
  * at most 2% of the name-free phrases change wording,
  * the trap word (a real word that sounds like the name) survives.

Synthetic voices are not your voice in your room — treat a PASS as "safe to
turn on", then watch the dashboard's recently-heard list for the real effect.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import wave

NAME_TEMPLATES = [
    "Hey {n}.", "Hey {n}, turn off the kitchen lights.", "{n}, what's the weather tomorrow?",
    "Okay {n}, set a timer for ten minutes.", "Hey {n}, is the front door locked?",
    "{n}, play some jazz in the living room.", "Hi {n}, what time is it?",
    "Hey {n}, remind me to call mom at five.", "{n}, how warm is it outside?",
    "Hey {n}, turn the volume down a bit.", "{n}, add milk to the shopping list.",
    "Hey {n}, good night.", "Ok {n}, dim the bedroom lights to thirty percent.",
    "{n}, what's on my calendar today?", "Hey {n}, stop.",
]
FREE_TEMPLATES = [
    "Turn off the kitchen lights.", "What's the weather tomorrow?", "Set a timer for ten minutes.",
    "Is the front door locked?", "Play some jazz in the living room.", "What time is it?",
    "Remind me to call mom at five.", "How warm is it outside?", "Add milk to the shopping list.",
    "Dim the bedroom lights to thirty percent.", "Good night everyone.", "What's on my calendar today?",
    "I need to install {t} on my old laptop.", "My brother runs {t} at work.", "Is {t} free to download?",
]
VOICES = ["alba", "jean", "marius", "jane", "charles", "eve", "george", "vera"]


def words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]", " ", s.lower()).split()


def wav_bytes(audio, sample_rate: int) -> bytes:
    import numpy as np
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", required=True, help="the wake name, e.g. Jarvis")
    ap.add_argument("--trap", required=True, help="a real word that sounds like the name")
    ap.add_argument("--voices", type=int, default=6)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="float16")
    ap.add_argument("--language", default=None, help="unset = auto-detect, like the bot's default")
    a = ap.parse_args()

    from faster_whisper import WhisperModel
    from pocket_tts import TTSModel

    tts = TTSModel.load_model(language="english")
    stt = WhisperModel(a.model, device=a.device, compute_type=a.compute)
    rows = []
    for voice in VOICES[: a.voices]:
        state = tts.get_state_for_audio_prompt(voice)
        cases = [("name", t) for t in NAME_TEMPLATES] + [("free", t) for t in FREE_TEMPLATES]
        for kind, tpl in cases:
            text = tpl.format(n=a.name, t=a.trap)
            audio = tts.generate_audio(state, text).numpy().reshape(-1)
            data = wav_bytes(audio, tts.sample_rate)
            out = {}
            for mode, kw in (("off", {}), ("on", {"hotwords": a.name})):
                segs, _ = stt.transcribe(io.BytesIO(data), language=a.language, beam_size=1,
                                         vad_filter=False, **kw)
                out[mode] = " ".join(s.text.strip() for s in segs).strip()
            rows.append({"voice": voice, "kind": kind, "text": text, **out})
            print(f"{voice:8} {kind:4} off={out['off']!r} on={out['on']!r}", file=sys.stderr)

    name, trap = a.name.lower(), a.trap.lower()
    name_rows = [r for r in rows if r["kind"] == "name"]
    free_rows = [r for r in rows if r["kind"] == "free"]

    def has_name(s):
        return name in words(s)

    off_rate = sum(has_name(r["off"]) for r in name_rows) / len(name_rows)
    on_rate = sum(has_name(r["on"]) for r in name_rows) / len(name_rows)
    changed = [r for r in free_rows if words(r["off"]) != words(r["on"])]
    trap_rows = [r for r in free_rows if trap in r["text"].lower()]
    trap_lost = [r for r in trap_rows if trap in words(r["off"]) and trap not in words(r["on"])]
    summary = {
        "clips": len(rows), "voices": a.voices,
        "name_rate_off": round(off_rate, 3), "name_rate_on": round(on_rate, 3),
        "free_changed": len(changed), "free_total": len(free_rows),
        "free_changed_pct": round(100 * len(changed) / len(free_rows), 2),
        "trap_rows": len(trap_rows), "trap_lost": len(trap_lost),
        "name_misses_off": sorted({r["off"] for r in name_rows if not has_name(r["off"])})[:12],
        "name_misses_on": sorted({r["on"] for r in name_rows if not has_name(r["on"])})[:12],
        "changed_examples": [{"off": r["off"], "on": r["on"]} for r in changed[:10]],
    }
    summary["PASS"] = bool(on_rate > off_rate and summary["free_changed_pct"] <= 2.0 and not trap_lost)
    print(json.dumps(summary, indent=1))
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
