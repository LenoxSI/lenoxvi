"""sonos_output — lenoxvi's Sonos audioClip output path.

Sits after the TTS service in the pipeline, captures the spoken reply into a
WAV clip, serves it over LAN HTTP, and tells the zone's Sonos Port/Amp to play
it as an audioClip overlay (ducks music without pausing it — design note).

Config (config.yaml):
    sonos:
      enabled, default_zone, clip_volume, zones: {name: {ip, player_uid}}
    clip_server:
      host_ip, port

The processor is pass-through: frames continue downstream, so the browser
client keeps its audio while Sonos gets the same reply.
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


class SonosOutput(FrameProcessor):
    """Captures TTS audio and plays it on the zone's Sonos as an audioClip."""

    _shared_clip_server = None  # module-level: ONE server for all pipelines
    _prune_task_started = False  # ONE hourly prune loop per process

    def __init__(self, config_path: str = "config.yaml", echo_gate=None, **kwargs):
        super().__init__(**kwargs)
        self._echo_gate = echo_gate
        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        sonos = cfg.get("sonos", {})
        self._enabled = bool(sonos.get("enabled"))
        self._zone = sonos.get("default_zone")
        self._volume = int(sonos.get("clip_volume", 70))
        self._zones = sonos.get("zones", {})
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
        # The clip server binds a fixed port (8765); binding per-pipeline
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
        # setup() is the guaranteed lifecycle hook in pipecat 1.10 —
        # FrameProcessor.start() is NOT auto-called for bare processors.
        await super().setup(setup)
        if self._enabled and not self._clip_server._runner:
            await self._clip_server.start()

    async def cleanup(self):
        # Shared server: do NOT stop it here — other pipelines may still
        # reference it, and the bot process exit releases the port anyway.
        await super().cleanup()

    async def _speak_to_sonos(self, pcm: bytes) -> None:
        # Block echo handling for exactly as long as this clip will sound:
        # the mic will hear it off the zone speakers (no AEC by design).
        if self._echo_gate is not None:
            duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
            self._echo_gate.block(duration + 1.5)  # + Sonos load (~200ms) + tail margin
        zone = self._zones.get(self._zone)
        if not zone:
            logger.warning("sonos_output: no zone config, skipping Sonos playback")
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
        logger.info(f"sonos_output: playing clip {url} on '{self._zone}' at vol {self._volume}")

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
        elif isinstance(frame, TTSAudioRawFrame):
            self._pcm.extend(frame.audio)
            self._sample_rate = frame.sample_rate or self._sample_rate
            self._channels = frame.num_channels or self._channels
        elif isinstance(frame, (TTSStoppedFrame, CancelFrame)):
            if self._enabled and len(self._pcm) > 0:
                pcm = bytes(self._pcm)
                self._pcm.clear()
                # Fire-and-forget: never block the pipeline on Sonos I/O
                self.create_task(self._speak_to_sonos(pcm))

        await self.push_frame(frame, direction)