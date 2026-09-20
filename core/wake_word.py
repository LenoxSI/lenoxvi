"""wake_word — openWakeWord gate + turn lock for lenoxvi (Phase 4).

Sits FIRST in the pipeline (before VAD/STT) and gates AUDIO, not
transcriptions: in IDLE/LOCKED the audio never reaches STT, so the GPU
doesn't transcribe chatter and nothing can reach the brain. The wake
listener itself always hears everything.

Turn semantics (plan of record, 2026-09-15):
- IDLE: audio feeds OWW only. "Hey Willow" (score >= threshold) -> AWAKE.
- AWAKE: audio flows to VAD/STT (the brain works normally). Falls back to
  IDLE after AWAKE_TIMEOUT of no downstream audio activity.
- LOCKED: bot speaking. Audio stops feeding BOTH OWW and STT — her voice
  can't re-wake herself, even if she literally says the wake phrase
  (e.g. when asked to repeat it).

This structurally retires the echo-gate duration hack (the lock is the
gate). The text-containment check stays as a cheap safety net (mid-turn
echoes from room speakers). Dedup + TTS tag-stripping stay: they fix
unrelated bugs (lull double-fire, flash_v2_5 not rendering v3 tags).
"""

import asyncio
import time
from enum import Enum
from pathlib import Path

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

WAKE_MODEL = "Hey_Willow_trial21_96x2_85000_20260621_163001.onnx"
WAKE_THRESHOLD = 0.40  # round 2 (2026-09-19): real wakes 0.84-1.00; 0.35 nearly let a marginal 0.38 wake through
AWAKE_TIMEOUT = 15.0  # no audio activity for this long -> back to IDLE (env: LVI_AWAKE_TIMEOUT)


class TurnState(Enum):
    IDLE = "idle"        # listening for the wake word only
    AWAKE = "awake"      # conversation active; audio flows
    LOCKED = "locked"    # bot speaking; mic fully ignored


