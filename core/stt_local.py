"""Local faster-whisper STT service for LenoxVI (pipecat 1.11).

Subclasses pipecat's Whisper base STT (the OpenAI-API one) and overrides
`_transcribe` to run faster-whisper on this machine instead of hitting the
OpenAI API: the fixed Embedded model (stt_catalog: base on the CPU, int8).
The base class gives us segmentation, VAD interplay, metrics and
TranscriptionFrame plumbing for free.
"""

import asyncio
import io
import logging
import re
import statistics
import time
from collections import deque
from typing import Optional

from loguru import logger
from openai.types.audio import Transcription

from pipecat.services.whisper.base_stt import BaseWhisperSTTService

import stt_catalog


class _NoHFTokenNag(logging.Filter):
    """Drop the Hugging Face Hub's "set a HF_TOKEN" server notice. Model
    downloads work without an account; on a fresh box the notice reads like
    an error. Every other Hub warning still logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "HF_TOKEN" not in record.getMessage()


logging.getLogger("huggingface_hub.utils._http").addFilter(_NoHFTokenNag())

# Whisper invents these from near-silence (caption-heavy training data).
# Dropped only when the WHOLE utterance is one of them.
_PHANTOM_PHRASES = frozenset({
    "thanks for watching",
    "thank you for watching",
    "thanks for watching everyone",
    "thank you so much for watching",
    "please subscribe",
    "like and subscribe",
    "please like and subscribe",
    "dont forget to subscribe",
    "see you in the next video",
})
_PHANTOM_PREFIXES = ("subtitles by", "captions by", "transcribed by", "transcription by")


def is_phantom_transcript(text: str) -> bool:
    """True when a transcript is Whisper making text out of noise: a known
    caption phrase as the whole utterance.

    No segment-score test here: faster-whisper already skips segments with
    no_speech_prob > 0.6 unless avg_logprob > -1 (its defaults, left on), so
    no segment it yields could ever meet that rule here."""
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).split())
    if not norm:
        return False
    return norm in _PHANTOM_PHRASES or norm.startswith(_PHANTOM_PREFIXES)

# ── what the dashboard's STT card shows (read by /status, no extra polling) ──
# Load state of the ONE shared model, and per-command transcription time
# (last 10, in memory, reset on restart). Load time stays in the log only.
_STATUS: dict = {"state": "not_loaded"}
_LAT_MS: deque = deque(maxlen=10)
# "compute": Whisper's own transcribe time (local provider).
# "e2e": end of speech -> final transcript, fed by stt_fallback.SttTimer
#        (cloud provider; Whisper's compute time is then NOT recorded, so a
#        fallback turn isn't counted twice).
_LAT_MODE = {"mode": "compute"}


def set_latency_mode(mode: str) -> None:
    _LAT_MODE["mode"] = "e2e" if mode == "e2e" else "compute"


def latency_mode() -> str:
    return _LAT_MODE["mode"]


def note_latency(ms: float) -> None:
    _LAT_MS.append(float(ms))


def _set_status(state: str, model: str, compute: str, error: str | None = None) -> None:
    global _STATUS
    _STATUS = {"state": state, "model": model, "compute": compute}
    if error:
        _STATUS["error"] = error


def status() -> dict:
    out = dict(_STATUS)
    out["samples"] = len(_LAT_MS)
    lat = list(_LAT_MS)  # snapshot: the executor thread appends concurrently
    if lat:
        out["latency_ms_median"] = round(statistics.median(lat))
    return out


def _reset_status_for_tests() -> None:
    global _STATUS
    _STATUS = {"state": "not_loaded"}
    _LAT_MS.clear()


def _new_model(model: str, device: str, compute: str):
    """The one place a WhisperModel is constructed (patchable in tests)."""
    from faster_whisper import WhisperModel
    return WhisperModel(model, device=device, compute_type=compute)


def _embedded() -> tuple[str, str, str]:
    """The one Whisper this appliance runs: stt_catalog's Embedded model."""
    return stt_catalog.EMBEDDED_MODEL, stt_catalog.EMBEDDED_DEVICE, stt_catalog.EMBEDDED_COMPUTE


def _live_language():
    """stt.language, read per utterance so a dashboard change applies to the
    next thing said — config > env, same chain as at construction."""
    from config import cfg_env
    return cfg_env("stt", "language", "LVI_STT_LANGUAGE")


_language_hook = _live_language


def _rss_mb() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def _trim_heap(why: str) -> None:
    """Hand freed heap back to the OS (glibc malloc_trim). Loading a model
    streams its weights through RAM; in a long-lived, fragmented heap that
    transient peak was KEPT (2026-10-15: the first voice turn of a 7.5 h-old
    process took the bot 768 MB -> 2.8 GB, of which only ~612 MB was the
    model's real cost). Never raises."""
    try:
        import ctypes

        before = _rss_mb()
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        logger.info(f"stt_local: malloc_trim after {why}: RSS {before} -> {_rss_mb()} MB")
    except Exception as e:  # non-glibc / no libc: harmless to skip
        logger.debug(f"stt_local: malloc_trim unavailable: {e}")


