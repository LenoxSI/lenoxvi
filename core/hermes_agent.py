"""hermes_agent — lenoxvi's real brain: the Hermes agent over the LAN API.

Replaces brain_stub.py in the pipeline (Phase 3). Same turn shape — buffers
user speech, waits for the end-of-turn lull, then gets a reply — but the reply
comes from the Hermes API server (OpenAI-compatible, bearer auth) instead of a
canned echo. The agent keeps its full toolset + memory; our system prompt
layers voice-appropriate instructions on top (short, spoken-word replies).

Config (.env on the host):
    LVI_HERMES_URL   — e.g. http://203.0.113.135:8642/v1
    LVI_HERMES_KEY   — the API server's bearer key
    LVI_HERMES_MODEL — defaults to "hermes-agent"
"""

import json
import time
import urllib.request

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TTSSpeakFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

VOICE_SYSTEM_PROMPT = (
    "You are the house voice assistant, currently talking to the owner over speakers. "
    "Replies are spoken aloud by TTS, so keep them SHORT — one to three sentences, "
    "plain conversational English. No markdown, no bullet points, no emojis, no "
    "URLs. You have all your normal tools and memory; use them when asked, but "
    "never read out raw command output — summarize it conversationally."
)


class HermesAgent(FrameProcessor):
    """Buffers user speech; on end-of-turn lull, asks the Hermes API for a reply."""

    def __init__(self, lull_secs: float = 0.6, max_history: int = 10, echo_gate=None, **kwargs):
        super().__init__(**kwargs)
        import os

        self._url = os.getenv("LVI_HERMES_URL", "http://203.0.113.135:8642/v1")
        self._key = os.environ["LVI_HERMES_KEY"]
        self._model = os.getenv("LVI_HERMES_MODEL", "hermes-agent")
        self._lull_secs = lull_secs
        self._max_history = max_history
        self._echo_gate = echo_gate
        self._buffer: list[str] = []
        self._history: list[dict] = []
        self._last_transcription: float = 0.0
        self._speaking = False
        self._flushed = False
        # Dedup: same utterance re-heard inside this window is treated as an
        # echo/repeat (observed live: the lull-timer can double-fire while the
        # bot is still speaking a slow reply).
        self._last_user_text: str = ""
        self._last_user_time: float = 0.0
        self._dedup_window: float = 10.0
        # Server-side conversation chain (per-connection): "voice-<zone>-<ts>"
        import os
        import uuid

        self._conversation = os.getenv(
            "LVI_CONVERSATION", f"voice-{os.getenv('LVI_ZONE', 'zone')}-{uuid.uuid4().hex[:8]}"
        )
        logger.info(f"hermes_agent: conversation '{self._conversation}'")

    def _ask_hermes(self, text: str) -> str:
        """One chat completion; returns the reply text.

        Uses a server-side `conversation` name (session label for the dashboard)
        plus a capped client-side history (continuity without token growth —
        the conversation param does NOT chain context on /chat/completions,
        verified live 2026-09-15: turn 2 didn't know turn 1's content).
        Input-token growth on tool-heavy turns is the server-side agent loop
        re-feeding context per step (measured: 13k no-tools vs 68k two-lights
        with retry) — not client history.
        """
        payload = json.dumps({
            "model": self._model,
            "stream": False,
            "conversation": self._conversation,
            "messages": [
                {"role": "system", "content": VOICE_SYSTEM_PROMPT},
                *self._history,
                {"role": "user", "content": text},
            ],
        }).encode()
        req = urllib.request.Request(
            f"{self._url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        reply = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        logger.info(
            f"hermes_agent: tokens in={usage.get('prompt_tokens', '?')} "
            f"out={usage.get('completion_tokens', '?')}"
        )
        return reply

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            self._flushed = False
        elif isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                if self._echo_gate and self._echo_gate.blocked():
                    logger.debug(f"hermes_agent: echo-gate dropped [{frame.text.strip()}]")
                else:
                    self._buffer.append(frame.text.strip())
                    self._last_transcription = time.monotonic()

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
            if text:
                import time as _t

                now = _t.monotonic()
                if text == self._last_user_text and (now - self._last_user_time) < self._dedup_window:
                    logger.info(f"hermes_agent: dedup dropped repeat [{text[:60]}]")
                    self._speaking = False
                    self._flushed = True
                else:
                    self._last_user_text = text
                    self._last_user_time = now
                    # Blocking HTTP in a thread so the pipeline stays live.
                    # Stream-and-ack: if the brain takes >3s, speak a filler
                    # first so the house doesn't feel dead (tool-heavy turns
                    # measured at 20-40s+ server-side, 2026-09-15).
                    import asyncio

                    ask = asyncio.create_task(asyncio.to_thread(self._ask_hermes, text))
                    try:
                        reply = await asyncio.wait_for(asyncio.shield(ask), timeout=3.0)
                    except TimeoutError:
                        await self.push_frame(TTSSpeakFrame(text="On it — one sec."))
                        reply = await ask
                    self._history.append({"role": "user", "content": text})
                    self._history.append({"role": "assistant", "content": reply})
                    self._history = self._history[-self._max_history:]
                    logger.info(f"hermes_agent: [{text}] -> [{reply[:120]}]")
                    await self.push_frame(TTSSpeakFrame(text=reply))

        await self.push_frame(frame, direction)