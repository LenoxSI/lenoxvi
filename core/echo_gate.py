"""Echo gate — interim self-loop breaker for lenoxvi (pre-wake-word phases).

The mic hears the bot's own replies from the browser and Sonos speakers; the
stub brain then replies to itself, which plays again, which is heard again —
a feedback loop (observed live 2026-09-15: she kept repeating herself).

The real fix is the Phase 4 wake-word gate + turn lock. Until then, this gate
blocks transcription handling while the bot is speaking: TTS start sets a
provisional block, and the actual clip duration (known from PCM length)
extends it past the Sonos playback tail.

Zero infrastructure: one deadline timestamp, checked by the stub brain.
"""

import time


class EchoGate:
    def __init__(self):
        self._blocked_until: float = 0.0

    def block(self, seconds: float) -> None:
        """Block transcription handling for `seconds` from now."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    def blocked(self) -> bool:
        return time.monotonic() < self._blocked_until