class WakeWordFilter(FrameProcessor):
    """Wake-word gate + turn lock. Non-audio frames always pass through."""

    def __init__(self, model_path: str = WAKE_MODEL, threshold: float = WAKE_THRESHOLD,
                 echo_gate=None, **kwargs):
        import os

        super().__init__(**kwargs)
        self._model_path = str(Path(model_path))
        self._threshold = threshold
        self._echo_gate = echo_gate
        self._awake_timeout = float(os.getenv("LVI_AWAKE_TIMEOUT", str(AWAKE_TIMEOUT)))
        self._state = TurnState.IDLE
        self._oww = None  # lazy-loaded in setup
        self._name = ""
        self._frame_size = 1280  # OWW 80ms @ 16kHz
        self._audio_buf = bytearray()
        self._last_activity = time.monotonic()

    async def setup(self, setup):
        await super().setup(setup)
        from openwakeword.model import Model

        self._oww = await asyncio.to_thread(Model, wakeword_model_paths=[self._model_path])
        self._name = list(self._oww.models.keys())[0]
        logger.info(f"wake_word: model '{self._name}' loaded; threshold={self._threshold}; state=IDLE")

    def _score(self, pcm: bytes) -> float:
        audio = np.frombuffer(pcm, dtype=np.int16)
        return float(self._oww.predict(audio)[self._name])

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        now = time.monotonic()

        if isinstance(frame, InputAudioRawFrame):
            # Feed the wake listener in EVERY state (2026-09-19): starving OWW
            # during AWAKE/LOCKED spliced a multi-second hole in its spectrogram
            # buffer; on resume it false-fired 0.93-0.98 within 50ms of gate
            # close — an endless close->rewake loop that kept the ring purple
            # forever (decaying scores vs a real wake's steady 1.0 were the
            # tell). Continuous feeding = no discontinuity, no false wakes.
            self._audio_buf.extend(frame.audio)
            while len(self._audio_buf) >= self._frame_size:
                chunk = bytes(self._audio_buf[: self._frame_size])
                del self._audio_buf[: self._frame_size]
                score = await asyncio.to_thread(self._score, chunk)
                # Diag (2026-09-16): once/sec report of the best OWW score and
                # the input peak, so a silent gate is distinguishable from a
                # gate that never got audio. Remove once the node is stable.
                self._diag_best = max(getattr(self, "_diag_best", 0.0), score)
                pk = int(np.abs(np.frombuffer(chunk, dtype=np.int16)).max()) if chunk else 0
                self._diag_peak = max(getattr(self, "_diag_peak", 0), pk)
                if now - getattr(self, "_diag_t", 0.0) >= 1.0:
                    logger.info(f"wake_word: idle diag best_score={self._diag_best:.2f} in_peak={self._diag_peak}")
                    self._diag_best, self._diag_peak, self._diag_t = 0.0, 0, now
                if score >= self._threshold and self._state is TurnState.IDLE:
                    # Only IDLE acts on the wake score. Drop buffered audio
                    # (wake-phrase tail) but keep flowing audio to VAD/STT:
                    # swallowing it would deadlock VAD (it sits downstream and
                    # needs audio to signal fresh speech).
                    self._audio_buf.clear()
                    self._state = TurnState.AWAKE
                    self._last_activity = now
                    logger.info(f"wake_word: HEY WILLOW (score {score:.2f}) -> AWAKE")
                    await self._send_led("oww-awake")
                    break

            # Audio gating: only IDLE swallows downstream audio. LOCKED = bot
            # speaking (her voice can't re-trigger STT); echo-gate blocked =
            # Sonos clip still sounding (SonosOutput blocks it for clip
            # duration + margin; clips play ASYNC after the bot's browser
            # audio ends — observed live 2026-09-15).
            if self._state is TurnState.LOCKED:
                return
            if self._echo_gate and self._echo_gate.blocked():
                return
            if self._state is TurnState.AWAKE:
                await self.push_frame(frame, direction)  # VAD/STT get it
                # Timeout check must run HERE: in a quiet room only audio
                # frames flow, so the non-audio elif below would never fire.
                # Raw audio is NOT activity — activity is a VAD speech start
                # or a bot turn frame.
                if (now - self._last_activity) > self._awake_timeout:
                    self._state = TurnState.IDLE
                    logger.debug("wake_word: -> IDLE (timeout)")
                    await self._send_led("oww-sleeping")
                return
            # IDLE: audio already fed the wake listener above — swallow
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._state = TurnState.LOCKED
            self._swallowing_bleed = False
            logger.debug("wake_word: LOCKED (bot speaking)")
            await self._send_led("bot-started-speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state = TurnState.AWAKE
            self._last_activity = now
            logger.debug("wake_word: -> AWAKE (bot finished)")
            await self._send_led("bot-stopped-speaking")
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            # Real activity: user is talking (this is what keeps AWAKE alive)
            if self._state is TurnState.AWAKE:
                self._last_activity = now
        elif self._state is TurnState.AWAKE and (now - self._last_activity) > self._awake_timeout:
            self._state = TurnState.IDLE
            logger.debug("wake_word: -> IDLE (timeout)")
            await self._send_led("oww-sleeping")

        await self.push_frame(frame, direction)

    async def _send_led(self, msg_type: str):
        """Push a state message downstream so the node's LED ring can follow
        the wake/listen/speak cycle. Transport is reachable via the frame
        pipeline's task; OutputTransportMessageFrame rides the data channel.
        Filtered per direction below (only downstream)."""
        try:
            from pipecat.frames.frames import OutputTransportMessageFrame
            await self.push_frame(
                OutputTransportMessageFrame(message={"type": msg_type})
            )
        except Exception as e:  # LED state is cosmetic — never kill the turn
            logger.debug(f"wake_word: LED msg '{msg_type}' failed: {e}")