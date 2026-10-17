"""TTS hot standby: primary cloud TTS -> Pocket (local) on hard failure.

pipecat's ServiceSwitcherStrategyFailover switches when the active service
pushes an ErrorFrame AND is_usable is False (pipecat flips usability
before the frame travels: frame_processor.push_error_frame). It never
switches BACK, so that's ours. While Pocket is active the primary gets
no speaks, so TTSTracked's on-speak revive can't fire. recover() probes
the primary every tts.revive_cooldown_secs and queues ONE manual switch
frame when its websocket opens. The frame rides the pipeline in order,
so it lands between utterances.

Hard errors only, no first-chunk timeout: a slow-but-alive primary
would start Pocket and then play its own late audio too (two voices).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import ManuallySwitchServiceFrame
from pipecat.pipeline.service_switcher import (ServiceSwitcher,
                                               ServiceSwitcherStrategyFailover)


def _note(room, provider, usable) -> None:
    # Fail-open: observability must never take down the voice pipeline.
    try:
        from dashboard import registry
        registry.note_tts(room, provider, usable)
    except Exception:
        logger.opt(exception=True).warning("tts fallback: health note failed")


async def recover(primary, request_switch: Callable, cooldown: float,
                  sleep=asyncio.sleep) -> bool:
    """Probe the primary until it revives, then request ONE switch back."""
    while True:
        await sleep(cooldown)
        await primary._try_revive()
        if primary.is_usable:
            await request_switch(primary)
            return True


def cancel_recovery(switcher) -> None:
    task = getattr(switcher, "_lenoxvi_recovery", None)
    if task is not None and not task.done():
        task.cancel()
    switcher._lenoxvi_recovery = None


async def on_switched(switcher, service) -> None:
    primary = switcher._lenoxvi_primary
    room = switcher._lenoxvi_room
    if service is primary:
        cancel_recovery(switcher)
        logger.warning(f"tts fallback: {primary._tts_provider} is back, "
                       "switching off Pocket")
        _note(room, primary._tts_provider, True)
        return
    logger.warning(f"tts fallback: {primary._tts_provider} is down, "
                   "Pocket (local) is speaking")
    _note(room, "pocket (fallback)", True)
    cancel_recovery(switcher)

    async def _request(svc):
        await switcher._lenoxvi_queue(ManuallySwitchServiceFrame(service=svc))

    switcher._lenoxvi_recovery = asyncio.create_task(
        recover(primary, _request, switcher._lenoxvi_cooldown))


def wrap_with_fallback(primary, pocket, *, room,
                       queue_frame: Callable[..., Awaitable[None]],
                       cooldown: float) -> ServiceSwitcher:
    """ServiceSwitcher([primary, pocket], Failover) + the switch-back loop."""
    switcher = ServiceSwitcher(services=[primary, pocket],
                               strategy_type=ServiceSwitcherStrategyFailover)
    switcher._lenoxvi_primary = primary
    switcher._lenoxvi_room = room
    switcher._lenoxvi_queue = queue_frame
    switcher._lenoxvi_cooldown = cooldown
    switcher._lenoxvi_recovery = None

    @switcher.strategy.event_handler("on_service_switched")
    async def _h(strategy, service):
        await on_switched(switcher, service)

    return switcher
