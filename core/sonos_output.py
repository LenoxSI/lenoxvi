"""sonos_output — LenoxVI's Sonos audioClip output path.

Sits after the TTS service in the pipeline, captures the spoken reply into an
MP3 clip, serves it over LAN HTTP, and tells the zone's Sonos Port/Amp to play
it as an audioClip overlay (ducks music without pausing it).

Config (config.yaml):
    sonos:
      enabled, default_zone, clip_volume, zones: {name: {ip, player_uid}}
    clip_server:
      host_ip, port

default_zone is still accepted (old configs load fine) but no longer routes
replies: a room with no speaker mapping (`room_zones`) stays silent until
it is mapped, and nothing is ducked for it.

The processor is pass-through: frames continue downstream, so the browser
client keeps its audio while Sonos gets the same reply.

LED ring: this processor is the REAL-TIME speaking signal —
it sits directly downstream of TTS, so TTSStarted/TTSStopped frames
reach it the moment speech starts/ends, even while IntelligenceAgent is
blocked on an intelligence call (the wake gate's speaking view rides upstream
frames, which stall behind the intelligence). Speaking LEDs go out on the
pipeline-free led_state wire.
"""

import asyncio
import os
import threading
import time
import uuid
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

from node_speaker import BUILTIN
from paths import clips_dir
from zone_config import LiveZoneConfig


class ClipServer:
    """Tiny LAN HTTP server so Sonos players can fetch TTS clips."""

    def __init__(self, clips_dir: Path, host_ip: str, port: int):
        self._dir = clips_dir
        self._host_ip = host_ip
        self._port = port
        self._runner = None

    async def start(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        # A bind failure (EADDRINUSE —
        # stale process, suite re-entry, port conflict) must NOT leave a
        # half-constructed runner behind: on the NEXT start() the
        # singleton's _runner would look "set up" while nothing listens,
        # and every clip URL 404s. Clean up fully before raising, so a
        # retry starts from zero.
        runner = web.AppRunner(app := web.Application(), access_log=None)
        app.router.add_static("/", self._dir, show_index=False)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self._host_ip, self._port)
            await site.start()
        except Exception:
            try:
                await runner.cleanup()
            except Exception:
                pass
            raise
        self._runner = runner  # set ONLY after a successful bind
        logger.info(f"clip server on http://{self._host_ip}:{self._port}/")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def url_for(self, filename: str) -> str:
        from urllib.parse import quote
        # Quote the path component: a filename with spaces/reserved chars
        # breaks the Sonos audioClip fetcher silently (the
        # "Sonos Port" test tone). Safe for every existing name.
        return f"http://{self._host_ip}:{self._port}/{quote(filename)}"


# ── process-wide singleton ──
# SonosOutput and WiiMOutput each carried their OWN class-level
# _shared_clip_server, so two backends active at once raced for one
# port: the second pipeline's setup() died with EADDRINUSE, and the
# dashboard's test-clip lane could bind 8765 out from under a
# reconnecting node. ONE server per process, ALL callers share it.


_SHARED_CLIP_SERVER: "ClipServer | None" = None
_SHARED_CLIP_LOCK: asyncio.Lock | None = None


async def shared_clip_server() -> "ClipServer":
    """The ONE clip server for this process (creates + starts on first use).

    Every caller — SonosOutput.setup, WiiMOutput.setup, and the
    dashboard's test-clip route — goes through here. Never bind a second
    server on the port; never leak an orphan server. Config read through
    config.cfg() so the suite's throwaway config works unchanged.
    """
    global _SHARED_CLIP_SERVER, _SHARED_CLIP_LOCK
    if _SHARED_CLIP_LOCK is None:
        _SHARED_CLIP_LOCK = asyncio.Lock()
    async with _SHARED_CLIP_LOCK:
        if _SHARED_CLIP_SERVER is None:
            from config import cfg
            cs = cfg("clip_server") or {}
            host_ip = cs.get("host_ip")
            if not host_ip:
                raise RuntimeError("clip_server.host_ip not set in config.yaml")
            _SHARED_CLIP_SERVER = ClipServer(clips_dir(), host_ip,
                                             int(cs.get("port", 8765)))
        if _SHARED_CLIP_SERVER._runner is None:
            await _SHARED_CLIP_SERVER.start()
        return _SHARED_CLIP_SERVER


def zone_volume_overrides(overrides: dict | None):
    """Set the live per-zone volume overrides (dashboard slider lane).

    Applied on the NEXT clip — no restart, by design: volume tuning is a
    by-ear loop and waiting for a restart between attempts would make
    the dashboard useless for it.
    """
    global _LIVE_ZONE_VOLUMES
    _LIVE_ZONE_VOLUMES = {k: int(v) for k, v in (overrides or {}).items()
                          if isinstance(v, (int, float))}


def live_zone_volume(zone: str) -> int | None:
    """The dashboard's live override for `zone`, if one is set."""
    v = _LIVE_ZONE_VOLUMES.get(zone)
    return int(v) if v is not None else None


_LIVE_ZONE_VOLUMES: dict = {}


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


