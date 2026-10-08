"""led_state — direct data-channel LED sends, pipeline-free.

Why this exists (2026-09-30 12:23 incident): LED messages used to ride
the pipecat pipeline as OutputTransportMessageFrames pushed by the wake
gate. A pipecat processor stalls frames in BOTH directions while it
works — HermesAgent blocks 3-40s on intelligence calls — so every LED message
queued behind the intelligence and arrived as one burst when it returned. The
node's ring held stale purple through the whole "On it." -> answer gap
(30s): the gate logged LOCKED at 12:24:16.938 for speech that actually
started 12:23:47.286, and the oww-sleeping sent at 12:23:49.394 never
reached the node until the intelligence returned.

The fix: send straight to the WebRTC connection via the output
transport's send_message (SmallWebRTCClient.send_message ->
send_app_message — no pipeline, no processor queues). Same
{"type": ...} wire the firmware's RTVI dispatcher already hashes, so
the node needs no changes. Messages sent before the data channel opens
are buffered by the connection and flushed in order; on a closing
connection they're discarded by the client's own guards.

Per-pipeline: each WebRTC connection gets its own pipeline AND its own
LedSender (bot.py wires them), so a wake in room A lights room A's
ring and nothing else — no global routing needed.

Who sends what (state ownership):
  - wake_word.WakeWordFilter: oww-awake / oww-sleeping (its own state
    changes — it IS the authority on awake vs idle).
  - sonos_output.SonosOutput: bot-started-speaking / bot-stopped-
    speaking — it sits directly downstream of TTS, so TTSStarted/
    TTSStopped frames reach it the instant speech begins/ends, even
    while HermesAgent is blocked upstream (the filler "On it." proved
    it: TTS generated and Sonos played at 12:23:47 while the intelligence
    ground on until 12:24:16). End-of-speech maps to the GATE's actual
    state: purple when the turn continues, dark when the gate already
    timed out mid-intelligence-call.
"""

from loguru import logger


class LedSender:
    """Fire-and-forget LED state sender bound to one pipeline's transport."""

    def __init__(self, output_transport):
        self._out = output_transport

    def send(self, msg_type: str) -> None:
        """Schedule a send on the running loop. Never raises — LED state
        is cosmetic and must never break a turn (same contract the old
        pipeline-path sender had)."""
        import asyncio

        try:
            asyncio.get_running_loop().create_task(self._send(msg_type))
        except RuntimeError:  # no running loop (sync/test context)
            logger.debug(f"led_state: no running loop for '{msg_type}'")
        except Exception as e:
            logger.debug(f"led_state: schedule '{msg_type}' failed: {e}")

    async def _send(self, msg_type: str) -> None:
        from pipecat.frames.frames import OutputTransportMessageFrame

        try:
            await self._out.send_message(
                OutputTransportMessageFrame(message={"type": msg_type})
            )
            logger.info(f"led_state: -> node '{msg_type}'")
        except Exception as e:
            logger.debug(f"led_state: send '{msg_type}' failed: {e}")