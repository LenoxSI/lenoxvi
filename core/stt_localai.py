"""LocalAI as the speech-to-text engine behind LocalWhisperSTTService.

A stand-in for the faster-whisper model object with the same transcribe()
shape, so the service around it (phantom filter, live language, latency,
status) runs unchanged. If LocalAI fails, the SAME clip goes to the
appliance model (Whisper base) — no lost words — and LocalAI is marked down
for everyone (localai's shared state) until its background probe sees it
answer again.
"""
from __future__ import annotations

import io
import threading
from typing import Any, Callable

import localai
from loguru import logger

_BASE_LOCK = threading.Lock()
_BASE = {"failed": None}


def whisper_base():
    """The Embedded model (stt_catalog: base on the CPU, int8) — the SAME
    shared model the local Whisper service uses — loaded once: normally at
    startup; if that failed, at most once more here (two rooms can't load it
    twice, and a failed load isn't retried on every turn)."""
    import stt_local
    if stt_local.LocalWhisperSTTService._shared_model is not None:
        return stt_local.LocalWhisperSTTService._shared_model
    with _BASE_LOCK:
        if stt_local.LocalWhisperSTTService._shared_model is None:
            if _BASE["failed"]:
                raise RuntimeError(f"Whisper base failed to load: {_BASE['failed']}")
            try:
                stt_local.preload_shared_model()
            except Exception as e:  # noqa: BLE001
                _BASE["failed"] = type(e).__name__
                raise
    return stt_local.LocalWhisperSTTService._shared_model


class _Segment:
    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


class _Info:
    # engine: set only when LocalAI produced the text, so the service's heard
    # log names who served THIS clip (a fallback turn returns Whisper's info).
    __slots__ = ("engine", "language")

    def __init__(self, language, engine=None):
        self.language = language
        self.engine = engine


def _reset_for_tests() -> None:
    _BASE["failed"] = None


def status() -> dict:
    """The dashboard's STT block while LocalAI is the provider (same keys the
    cloud providers use, so the chip logic stays one shape)."""
    try:
        model = localai.settings().get("model_stt") or None
    except Exception:  # noqa: BLE001 — status must never raise
        model = None
    # localai_model, not model: the dashboard merges this over Whisper's own
    # status, whose `model` is the fallback actually loaded (base).
    out = {"provider": "localai", "localai_model": model}
    reason = localai.down_reason() or (None if model else "no LocalAI speech-to-text model picked")
    if reason:
        out.update(active="whisper", fallback_reason=reason, fallback_rooms=[])
    else:
        out["active"] = "localai"
    return out


class LocalAIWhisper:
    """faster-whisper-shaped model object that asks LocalAI first."""

    def __init__(self, model: str, fallback: Callable[[], Any], url: str | None = None,
                 timeout: float | None = None):
        self.model = model
        self._fallback = fallback
        self._url = url  # fixed when the session was built (None = read settings)
        self._timeout = timeout  # None = the default STT budget

    def transcribe(self, audio, language=None, beam_size=1, vad_filter=False):
        data = audio.read() if hasattr(audio, "read") else bytes(audio)
        # No model picked = a config gap on THIS card: fall back for the turn,
        # never mark the shared box down (the TTS card may use it fine).
        if self.model and localai.should_try():
            try:
                text = localai.transcribe(data, self.model, language, url=self._url, timeout=self._timeout)
            except localai.LocalAIError as e:
                if e.box_down:
                    localai.mark_down(e.reason)
                else:
                    logger.warning(f"stt localai: {e.reason} — Whisper base takes this turn")
            except Exception as e:  # noqa: BLE001 — never lose the clip to a client bug
                logger.warning(f"stt localai: {type(e).__name__} — Whisper base takes this turn")
            else:
                return [_Segment(text)], _Info(language, engine=f"localai: {self.model}")
        return self._fallback().transcribe(io.BytesIO(data), language=language,
                                           beam_size=beam_size, vad_filter=vad_filter)
