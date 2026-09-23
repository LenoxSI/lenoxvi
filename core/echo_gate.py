"""Echo gate — duration-based echo containment, the permanent safety net.

The mic hears the bot's own replies from the browser and Sonos speakers;
without containment that's a feedback loop (observed live 2026-09-15:
she kept repeating herself).

The wake-word turn lock (wake_word.py) is the primary gate — LOCKED
while the bot speaks — but Sonos clips play ASYNC after the browser
audio ends, and late whisper transcriptions of her own voice can
arrive after the lock releases. This gate covers that tail.

Role (ratified 2026-09-20, superseding the old "interim" framing):
PERMANENT defense-in-depth behind the turn lock, NOT scaffolding —
do not remove. EchoGate.block(), called from SonosOutput._speak_to_sonos,
sets a duration (clip length + load + tail margin) whenever a clip ships; blocked() consumers drop
echo transcriptions during that window. Layers alongside: text
containment (hermes_agent._is_own_echo), dedup, wake-phrase drop.

Consumers of blocked(): wake_word.py, hermes_agent.py, brain_stub.py.
Driver of block(): sonos_output.py (_speak_to_sonos).

Zero infrastructure: one deadline timestamp, monotonic clock.
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