"""WiiM output backend — interrupt-no-restore reply playback.

Sibling of sonos_output.py, same FrameProcessor contract (TTSStarted /
TTSAudioRaw / TTSStopped → MP3 → clip server URL → play), but with WiiM
semantics established by the arrival verify:

  - playPromptUrl is a STUB on this firmware generation (Linkplay
    4.8.827634): acks OK, never fetches. NOT used.
  - Replies play via setPlayerCmd:play:<url> — proven to fetch and play
    the exact same clips the Sonos path uses (arrival test, mode 10).
  - Interrupt semantics: music stops when
    she speaks, NO restore. Spotify Connect sessions are client-owned
    and unresumable by API; building a half-working restore would lie.
  - No duck holder: there's nothing to duck (the interrupt IS the duck).
  - No per-prompt volume on the firmware. setPlayerCmd:vol is the MAIN
    volume — snapshot it before the reply, set the voice volume, and
    restore the user's music volume after the clip, so a loud music
    setting doesn't blast her reply and vice versa.
  - HTTPS-only API (port 80 closed): every command rides TLS with an
    unverified context — the unit serves a self-signed cert. Commands
    are percent-encoded whole; response 'OK' is documented-cheap (the
    official HTTP API doc: "Response is always 'OK'"), so playback is
    confirmed via getPlayerStatus polling, never via the ack.

Config: wiim: {enabled, default_zone, zones: {name: {ip}}, zone_volumes}
— the same knob set Sonos has, minus player_uid (HTTP API is UID-less).
default_zone is still read (old configs load fine) but no longer routes
replies: a room with no speaker mapping stays silent until it is mapped.
Zone volume resolution mirrors sonos_output: live override (dashboard
slider) > config wiim.zone_volumes > global clip_volume.
"""

import asyncio
import json
import ssl
import time
import urllib.request
import urllib.parse
import uuid

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    Frame,
    TTSStartedFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from node_speaker import BUILTIN
from paths import clips_dir
from zone_config import LiveZoneConfig

# Shared SSL context: the WiiM serves a self-signed cert on 443.
_SSL_CTX = ssl._create_unverified_context()
_SSL_CTX.set_ciphers("ALL:@SECLEVEL=0")  # match pywiim: old TLS on LinkPlay firmware


def wiim_api(ip: str, cmd: str, timeout: float = 4.0) -> str:
    """One HTTP API command. Returns the raw body ('OK', JSON, …).

    Runs on the pipeline's event loop — every caller wraps it in
    asyncio.to_thread (the API can stall if the unit naps; a stalled
    event loop stalls the whole voice pipeline).
    """
    url = f"https://{ip}/httpapi.asp?command={cmd}"
    with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as r:
        return r.read().decode(errors="replace")


def wiim_get_status(ip: str, timeout: float = 3.0) -> dict | None:
    """getPlayerStatus as a dict, or None (unreachable/garbage)."""
    try:
        return json.loads(wiim_api(ip, "getPlayerStatus", timeout))
    except Exception:
        return None


def wiim_volume_overrides(overrides: dict | None):
    """Live per-zone volume overrides for the WiiM backend (slider lane)."""
    global _LIVE_WIIM_VOLUMES
    _LIVE_WIIM_VOLUMES = {k: int(v) for k, v in (overrides or {}).items()
                          if isinstance(v, (int, float))}


def live_wiim_volume(zone: str) -> int | None:
    v = _LIVE_WIIM_VOLUMES.get(zone)
    return int(v) if v is not None else None


_LIVE_WIIM_VOLUMES: dict = {}

# Music-volume snapshot per speaker IP while any clip holds the voice
# volume, and the newest clip's token (only it restores). Module level, not
# per WiiMOutput: two rooms can share one speaker, and a reconnecting
# node's old pipeline can still be mid-clip, so per-pipeline copies saved
# one pipeline's voice volume as another's music level.
_VOL_SAVED: dict[str, int | None] = {}
_VOL_TOKEN: dict[str, int] = {}
_VOL_LOCKS: dict[str, tuple] = {}  # ip -> (event loop, asyncio.Lock)

