"""TTS hot standby: primary cloud TTS -> Pocket (local) on hard failure.

pipecat's ServiceSwitcherStrategyFailover switches when the active service
pushes an ErrorFrame AND is_usable is False (pipecat flips usability
before the frame travels: frame_processor.push_error_frame).

Two gaps are ours to close:

1. A primary that can't connect AT ALL (no internet at boot, or a session
   that starts mid-outage) is not "unusable" to pipecat: a failed connect
   is a connectivity error, which pipecat treats as temporary, so every
   speak just fails and failover never fires. on_primary_connection_error
   escalates a connection error on the ACTIVE primary to a permanent one,
   which flips it unusable and lets the switcher fail over.

2. The switcher never switches BACK. While Pocket is active the primary
   gets no speaks, so TTSTracked's on-speak revive can't fire. recover()
   probes every tts.revive_cooldown_secs: a cheap TCP check of the
   primary's host first (no websocket attempt, no log noise while the
   network is down), then the real websocket revive. It waits until
   Pocket is idle before queueing the switch frame (a switch mid-reply
   would mix the two voices into one clip), then confirms the switch
   actually landed and keeps trying if it was refused.

Hard errors only, no first-chunk timeout: a slow-but-alive primary
would start Pocket and then play its own late audio too (two voices).
"""
from __future__ import annotations

import asyncio
import urllib.parse
from typing import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import ManuallySwitchServiceFrame
from pipecat.pipeline.service_switcher import (ServiceSwitcher,
                                               ServiceSwitcherStrategyFailover)

_DEFAULT = object()
_MIN_COOLDOWN = 1.0


def _note(room, provider, usable) -> None:
    # Fail-open: observability must never take down the voice pipeline.
    try:
        from dashboard import registry
        registry.note_tts(room, provider, usable)
    except Exception:
        logger.opt(exception=True).warning("tts fallback: health note failed")


def _primary_endpoint(primary):
    """(host, port) of the primary's service URL, or None if unknown."""
    url = getattr(primary, "_url", None) or getattr(primary, "_base_url", None)
    if not url:
        return None
    u = urllib.parse.urlparse(str(url))
    if not u.hostname:
        return None
    port = u.port or (443 if u.scheme in ("wss", "https") else 80)
    return (u.hostname, port)


async def _tcp_reachable(endpoint, timeout: float = 3.0) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(*endpoint), timeout)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


def _pocket_idle_default() -> bool:
    try:
        import tts_pocket
        return not tts_pocket.GEN_LOCK.locked()
    except Exception:
        return True


async def recover(primary, request_switch: Callable, cooldown: float,
                  sleep=asyncio.sleep, *, reachable=_DEFAULT, pocket_idle=None,
                  is_active=None, closed=None, confirm_secs: float = 5.0) -> bool:
    """Probe the primary until it revives, then switch back to it.

    Returns True once the switch is confirmed (or requested, when there is
    no is_active to confirm with); False when the session closed first.
    """
    if reachable is _DEFAULT:
        endpoint = _primary_endpoint(primary)
        reachable = (lambda: _tcp_reachable(endpoint)) if endpoint else None
    pocket_idle = pocket_idle or _pocket_idle_default
    while True:
        if closed and closed():
            return False
        await sleep(cooldown)
        if closed and closed():
            return False
        if reachable is not None and not await reachable():
            continue                      # network still down: stay quiet
        await primary._try_revive()
        if not primary.is_usable:
            continue
        while not pocket_idle():          # never switch mid-reply
            if closed and closed():
                return False
            await sleep(0.25)
        await request_switch(primary)
        if is_active is None:
            return True
        waited = 0.0
        while waited < confirm_secs:
            if is_active():
                return True
            await asyncio.sleep(0.05)
            waited += 0.05
        logger.warning("tts fallback: switch back was refused — will retry")


def cancel_recovery(switcher) -> None:
    task = getattr(switcher, "_lenoxvi_recovery", None)
    if task is not None and not task.done():
        task.cancel()
    switcher._lenoxvi_recovery = None


def close_fallback(switcher) -> None:
    """Session over: stop recovery for good (no probes on a dead session)."""
    switcher._lenoxvi_closed = True
    cancel_recovery(switcher)


async def on_primary_connection_error(switcher, error) -> None:
    """Escalate a connect failure on the ACTIVE, still-usable primary to a
    permanent error, so the failover strategy switches to Pocket. Failed
    recovery probes (Pocket active) and repeats (already unusable) are
    ignored."""
    primary = switcher._lenoxvi_primary
    if getattr(switcher, "_lenoxvi_closed", False):
        return
    if switcher.strategy.active_service is not primary or not primary.is_usable:
        return
    # Only when there is NO socket (the connect itself failed). Errors on a
    # live socket also fire this event; pipecat's own reconnects (then its
    # permanent verdict) handle those — a blip must not cost a voice switch.
    if getattr(primary, "_websocket", None) is not None:
        return
    logger.warning(f"tts fallback: {primary._tts_provider} unreachable ({error}) — "
                   "marking it down")
    await primary.push_error(f"{primary._tts_provider} unreachable: {error}",
                             force_treat_as_permanent=True)


async def on_switched(switcher, service) -> None:
    primary = switcher._lenoxvi_primary
    room = switcher._lenoxvi_room
    if service is primary:
        cancel_recovery(switcher)
        primary._tts_note_suppressed = False
        logger.warning(f"tts fallback: {primary._tts_provider} is back, "
                       "switching off Pocket")
        _note(room, primary._tts_provider, True)
        return
    # While Pocket speaks, the primary's own health notes (a probe's
    # set_usable(True)) must not paint the dashboard chip green early.
    primary._tts_note_suppressed = True
    logger.warning(f"tts fallback: {primary._tts_provider} is down, "
                   "Pocket (local) is speaking")
    _note(room, "pocket (fallback)", True)
    cancel_recovery(switcher)

    async def _request(svc):
        await switcher._lenoxvi_queue(ManuallySwitchServiceFrame(service=svc))

    strategy = getattr(switcher, "strategy", None)
    is_active = ((lambda: strategy.active_service is primary)
                 if strategy is not None else None)
    switcher._lenoxvi_recovery = asyncio.create_task(recover(
        primary, _request, switcher._lenoxvi_cooldown, is_active=is_active,
        closed=lambda: getattr(switcher, "_lenoxvi_closed", False)))


def wrap_with_fallback(primary, pocket, *, room,
                       queue_frame: Callable[..., Awaitable[None]],
                       cooldown: float) -> ServiceSwitcher:
    """ServiceSwitcher([primary, pocket], Failover) + the escalation hook +
    the switch-back loop."""
    switcher = ServiceSwitcher(services=[primary, pocket],
                               strategy_type=ServiceSwitcherStrategyFailover)
    switcher._lenoxvi_primary = primary
    switcher._lenoxvi_room = room
    switcher._lenoxvi_queue = queue_frame
    try:
        switcher._lenoxvi_cooldown = max(float(cooldown), _MIN_COOLDOWN)
    except (TypeError, ValueError):
        switcher._lenoxvi_cooldown = 30.0
    switcher._lenoxvi_recovery = None
    switcher._lenoxvi_closed = False

    @switcher.strategy.event_handler("on_service_switched")
    async def _h(strategy, service):
        await on_switched(switcher, service)

    @primary.event_handler("on_connection_error")
    async def _ce(service, error):
        await on_primary_connection_error(switcher, error)

    return switcher
