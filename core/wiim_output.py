"""WiiM output backend — interrupt-no-restore reply playback.

Sibling of sonos_output.py, same FrameProcessor contract (TTSStarted /
TTSAudioRaw / TTSStopped → MP3 → clip server URL → play), but with WiiM
semantics established by the 2026-10-04 arrival verify (design note
correction):

  - playPromptUrl is a STUB on this firmware generation (Linkplay
    4.8.827634): acks OK, never fetches. NOT used.
  - Replies play via setPlayerCmd:play:<url> — proven to fetch and play
    the exact same clips the Sonos path uses (arrival test, mode 10).
  - Interrupt semantics, ratified by the owner 2026-10-04: music stops when
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
Zone volume resolution mirrors sonos_output: live override (dashboard
slider) > config wiim.zone_volumes > global clip_volume.
"""

import asyncio
import json
import ssl
import time
import urllib.request
import urllib.parse
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    Frame,
    TTSStartedFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Shared SSL context: the WiiM serves a self-signed cert on 443.
_SSL_CTX = ssl._create_unverified_context()
_SSL_CTX.set_ciphers("ALL:@SECLEVEL=0")  # match pywiim: old TLS on LinkPlay firmware


def wiim_api(ip: str, cmd: str, timeout: float = 4.0) -> str:
    """One HTTP API command. Returns the raw body ('OK', JSON, …).

    Runs on the pipeline's event loop — every caller wraps it in
    asyncio.to_thread (the API can stall if the unit naps; a stalled
    voice pipeline is the M4/M5 lesson from the Opus review).
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


class WiiMOutput(FrameProcessor):
    """Captures TTS audio and plays it on the zone's WiiM via play + vol snap/restore."""

    def __init__(self, config_path: str | None = None, echo_gate=None,
                 zone_selector=None, wake_gate=None, led=None, **kwargs):
        super().__init__(**kwargs)
        self._echo_gate = echo_gate
        self._wake_gate = wake_gate
        self._led = led
        self._end_of_speech_timer = None
        self._pcm: list[bytes] = []
        self._sample_rate = 16000
        self._channels = 1
        # Optional sync callable returning the reply zone NAME (same
        # contract as SonosOutput's zone_selector — bot.py's _reply_zone).
        self._zone_selector = zone_selector
        from config import cfg, DEFAULT_CLIP_VOLUME
        wiim = cfg("wiim") or {}
        self._enabled = bool(wiim.get("enabled"))
        self._zone = wiim.get("default_zone")
        self._volume = int(wiim.get("clip_volume", DEFAULT_CLIP_VOLUME))  # 041 L3: one default
        self._zone_volumes = {k: int(v) for k, v in (wiim.get("zone_volumes") or {}).items()
                              if isinstance(v, (int, float))}
        self._zones = dict(wiim.get("zones") or {})
        # Clip server: process-wide singleton in sonos_output (Opus audit
        # 2026-10-04, finding 2) — bound in setup() via shared_clip_server().
        self._clip_server = None
        self._speech_epoch = 0  # Fix A: generation stamp; re-anchors only while current

    async def setup(self, setup):
        """Pipecat lifecycle hook — the ONLY reliable place to start the
        shared clip server (SonosOutput does the same). The constructor
        can't await; without this the WiiM fetches a URL nobody serves
        and replies die silently (2026-10-04 bedroom outage: LEDs cycled,
        zero audio — nothing listening on 8765 because no SonosOutput
        existed in the pipeline to start it). Same singleton as Sonos —
        one server, one port, all backends."""
        await super().setup(setup)
        if self._enabled:
            from sonos_output import shared_clip_server
            self._clip_server = await shared_clip_server()
            logger.info(f"wiim_output: backend ready — zones {list(self._zones)}, "
                        f"default '{self._zone}', global vol {self._volume}")

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

    async def _speak_to_wiim(self, pcm: bytes, epoch: int | None = None) -> None:
        """Interrupt-no-restore reply: snap vol → set voice vol → play clip →
        restore music vol. Echo gate blocks for the clip duration; the LED
        end-of-speech timer is set in process_frame (same timing basis as
        SonosOutput)."""
        if epoch is None:
            epoch = self._speech_epoch  # direct call (tests): act as current
        if not self._enabled:
            logger.warning("wiim_output: reply dropped — backend disabled in config")
            return
        zone_name = self._zone
        if self._zone_selector:
            try:
                zone_name = self._zone_selector() or self._zone
            except Exception as e:
                logger.debug(f"wiim_output: zone_selector failed: {e}")
        zone = self._zones.get(zone_name)
        if not zone:
            logger.warning(f"wiim_output: zone '{zone_name}' not in wiim zones {list(self._zones)}, skipping")
            return
        ip = zone["ip"]
        duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
        vol = self._volume_for(zone_name)

        # Echo gate: her voice will come off the WiiM speakers; block the
        # mic from hearing the reply (no AEC by design).
        if self._echo_gate is not None:
            self._echo_gate.block(duration + 1.5)

        # MP3 (proven on this unit; WAV unreliable on LinkPlay firmware).
        # WiiM hardware fade (the owner's ear, 2026-10-04): the Pro ramps
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
        clip_path = Path("clips") / f"wiim-reply-{int(time.time())}-{id(self) % 10000}.mp3"
        try:
            clip_path.parent.mkdir(parents=True, exist_ok=True)
            clip_path.write_bytes(mp3)
        except OSError as e:
            logger.error(f"wiim_output: clip write failed: {e}")
            return
        if self._clip_server is None:
            from sonos_output import shared_clip_server
            self._clip_server = await shared_clip_server()
        url = self._clip_server.url_for(clip_path.name)
        logger.info(f"wiim_output: playing clip {url} on '{zone_name}' ({ip}) at vol {vol}")

        # ── vol snapshot → set → play → confirm → restore ──
        # All API calls off the event loop (to_thread); 'OK' is cheap
        # (documented), so the play confirm rides getPlayerStatus.
        def _net():
            st = wiim_get_status(ip)
            raw = st.get("vol") if st else None
            before_vol = int(raw) if raw is not None else None
            if before_vol is not None:
                wiim_api(ip, f"setPlayerCmd:vol:{vol}")
            wiim_api(ip, "setPlayerCmd:play:" + urllib.parse.quote(url, safe=""))
            return before_vol

        try:
            before_vol = await asyncio.to_thread(_net)
        except Exception as e:
            logger.error(f"wiim_output: play failed on {ip}: {e}")
            return
        # ── Fix A (2026-10-05): LOAD-ANCHORED TIMERS ──
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
                # max-extend over the entry-time block: the encode+play
                # latency between entry and NOW ate into that window
                self._echo_gate.block(duration + 1.5)
        # Restore the music volume after the clip sounds (interrupt-
        # no-restore RATIFIED: music doesn't come back; its volume does).
        try:
            await asyncio.sleep(duration + 0.6)
            if before_vol is not None:
                await asyncio.to_thread(wiim_api, ip, f"setPlayerCmd:vol:{before_vol}")
        except Exception:
            pass  # restore is best-effort; the reply already played

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
            self._led.send("oww-sleeping" if state_val in (None, "idle") else "purple")

    # ── frame contract (same as SonosOutput) ─────────────────────────────

    # ── B3 barge-in (design note) ────────────────────────────────────

    async def stop_current_clip(self) -> None:
        """Kill the sounding reply clip (wake-word barge-in). WiiM: a
        plain setPlayerCmd:stop. Idempotent: no clip, nothing to do."""
        self._speech_epoch += 1  # stale in-flight speak tasks can't re-anchor
        self._cancel_end_of_speech()
        # Echo-gate rider (030): the clip is dead — its duration gate must
        # not eat the interrupting question. relax() never extends a
        # lapsed gate, and text containment stays armed.
        if self._echo_gate is not None:
            try:
                self._echo_gate.relax()
            except Exception:
                pass
        zone_name = self._zone
        if self._zone_selector:
            try:
                zone_name = self._zone_selector() or self._zone
            except Exception:
                pass
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
            if self._enabled and len(self._pcm) > 0:
                pcm = bytes(self._pcm)
                self._pcm.clear()
                # Fix A (2026-10-05): load-anchored timers — the play call
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
                self.create_task(self._speak_to_wiim(pcm, epoch=gen))
            elif self._led:
                self._schedule_end_of_speech(0.0)
        elif isinstance(frame, CancelFrame):
            self._cancel_end_of_speech()
            if self._led:
                self._led.send("oww-sleeping")
            self._pcm.clear()
        await self.push_frame(frame, direction)