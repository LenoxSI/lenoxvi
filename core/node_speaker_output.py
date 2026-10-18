"""Echo gate for replies played on a node's built-in speaker.

The reply audio already reaches the node (it passes through to
transport.output()); this stage only does what the Sonos/WiiM backends
do when THEY play a reply: arm the echo gate for the reply's duration
plus margin, so the speaker's tail (prebuffer + room reverb after the
node's own turn lock lets go) can't come back as a transcript.

LEDs and the wake gate are unchanged: SonosOutput already drives them
for every reply, generation-anchored, whichever zone plays it.
"""
from __future__ import annotations

from node_speaker import BUILTIN
from pipecat.frames.frames import (
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

TAIL_S = 1.5  # same margin the Sonos/WiiM backends use


class NodeSpeakerOutput(FrameProcessor):
    def __init__(self, echo_gate=None, zone_selector=None, **kwargs):
        super().__init__(**kwargs)
        self._echo_gate = echo_gate
        self._zone_selector = zone_selector
        self._bytes_per_s = 0.0
        self._bytes = 0

    def _zone(self):
        if not self._zone_selector:
            return None
        try:
            return self._zone_selector()
        except Exception:  # noqa: BLE001 — a routing hint must never break the reply
            return None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._bytes, self._bytes_per_s = 0, 0.0
        elif isinstance(frame, TTSAudioRawFrame):
            self._bytes += len(frame.audio)
            self._bytes_per_s = 2.0 * frame.sample_rate * max(frame.num_channels, 1)
        elif isinstance(frame, TTSStoppedFrame):
            if self._bytes and self._bytes_per_s and self._echo_gate is not None and self._zone() == BUILTIN:
                self._echo_gate.block(self._bytes / self._bytes_per_s + TAIL_S)
            self._bytes = 0
        await self.push_frame(frame, direction)