# Bound on the volume restore when the speak task is cancelled (bot
# stopping mid-reply): best effort, never holds up shutdown for long.
_RESTORE_ON_CANCEL_S = 3.0


def _vol_lock(ip: str) -> asyncio.Lock:
    """The per-speaker volume lock (one per event loop: an asyncio.Lock
    must not be shared across loops)."""
    loop = asyncio.get_running_loop()
    held = _VOL_LOCKS.get(ip)
    if held is None or held[0] is not loop:
        held = _VOL_LOCKS[ip] = (loop, asyncio.Lock())
    return held[1]


class WiiMOutput(LiveZoneConfig, FrameProcessor):
    _section_name = "wiim"

    """Captures TTS audio and plays it on the zone's WiiM via play + vol snap/restore."""

    def __init__(self, config_path: str | None = None, echo_gate=None,
                 zone_selector=None, wake_gate=None, led=None, **kwargs):
        super().__init__(**kwargs)
        self._echo_gate = echo_gate
        self._wake_gate = wake_gate
        self._led = led
        self._end_of_speech_timer = None
        # bytearray, not a list (SonosOutput does the same): extend() on a
        # list adds one element per byte — ≈1M of them for a 20 s reply,
        # built on the event loop.
        self._pcm: bytearray = bytearray()
        self._sample_rate = 16000
        self._channels = 1
        # Optional sync callable returning the reply zone NAME (same
        # contract as SonosOutput's zone_selector — bot.py's _reply_zone).
        self._zone_selector = zone_selector
        from config import cfg
        wiim = cfg("wiim") or {}
        # enabled / zones / zone_volumes / clip_volume are read at reply time
        # (zone_config.LiveZoneConfig): adding the first WiiM zone on the
        # dashboard (which also enables the backend) reaches sessions that
        # are already running. The clip server binds on first use.
        self._zone = wiim.get("default_zone")  # read for old configs; never a reply fallback
        # Clip server: process-wide singleton in sonos_output — bound in
        # setup() via shared_clip_server().
        self._clip_server = None
        self._speech_epoch = 0  # generation stamp; re-anchors only while current
        self._barge_epoch = 0  # bumped only by a barge-in (see SonosOutput)

    async def setup(self, setup):
        """Pipecat lifecycle hook — the ONLY reliable place to start the
        shared clip server (SonosOutput does the same). The constructor
        can't await; without this the WiiM fetches a URL nobody serves
        and replies die silently (LEDs cycled, zero audio — nothing
        listening on 8765 because no SonosOutput existed in the pipeline
        to start it). Same singleton as Sonos —
        one server, one port, all backends. Never fatal: a bad host_ip
        must not fail setup for every room (speaking binds lazily and
        reports its own error)."""
        await super().setup(setup)
        if self._enabled:
            from sonos_output import shared_clip_server
            try:
                self._clip_server = await shared_clip_server()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"wiim_output: clip server not ready ({e}) — will retry when speaking")
                return
            logger.info(f"wiim_output: backend ready — zones {list(self._zones)}, "
                        f"global vol {self._volume}")

    async def cleanup(self):
        # Shared server: do NOT stop it — other pipelines may reference
        # it and process exit releases the port (same call as SonosOutput).
        await super().cleanup()

    # ── volume resolution: live override > config per-zone > global ──

    def _volume_for(self, zone: str) -> int:
        live = live_wiim_volume(zone)
        if live is not None:
            return live
        if zone in self._zone_volumes:
            return self._zone_volumes[zone]
        return self._volume

    # ── reply playback ──────────────────────────────────────────────────

    def _barged(self, barge: int | None) -> bool:
        return barge is not None and barge != self._barge_epoch

    def _reply_zone_name(self):
        """The reply's zone from the room mapping, or None. None means the
        room has no speaker: the reply is not played (default_zone is not
        a fallback)."""
        if not self._zone_selector:
            return None
        try:
            return self._zone_selector() or None
        except Exception as e:  # noqa: BLE001 — a routing hint must never break the reply
            logger.debug(f"wiim_output: zone_selector failed: {e}")
            return None

    def owns_reply(self) -> bool:
        """This reply sounds on one of our (enabled) WiiM zones."""
        if not self._enabled:
            return False
        zone_name = self._reply_zone_name()
        return zone_name is not None and zone_name != BUILTIN and zone_name in self._zones

    async def _speak_to_wiim(self, pcm: bytes, epoch: int | None = None,
                             barge: int | None = None) -> None:
        """Interrupt-no-restore reply: snap vol → set voice vol → play clip →
        restore music vol. Echo gate blocks for the clip duration; the LED
        end-of-speech timer is set in process_frame (same timing basis as
        SonosOutput)."""
        if epoch is None:
            epoch = self._speech_epoch  # direct call (tests): act as current
        if not self._enabled:
            logger.warning("wiim_output: reply dropped — backend disabled in config")
            return
        zone_name = self._reply_zone_name()
        if zone_name is None:  # no speaker mapping: nothing plays
            logger.debug("wiim_output: reply room has no speaker mapping, skipping")
            return
        zone = self._zones.get(zone_name)
        if zone_name == BUILTIN:  # the node's own speaker plays it
            logger.debug("wiim_output: reply zone is the node's built-in speaker, skipping")
            return
        if not zone:
            logger.warning(f"wiim_output: zone '{zone_name}' not in wiim zones {list(self._zones)}, skipping")
            return
        ip = zone["ip"]
        duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
        vol = self._volume_for(zone_name)

        if self._barged(barge):  # barge-in while the clip was being built
            logger.info("wiim_output: barge-in before the clip loaded, dropped")
            return

        # MP3 (proven on this unit; WAV unreliable on LinkPlay firmware).
        # WiiM hardware fade (heard by ear): the Pro ramps
        # volume in over the first ~1s of main-media playback and ramps
        # out at the end — long replies sound fine (ramp rides silence
        # while she's mid-sentence) but SHORT replies ramp in, peak, and
        # ramp out before finishing: "quiet, ramps up, ramps down."
        # There is no firmware fade knob (HTTP API doc: vol/mute only),
        # so the clip carries the fix: 0.25s lead-in silence gives the
        # ramp nothing to swallow, and 0.35s tail holds the end audible.
        sr = int(self._sample_rate)
        ch = max(int(self._channels), 1)
        lead_in = b"\x00" * int(2 * ch * sr * 0.25)
        tail = b"\x00" * int(2 * ch * sr * 0.35)

        def _encode(pcm_bytes):
            import lameenc
            enc = lameenc.Encoder()
            enc.set_bit_rate(96)
            enc.set_in_sample_rate(sr)
            enc.set_channels(ch)
            enc.set_quality(2)
            return bytes(enc.encode(pcm_bytes)) + bytes(enc.flush())

        try:
            # Lead-in + tail ride INSIDE the clip: the Pro's fade envelope
            # never touches her words, and the tail keeps the end of short
            # replies audible instead of riding the fade-out down.
            mp3 = await asyncio.to_thread(_encode, lead_in + pcm + tail)
        except Exception as e:
            logger.error(f"wiim_output: encode failed: {e}")
            return
        # Unique per clip: a filler and a fast reply in the same second
        # must never share a name (the atomic write would swap the file
        # under a fetch still in flight).
        clip_path = clips_dir() / f"wiim-reply-{int(time.time())}-{uuid.uuid4().hex[:12]}.mp3"
        try:
            from sonos_output import _write_clip
            # off the loop, and awaited before the URL is handed out
            await asyncio.to_thread(_write_clip, clip_path, mp3)
        except OSError as e:
            logger.error(f"wiim_output: clip write failed: {e}")
            return
        try:
            if self._clip_server is None:
                from sonos_output import shared_clip_server
                self._clip_server = await shared_clip_server()
            url = self._clip_server.url_for(clip_path.name)
        except Exception as e:  # noqa: BLE001 — a clip nobody can serve is dropped, never raised out of the task
            logger.error(f"wiim_output: clip server not ready, reply dropped: {e}")
            return
        logger.info(f"wiim_output: playing clip {url} on '{zone_name}' ({ip}) at vol {vol}")

        # ── vol snapshot → set → play → confirm → restore ──
        # All API calls off the event loop (to_thread); 'OK' is cheap
        # (documented), so the play confirm rides getPlayerStatus.
        if self._barged(barge):
            logger.info("wiim_output: barge-in during the encode, clip dropped")
            return
        # Overlapping clips (a filler, then the reply) share ONE music-volume
        # snapshot: re-snapshotting would save the filler's voice volume as
        # the music level, and the filler's restore would dip the reply.
        async with _vol_lock(ip):
            # Clips play in lock order; one whose generation is no longer
            # current is dropped, so a slow filler can't replace the reply.
            if epoch != self._speech_epoch:
                logger.info("wiim_output: a newer reply is already queued, older clip dropped")
                return
            need_snap = ip not in _VOL_SAVED
            saved = _VOL_SAVED.get(ip)

            def _set_voice_volume():
                before = saved
                if need_snap:
                    st = wiim_get_status(ip)
                    raw = st.get("vol") if st else None
                    before = int(raw) if raw is not None else None
                if before is not None:
                    wiim_api(ip, f"setPlayerCmd:vol:{vol}")
                return before

            def _play(before):
                try:
                    wiim_api(ip, "setPlayerCmd:play:" + urllib.parse.quote(url, safe=""))
                except Exception:
                    # Nothing restores a clip that never played: put the
                    # music volume back now, or it stays at the voice
                    # volume. Only the snapshot's owner does (a shared
                    # snapshot is still restored by the clip that took it).
                    if need_snap and before is not None:
                        try:
                            wiim_api(ip, f"setPlayerCmd:vol:{before}")
                        except Exception as e:  # noqa: BLE001 — best effort; the play error is the one to report
                            logger.warning(f"wiim_output: volume restore after a failed play failed on {ip}: {e}")
                    raise
                return before

            async def _net():
                before = await asyncio.to_thread(_set_voice_volume)
                # Echo gate: her voice will come off the WiiM speakers; block
                # the mic from hearing the reply (no AEC by design). Armed
                # here, right before the play command, so a clip that fails
                # before it never deafens the mic. The gate is one deadline
                # shared by every clip: a failure never relaxes it. A
                # barge-in already relaxed it; this clip is stopped below.
                if self._echo_gate is not None and not self._barged(barge):
                    self._echo_gate.block(duration + 1.5)
                return await asyncio.to_thread(_play, before)

            # Shielded: a bot stop landing while the play command is in flight
            # can't abandon it half-done. The steps set the voice volume and
            # play either way; without this, nothing would put the music
            # volume back (the restore timer below never starts).
            fut = asyncio.ensure_future(_net())
            try:
                before_vol = await asyncio.shield(fut)
            except asyncio.CancelledError:
                await self._undo_inflight_play(ip, fut, need_snap)
                raise
            except Exception as e:
                logger.error(f"wiim_output: play failed on {ip}: {e}")
                return
            _VOL_SAVED[ip] = before_vol
            token = _VOL_TOKEN.get(ip, 0) + 1
            _VOL_TOKEN[ip] = token
        if self._barged(barge):
            # The barge-in landed while the play command was in flight:
            # stop it and put the music volume back, no timers left behind.
            try:
                await asyncio.to_thread(wiim_api, ip, "setPlayerCmd:stop")
                await self._restore_volume(ip, token)
            except Exception as e:  # noqa: BLE001 — best-effort; the barge-in already stopped it once
                logger.warning(f"wiim_output: late barge-in stop failed: {e}")
            logger.info("wiim_output: barge-in during load, clip stopped")
            return
        # ── LOAD-ANCHORED TIMERS ──
        # Same disease as SonosOutput: generation-anchored timers fired
        # early on the v4 dialogue lane. The play command just went out —
        # re-anchor the LED + wake gate NOW (echo gate already re-blocks
        # above, before the play call — its duration counts the pads).
        if epoch == self._speech_epoch:
            if self._led:
                # clip = lead_in(0.25) + speech + tail(0.35); LED fires
                # after the whole clip (incl. pads) has sounded + margin
                self._schedule_end_of_speech(duration + 0.25 + 0.35 + 0.6)
            if self._wake_gate is not None:
                try:
                    self._wake_gate.note_speech(duration)
                except Exception:
                    pass  # gate hint must never break the reply
            if self._echo_gate is not None:
                # max-extend over the pre-play block: a slow play command
                # ate into that window before the unit acked
                self._echo_gate.block(duration + 1.5)
        # Restore the music volume after the clip sounds (interrupt-
        # no-restore RATIFIED: music doesn't come back; its volume does).
        try:
            await asyncio.sleep(duration + 0.6)
        except asyncio.CancelledError:
            # The bot is stopping mid-reply: still put the music volume
            # back, bounded so shutdown never waits on a napping unit.
            try:
                await asyncio.wait_for(self._restore_volume(ip, token, timeout=2.0),
                                       timeout=_RESTORE_ON_CANCEL_S)
            except Exception as e:  # noqa: BLE001 — best effort; the cancel itself still propagates
                logger.warning(f"wiim_output: music volume restore on stop failed on '{zone_name}' ({ip}): {e!r}")
            except BaseException:  # noqa: BLE001 — a second cancel mid-restore; the first still propagates
                pass
            raise
        try:
            await self._restore_volume(ip, token)
        except Exception as e:  # noqa: BLE001 — best effort; the reply already played and the next clip retries
            logger.warning(f"wiim_output: music volume restore failed on '{zone_name}' ({ip}): {e}")

    async def _undo_inflight_play(self, ip: str, fut, need_snap: bool) -> None:
        """The task was cancelled mid play command: let the command finish
        (bounded) and put the music volume back if this clip took the
        snapshot. A shared snapshot stays with the clip that owns it. Best
        effort; the caller re-raises the cancel."""
        try:
            before = await asyncio.wait_for(asyncio.shield(fut), timeout=_RESTORE_ON_CANCEL_S)
        except BaseException:  # noqa: BLE001 — the play failed (it restored itself) or the unit is napping
            return
        if need_snap and before is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(wiim_api, ip, f"setPlayerCmd:vol:{before}"),
                                       timeout=_RESTORE_ON_CANCEL_S)
                logger.info(f"wiim_output: stopped mid play command; music volume {before} restored on {ip}")
            except BaseException as e:  # noqa: BLE001 — best effort on the way out
                logger.warning(f"wiim_output: volume restore after a mid-play stop failed on {ip}: {e}")

    async def _restore_volume(self, ip: str, token: int, timeout: float = 4.0) -> None:
        """Put the music volume back - only from the newest clip on `ip`
        (an older clip's timer firing mid-reply would dip the reply).
        Holds the speaker's lock: a clip arriving mid-restore must not find
        the snapshot gone and save the voice volume as the music level."""
        async with _vol_lock(ip):
            if _VOL_TOKEN.get(ip) != token or ip not in _VOL_SAVED:
                return
            before = _VOL_SAVED[ip]
            if before is not None:
                await asyncio.to_thread(wiim_api, ip, f"setPlayerCmd:vol:{before}", timeout)
            # Dropped only once the music volume is back: after a failed
            # write the next clip keeps this snapshot instead of saving
            # the voice volume as the music level.
            _VOL_SAVED.pop(ip, None)

    # ── LED / end-of-speech ──────────────────────────────────────────────

    def _schedule_end_of_speech(self, delay: float) -> None:
        self._cancel_end_of_speech()
        try:
            self._end_of_speech_timer = asyncio.get_running_loop().call_later(
                delay, self._fire_end_of_speech
            )
        except RuntimeError:
            pass

    def _cancel_end_of_speech(self) -> None:
        if self._end_of_speech_timer is not None:
            self._end_of_speech_timer.cancel()
            self._end_of_speech_timer = None

    def _fire_end_of_speech(self) -> None:
        if self._led:
            state = getattr(self._wake_gate, "state", None)
            state_val = getattr(state, "value", state)
            # The firmware's end message (rtvi.cpp); "purple" was ignored.
            self._led.send("oww-sleeping" if state_val in (None, "idle")
                           else "bot-stopped-speaking")

    # ── frame contract (same as SonosOutput) ─────────────────────────────

    # ── Wake-word barge-in ─────────────────────────────────────────────

    async def stop_current_clip(self) -> None:
        """Kill the sounding reply clip (wake-word barge-in). WiiM: a
        plain setPlayerCmd:stop. Idempotent: no clip, nothing to do."""
        self._speech_epoch += 1  # stale in-flight speak tasks can't re-anchor
        self._barge_epoch += 1  # ...and in-flight ones drop their clip
        self._cancel_end_of_speech()
        # Echo-gate rider: the clip is dead — its duration gate must
        # not eat the interrupting question. relax() never extends a
        # lapsed gate, and text containment stays armed.
        if self._echo_gate is not None:
            try:
                self._echo_gate.relax()
            except Exception:
                pass
        zone_name = self._reply_zone_name()
        zone = self._zones.get(zone_name)
        if not zone:
            return
        try:
            await asyncio.to_thread(wiim_api, zone["ip"], "setPlayerCmd:stop")
            logger.info(f"wiim_output: barge-in stopped clip on '{zone_name}'")
        except Exception as e:
            logger.warning(f"wiim_output: barge-in stop failed: {e}")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._pcm.clear()
            if self._led:
                self._cancel_end_of_speech()
                self._led.send("bot-started-speaking")
        elif isinstance(frame, TTSAudioRawFrame):
            self._pcm.extend(frame.audio)
            self._sample_rate = frame.sample_rate or self._sample_rate
            self._channels = frame.num_channels or self._channels
        elif isinstance(frame, TTSStoppedFrame):
            # Only replies in a WiiM zone are ours: for every other reply
            # SonosOutput owns the LED end + speech timers (a second owner
            # fired early here).
            if self.owns_reply() and len(self._pcm) > 0:
                pcm = bytes(self._pcm)
                self._pcm.clear()
                # Load-anchored timers — the play call
                # in _speak_to_wiim re-anchors LED + wake gate when the
                # clip actually starts; this generation-time schedule is
                # the fallback (task death). Epoch handoff = stale-guard.
                self._speech_epoch += 1
                gen = self._speech_epoch
                if self._led:
                    duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
                    self._schedule_end_of_speech(duration + 0.6)
                if self._wake_gate is not None:
                    try:
                        self._wake_gate.note_speech(len(pcm) / (2 * self._sample_rate * max(self._channels, 1)))
                    except Exception:
                        pass
                self.create_task(self._speak_to_wiim(pcm, epoch=gen, barge=self._barge_epoch))
            else:
                self._pcm.clear()
        elif isinstance(frame, CancelFrame):
            self._cancel_end_of_speech()
            if self._led:
                self._led.send("oww-sleeping")
            self._pcm.clear()
        await self.push_frame(frame, direction)