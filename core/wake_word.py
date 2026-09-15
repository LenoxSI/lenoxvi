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
WAKE_THRESHOLD = 0.35  # tuned down from 0.5: the owner's normal-voice scores sit ~0.57
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
            # LOCKED = browser playback (BotStarted/StoppedSpeaking frames).
            # echo-gate blocked = Sonos clip still sounding (SonosOutput blocks
            # it for clip duration + margin; clips play ASYNC after the bot's
            # browser audio ends — observed live: fillers and replies leaking
            # into user transcriptions through the open mic, 2026-09-15).
            if self._state is TurnState.LOCKED:
                return
            if self._echo_gate and self._echo_gate.blocked():
                return

            if self._state is TurnState.AWAKE:
                self._last_activity = now
                await self.push_frame(frame, direction)  # VAD/STT get it
                return

            # IDLE: feed the wake listener, swallow downstream
            self._audio_buf.extend(frame.audio)
            while len(self._audio_buf) >= self._frame_size:
                chunk = bytes(self._audio_buf[: self._frame_size])
                del self._audio_buf[: self._frame_size]
                score = await asyncio.to_thread(self._score, chunk)
                if score >= self._threshold:
                    # Drop buffered audio (wake-phrase tail) but keep flowing
                    # audio to VAD/STT: the observed failure (2026-09-15) was a
                    # REPEATED wake word after the gate opened — that's a
                    # standalone wake-phrase transcription, which hermes_agent
                    # drops. Swallowing audio here would deadlock VAD (it sits
                    # downstream and needs audio to signal fresh speech).
                    self._audio_buf.clear()
                    self._state = TurnState.AWAKE
                    self._last_activity = now
                    logger.info(f"wake_word: HEY WILLOW (score {score:.2f}) -> AWAKE")
                    break
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._state = TurnState.LOCKED
            self._swallowing_bleed = False
            logger.debug("wake_word: LOCKED (bot speaking)")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state = TurnState.AWAKE
            self._last_activity = now
            logger.debug("wake_word: -> AWAKE (bot finished)")
        elif self._state is TurnState.AWAKE and (now - self._last_activity) > self._awake_timeout:
            self._state = TurnState.IDLE
            logger.debug("wake_word: -> IDLE (timeout)")

        await self.push_frame(frame, direction)