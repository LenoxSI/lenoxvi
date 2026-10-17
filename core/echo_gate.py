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
containment (intelligence_agent._is_own_echo), dedup, wake-phrase drop.

Consumers of blocked(): wake_word.py, intelligence_agent.py, intelligence_stub.py.
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

    def relax(self, tail: float = 0.5) -> None:
        """Shorten an active block to a small tail (wake/barge-in rider).
        A confirmed WAKE means the human is talking on
        purpose — the duration gate must not eat their follow-up question
        (measured live: a wake during a sounding filler, the
        question died in the gate, the user had to wake twice). The tail keeps
        a sliver of margin for the clip's fade-out; text containment
        (_is_own_echo, which holds her spoken texts) stays armed as the
        smart layer against her own voice. Never extends a lapsed gate."""
        now = time.monotonic()
        if self._blocked_until > now + tail:
            self._blocked_until = now + tail

    def blocked(self) -> bool:
        return time.monotonic() < self._blocked_until