"""sonos_output — lenoxvi's Sonos audioClip output path.

Sits after the TTS service in the pipeline, captures the spoken reply into an
MP3 clip, serves it over LAN HTTP, and tells the zone's Sonos Port/Amp to play
it as an audioClip overlay (ducks music without pausing it — design note).

Config (config.yaml):
    sonos:
      enabled, default_zone, clip_volume, zones: {name: {ip, player_uid}}
    clip_server:
      host_ip, port

The processor is pass-through: frames continue downstream, so the browser
client keeps its audio while Sonos gets the same reply.

LED ring (2026-09-30): this processor is the REAL-TIME speaking signal —
it sits directly downstream of TTS, so TTSStarted/TTSStopped frames
reach it the moment speech starts/ends, even while HermesAgent is
blocked on a brain call (the wake gate's speaking view rides upstream
frames, which stall behind the brain). Speaking LEDs go out on the
pipeline-free led_state wire.
"""

import asyncio
import time
from pathlib import Path

import yaml
from aiohttp import web
from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ClipServer:
    """Tiny LAN HTTP server so Sonos players can fetch TTS clips."""

    def __init__(self, clips_dir: Path, host_ip: str, port: int):
        self._dir = clips_dir
        self._host_ip = host_ip
        self._port = port
        self._runner = None

    async def start(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        app = web.Application()
        app.router.add_static("/", self._dir, show_index=False)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host_ip, self._port)
        await site.start()
        logger.info(f"clip server on http://{self._host_ip}:{self._port}/")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def url_for(self, filename: str) -> str:
        return f"http://{self._host_ip}:{self._port}/{filename}"


def _prune_clips(clips_dir: Path, retention_days: int) -> int:
    """Age-gated .mp3 prune. Returns count removed. Shared by the startup
    pass and the hourly task — one implementation, no drift."""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    if clips_dir.is_dir():
        for f in clips_dir.glob("*.mp3"):
            if f.name.startswith("duck-"):
                continue  # the duck-holder silence clip is permanent
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass  # raced a fetch; leave it for next pass
    return removed


async def _clip_prune_loop(clips_dir: Path, retention_days: int):
    """Hourly clip prune (2026-09-20): the old construction-time-only prune
    never fired on a quiet month — the node holds ONE long-lived session, so
    SonosOutput constructs ~once per boot, not per session. This task runs
    every hour regardless of restarts/sessions. Lives and dies with the bot
    (which systemd keeps alive)."""
    while True:
        await asyncio.sleep(3600)
        removed = _prune_clips(clips_dir, retention_days)
        if removed:
            logger.info(f"sonos_output: hourly prune removed {removed} clip(s)")


def _extract_clip_id(resp) -> str | None:
    """Defensively pull a clip id out of a loadAudioClip response.
    Shape varies by firmware (list[dict] / dict, 'id'/'clipId' keys);
    log the response once elsewhere and tighten this if it misses."""
    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("id", "clipId", "clip_id") and isinstance(v, str) and v:
                    return v
                r = _walk(v)
                if r:
                    return r
        elif isinstance(o, (list, tuple)):
            for item in o:
                r = _walk(item)
                if r:
                    return r
        return None
    try:
        return _walk(resp)
    except Exception:
        return None


