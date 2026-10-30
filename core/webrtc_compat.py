"""SmallWebRTC compatibility for the ESP32 nodes (libpeer).

pipecat 1.13 builds every SmallWebRTC peer connection with
bundlePolicy=MAX_BUNDLE (added for screen share). With aiortc 1.15 the SDP
answer then carries NO ICE candidates and a 0.0.0.0 connection address. The
nodes run libpeer, which has no trickle ICE: the answer is the only place they
learn where the bot is, so they never start connectivity checks and never
connect (every node offline; tests/webrtc_answer_test.py pins it).

Our nodes are audio + data channel only — no screen share — so aiortc's default
bundle policy (the pipecat 1.11 behaviour) is the right one. apply() swaps the
RTCConfiguration that pipecat's connection module uses for one that drops the
bundlePolicy argument. Idempotent; a no-op on a pipecat that doesn't pass one.
"""
from loguru import logger

_MARK = "_lenoxvi_default_bundle"


def apply() -> bool:
    """Restore aiortc's default bundle policy for SmallWebRTC peers."""
    try:
        import pipecat.transports.smallwebrtc.connection as conn
    except ImportError as e:  # transport not installed: nothing to fix
        logger.debug(f"webrtc_compat: smallwebrtc not importable ({e})")
        return False
    orig = conn.RTCConfiguration
    if getattr(orig, _MARK, False):
        return True

    def _rtc_configuration(*args, **kwargs):
        kwargs.pop("bundlePolicy", None)
        return orig(*args, **kwargs)

    setattr(_rtc_configuration, _MARK, True)
    conn.RTCConfiguration = _rtc_configuration
    return True