def preload_shared_model() -> None:
    """Load + warm the shared Whisper model — the fixed Embedded model
    (stt_catalog: base on the CPU, int8) — at bot STARTUP (sync, before the
    server runs), instead of lazily on the first voice turn.

    Why: the lazy load happened inside a heap fragmented by hours of
    sessions, so its transient peak stuck as RSS. At startup the heap is
    fresh and malloc_trim returns the peak. Bonus: the first wake of the day
    doesn't wait for the model. Failure is non-fatal — the lazy path in
    ensure_model() still works."""
    if LocalWhisperSTTService._shared_model is not None:
        return
    import numpy as np

    model, device, compute = _embedded()
    t0 = time.monotonic()
    before = _rss_mb()
    logger.info(f"stt_local: preloading faster-whisper {model} on {device} ({compute}) at startup")
    _set_status("loading", model, compute)
    try:
        m = _new_model(model, device, compute)
        # Warm-up: the first encode sets up the decoder — do it now, not
        # inside someone's first question. 1 s of silence, result discarded.
        # Inside the try: a warm-up that fails must show as an error on the
        # dashboard, not a "Loading…" that never ends.
        segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language="en")
        list(segs)
    except Exception as e:
        # Recorded for the dashboard, then re-raised: bot.py treats a failed
        # preload as non-fatal (the lazy path tries again).
        _set_status("error", model, compute, error=f"{type(e).__name__}: {e}"[:300])
        raise
    LocalWhisperSTTService._shared_model = m
    _set_status("ready", model, compute)
    logger.info(f"stt_local: whisper ready in {time.monotonic() - t0:.1f}s "
                f"(RSS {before} -> {_rss_mb()} MB before trim)")
    _trim_heap("startup preload")


class LocalWhisperSTTService(BaseWhisperSTTService):
    """Segmented STT backed by the local faster-whisper model.

    The model is fixed (stt_catalog's Embedded base on the CPU, int8), cached
    at module level and shared across ALL pipelines: the bot constructs a
    fresh STT per WebRTC offer, so instance-caching would reload it on every
    node reconnect (node blip, crash-retry, reboot). One model per process.

    Args:
        language: ISO language hint, None for auto-detect (the live
            stt.language, read per utterance, wins; this is its fallback).
        model_override: a stand-in with faster-whisper's transcribe() shape
            (LocalAI), used instead of the shared model.
    """

    _shared_model = None
    _shared_lock = asyncio.Lock()

    def __init__(
        self,
        *,
        language: Optional[str] = None,
        model_override=None,
        **kwargs,
    ):
        super().__init__(api_key="sk-local-not-used", **kwargs)
        self._language = language
        self._fw_model = None
        # A stand-in with faster-whisper's transcribe() shape (LocalAI): used
        # instead of the shared model, which is then never loaded here.
        self._model_override = model_override

    async def ensure_model(self):
        """Load the model once per process, shared across pipelines."""
        if self._model_override is not None:
            self._fw_model = self._model_override
            return
        async with self._shared_lock:
            if LocalWhisperSTTService._shared_model is not None:
                self._fw_model = LocalWhisperSTTService._shared_model
                return
            model, device, compute = _embedded()
            logger.info(f"Loading faster-whisper {model} on {device} ({compute})")
            _set_status("loading", model, compute)
            try:
                LocalWhisperSTTService._shared_model = await asyncio.to_thread(_new_model, model, device, compute)
            except Exception as e:
                _set_status("error", model, compute, error=f"{type(e).__name__}: {e}"[:300])
                raise
            _set_status("ready", model, compute)
            self._fw_model = LocalWhisperSTTService._shared_model
            _trim_heap("lazy whisper load")

    async def cleanup(self):
        """Close the base class's unused OpenAI client explicitly.

        BaseWhisperSTTService (pipecat's cloud-Whisper base) builds an
        AsyncOpenAI/httpx2 client in __init__ even though this service never
        calls OpenAI. Left alone, it is closed only by the openai SDK's
        finalizer when GC eventually reclaims the session — a stray
        aclose() task scheduled per dead session. Close it at teardown,
        bounded so a hung close can never stall pipeline shutdown.
        """
        await super().cleanup()
        client = getattr(self, "_client", None)
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=2.0)
            except Exception as e:  # never block teardown on an unused client
                logger.debug(f"local-whisper: unused OpenAI client close: {e!r}")
            self._client = None

    async def _transcribe(self, audio: bytes) -> Transcription:
        """Transcribe raw WAV audio bytes with faster-whisper.

        The base class hands us WAV bytes; we run the (blocking) transcribe
        call in a thread so the event loop stays live.
        """
        await self.ensure_model()
        loop = asyncio.get_running_loop()
        # Language is read per utterance (dashboard changes apply live).
        try:
            language = stt_catalog.language_value(_language_hook())
        except Exception:  # noqa: BLE001 — a config hiccup must never cost a transcript
            language = stt_catalog.language_value(self._language)

        def _run():
            t0 = time.monotonic()
            segments, info = self._fw_model.transcribe(
                io.BytesIO(audio),
                language=language,
                beam_size=1,
                vad_filter=False,  # pipecat's Silero VAD already gates segments
            )
            # faster-whisper decodes lazily: the work happens while the
            # segments are drained, so the timer stops after the join.
            segs = list(segments)
            text = " ".join(s.text.strip() for s in segs).strip()
            if is_phantom_transcript(text):
                logger.info(f"local-whisper dropped phantom [{text}]")
                text = ""
            if _LAT_MODE["mode"] == "compute":
                _LAT_MS.append((time.monotonic() - t0) * 1000)
            return text, info.language

        text, lang = await loop.run_in_executor(None, _run)
        if language:
            lang = language
        logger.debug(f"local-whisper heard: [{text}] ({lang})")
        return Transcription(text=text)