class SonosOutput(FrameProcessor):
    """Captures TTS audio and plays it on the zone's Sonos as an audioClip."""

    _shared_clip_server = None  # class-level: ONE server for all pipelines
    _prune_task_started = False  # ONE hourly prune loop per process

    def __init__(self, config_path: str | None = None, echo_gate=None,
                 zone_selector=None, wake_gate=None, led=None, **kwargs):
        super().__init__(**kwargs)
        self._echo_gate = echo_gate
        # Direct LED wire + the wake gate's state (bot.py wires both).
        # Speaking LEDs fire HERE, from real-time TTS frames — this
        # processor sits downstream of the brain, so TTSStarted/
        # TTSStopped reach it the moment speech starts/ends even while
        # HermesAgent is blocked on a brain call. The old speaking signal
        # (BotStartedSpeakingFrame at the wake gate) rides upstream frames
        # and stalled behind the brain (2026-09-30 12:23 incident).
        self._wake_gate = wake_gate
        self._led = led
        # Duck holder (2026-09-30): the audioClip duck only held while a
        # REPLY clip sounded — the rest of the awake window (user speaking,
        # follow-up listening) ran at full music volume straight into an
        # open mic, and VAD+STT transcribed lyrics as user turns (the
        # "Nantucket." fragment, 12:23). While the wake gate is awake,
        # hold a SILENCE clip loaded on the reply zone: same duck, no
        # sound. Reply clips replace it; refresh keeps it alive; gate
        # close cancels it and the music unducks. Polls the gate's live
        # state — no pipeline frames, so a blocked brain can't stall it.
        self._duck_while_awake = True  # set from config below, once sonos cfg is parsed
        self._duck_task = None
        self._duck_holding = False
        self._duck_clip_id = None
        self._duck_url = None
        self._duck_logged_response = False
        # Monotonic time until which a REPLY clip is sounding — the holder
        # must not refresh silence over it (a loadAudioClip replaces the
        # active clip; refreshing mid-reply would mute her mid-sentence).
        self._clip_active_until = 0.0
        # Optional sync callable returning a zone NAME for the reply clip
        # (room-aware routing: the dashboard registry records which
        # room's wake opened the turn; bot.py passes a selector that
        # maps it through config.yaml room_zones). Must never raise.
        self._zone_selector = zone_selector
        # Read through core/config so LVI_CONFIG (if ever set) applies here
        # too — a direct Path("config.yaml") default would let the bot read
        # two different config files in one process.
        from config import CONFIG_PATH

        cfg = yaml.safe_load(Path(config_path or CONFIG_PATH).read_text()) or {}
        sonos = cfg.get("sonos", {})
        self._enabled = bool(sonos.get("enabled"))
        self._zone = sonos.get("default_zone")
        self._volume = int(sonos.get("clip_volume", 70))
        self._zones = sonos.get("zones", {})
        self._duck_while_awake = bool(sonos.get("duck_while_awake", True))
        cs = cfg.get("clip_server", {})
        # Opportunistic clip pruning (2026-09-20): once at startup AND hourly
        # via _clip_prune_loop (a quiet month with no restarts must not grow
        # clips/ — the node holds one long-lived session, so construction
        # alone fires ~never). Retention: sonos.clip_retention_days (48h).
        retention_days = int(sonos.get("clip_retention_days", 7))
        removed = _prune_clips(Path("clips"), retention_days)
        if removed:
            logger.info(f"sonos_output: pruned {removed} clip(s) older than {retention_days}d")
        if not SonosOutput._prune_task_started:
            SonosOutput._prune_task_started = True
            asyncio.get_event_loop().create_task(_clip_prune_loop(Path("clips"), retention_days))
        # The clip server binds clip_server.port; binding per-pipeline
        # leaks the port when a pipeline is cancelled without cleanup()
        # (idle-timeout cancel path), and the next session then dies with
        # "[Errno 98] address already in use" (seen 2026-09-15 with the
        # always-reconnecting ESP32 node). Bind ONCE, share across pipelines.
        if SonosOutput._shared_clip_server is None:
            SonosOutput._shared_clip_server = ClipServer(
                Path("clips"),
                # host_ip is REQUIRED config: it's the address Sonos players
                # fetch clips from, so it can't default to a guess. A missing
                # value fails the TCPSite bind loudly at startup.
                cs.get("host_ip") or "<bot-host-ip>",
                int(cs.get("port", 8765)),
            )
        self._clip_server = SonosOutput._shared_clip_server
        self._pcm = bytearray()
        self._sample_rate = 24000
        self._channels = 1
        self._clip_count = 0
        if not self._enabled:
            logger.info("sonos_output disabled in config.yaml")
        elif self._zone not in self._zones:
            logger.warning(f"sonos_output: zone '{self._zone}' not in config zones {list(self._zones)}")

    async def setup(self, setup):
        # setup() is the guaranteed lifecycle hook in pipecat 1.11 —
        # FrameProcessor.start() is NOT auto-called for bare processors.
        await super().setup(setup)
        if self._enabled and not self._clip_server._runner:
            await self._clip_server.start()
        # Eternal duck-holder: poll the wake gate, hold the audioClip
        # duck while she's listening (2026-09-30).
        self._start_duck_holder()

    async def cleanup(self):
        # Shared server: do NOT stop it here — other pipelines may still
        # reference it, and the bot process exit releases the port anyway.
        await super().cleanup()

    async def _ensure_duck_clip(self) -> str:
        """Lazily create the permanent silence clip (8s @ 24kHz mono) and
        return its served URL. Volume 0 content — the audioClip duck is
        what we want, not the sound."""
        if self._duck_url:
            return self._duck_url
        import lameenc

        pcm = b"\x00\x00" * (24000 * 8)  # 8s of silence, 16-bit mono
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(96)
        encoder.set_in_sample_rate(24000)
        encoder.set_channels(1)
        encoder.set_quality(2)
        mp3 = bytes(encoder.encode(pcm)) + bytes(encoder.flush())
        p = Path("clips") / "duck-silence.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(mp3)
        self._duck_url = self._clip_server.url_for(p.name)
        logger.info(f"sonos_output: duck-holder clip ready ({p})")
        return self._duck_url

    async def _duck_holder_loop(self) -> None:
        """Eternal per-pipeline duck state machine. Polls the wake gate's
        live state every 0.5s (no pipeline frames — immune to brain
        stalls): idle→awake = connect to the wake room's zone and load the
        8s silence clip (the audioClip duck); awake = refresh at 6s so it
        never lapses, but NEVER while a reply clip is sounding (a load
        would replace her clip mid-sentence); awake→idle = cancel the
        holder clip (snappy unduck) or let it expire."""
        url = None
        try:
            url = await self._ensure_duck_clip()
        except Exception as e:
            logger.warning(f"sonos_output: duck clip create failed: {e}")
            return
        sw = None
        zone_name = None
        next_refresh = 0.0
        try:
            while True:
                await asyncio.sleep(0.5)
                gate_state = getattr(self._wake_gate, "state", None)
                state_val = getattr(gate_state, "value", gate_state)
                awake = state_val not in (None, "idle")

                if not awake:
                    if sw is not None:
                        # Gate closed: cancel the holder clip (if we have
                        # its id) and drop the connection. The music
                        # unducks the moment no clip is active.
                        try:
                            if self._duck_clip_id:
                                await sw.cancel_clip(self._duck_clip_id)
                        except Exception:
                            pass
                        try:
                            await sw.close()
                        except Exception:
                            pass
                        sw = None
                        self._duck_holding = False
                        self._duck_clip_id = None
                        logger.info("sonos_output: duck-holder released")
                    continue

                # Awake: ensure the duck is held
                if sw is None:
                    zone_name = self._zone
                    if self._zone_selector:
                        try:
                            zone_name = self._zone_selector() or self._zone
                        except Exception:
                            pass
                    zone = self._zones.get(zone_name)
                    if not zone:
                        continue  # unmapped zone — can't duck
                    from sonos_websocket import SonosWebsocket

                    try:
                        sw = SonosWebsocket(
                            zone["ip"], player_id=zone["player_uid"]
                        )
                        await sw.connect()
                    except Exception as e:
                        logger.warning(f"sonos_output: duck-holder connect failed: {e}")
                        sw = None
                        await asyncio.sleep(2.0)  # retry next tick
                        continue
                    self._duck_holding = True
                    next_refresh = 0.0  # load immediately
                    logger.info(f"sonos_output: duck-holder active on '{zone_name}'")

                if time.monotonic() < self._clip_active_until:
                    # A reply clip is sounding: it owns the duck. Wait it
                    # out (no refresh — a load would replace her clip).
                    continue
                if time.monotonic() >= next_refresh:
                    # (Re)load the silence clip. A loadAudioClip REPLACES
                    # the active clip on the player, so this both starts
                    # and refreshes the duck. Capture the clip id for a
                    # snappy cancel on release.
                    try:
                        resp = await sw.play_clip(url, volume=self._volume)
                    except Exception as e:
                        logger.warning(f"sonos_output: duck load failed: {e}")
                        await asyncio.sleep(2.0)
                        continue
                    clip_id = _extract_clip_id(resp)
                    if clip_id:
                        self._duck_clip_id = clip_id
                    if not self._duck_logged_response:
                        logger.info(f"sonos_output: duck load resp: {str(resp)[:140]}")
                        self._duck_logged_response = True
                    next_refresh = time.monotonic() + 6.0
        except asyncio.CancelledError:
            pass
        finally:
            if sw is not None:
                try:
                    if self._duck_clip_id:
                        await sw.cancel_clip(self._duck_clip_id)
                except Exception:
                    pass
                try:
                    await sw.close()
                except Exception:
                    pass
            self._duck_holding = False

    def _start_duck_holder(self) -> None:
        """Fire the eternal holder loop as a loop task (safe from any
        context). Started once per pipeline at setup; cancelled at
        CancelFrame."""
        if not self._duck_while_awake or self._duck_task is not None:
            return
        try:
            self._duck_task = asyncio.get_running_loop().create_task(
                self._duck_holder_loop()
            )
        except RuntimeError:
            pass

    def _stop_duck_holder(self) -> None:
        if self._duck_task is not None:
            self._duck_task.cancel()
            self._duck_task = None

    async def _speak_to_sonos(self, pcm: bytes) -> None:
        # Block echo handling for exactly as long as this clip will sound:
        # the mic will hear it off the zone speakers (no AEC by design).
        if self._echo_gate is not None:
            duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
            self._echo_gate.block(duration + 1.5)  # + Sonos load (~200ms) + tail margin
        zone_name = self._zone
        if self._zone_selector:
            try:
                zone_name = self._zone_selector() or self._zone
            except Exception as e:  # routing hint must never break the reply
                logger.debug(f"sonos_output: zone_selector failed: {e}")
        zone = self._zones.get(zone_name)
        if not zone:
            logger.warning(f"sonos_output: zone '{zone_name}' not in config zones {list(self._zones)}, skipping Sonos playback")
            return
        # MP3, not WAV — the proven-working format for Sonos audioClip
        # (design note; WAV is not in Sonos's documented supported set).
        import lameenc

        encoder = lameenc.Encoder()
        encoder.set_bit_rate(96)
        encoder.set_in_sample_rate(self._sample_rate)
        encoder.set_channels(self._channels)
        encoder.set_quality(2)  # highest quality = slowest encode is fine here
        mp3 = bytes(encoder.encode(pcm)) + bytes(encoder.flush())

        self._clip_count += 1
        filename = f"reply-{int(time.time())}-{self._clip_count}.mp3"
        clip_path = Path("clips") / filename
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(mp3)
        url = self._clip_server.url_for(filename)
        logger.info(f"sonos_output: playing clip {url} on '{zone_name}' at vol {self._volume}")

        from sonos_websocket import SonosWebsocket

        sw = SonosWebsocket(zone["ip"], player_id=zone["player_uid"])
        try:
            await sw.connect()
            await sw.play_clip(url, volume=self._volume)
        finally:
            await sw.close()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            self._pcm.clear()
            if self._led:  # speech starts sounding within ~0.5s — light now
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
                if self._led:
                    # TTSStopped = GENERATION end, not playback end. ElevenLabs
                    # bursts the whole clip through the pipeline in <1s while
                    # the Sonos sounds it for `duration` seconds (measured
                    # 2026-09-30 15:16: cyan flashed 0.6s, then purple through
                    # 18s of actual speech). Schedule the end-of-speaking LED
                    # for when the audio stops SOUNDING, not when frames stop.
                    duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
                    self._schedule_end_of_speech(duration + 0.5)  # Sonos load ~0.2 + tail
                # Duck ownership: while THIS clip sounds, the duck-holder must not
                # refresh silence over it (a loadAudioClip replaces the active
                # clip — refreshing mid-reply would mute her mid-sentence).
                duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
                self._clip_active_until = time.monotonic() + duration + 1.0
                # Fire-and-forget: never block the pipeline on Sonos I/O
                self.create_task(self._speak_to_sonos(pcm))
            elif self._led:
                # No clip to time (Sonos disabled): end immediately.
                self._schedule_end_of_speech(0.0)
        elif isinstance(frame, CancelFrame):
            # Session teardown: ring goes dark, no matter what.
            self._cancel_end_of_speech()
            self._stop_duck_holder()
            if self._led:
                self._led.send("oww-sleeping")
            self._pcm.clear()

        await self.push_frame(frame, direction)

    def _schedule_end_of_speech(self, delay: float) -> None:
        """End-of-speaking LED fires `delay` seconds from now (clip
        duration + load margin). Loop-native timer: no pipeline frames, no
        processors — can't be stalled by a blocked brain."""
        self._cancel_end_of_speech()
        try:
            self._end_of_speech_timer = asyncio.get_running_loop().call_later(
                delay, self._fire_end_of_speech
            )
        except RuntimeError:  # no running loop (sync/test context)
            pass

    def _cancel_end_of_speech(self) -> None:
        if self._end_of_speech_timer is not None:
            self._end_of_speech_timer.cancel()
            self._end_of_speech_timer = None

    def _fire_end_of_speech(self) -> None:
        self._end_of_speech_timer = None
        if not self._led:
            return
        # Honest end state = the gate's LIVE state at the moment speech
        # stops sounding: purple while the turn lives on, dark when the
        # gate already timed out mid-brain-call.
        gate_state = getattr(self._wake_gate, "state", None)
        state_val = getattr(gate_state, "value", gate_state)
        self._led.send("bot-stopped-speaking"
                       if state_val not in (None, "idle")
                       else "oww-sleeping")