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
from typing import Any, Callable

import localai

_ACTIVE = {"engine": "localai"}


class _Segment:
    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


class _Info:
    __slots__ = ("language",)

    def __init__(self, language):
        self.language = language


def _reset_for_tests() -> None:
    _ACTIVE["engine"] = "localai"


def active() -> str:
    """Which engine heard the last utterance: "localai" or "whisper"."""
    return _ACTIVE["engine"]


def status() -> dict:
    """The dashboard's STT block while LocalAI is the provider (same keys the
    cloud providers use, so the chip logic stays one shape)."""
    try:
        model = localai.settings().get("model_stt") or None
    except Exception:  # noqa: BLE001 — status must never raise
        model = None
    out = {"provider": "localai", "model": model}
    reason = localai.down_reason()
    if reason:
        out.update(active="whisper", fallback_reason=reason, fallback_rooms=[])
    else:
        out["active"] = "localai"
    return out


class LocalAIWhisper:
    """faster-whisper-shaped model object that asks LocalAI first."""

    def __init__(self, model: str, fallback: Callable[[], Any]):
        self.model = model
        self._fallback = fallback

    def transcribe(self, audio, language=None, beam_size=1, vad_filter=False):
        data = audio.read() if hasattr(audio, "read") else bytes(audio)
        if not self.model:
            localai.mark_down("no LocalAI speech-to-text model picked")
        elif localai.should_try():
            try:
                text = localai.transcribe(data, self.model, language)
            except localai.LocalAIError as e:
                localai.mark_down(e.reason)
            else:
                _ACTIVE["engine"] = "localai"
                return [_Segment(text)], _Info(language)
        _ACTIVE["engine"] = "whisper"
        return self._fallback().transcribe(io.BytesIO(data), language=language,
                                           beam_size=beam_size, vad_filter=vad_filter)
