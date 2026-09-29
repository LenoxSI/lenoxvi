"""wake_word — openWakeWord gate + turn lock for lenoxvi (Phase 4).

Sits FIRST in the pipeline (before VAD/STT) and gates AUDIO, not
transcriptions: in IDLE/LOCKED the audio never reaches STT, so the GPU
doesn't transcribe chatter and nothing can reach the brain. The wake
listener itself always hears everything.

Turn semantics (plan of record, 2026-09-15):
- IDLE: audio feeds OWW only. "Hey Willow" (score >= threshold) -> AWAKE.
- AWAKE: audio flows to VAD/STT (the brain works normally). Falls back to
  IDLE after AWAKE_TIMEOUT of no speech-start or bot-turn activity.
- LOCKED: bot speaking. STT is gated; the wake listener is still fed
  (since 2026-09-19 — starving OWW mid-turn caused false rewakes) but its
  score is ignored outside IDLE, so her voice can't re-wake herself, even
  if she literally says the wake phrase (e.g. when asked to repeat it).

The echo gate stays as permanent defense-in-depth behind the lock (see
echo_gate.py). The text-containment check stays as a cheap safety net
(mid-turn echoes from room speakers). Dedup + TTS tag-stripping stay: they fix
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
    OutputTransportMessageFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

WAKE_MODEL = "Hey_Willow_trial21_96x2_85000_20260621_163001.onnx"
WAKE_THRESHOLD = 0.40  # round 2 (2026-09-19): real wakes 0.84-1.00; 0.35 nearly let a marginal 0.38 wake through
AWAKE_TIMEOUT = 15.0  # no audio activity for this long -> back to IDLE (config: wake.awake_timeout > env: LVI_AWAKE_TIMEOUT)


class TurnState(Enum):
    IDLE = "idle"        # listening for the wake word only
    AWAKE = "awake"      # conversation active; audio flows
    LOCKED = "locked"    # bot speaking; mic fully ignored


class WakeWordFilter(FrameProcessor):
    """Wake-word gate + turn lock. Non-audio frames always pass through.

    The OWW model is cached at class level and shared across pipelines
    (the bot builds a fresh filter per WebRTC offer — a per-instance model
    reloads the ONNX + melspectrogram + embedding stack on every node
    reconnect). OWW keeps rolling audio buffers, so each new pipeline
    resets the shared model in setup() to start clean.
    """

    _shared_oww = None  # class-level: ONE Model for all pipelines

    def __init__(self, model_path: str = WAKE_MODEL, threshold: float = None,
                 echo_gate=None, on_wake=None, **kwargs):
        from config import cfg_env

        super().__init__(**kwargs)
        # Optional sync callback fired on IDLE -> AWAKE. The dashboard's
        # room registry uses it to record which room's wake opened the
        # turn (room-aware reply routing); must be cheap and must never
        # raise into the pipeline.
        self._on_wake = on_wake
        # Model path: config.yaml wake.model > LVI_WAKE_MODEL env > the repo-root
        # default (a trained .onnx; gitignored — see config.example.yaml).
        self._model_path = str(Path(cfg_env("wake", "model", "LVI_WAKE_MODEL", model_path) or model_path))
        # threshold: config.yaml wake.threshold > LVI_WAKE_THRESHOLD > code default
        _default_thresh = WAKE_THRESHOLD if threshold is None else threshold
        _thresh = cfg_env("wake", "threshold", "LVI_WAKE_THRESHOLD", str(_default_thresh))
        self._threshold = float(_thresh)
        self._echo_gate = echo_gate
        _awake = cfg_env("wake", "awake_timeout", "LVI_AWAKE_TIMEOUT", str(AWAKE_TIMEOUT))
        self._awake_timeout = float(_awake)
        self._state = TurnState.IDLE
        self._oww = None  # lazy-loaded in setup
        self._name = ""
        # OWW's canonical frame is 1280 SAMPLES (80ms @ 16kHz) = 2560 bytes
        # int16 (verified in openWakeWord 0.4.0: predict() expects multiples
        # of 80ms/1280 samples). This buffer is BYTES — 1280 bytes here would
        # feed OWW at half cadence, doubling predict() calls on the 24/7 hot
        # path vs the designed rate.
        self._frame_size = 2560
        self._audio_buf = bytearray()
        self._last_activity = time.monotonic()

    async def setup(self, setup):
        await super().setup(setup)
        if WakeWordFilter._shared_oww is None:
            from openwakeword.model import Model

            WakeWordFilter._shared_oww = await asyncio.to_thread(
                Model, wakeword_model_paths=[self._model_path]
            )
        # Fresh pipeline: clear the shared model's rolling audio buffers so
        # the previous session's tail can't bleed scores into this one.
        # One live session assumed — two concurrent pipelines would share
        # one set of rolling buffers.
        WakeWordFilter._shared_oww.reset()
        self._oww = WakeWordFilter._shared_oww
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
                if score >= self._threshold and self._state is TurnState.IDLE:
                    # Only IDLE acts on the wake score. Drop buffered audio
                    # (wake-phrase tail) but keep flowing audio to VAD/STT:
                    # swallowing it would deadlock VAD (it sits downstream and
                    # needs audio to signal fresh speech).
                    self._audio_buf.clear()
                    self._state = TurnState.AWAKE
                    self._last_activity = now
                    logger.info(f"wake_word: HEY WILLOW (score {score:.2f}) -> AWAKE")
                    if self._on_wake:
                        try:
                            self._on_wake()
                        except Exception as e:  # observer must never break the gate
                            logger.debug(f"wake_word: on_wake callback failed: {e}")
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
        """Push an LED state message downstream over the data channel, so the
        node's LED ring can follow the wake/listen/speak cycle."""
        try:
            await self.push_frame(
                OutputTransportMessageFrame(message={"type": msg_type})
            )
        except Exception as e:  # LED state is cosmetic — never kill the turn
            logger.debug(f"wake_word: LED msg '{msg_type}' failed: {e}")