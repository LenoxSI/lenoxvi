"""Headless GPU transcribe test: proves faster-whisper runs on a CUDA GPU.

Generates a 1.5s 300Hz amplitude-modulated tone, transcribes it (expect
empty/silence output, NOT an error), and prints VRAM usage. Run with the
venv python; LD_LIBRARY_PATH for the CUDA wheels is set by the systemd
unit (lenoxvi-bot.service), not by any repo script.
"""

import asyncio
import io
import wave

import numpy as np


def make_wav(seconds: float = 1.5, sr: int = 16000) -> bytes:
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    # "speech-like": amplitude-modulated tone so VAD-ish segments have energy
    sig = 0.5 * np.sin(2 * np.pi * 300 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    pcm = (sig * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


async def main():
    from faster_whisper import WhisperModel

    print("loading large-v3-turbo on cuda float16 ...")
    model = await asyncio.to_thread(
        WhisperModel, "large-v3-turbo", device="cuda", compute_type="float16"
    )
    print("model loaded; transcribing test WAV ...")
    segments, info = model.transcribe(io.BytesIO(make_wav()), beam_size=1, vad_filter=False)
    text = " ".join(s.text.strip() for s in segments).strip()
    print(f"transcribed: [{text!r}] lang={info.language}")
    print("GPU-STT-OK")


asyncio.run(main())