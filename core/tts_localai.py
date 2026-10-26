"""LocalAI as a text-to-speech provider.

One request per sentence (LocalAI sends whole clips, no streaming), with the
sample rate read from the WAV header. A sentence that fails — and the rest of
that reply — is spoken by the appliance Kokoro, and LocalAI is marked down
for everyone (localai's shared state) until its background probe sees it
answer. The whole clip is awaited before any of it is pushed, so nothing
late from LocalAI can ever play over Kokoro.

When LocalAI runs Kokoro with the same voice, the handoff is the same model
and voice on another machine: standby_voice_for() picks it.
"""
from __future__ import annotations

import asyncio

import localai
import tts_kokoro
import tts_numbers
from loguru import logger

_service_cls: type | None = None


def standby_voice_for(localai_voice: str, configured: str | None) -> str:
    """The appliance Kokoro voice to fall back to: the LocalAI voice when it
    is a Kokoro voice, else the configured Kokoro voice, else the default."""
    if localai_voice in tts_kokoro.KOKORO_VOICES:
        return localai_voice
    if configured and configured in tts_kokoro.KOKORO_VOICES:
        return configured
    return tts_kokoro.DEFAULT_VOICE


def _service_class(mixin: type) -> type:
    """Build (once) TrackedLocalAITTSService(mixin, TTSService)."""
    global _service_cls
    if _service_cls is not None and issubclass(_service_cls, mixin):
        return _service_cls
    import numpy as np
    from pipecat.audio.utils import create_stream_resampler
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
    from pipecat.services.settings import TTSSettings
    from pipecat.services.tts_service import TTSService

    class TrackedLocalAITTSService(mixin, TTSService):
        _tts_provider = "localai"

        def __init__(self, *, model: str, voice: str, speed: float, standby_voice: str,
                     url: str | None = None, **kwargs):
            # Whole clips per sentence: a slow one must not trip pipecat's
            # 3 s idle stop (same reason as Kokoro).
            kwargs.setdefault("stop_frame_timeout_s", tts_kokoro.STOP_FRAME_TIMEOUT_S)
            TTSService.__init__(self, push_start_frame=True, push_stop_frames=True,
                                settings=TTSSettings(model=model, voice=voice, language=None), **kwargs)
            self._la_model = model
            self._la_voice = voice
            self._la_speed = float(speed)
            self._la_url = url  # fixed when the session was built
            self._standby_voice = standby_voice
            # One resampler per source: LocalAI's rate and Kokoro's can differ.
            self._la_resampler = create_stream_resampler()
            self._kk_resampler = create_stream_resampler()
            self._noted_engine = "localai"

        async def _localai_frame(self, part: str, context_id: str):
            pcm, rate = await asyncio.to_thread(localai.speech, part, self._la_model,
                                                self._la_voice, self._la_speed, url=self._la_url)
            audio = await self._la_resampler.resample(pcm, rate, self.sample_rate)
            return TTSAudioRawFrame(audio=audio, sample_rate=self.sample_rate, num_channels=1,
                                    context_id=context_id)

        async def _kokoro_frames(self, part: str, context_id: str):
            k = tts_kokoro.get_shared_model()
            if k is None:
                yield ErrorFrame(error="LocalAI is down and the Kokoro fallback isn't loaded")
                return
            v = self._standby_voice
            async with tts_kokoro.GEN_LOCK:
                async for samples, rate in k.create_stream(part, voice=v, lang=tts_kokoro.language_for(v),
                                                           speed=tts_kokoro._speed(self._la_speed)):
                    pcm = (samples * 32767).astype(np.int16).tobytes()
                    audio = await self._kk_resampler.resample(pcm, rate, self.sample_rate)
                    yield TTSAudioRawFrame(audio=audio, sample_rate=self.sample_rate, num_channels=1,
                                           context_id=context_id)

        def _note_engine(self, engine: str) -> None:
            """Tell the dashboard which engine is speaking (only on a change)."""
            if engine == self._noted_engine:
                return
            self._noted_engine = engine
            try:
                from dashboard import registry
                registry.note_tts(getattr(self, "_tts_room", None),
                                  "localai" if engine == "localai" else "kokoro (fallback)", True)
            except Exception:  # noqa: BLE001 — a status note must never cost audio
                logger.opt(exception=True).debug("tts localai: health note failed")

        async def run_tts(self, text: str, context_id: str):
            on_kokoro = False
            for part in tts_kokoro.sentences(tts_numbers.spell_numbers(text)):
                # No model picked = a config gap on THIS card: Kokoro speaks,
                # the shared box isn't marked down (STT may use it fine).
                if not on_kokoro and self._la_model and localai.should_try():
                    try:
                        frame = await self._localai_frame(part, context_id)
                    except localai.LocalAIError as e:
                        if e.box_down:
                            localai.mark_down(e.reason)
                        else:
                            logger.warning(f"tts localai: {e.reason} — Kokoro speaks this reply")
                    except Exception as e:  # noqa: BLE001 — never go silent on a client bug
                        logger.warning(f"tts localai: {type(e).__name__} — Kokoro speaks this reply")
                    else:
                        self._note_engine("localai")
                        yield frame
                        continue
                on_kokoro = True
                self._note_engine("kokoro")
                async for f in self._kokoro_frames(part, context_id):
                    yield f

        async def _try_revive(self) -> None:
            return None  # no websocket; localai.should_try() owns recovery

    _service_cls = TrackedLocalAITTSService
    return _service_cls


def make_service(*, mixin: type, model: str, voice: str, speed: float, standby_voice: str,
                 url: str | None = None):
    """A per-session LocalAI TTS service (the Kokoro fallback uses the
    process's shared Kokoro model, loaded by tts_kokoro.warm/preload)."""
    return _service_class(mixin)(model=model, voice=voice, speed=speed, standby_voice=standby_voice, url=url)
