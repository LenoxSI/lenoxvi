"""Echo intelligence for LenoxVI: no brain picked yet.

Runs when config.yaml says `intelligence.kind: stub` (a fresh install, or a
bench run without an intelligence server). It repeats what it heard and
points at the dashboard. Production uses IntelligenceAgent via a lane
(hermes / openclaw / nanobot); an unset kind means hermes, not this.

Frame timing (pipecat 1.11, segmented STT + VADProcessor):
  - VADUserStartedSpeakingFrame -> start buffering
  - TranscriptionFrame(s)       -> append text
  - a lull of ~0.6s with no new transcription -> user is done; flush

The debounce exists because TranscriptionFrames land AFTER the
VAD stop frame, so reacting to the stop frame itself would flush too
early. A time-based lull is race-free.
"""

import time

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TTSSpeakFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class StubIntelligence(FrameProcessor):
    """Buffers user speech and emits a TTSSpeakFrame after an end-of-turn lull."""

    def __init__(self, lull_secs: float = 0.6, max_history: int = 6, echo_gate=None, **kwargs):
        super().__init__(**kwargs)
        self._buffer: list[str] = []
        self._history: list[dict] = []
        self._max_history = max_history
        self._last_transcription: float = 0.0
        self._lull_secs = lull_secs
        self._speaking = False
        self._flushed = False
        self._echo_gate = echo_gate

    def _reply(self, text: str) -> str:
        t = text.lower()
        # What a fresh install says before a brain is set up: true on any
        # hardware or voice, and it points at the dashboard, not the YAML.
        if "your name" in t or "who are you" in t:
            return ("I'm LenoxVI, with no brain picked yet. Pick one on the "
                    "dashboard's System tab.")
        if any(w in t for w in ("bye", "goodbye", "good night")):
            return "Bye!"
        return (f"I heard: {text.strip()}. Pick a brain on the dashboard's System tab "
                "to get real answers.")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            self._flushed = False
        elif isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                if self._echo_gate and self._echo_gate.blocked():
                    # the mic heard the bot's own reply off the speakers —
                    # drop it or the stub replies to itself forever
                    logger.debug(f"stub-intelligence: echo-gate dropped [{frame.text.strip()}]")
                else:
                    self._buffer.append(frame.text.strip())
                    self._last_transcription = time.monotonic()
        # Let silence pass through; check lull on any frame once speaking stopped.
        if (
            self._speaking
            and self._flushed is False
            and self._buffer
            and self._last_transcription > 0
            and (time.monotonic() - self._last_transcription) >= self._lull_secs
        ):
            self._speaking = False
            self._flushed = True
            text = " ".join(self._buffer).strip()
            self._buffer.clear()
            reply = self._reply(text)
            self._history.append({"role": "user", "content": text})
            self._history.append({"role": "assistant", "content": reply})
            self._history = self._history[-self._max_history:]
            logger.info(f"stub-intelligence: [{text}] -> [{reply}]")
            await self.push_frame(TTSSpeakFrame(text=reply))

        await self.push_frame(frame, direction)