"""Local faster-whisper STT service for lenoxvi (pipecat 1.11).

Subclasses pipecat's Whisper base STT (the OpenAI-API one) and overrides
`_transcribe` to run faster-whisper locally on the GPU instead of hitting
the OpenAI API. The base class gives us segmentation, VAD interplay, metrics
and TranscriptionFrame plumbing for free.
"""

import asyncio
import io
from typing import Optional

from loguru import logger
from openai.types.audio import Transcription

from pipecat.services.whisper.base_stt import BaseWhisperSTTService


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
            from faster_whisper import WhisperModel  # deferred import

            LocalWhisperSTTService._shared_model = await asyncio.to_thread(
                WhisperModel,
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
            self._fw_model = LocalWhisperSTTService._shared_model

    async def _transcribe(self, audio: bytes) -> Transcription:
        """Transcribe raw WAV audio bytes with faster-whisper.

        The base class hands us WAV bytes; we run the (blocking) transcribe
        call in a thread so the event loop stays live.
        """
        await self.ensure_model()
        loop = asyncio.get_running_loop()

        def _run():
            segments, info = self._fw_model.transcribe(
                io.BytesIO(audio),
                language=self._language,
                beam_size=1,
                vad_filter=False,  # pipecat's Silero VAD already gates segments
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            return text, info.language

        text, lang = await loop.run_in_executor(None, _run)
        if self._language:
            lang = self._language
        logger.debug(f"local-whisper heard: [{text}] ({lang})")
        return Transcription(text=text)