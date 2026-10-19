"""Local faster-whisper STT service for lenoxvi (pipecat 1.11).

Subclasses pipecat's Whisper base STT (the OpenAI-API one) and overrides
`_transcribe` to run faster-whisper locally on the GPU instead of hitting
the OpenAI API. The base class gives us segmentation, VAD interplay, metrics
and TranscriptionFrame plumbing for free.
"""

import asyncio
import io
import statistics
import time
from collections import deque
from typing import Optional

from loguru import logger
from openai.types.audio import Transcription

from pipecat.services.whisper.base_stt import BaseWhisperSTTService

import stt_catalog

# ── what the dashboard's STT card shows (read by /status, no extra polling) ──
# Load state of the ONE shared model, and per-command transcription time
# (last 10, in memory, reset on restart). Load time stays in the log only.
_STATUS: dict = {"state": "not_loaded"}
_LAT_MS: deque = deque(maxlen=10)


def _set_status(state: str, model: str, device: str, compute: str, error: str | None = None) -> None:
    global _STATUS
    _STATUS = {"state": state, "model": model, "device": device, "compute": compute}
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
    """Hand freed heap back to the OS (glibc malloc_trim). Loading the model
    streams ~1.6 GB through RAM on its way to the GPU; in a long-lived,
    fragmented heap that transient peak was KEPT (2026-10-15: the first voice
    turn of a 7.5 h-old process took the bot 768 MB -> 2.8 GB, of which only
    ~612 MB was the model's real cost). Never raises."""
    try:
        import ctypes

        before = _rss_mb()
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        logger.info(f"stt_local: malloc_trim after {why}: RSS {before} -> {_rss_mb()} MB")
    except Exception as e:  # non-glibc / no libc: harmless to skip
        logger.debug(f"stt_local: malloc_trim unavailable: {e}")


def preload_shared_model(model: str, device: str, compute_type: str) -> None:
    """Load + warm the shared Whisper model at bot STARTUP (sync, before the
    server runs), instead of lazily on the first voice turn.

    Why: the lazy load happened inside a heap
    fragmented by hours of sessions, so its transient peak stuck as RSS. At
    startup the heap is fresh, the warm-up pulls cuBLAS/CUDA in too, and
    malloc_trim returns the peak. Bonus: the first wake of the day no
    longer waits ~2.5 s for the model. Failure is non-fatal — the lazy path in
    ensure_model() still works."""
    if LocalWhisperSTTService._shared_model is not None:
        return
    import numpy as np

    t0 = time.monotonic()
    before = _rss_mb()
    logger.info(f"stt_local: preloading faster-whisper {model} on {device} ({compute_type}) at startup")
    _set_status("loading", model, device, compute_type)
    try:
        m = _new_model(model, device, compute_type)
        # Warm-up: the first encode loads cuBLAS (+~0.5 GB) — do it now, not
        # inside someone's first question. 1 s of silence, result discarded.
        # Inside the try: a broken CUDA lib fails HERE, and must show as an
        # error on the dashboard, not a "Loading…" that never ends.
        segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language="en")
        list(segs)
    except Exception as e:
        # Recorded for the dashboard, then re-raised: bot.py treats a failed
        # preload as non-fatal (the lazy path tries again).
        _set_status("error", model, device, compute_type, error=f"{type(e).__name__}: {e}"[:300])
        raise
    LocalWhisperSTTService._shared_model = m
    _set_status("ready", model, device, compute_type)
    logger.info(f"stt_local: whisper ready in {time.monotonic() - t0:.1f}s "
                f"(RSS {before} -> {_rss_mb()} MB before trim)")
    _trim_heap("startup preload")


class LocalWhisperSTTService(BaseWhisperSTTService):
    """Segmented STT backed by a local faster-whisper model.

    The heavy model is cached at module level and shared across ALL
    pipelines: the bot constructs a fresh STT per WebRTC offer, so
    instance-caching reloads large-v3-turbo onto the GPU on every node
    reconnect (node blip, crash-retry, reboot) — seconds of deaf STT
    and VRAM churn. One model per bot process instead.

    Args:
        model: faster-whisper model size/name (e.g. "large-v3-turbo").
        device: "cuda" or "cpu".
        compute_type: faster-whisper compute type (e.g. "float16" on GPU).
        language: ISO language hint, None for auto-detect.
    """

    _shared_model = None
    _shared_lock = asyncio.Lock()

    def __init__(
        self,
        *,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        language: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(api_key="sk-local-not-used", **kwargs)
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._fw_model = None

    async def ensure_model(self):
        """Load the model once per process, shared across pipelines."""
        async with self._shared_lock:
            if LocalWhisperSTTService._shared_model is not None:
                self._fw_model = LocalWhisperSTTService._shared_model
                return
            logger.info(
                f"Loading faster-whisper {self._model_name} on {self._device} ({self._compute_type})"
            )
            _set_status("loading", self._model_name, self._device, self._compute_type)
            try:
                LocalWhisperSTTService._shared_model = await asyncio.to_thread(
                    _new_model, self._model_name, self._device, self._compute_type
                )
            except Exception as e:
                _set_status("error", self._model_name, self._device, self._compute_type,
                            error=f"{type(e).__name__}: {e}"[:300])
                raise
            _set_status("ready", self._model_name, self._device, self._compute_type)
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
            text = " ".join(s.text.strip() for s in segments).strip()
            _LAT_MS.append((time.monotonic() - t0) * 1000)
            return text, info.language

        text, lang = await loop.run_in_executor(None, _run)
        if language:
            lang = language
        logger.debug(f"local-whisper heard: [{text}] ({lang})")
        return Transcription(text=text)