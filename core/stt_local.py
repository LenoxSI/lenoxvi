"""Local faster-whisper STT service for lenoxvi (pipecat 1.10).

Subclasses pipecat's Whisper base STT (the OpenAI-API one) and overrides
`_transcribe` to run faster-whisper locally on the GPU instead of hitting
the OpenAI API. The base class gives us segmentation, VAD interplay, metrics
and TranscriptionFrame plumbing for free.
"""

import asyncio
from typing import Optional

from loguru import logger
from openai.types.audio import Transcription

from pipecat.services.whisper.base_stt import BaseWhisperSTTService


class LocalWhisperSTTService(BaseWhisperSTTService):
    """Segmented STT backed by a local faster-whisper model.

    Args:
        model: faster-whisper model size/name (e.g. "large-v3-turbo").
        device: "cuda" or "cpu".
        compute_type: faster-whisper compute type (e.g. "float16" on GPU).
        language: ISO language hint, None for auto-detect.
    """

    def __init__(
        self,
        *,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        language: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(api_key="sk-local-whisper-unused", **kwargs)
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._fw_model = None
        self._lock = asyncio.Lock()

    async def ensure_model(self):
        """Lazy-load the model on first use (keeps startup fast, and lets the
        pipeline start even before the GPU context is warm)."""
        async with self._lock:
            if self._fw_model is not None:
                return
            logger.info(
                f"Loading faster-whisper {self._model_name} on {self._device} ({self._compute_type})"
            )
            from faster_whisper import WhisperModel  # deferred import

            self._fw_model = await asyncio.to_thread(
                WhisperModel,
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )

    async def _transcribe(self, audio: bytes) -> Transcription:
        """Transcribe raw WAV audio bytes with faster-whisper.

        The base class hands us WAV bytes; we run the (blocking) transcribe
        call in a thread so the event loop stays live.
        """
        import io

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
        if self._settings.language:
            lang = self._settings.language
        logger.debug(f"local-whisper heard: [{text}] ({lang})")
        return Transcription(text=text)