def _write_clip(path: Path, data: bytes) -> None:
    """Write one clip file. Callers run it in a thread (file I/O stays off
    the event loop) and await it before handing out the clip's URL.
    Temp + rename: the clip server (on the loop) can serve a file while a
    thread writes it, and every pipeline rewrites the same duck clip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # the prune only sees *.mp3: a stray temp would stay forever
        raise


async def _clip_prune_loop(clips_dir: Path, retention_days: int):
    """Hourly clip prune: the old construction-time-only prune
    never fired on a quiet month — the node holds ONE long-lived session, so
    SonosOutput constructs ~once per boot, not per session. This task runs
    every hour regardless of restarts/sessions. Lives and dies with the bot
    (which systemd keeps alive)."""
    while True:
        await asyncio.sleep(3600)
        removed = await asyncio.to_thread(_prune_clips, clips_dir, retention_days)
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


_DUCK_CLIP_RETRY_S = 30.0  # duck-clip create retry while awake: a broken disk must not spin an encode loop


class SonosOutput(LiveZoneConfig, FrameProcessor):
    _section_name = "sonos"

    """Captures TTS audio and plays it on the zone's Sonos as an audioClip."""

    _prune_task_started = False  # ONE hourly prune loop per process
    _zones_audited = False  # ONE capability audit per process
    _audit_task = None  # keep a ref: a GC'd task dies mid-audit

    def __init__(self, config_path: str | None = None, echo_gate=None,
                 zone_selector=None, wake_gate=None, led=None, reply_elsewhere=None,
                 room_selector=None, **kwargs):
        super().__init__(**kwargs)
        self._echo_gate = echo_gate
        # Optional sync callable returning this pipeline's room name: only
        # used to name the room in the "no speaker mapping" log line.
        self._room_selector = room_selector
        # True while another backend (a WiiM zone) sounds the reply: that
        # backend owns the LED end and speech timers; two owners meant this
        # one's early generation-time fallback flipped the ring mid-reply.
        self._reply_elsewhere = reply_elsewhere
        # Direct LED wire + the wake gate's state (bot.py wires both).
        # Speaking LEDs fire HERE, from real-time TTS frames — this
        # processor sits downstream of the intelligence, so TTSStarted/
        # TTSStopped reach it the moment speech starts/ends even while
        # IntelligenceAgent is blocked on an intelligence call. The old speaking signal
        # (BotStartedSpeakingFrame at the wake gate) rides upstream frames
        # and stalled behind the intelligence.
        self._wake_gate = wake_gate
        self._led = led
        # Duck holder: the audioClip duck only held while a
        # REPLY clip sounded — the rest of the awake window (user speaking,
        # follow-up listening) ran at full music volume straight into an
        # open mic, and VAD+STT transcribed lyrics as user turns. While
        # the wake gate is awake,
        # hold a SILENCE clip loaded on the reply zone: same duck, no
        # sound. Reply clips replace it; refresh keeps it alive; gate
        # close cancels it and the music unducks. Polls the gate's live
        # state — no pipeline frames, so a blocked intelligence can't stall it.
        self._duck_while_awake = True  # set from config below, once sonos cfg is parsed
        self._duck_task = None
        self._duck_holding = False
        self._duck_clip_id = None
        self._duck_url = None
        self._duck_logged_response = False
        # Reply clip id (from loadAudioClip response) — cancelled on gate
        # close so a still-sounding silence TAIL can't hold the duck after
        # the conversation ends.
        self._reply_clip_id = None
        # Monotonic time until which a REPLY clip is sounding — the holder
        # must not refresh silence over it (a loadAudioClip replaces the
        # active clip; refreshing mid-reply would mute her mid-sentence).
        self._clip_active_until = 0.0
        # Reply clips between connect and load: the holder must not load its
        # silence over one either (that window is before _clip_active_until).
        self._reply_loading = 0
        # Duration-timed end-of-speaking LED (see process_frame): a loop
        # timer, cancelled on new speech / session teardown. (Re-added
        # — the duck-holder insertion clobbered this line and
        # every TTS frame threw AttributeError, killing reply dispatch.)
        self._end_of_speech_timer = None
        # Optional sync callable returning a zone NAME for the reply clip
        # (room-aware routing: the dashboard registry records which
        # room's wake opened the turn; bot.py passes a selector that
        # maps it through config.yaml room_zones). Must never raise.
        self._zone_selector = zone_selector
        # Read through core/config's cached cfg() (LVI_CONFIG applies, and no
        # file read on the event loop for every session). An explicit
        # config_path (suite fixtures only) is still read directly.
        from config import cfg

        if config_path:
            sonos = (yaml.safe_load(Path(config_path).read_text()) or {}).get("sonos") or {}
            self._static_section = sonos
        else:
            sonos = cfg("sonos") or {}
        # enabled / zones / zone_volumes / clip_volume are read at reply time
        # (zone_config.LiveZoneConfig): a zone added or a volume reset on the
        # dashboard reaches sessions that are already running. Per-zone
        # volume: config `sonos.zone_volumes` is the durable copy; the live
        # layer (zone_volume_overrides below) is the slider's instant one.
        self._duck_while_awake = bool(sonos.get("duck_while_awake", True))
        # Opportunistic clip pruning: once per pipeline at
        # setup() (in a thread: the glob/stat/unlink pass grows with clips/)
        # AND hourly via _clip_prune_loop (a quiet month with no restarts
        # must not grow clips/ — the node holds one long-lived session, so
        # setup alone fires ~never). Retention: sonos.clip_retention_days.
        # A hand-edited bad value must not fail every session build; a
        # negative one would prune every clip, including ones still playing.
        raw_days = sonos.get("clip_retention_days", 7)
        try:
            retention_days = int(raw_days)
            if isinstance(raw_days, bool) or retention_days < 0:
                raise ValueError(raw_days)
        except (TypeError, ValueError, OverflowError):
            logger.warning(f"sonos_output: sonos.clip_retention_days {raw_days!r}"
                           f" is not a whole number of days >= 0 — using 7")
            retention_days = 7
        self._retention_days = retention_days
        if not SonosOutput._prune_task_started:
            SonosOutput._prune_task_started = True
            try:
                asyncio.get_event_loop().create_task(_clip_prune_loop(clips_dir(), retention_days))
            except RuntimeError:
                # No event loop (sync/test context — exposed by the
                # test suite): construction must never crash
                # the pipeline builder; pruning happens on next real loop.
                SonosOutput._prune_task_started = False
                logger.debug("sonos_output: no event loop at construction — prune task deferred")
        # The clip server is a process-wide singleton: ONE server shared with WiiMOutput and
        # the dashboard's test-clip lane. Constructing here only CREATES
        # it if absent; binding happens in async setup() via
        # shared_clip_server() — the constructor can't await. A missing
        # host_ip raises there, loudly.
        self._clip_server = None  # bound in setup() via shared_clip_server()
        self._pcm = bytearray()
        self._sample_rate = 24000
        self._channels = 1
        self._speech_epoch = 0  # generation stamp; load-anchored timers re-anchor only while current
        self._play_lock: asyncio.Lock | None = None  # one load at a time, in order (made on the loop)
        # Bumped ONLY by a barge-in (a new generation must not kill an older
        # clip still loading). A speak task that sees it move drops its clip.
        self._barge_epoch = 0
        if not self._enabled:
            logger.info("sonos_output disabled in config.yaml")
        # Zone capability audit: audioClip works on every S2
        # player (Era/Arc/Roam/Five/Ray/SYMFONISK included) but NOT on S1
        # legacy gear — and our playback path can't notice: we construct
        # SonosWebsocket(ip, player_id=uid) with the id from config, which
        # BYPASSES the library's AUDIO_CLIP capability check
        # (get_player_id() short-circuits on a provided id; an early reading
        # said "S2 Port/Amp only" but the real rule is the capability flag,
        # per docs.sonos.com/reference/audioclip). Audit ONCE per process:
        # connect WITHOUT player_id (forcing the library to resolve the id
        # — and check AUDIO_CLIP — itself), cross-check the resolved id
        # against config's player_uid, and log the verdict per zone. Log-only:
        # a dead/legacy zone must never crash the bot at startup or
        # mid-session.
        if (self._enabled and self._zones
                and not SonosOutput._zones_audited):
            SonosOutput._zones_audited = True
            try:
                SonosOutput._audit_task = (
                    asyncio.get_event_loop().create_task(
                        self._audit_zone_capabilities()
                    )
                )
            except RuntimeError:
                pass  # no loop (sync/test context) — audit is best-effort

    async def setup(self, setup):
        # setup() is the guaranteed lifecycle hook in pipecat 1.11 —
        # FrameProcessor.start() is NOT auto-called for bare processors.
        await super().setup(setup)
        # Singleton lane: one server
        # per process, shared with WiiMOutput + the dashboard. Constructed
        # and bound HERE (or reused if WiiM/setup already bound it) —
        # never a second bind on the port. Only when Sonos is on, and never
        # fatal: a bad host_ip must not fail setup for every room (speaking
        # binds lazily and reports its own error).
        if self._enabled:
            try:
                self._clip_server = await shared_clip_server()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"sonos_output: clip server not ready ({e}) — will retry when speaking")
        # Startup prune pass, off the event loop.
        try:
            removed = await asyncio.to_thread(_prune_clips, clips_dir(), self._retention_days)
        except Exception as e:  # noqa: BLE001 — pruning must never break pipeline setup
            logger.warning(f"sonos_output: startup clip prune failed: {e}")
            removed = 0
        if removed:
            logger.info(f"sonos_output: pruned {removed} clip(s) older than {self._retention_days}d")
        # Eternal duck-holder: poll the wake gate, hold the audioClip
        # duck while she's listening.
        self._start_duck_holder()

    async def cleanup(self):
        # Shared server: do NOT stop it here — other pipelines may still
        # reference it, and the bot process exit releases the port anyway.
        # The duck-holder task dies with THIS pipeline
        # whatever ended it (EndFrame/error shutdown), not just CancelFrame.
        self._stop_duck_holder()
        await super().cleanup()

    async def _audit_zone_capabilities(self) -> None:
        """Log-only AUDIO_CLIP capability verdict for every configured zone.

        Connects without player_id so the library must resolve the id via
        get_player_id() — that path raises Unsupported when the player lacks
        the AUDIO_CLIP capability. Also cross-checks the resolved id against
        config's player_uid (a mismatch means config points at the wrong
        player — clips would load on the wrong speaker). Verdict per zone in
        the log; failures are warnings, never exceptions: a dead zone at
        boot is information, not a crash.
        """
        from sonos_websocket import SonosWebsocket
        from sonos_websocket.exception import Unsupported

        for name, zone in self._zones.items():
            uid = zone.get("player_uid")
            ip = zone.get("ip")
            if not uid or not ip:
                logger.warning(
                    f"sonos_output: zone '{name}' missing ip/player_uid — "
                    f"cannot audit; clips will fail for this zone at runtime"
                )
                continue
            sw = SonosWebsocket(ip)  # NO player_id: force the capability check
            try:
                try:
                    resolved = await asyncio.wait_for(sw.get_player_id(), 15)
                except Unsupported:
                    logger.warning(
                        f"sonos_output: zone '{name}' ({ip}) does NOT support "
                        f"AUDIO_CLIP — S1/legacy player? Clips will fail; "
                        f"replace the zone or the speaker"
                    )
                    continue
                if resolved != uid:
                    logger.warning(
                        f"sonos_output: zone '{name}' player_uid mismatch: "
                        f"config says {uid}, player reports {resolved} — "
                        f"updating config will make clip loads target the "
                        f"intended player"
                    )
                else:
                    logger.info(
                        f"sonos_output: zone '{name}' ({ip}) AUDIO_CLIP capable, "
                        f"player_uid {uid} verified"
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"sonos_output: zone '{name}' ({ip}) audit timed out — "
                    f"speaker offline or slow; clips will fail until it's back"
                )
            except Exception as e:
                logger.warning(
                    f"sonos_output: zone '{name}' ({ip}) audit failed: {e} — "
                    f"proceeding; playback errors (if any) will surface at clip time"
                )
            finally:
                try:
                    await sw.close()
                except Exception:
                    pass

    async def _ensure_duck_clip(self) -> str:
        """Lazily create the permanent silence clip (30s @ 24kHz mono) and
        return its served URL. Volume 0 content — the audioClip duck is
        what we want, not the sound."""
        if self._duck_url:
            return self._duck_url
        import lameenc

        pcm = b"\x00\x00" * (24000 * 30)  # 30s of silence, 16-bit mono
        # Same blocking-encode rule — one-shot, but the first holder
        # start happens on the pipeline's event loop.
        def _encode():
            encoder = lameenc.Encoder()
            encoder.set_bit_rate(96)
            encoder.set_in_sample_rate(24000)
            encoder.set_channels(1)
            encoder.set_quality(2)
            return bytes(encoder.encode(pcm)) + bytes(encoder.flush())

        mp3 = await asyncio.to_thread(_encode)
        p = clips_dir() / "duck-silence.mp3"
        await asyncio.to_thread(_write_clip, p, mp3)
        server = self._clip_server or await shared_clip_server()
        self._duck_url = server.url_for(p.name)
        logger.info(f"sonos_output: duck-holder clip ready ({p})")
        return self._duck_url

    async def _duck_holder_loop(self) -> None:
        """Eternal per-pipeline duck state machine. Polls the wake gate's
        live state every 0.5s (no pipeline frames — immune to intelligence
        stalls): idle→awake = connect to the wake room's zone and load the
        30s silence clip (the audioClip duck); awake = refresh at 24s so
        it never lapses, but never while her SPEECH is sounding (a load
        would replace her clip mid-sentence — and the reply clip's silence
        tail makes the post-reply handoff gapless); awake→idle = cancel
        the holder clip (snappy unduck) or let it expire.
        A failed duck-clip create (disk full, clip server not bound yet) is
        retried on the next wake or every _DUCK_CLIP_RETRY_S while awake:
        one transient failure must not end ducking for the session."""
        clip_failures = 0

        async def _duck_clip_url():
            nonlocal clip_failures, next_clip_try
            try:
                url = await self._ensure_duck_clip()
            except Exception as e:  # noqa: BLE001 — the holder outlives a failed create and retries
                next_clip_try = time.monotonic() + _DUCK_CLIP_RETRY_S
                clip_failures += 1
                if clip_failures == 1:
                    logger.warning(f"sonos_output: duck clip create failed: {e} — retrying on wake")
                else:
                    logger.debug(f"sonos_output: duck clip create failed again: {e}")
                return None
            if clip_failures:
                logger.info(f"sonos_output: duck clip create recovered after {clip_failures} failure(s)")
                clip_failures = 0
            return url

        sw = None
        zone_name = None
        next_refresh = 0.0
        next_clip_try = 0.0
        was_awake = False
        try:
            # A disabled backend prepares nothing: the clip is made on the
            # first wake after it is enabled.
            url = await _duck_clip_url() if self._enabled else None
            while True:
                await asyncio.sleep(0.5)
                gate_state = getattr(self._wake_gate, "state", None)
                state_val = getattr(gate_state, "value", gate_state)
                # `enabled` is read live: a backend switched off mid-session
                # releases its duck like a closed gate.
                awake = state_val not in (None, "idle") and self._enabled
                woke, was_awake = awake and not was_awake, awake

                if not awake:
                    if sw is not None:
                        # Gate closed: cancel the holder clip AND any
                        # still-active reply-tail clip (an 8s tail
                        # sounding past the conversation would hold the
                        # duck), then drop the connection. The music
                        # unducks the moment no clip is active.
                        for cid in (self._duck_clip_id, self._reply_clip_id):
                            if cid:
                                try:
                                    await asyncio.wait_for(
                                        sw.cancel_clip(cid), timeout=2.0
                                    )
                                except Exception:
                                    pass  # player gone: clip dies with it
                        try:
                            await asyncio.wait_for(sw.close(), timeout=2.0)
                        except Exception:
                            pass
                        sw = None
                        self._duck_holding = False
                        self._duck_clip_id = None
                        self._reply_clip_id = None
                        logger.info("sonos_output: duck-holder released")
                    continue

                # Awake: ensure the duck is held
                if url is None:
                    if not woke and time.monotonic() < next_clip_try:
                        continue
                    url = await _duck_clip_url()
                    if url is None:
                        continue
                if sw is None:
                    zone_name = self._reply_zone_name()
                    zone = self._zones.get(zone_name)
                    if not zone:
                        continue  # unmapped room or not a Sonos zone: nothing to duck
                    from sonos_websocket import SonosWebsocket

                    try:
                        sw = SonosWebsocket(
                            zone["ip"], player_id=zone["player_uid"]
                        )
                        # Every Sonos call is bounded —
                        # a hung player must never stall the holder.
                        await asyncio.wait_for(sw.connect(), timeout=5.0)
                    except Exception as e:
                        logger.warning(f"sonos_output: duck-holder connect failed: {e}")
                        if sw is not None:
                            try:
                                await asyncio.wait_for(sw.close(), timeout=2.0)
                            except Exception:
                                pass
                        sw = None
                        await asyncio.sleep(2.0)  # retry next tick
                        continue
                    self._duck_holding = True
                    next_refresh = 0.0  # load immediately
                    logger.info(f"sonos_output: duck-holder active on '{zone_name}'")

                if self._reply_loading or time.monotonic() < self._clip_active_until:
                    # Her SPEECH is sounding: it owns the duck (a load
                    # would replace her clip mid-sentence). The reply clip
                    # carries a silence TAIL, so the moment this window
                    # passes, refreshing silence over the tail is
                    # silence-for-silence — a gapless handoff from "she
                    # stopped talking" to "still listening" (kills the
                    # ~1s full-volume blip after every reply).
                    next_refresh = min(next_refresh, self._clip_active_until)
                    continue
                if time.monotonic() >= next_refresh:
                    # (Re)load the silence clip. A loadAudioClip REPLACES
                    # the active clip on the player, so this both starts
                    # and refreshes the duck. Capture the clip id for a
                    # snappy cancel on release.
                    try:
                        resp = await asyncio.wait_for(
                            sw.play_clip(url, volume=self._volume_for(zone_name)), timeout=5.0
                        )
                    except Exception as e:
                        # The websocket may be dead (Sonos reboot, wifi
                        # blip) — close and drop it so the NEXT tick
                        # reconnects instead of retrying on the corpse.
                        logger.warning(f"sonos_output: duck load failed: {e}")
                        try:
                            await asyncio.wait_for(sw.close(), timeout=2.0)
                        except Exception:
                            pass
                        sw = None
                        self._duck_clip_id = None
                        await asyncio.sleep(2.0)
                        continue
                    clip_id = _extract_clip_id(resp)
                    if clip_id:
                        self._duck_clip_id = clip_id
                    if not self._duck_logged_response:
                        logger.info(f"sonos_output: duck load resp: {str(resp)[:140]}")
                        self._duck_logged_response = True
                    next_refresh = time.monotonic() + 24.0
        except asyncio.CancelledError:
            pass
        finally:
            if sw is not None:
                for cid in (self._duck_clip_id, self._reply_clip_id):
                    if cid:
                        try:
                            await asyncio.wait_for(sw.cancel_clip(cid), timeout=2.0)
                        except Exception:
                            pass
                try:
                    await asyncio.wait_for(sw.close(), timeout=2.0)
                except Exception:
                    pass
            self._duck_holding = False

    def _start_duck_holder(self) -> None:
        """Fire the eternal holder loop as a loop task (safe from any
        context). Started once per pipeline at setup; cancelled at
        CancelFrame. Started even while the backend is disabled: adding the
        first zone on the dashboard enables it on running sessions, and the
        loop reads `enabled` live (it never ducks or prepares a clip while
        disabled)."""
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

    def _volume_for(self, zone: str) -> int:
        """Clip volume for `zone`: live override (dashboard slider) >
        config `sonos.zone_volumes` > global `sonos.clip_volume`."""
        live = live_zone_volume(zone)
        if live is not None:
            return live
        if zone in self._zone_volumes:
            return self._zone_volumes[zone]
        return self._volume

    def _barged(self, barge: int | None) -> bool:
        return barge is not None and barge != self._barge_epoch

    async def _speak_to_sonos(self, pcm: bytes, epoch: int | None = None,
                              barge: int | None = None) -> None:
        # Unpadded speech duration: every speech-window timer —
        # echo gate, end-of-speech LED, wake-gate cap — uses this one
        # number; the 8s duck-tail padding below extends the CLIP (and
        # the duck window, which measures it again at load), not the
        # speech window.
        duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
        if epoch is None:
            epoch = self._speech_epoch  # direct call (tests): act as current
        if self._reply_is_builtin():
            # The node's own speaker plays it; its echo gate is owned by
            # NodeSpeakerOutput (anchored at playback start).
            logger.debug("sonos_output: reply zone is the node's built-in speaker, skipping")
            return
        # Zone first: a reply that plays nowhere must not arm the echo gate.
        zone_name = self._reply_zone_name()
        if zone_name is None:  # no speaker mapping: nothing plays
            logger.debug("sonos_output: reply room has no speaker mapping, skipping")
            return
        zone = self._zones.get(zone_name)
        if not zone:
            logger.warning(f"sonos_output: zone '{zone_name}' not in config zones {list(self._zones)}, skipping Sonos playback")
            return
        # Barge-in while this clip was still being built: drop it. Checked
        # again after the encode and before the play call; arming the echo
        # gate for a dead clip would swallow the interrupting question.
        if self._barged(barge):
            logger.info("sonos_output: barge-in before the clip loaded, dropped")
            return
        # The echo gate is armed in _load_clip, right before the play call:
        # a clip that fails on its way to the speaker never gates the mic
        # (the gate is one deadline shared by every clip, so an early arm
        # can't be taken back without cutting another clip's window).
        #
        # Duck-tail: pad 8s of silence onto the clip so
        # the duck outlives her speech — the holder refreshes its own
        # silence over this tail (silence-for-silence, no seam). ONLY when
        # the holder is enabled AND the gate still holds the turn: with
        # ducking off (or the gate already idle), the tail would duck the
        # music ~8s past every reply for nothing. The echo gate uses the
        # unpadded `duration`, so the mic reopens at her last syllable, not +8s.
        gate_state = getattr(self._wake_gate, "state", None)
        state_val = getattr(gate_state, "value", gate_state)
        if self._duck_while_awake and state_val not in (None, "idle"):
            pcm = pcm + b"\x00" * int(2 * self._sample_rate * 8)
        # Fire-and-forget task: a failed step (disk full, a clip server that
        # can't bind) is logged by name and the reply is dropped.
        step = "volume read"
        try:
            volume = self._volume_for(zone_name)
            # MP3, not WAV — the proven-working format for Sonos audioClip
            # (WAV is not in Sonos's documented supported set).
            step = "encoder import"
            import lameenc

            # lameenc encode is CPU-bound and blocks — it
            # runs in a thread. This coroutine IS the voice pipeline's event
            # loop context: a blocking encode stalls audio ingest, OWW
            # scoring and the LEDs for the encode duration every reply.
            def _encode(pcm_bytes):
                encoder = lameenc.Encoder()
                encoder.set_bit_rate(96)
                encoder.set_in_sample_rate(self._sample_rate)
                encoder.set_channels(self._channels)
                encoder.set_quality(2)  # highest quality = slowest encode is fine here
                return bytes(encoder.encode(pcm_bytes)) + bytes(encoder.flush())

            step = "encode"
            mp3 = await asyncio.to_thread(_encode, pcm)
            if self._barged(barge):
                logger.info("sonos_output: barge-in during the encode, clip dropped")
                return

            # Unique across pipelines and restarts: two rooms replying in the
            # same second must never share a name (the atomic write would swap
            # one room's reply under the other zone's fetch).
            step = "clip write"
            filename = f"reply-{int(time.time())}-{uuid.uuid4().hex[:12]}.mp3"
            clip_path = clips_dir() / filename
            # Awaited before the URL exists: the speaker never fetches a
            # clip that isn't on disk yet.
            await asyncio.to_thread(_write_clip, clip_path, mp3)
            step = "clip server bind"
            server = self._clip_server or await shared_clip_server()
            step = "clip URL"
            url = server.url_for(filename)
        except Exception as e:  # noqa: BLE001 — a fire-and-forget task must name what failed
            logger.error(f"sonos_output: reply not played on '{zone_name}': {step} failed: {e}")
            return
        logger.info(f"sonos_output: playing clip {url} on '{zone_name}' at vol {volume}")

        # Loads go one at a time, in order, and a clip whose generation is
        # no longer current is dropped: a filler whose connect was slow must
        # never load AFTER the reply and replace it (a load replaces the
        # active clip).
        if self._play_lock is None:
            self._play_lock = asyncio.Lock()
        async with self._play_lock:
            await self._load_clip(zone, zone_name, url, volume, duration, epoch, barge)

    def _stale(self, epoch: int) -> bool:
        return epoch != self._speech_epoch

    async def _load_clip(self, zone, zone_name, url, volume, duration, epoch, barge) -> None:
        if self._stale(epoch):
            logger.info("sonos_output: a newer reply is already queued, older clip dropped")
            return
        try:
            from sonos_websocket import SonosWebsocket

            sw = SonosWebsocket(zone["ip"], player_id=zone["player_uid"])
        except Exception as e:  # noqa: BLE001 — a fire-and-forget task must name what failed
            logger.error(f"sonos_output: reply not played on '{zone_name}': speaker client failed: {e}")
            return
        # From connect until the load returns this reply owns the player: the
        # duck holder skips its silence loads (one racing in replaced the
        # reply on long tool turns that started from IDLE).
        self._reply_loading += 1
        try:
            await asyncio.wait_for(sw.connect(), timeout=5.0)  # bounded
            if self._barged(barge):
                logger.info("sonos_output: barge-in before play, clip dropped")
                return
            if self._stale(epoch):
                logger.info("sonos_output: a newer reply arrived during connect, older clip dropped")
                return
            # Block echo handling for as long as this clip will sound (the mic
            # hears it off the zone speakers, no AEC by design). Armed before
            # the play call, with no await in between: the speaker can't sound
            # before it. If the play call then fails, the player may already be
            # sounding it, so the block stands.
            if self._echo_gate is not None:
                self._echo_gate.block(duration + 1.5)  # + Sonos load (~200ms) + tail margin
            resp = await asyncio.wait_for(
                sw.play_clip(url, volume=volume), timeout=5.0
            )
            if self._barged(barge):
                # The barge-in landed while the clip was loading: kill it now,
                # and leave no timers, clip id or echo block behind.
                cid = _extract_clip_id(resp)
                if cid:
                    await asyncio.wait_for(sw.cancel_clip(cid), timeout=5.0)
                logger.info(f"sonos_output: barge-in during load, cancelled clip {cid}")
                return
            # ── LOAD-ANCHORED TIMERS ──
            # Generation-anchored timers fired early on the v4 dialogue
            # lane (measured a 3.4s gap from generation to load):
            # end-of-speech LED flipped the ring to listening, the echo
            # gate reopened the mic, and note_speech capped the gate's
            # LOCKED safety release — all BEFORE her last syllable. The
            # clip is sounding NOW, so every speech-window timer anchors
            # HERE. `duration` (method top) is the UNPADDED speech time.
            # Stale-task guard: only re-anchor if this task's generation
            # is still the current one (a filler can race a reply; the
            # newer generation wins).
            if epoch == self._speech_epoch:
                if self._led:
                    self._schedule_end_of_speech(duration + 0.5)  # load ~0.2s already spent; +margin
                if self._echo_gate is not None:
                    self._echo_gate.block(duration + 1.5)  # max-extend: covers both generations
                if self._wake_gate is not None:
                    try:
                        self._wake_gate.note_speech(duration)
                    except Exception:
                        pass  # gate hint must never break the reply
            # Duck ownership starts HERE — the clip is actually sounding on
            # the player now. It covers her SPEECH only (`duration`, unpadded):
            # the holder's next refresh then lands inside the 8 s silence
            # tail, silence-on-silence. Timing it on the padded clip let the
            # tail run out first and the music swell before the refresh.
            # +1.0 s covers the player's start lag; the tail is 8 s of silence.
            self._clip_active_until = time.monotonic() + duration + 1.0
            self._reply_clip_id = _extract_clip_id(resp)
        except Exception as e:
            logger.warning(f"sonos_output: play failed on {zone_name}: {e} — generation-anchored fallback timers stand")
        finally:
            self._reply_loading -= 1
            try:
                await asyncio.wait_for(sw.close(), timeout=2.0)
            except Exception:
                pass

    # ── Wake-word barge-in ─────────────────────────────────────────────

    async def stop_current_clip(self) -> None:
        """Kill the sounding reply clip (wake-word barge-in). Idempotent:
        no clip, nothing to do. Sonos audioClip API: cancel by clipId —
        the duck-holder's proven path. The clip is replaced by SILENCE,
        not stopped dead, so the duck never pops."""
        cid = self._reply_clip_id
        self._reply_clip_id = None
        self._speech_epoch += 1  # stale in-flight speak tasks can't re-anchor
        self._barge_epoch += 1  # ...and in-flight ones drop their clip
        self._cancel_end_of_speech()
        self._clip_active_until = 0.0
        # Echo-gate rider: the clip is dead — its duration gate must
        # not eat the interrupting question. relax() never extends a
        # lapsed gate, and text containment stays armed.
        if self._echo_gate is not None:
            try:
                self._echo_gate.relax()
            except Exception:
                pass
        if not cid:
            return
        zone = self._zones.get(self._reply_zone_name())
        if not zone:
            return
        sw = None
        try:
            from sonos_websocket import SonosWebsocket
            sw = SonosWebsocket(zone["ip"], player_id=zone["player_uid"])
            await asyncio.wait_for(sw.connect(), timeout=5.0)
            await asyncio.wait_for(sw.cancel_clip(cid), timeout=5.0)
            logger.info(f"sonos_output: barge-in cancelled reply clip {cid}")
        except Exception as e:
            logger.warning(f"sonos_output: barge-in cancel failed: {e}")
        finally:
            if sw is not None:
                try:
                    await asyncio.wait_for(sw.close(), timeout=2.0)
                except Exception:
                    pass

    def _reply_owned_elsewhere(self) -> bool:
        try:
            return bool(self._reply_elsewhere and self._reply_elsewhere())
        except Exception:  # noqa: BLE001 — a routing hint must never break the reply
            return False

    def _reply_zone_name(self):
        """The reply's zone from the room mapping, or None. None means the
        room has no speaker: the reply is not played and nothing is ducked
        (default_zone is not a fallback)."""
        if not self._zone_selector:
            return None
        try:
            return self._zone_selector() or None
        except Exception as e:  # noqa: BLE001 — a routing hint must never break the reply
            logger.debug(f"sonos_output: zone_selector failed: {e}")
            return None

    def _room_label(self) -> str:
        try:
            room = self._room_selector() if self._room_selector else None
        except Exception:  # noqa: BLE001 — a log label must never break the reply
            room = None
        return room or "unknown"

    def _reply_is_builtin(self) -> bool:
        """This reply plays on the node's own speaker (zone `builtin`)."""
        if not self._zone_selector:
            return False
        try:
            return self._zone_selector() == BUILTIN
        except Exception:  # noqa: BLE001 — a routing hint must never break the reply
            return False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            self._pcm.clear()
            # Provider switches can change the audio rate between clips
            # (ElevenLabs 24k ↔ Deepgram 16k): reset to the constructor
            # default so a new clip never inherits the previous provider's
            # rate; the first TTSAudioRawFrame overwrites with the real one.
            self._sample_rate = 24000
            if self._led:  # speech starts sounding within ~0.5s — light now
                self._cancel_end_of_speech()
                self._led.send("bot-started-speaking")
        elif isinstance(frame, TTSAudioRawFrame):
            self._pcm.extend(frame.audio)
            self._sample_rate = frame.sample_rate or self._sample_rate
            self._channels = frame.num_channels or self._channels
        elif isinstance(frame, TTSStoppedFrame):
            if self._reply_owned_elsewhere():
                self._pcm.clear()  # a WiiM zone sounds it; WiiMOutput owns the timers
            elif len(self._pcm) > 0 and self._reply_zone_name() is None:
                # No speaker mapping for this room (or its built-in speaker
                # isn't confirmed yet): no backend plays the reply. Nothing
                # sounds, so the speaking LED ends now and the echo gate is
                # never armed; the wake gate follows the transport's own
                # speaking frames, which are untouched.
                self._pcm.clear()
                logger.info(f"sonos_output: room '{self._room_label()}' has no speaker mapping"
                            f" - reply not played (a built-in speaker plays once its node confirms it)")
                if self._led:
                    self._schedule_end_of_speech(0.0)
            elif self._enabled and len(self._pcm) > 0:
                pcm = bytes(self._pcm)
                self._pcm.clear()
                # Timers anchor at CLIP-LOAD time, set
                # inside _speak_to_sonos when play_clip returns. This
                # generation-time schedule is the FALLBACK: it only stands
                # if the speak task dies before loading (encode/zone/write
                # failure), so the ring never strands cyan forever. The
                # epoch handoff lets the load site drop stale re-anchors:
                # a filler racing a reply can't stomp the newer turn.
                self._speech_epoch += 1
                gen = self._speech_epoch
                if self._led:
                    # TTSStopped = GENERATION end, not playback end. ElevenLabs
                    # bursts the whole clip through the pipeline in <1s while
                    # the Sonos sounds it for `duration` seconds (measured:
                    # cyan flashed 0.6s, then purple through
                    # 18s of actual speech). Schedule the end-of-speaking LED
                    # for when the audio stops SOUNDING, not when frames stop.
                    duration = len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
                    self._schedule_end_of_speech(duration + 0.5)  # Sonos load ~0.2 + tail
                # The wake gate's LOCKED safety cap tracks the REAL
                # speech window (not its 30s fallback).
                if self._wake_gate is not None:
                    try:
                        self._wake_gate.note_speech(
                            len(pcm) / (2 * self._sample_rate * max(self._channels, 1))
                        )
                    except Exception:
                        pass  # gate hint must never break the reply
                # Duck ownership: starts when the clip
                # actually LOADS on the player (in _speak_to_sonos), not
                # here at generation end — the encode/write/connect/load
                # chain can exceed any margin set this early. The load
                # site re-anchors all speech timers.
                # Fire-and-forget: never block the pipeline on Sonos I/O
                self.create_task(self._speak_to_sonos(pcm, epoch=gen, barge=self._barge_epoch))
            elif len(self._pcm) > 0 and self._reply_is_builtin():
                # Sonos disabled, but the node's own speaker sounds this
                # reply (a speaker-only install): time the LEDs and the
                # wake gate to the speech, exactly as for a Sonos clip.
                duration = len(self._pcm) / (2 * self._sample_rate * max(self._channels, 1))
                self._pcm.clear()
                if self._led:
                    self._schedule_end_of_speech(duration + 0.5)
                if self._wake_gate is not None:
                    try:
                        self._wake_gate.note_speech(duration)
                    except Exception as e:  # noqa: BLE001 — gate hint must never break the reply
                        logger.debug(f"sonos_output: note_speech failed: {e}")
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
        processors — can't be stalled by a blocked intelligence."""
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
        # gate already timed out mid-intelligence-call.
        gate_state = getattr(self._wake_gate, "state", None)
        state_val = getattr(gate_state, "value", gate_state)
        self._led.send("bot-stopped-speaking"
                       if state_val not in (None, "idle")
                       else "oww-sleeping")