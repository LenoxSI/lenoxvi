"""Cloud STT (Deepgram Nova-3) with local Whisper as the hot standby.

pipecat's DeepgramSTTService already marks itself permanently unusable when
it can't connect — a 4xx handshake (bad key) at once, a dead network after
three quick failures — so ServiceSwitcherStrategyFailover moves the session
to Whisper by itself (unlike the TTS websocket, which needed an escalation
hook). What's ours:

1. Switching BACK. While Whisper listens, Deepgram gets no audio and has
   given up. recover_stt() probes api.deepgram.com:443 (cheap TCP, quiet
   while the network is down), reconnects the service, marks it usable and
   queues the switch frame — the TTS fallback's loop with an STT revive.
2. Status for the dashboard: per-room active service (+ the reason the last
   failover happened), aggregated: any room on Whisper shows the fallback.
3. SttTimer: end of speech (VAD stop) -> final transcript, the cloud
   provider's per-command time (stt_local records it in "e2e" mode).
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

import stt_catalog
import stt_local
from loguru import logger
from pipecat.frames.frames import (
    ManuallySwitchServiceFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.service_switcher import (
    ServiceSwitcher,
    ServiceSwitcherStrategyFailover,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from tts_fallback import _tcp_reachable

DEEPGRAM_ENDPOINT = ("api.deepgram.com", 443)
_DEFAULT = object()
_MIN_COOLDOWN = 1.0

# Process-wide view for the dashboard (one entry per live session/room).
_STATE: dict = {"provider": "whisper", "rooms": {}, "unavailable": None}


def _reset_for_tests() -> None:
    _STATE.update(provider="whisper", rooms={}, unavailable=None)


def set_provider(provider: str) -> None:
    _STATE["provider"] = provider


def note_unavailable(reason) -> None:
    """The cloud provider couldn't even be built (no key, no SDK): every
    session runs on Whisper and the card says why."""
    _STATE["unavailable"] = reason


def note_active(room, active: str, reason=None) -> None:
    _STATE["rooms"][room or "?"] = (active, reason)


def forget(room) -> None:
    _STATE["rooms"].pop(room or "?", None)


def status() -> dict:
    provider = _STATE["provider"]
    if provider == "whisper":
        return {"provider": "whisper"}
    out = {"provider": provider, "cloud_model": stt_catalog.DEEPGRAM_MODEL}
    if _STATE["unavailable"]:
        out.update(active="whisper", fallback_reason=_STATE["unavailable"], fallback_rooms=[])
        return out
    fallback = {room: why for room, (active, why) in _STATE["rooms"].items() if active == "whisper"}
    if fallback:
        out.update(active="whisper", fallback_reason=next(iter(fallback.values())) or "Deepgram unreachable",
                   fallback_rooms=sorted(fallback))
    else:
        out["active"] = "deepgram"
    return out


def make_deepgram(language):
    """(DeepgramSTTService, None) or (None, reason). Never raises: a missing
    key or SDK means Whisper, not a dead pipeline."""
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        return None, "DEEPGRAM_API_KEY is not set"
    try:
        from pipecat.services.deepgram.stt import DeepgramSTTService
    except Exception as e:  # noqa: BLE001 — the SDK is optional
        return None, f"deepgram-sdk is not installed ({e})"
    svc = DeepgramSTTService(
        api_key=key,
        settings=DeepgramSTTService.Settings(model=stt_catalog.DEEPGRAM_MODEL,
                                             language=stt_catalog.deepgram_language(language),
                                             smart_format=True))
    return svc, None


def _probe_deepgram():
    return _tcp_reachable(DEEPGRAM_ENDPOINT)


async def recover_stt(dg, request_switch: Callable, cooldown: float, sleep=asyncio.sleep, *,
                      reachable=_DEFAULT, is_active=None, closed=None, confirm_secs: float = 5.0) -> bool:
    """Probe Deepgram until it answers, reconnect it, switch back.

    True once the switch is confirmed (or requested, with no is_active);
    False when the session closed first."""
    if reachable is _DEFAULT:
        reachable = _probe_deepgram
    while True:
        if closed and closed():
            return False
        await sleep(cooldown)
        if closed and closed():
            return False
        if reachable is not None and not await reachable():
            continue                      # network still down: stay quiet
        try:
            await dg._do_reconnect()
        except Exception as e:  # noqa: BLE001 — not back yet; try next round
            logger.debug(f"stt fallback: Deepgram reconnect failed: {e}")
            continue
        await dg.set_usable(True)
        await request_switch(dg)
        if is_active is None:
            return True
        waited = 0.0
        while waited < confirm_secs:
            if is_active():
                return True
            await asyncio.sleep(0.05)
            waited += 0.05
        logger.warning("stt fallback: switch back was refused — will retry")


def _cancel_recovery(switcher) -> None:
    task = getattr(switcher, "_lenoxvi_recovery", None)
    if task is not None and not task.done():
        task.cancel()
    switcher._lenoxvi_recovery = None


def close_stt_fallback(switcher) -> None:
    """Session over: stop recovery for good and drop the room from status."""
    switcher._lenoxvi_closed = True
    _cancel_recovery(switcher)
    forget(switcher._lenoxvi_room)


async def _on_switched(switcher, service) -> None:
    dg = switcher._lenoxvi_primary
    room = switcher._lenoxvi_room
    _cancel_recovery(switcher)
    if service is dg:
        note_active(room, "deepgram")
        logger.warning("stt fallback: Deepgram is back — switching off local Whisper")
        return
    reason = getattr(switcher, "_lenoxvi_last_error", None) or "Deepgram unreachable"
    note_active(room, "whisper", reason)
    logger.warning(f"stt fallback: Deepgram is down ({reason}) — local Whisper is listening")
    if getattr(switcher, "_lenoxvi_closed", False):
        return

    async def _request(svc):
        await switcher._lenoxvi_queue(ManuallySwitchServiceFrame(service=svc))

    strategy = switcher.strategy
    switcher._lenoxvi_recovery = asyncio.create_task(recover_stt(
        dg, _request, switcher._lenoxvi_cooldown, is_active=lambda: strategy.active_service is dg,
        closed=lambda: getattr(switcher, "_lenoxvi_closed", False)))


def wrap_stt_with_fallback(dg, whisper, *, room, queue_frame: Callable[..., Awaitable[None]],
                           cooldown: float) -> ServiceSwitcher:
    """ServiceSwitcher([deepgram, whisper], Failover) + reason capture +
    the switch-back loop + status."""
    switcher = ServiceSwitcher(services=[dg, whisper], strategy_type=ServiceSwitcherStrategyFailover)
    switcher._lenoxvi_primary = dg
    switcher._lenoxvi_room = room
    switcher._lenoxvi_queue = queue_frame
    try:
        switcher._lenoxvi_cooldown = max(float(cooldown), _MIN_COOLDOWN)
    except (TypeError, ValueError):
        switcher._lenoxvi_cooldown = 30.0
    switcher._lenoxvi_recovery = None
    switcher._lenoxvi_closed = False
    switcher._lenoxvi_last_error = None

    # Remember WHY Deepgram gave up (shown on the card's fallback chip).
    orig_push_error = dg.push_error

    async def _push_error(*args, **kwargs):
        msg = kwargs.get("error_msg", args[0] if args else None)
        if msg:
            # The SDK appends request headers ("…: headers: {…}"); keep the
            # readable part for the card's tooltip.
            switcher._lenoxvi_last_error = str(msg).split(": headers:")[0][:160]
        return await orig_push_error(*args, **kwargs)
    dg.push_error = _push_error

    note_active(room, "deepgram")

    @switcher.strategy.event_handler("on_service_switched")
    async def _h(strategy, service):
        await _on_switched(switcher, service)

    return switcher


class SttTimer(FrameProcessor):
    """End of speech (VAD stop) -> final transcript, in ms, into stt_local's
    per-command ring. Sits right after the STT stage; passes every frame."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._stop_at = None

    def observe(self, frame, now=None):
        now = time.monotonic() if now is None else now
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._stop_at = now
            return None
        if isinstance(frame, TranscriptionFrame) and self._stop_at is not None:
            ms = (now - self._stop_at) * 1000
            self._stop_at = None
            return ms
        return None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            ms = self.observe(frame)
            if ms is not None:
                stt_local.note_latency(ms)
        await self.push_frame(frame, direction)
