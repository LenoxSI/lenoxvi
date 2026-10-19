"""lenoxvi house-voice bot: wake gate -> VAD -> whisper STT -> intelligence -> TTS.

Pipeline (Phase 4): WebRTC in -> wake gate (openWakeWord) ->
Silero VAD -> local faster-whisper STT -> intelligence (IntelligenceAgent via config.yaml
intelligence.kind, production = hermes; stub = bench fallback) -> ElevenLabs TTS
(voice from .env LVI_TTS_VOICE_ID) -> Sonos audioClip in the room's zone.

Run on the appliance host from the repo root:
  ~/venvs/lenoxvi/bin/python bot.py
Then open https://<bot-host-LAN-IP>:7860/client in a LAN browser
(mic access needs the HTTPS secure context; certs via scripts/make-cert.sh).

Requires .env (see .env.example) with all three keys:
  ELEVENLABS_API_KEY, LVI_TTS_VOICE_ID, LVI_HERMES_KEY.
"""

import asyncio
import os
import sys
import threading
import time

from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))

# Load .env (secrets live only on the host, gitignored) before any env reads.
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from intelligence_stub import StubIntelligence
from echo_gate import EchoGate
from intelligence_agent import IntelligenceAgent
from sonos_output import SonosOutput
from wiim_output import WiiMOutput
from stt_local import LocalWhisperSTTService
from wake_word import WakeWordFilter

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
# PipelineTask/PipelineParams (pipecat.pipeline.task) are deprecated aliases,
# removed in pipecat 2.0 — import the real names from pipeline.worker.
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.runner.utils import create_transport
from pipecat.runner.types import RunnerArguments
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
try:
    from pipecat.services.elevenlabs.dialogue.tts import ElevenLabsDialogueTTSService
except ImportError:  # older pipecat without the Text-to-Dialogue service
    ElevenLabsDialogueTTSService = None
try:
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from pipecat.services.deepgram.flux.tts import DeepgramFluxTTSService
    _DEEPGRAM_OK = True
except ImportError:  # older pipecat without the Deepgram services
    DeepgramTTSService = None
    DeepgramFluxTTSService = None
    _DEEPGRAM_OK = False
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport


class TTSTracked:
    """Mixin: relay pipecat's set_usable() verdicts to the dashboard
    registry for this pipeline's room (a node once went silent this
    way). An ElevenLabs websocket death makes pipecat mark the
    service PERMANENTLY unusable after 3 failed reconnects — the node
    keeps hearing, replies silently vanish, and the only clue sat in
    journald. Fail-open by design: observability must never take down
    the voice pipeline.

    Reconnect-on-next-utterance (follow-on fix): an unusable
    websocket service refuses every speak, so its own run_tts reconnect
    never fires and the room stays mute until a node/bot restart.
    _push_tts_frames gives a dead service one bounded websocket retry AT
    the next speak — only a socket that actually opens clears the
    verdict, and the health note below carries the revival to the
    dashboard chip."""

    _tts_room: str | None = None
    _tts_provider: str = "elevenlabs"
    _revive_last_attempt: float = 0.0

    def bind_tts_room(self, room: str | None) -> None:
        self._tts_room = room
        try:
            from dashboard import registry
            registry.note_tts(room, self._tts_provider, True)
        except Exception:
            logger.opt(exception=True).warning("tts health: initial note failed")

    async def set_usable(self, is_usable: bool):
        await super().set_usable(is_usable)
        # While a Pocket fallback is speaking for this room, the switcher owns
        # the chip; a recovery probe's verdict must not repaint it early.
        if getattr(self, "_tts_note_suppressed", False):
            return
        try:
            from dashboard import registry
            registry.note_tts(self._tts_room, self._tts_provider, is_usable)
        except Exception:
            logger.opt(exception=True).warning("tts health: note failed")

    async def _try_revive(self) -> None:
        """One bounded websocket retry for a service pipecat wrote off.

        Called from _push_tts_frames when a speak arrives while the
        service is unusable. The retry is the same _connect() run_tts
        would have done, hoisted above the usability gate — with a
        _disconnect() first, because the death spiral leaves finished
        receive/keepalive tasks that a bare _connect() would trust as
        alive. A cooldown (config tts.revive_cooldown_secs) keeps a live
        outage from taxing every utterance with a connect timeout.
        Never raises: a failed revive just leaves the service dead and
        the speak falls through to pipecat's silent skip."""
        try:
            from config import cfg_env
            cooldown = float(cfg_env("tts", "revive_cooldown_secs",
                                      "LVI_TTS_REVIVE_COOLDOWN", 30) or 30)
        except Exception:
            cooldown = 30.0
        now = time.monotonic()
        if now - self._revive_last_attempt < cooldown:
            return
        self._revive_last_attempt = now
        logger.warning(f"tts revive: {self._tts_provider} TTS is marked unusable — "
                       "retrying the websocket")
        try:
            await self._disconnect()
            await self._connect()
            if self._websocket is not None:
                await self.set_usable(True)
                logger.info(f"tts revive: {self._tts_provider} websocket is back — "
                            "service usable again")
        except Exception:
            logger.opt(exception=True).warning("tts revive: attempt failed "
                                               "(staying unusable)")

    async def _push_tts_frames(self, src_frame, *args, **kwargs):
        # Every speak funnels through here (all four of TTSService's call
        # sites). A dead service normally skips silently inside pipecat's
        # usability gate; the revive gives it one bounded shot at the
        # websocket first. Fail-open: _try_revive never raises, so a
        # revive bug can never eat the speak path.
        if not self.is_usable:
            await self._try_revive()
        return await super()._push_tts_frames(src_frame, *args, **kwargs)


class TrackedElevenLabsTTSService(TTSTracked, ElevenLabsTTSService):
    _tts_provider = "elevenlabs"


if ElevenLabsDialogueTTSService is not None:
    class TrackedElevenLabsDialogueTTSService(TTSTracked, ElevenLabsDialogueTTSService):
        _tts_provider = "elevenlabs"
else:
    TrackedElevenLabsDialogueTTSService = None

if DeepgramTTSService is not None:
    class TrackedDeepgramTTSService(TTSTracked, DeepgramTTSService):
        _tts_provider = "deepgram"
else:
    TrackedDeepgramTTSService = None

if DeepgramFluxTTSService is not None:
    class TrackedDeepgramFluxTTSService(TTSTracked, DeepgramFluxTTSService):
        _tts_provider = "deepgram"
else:
    TrackedDeepgramFluxTTSService = None


async def run_bot(transport: SmallWebRTCTransport, runner_args: RunnerArguments):
    from config import cfg, cfg_env

    # Secrets are checked ONCE at
    # pipeline construction with a clear error naming the provider and
    # the .env variable — not a bare KeyError per TTS request. Same
    # fail-loud law as the intelligence key: construction survives,
    # the missing secret says its name.
    tts_provider = str(cfg_env("tts", "provider", "LVI_TTS_PROVIDER", "elevenlabs") or "elevenlabs")
    _need_secret = ("ELEVENLABS_API_KEY" if tts_provider.startswith("eleven")
                    else "DEEPGRAM_API_KEY" if tts_provider.startswith("deepgram")
                    else None)
    if _need_secret and not os.environ.get(_need_secret):
        raise RuntimeError(
            f"tts.provider is {tts_provider!r} but {_need_secret} is not set — "
            f"add it to .env (see .env.example). No TTS can be constructed.")

    stt = LocalWhisperSTTService(
        model=cfg_env("stt", "model", "LVI_STT_MODEL", "large-v3-turbo"),
        device=cfg_env("stt", "device", "LVI_STT_DEVICE", "cuda"),
        compute_type=cfg_env("stt", "compute_type", "LVI_STT_COMPUTE", "float16"),
        language=cfg_env("stt", "language", "LVI_STT_LANGUAGE") or None,
    )
    # stt.provider: whisper (local, default) or a cloud provider with this
    # Whisper service as its fallback (wired below, once the task exists).
    stt_provider = str(cfg_env("stt", "provider", "LVI_STT_PROVIDER", "whisper") or "whisper").strip().lower()
    # TTS provider dispatch: tts.provider
    # selects the engine; each provider's knobs live under its own config
    # key. ElevenLabs stays the default (a config without tts.provider —
    # the fresh-clone state — must land on a provider that speaks).
    tts_provider = cfg_env("tts", "provider", "LVI_TTS_PROVIDER", "elevenlabs")
    if tts_provider == "pocket":
        # Pocket TTS (Kyutai, CPU-native, fully local: no key, no egress).
        # One model per process, preloaded at startup by tts_pocket.
        import tts_pocket
        if not tts_pocket.pocket_available():
            raise RuntimeError("tts.provider=pocket but pocket-tts is not installed — "
                               f"run: {tts_pocket.INSTALL_HINT}")
        tts = tts_pocket.make_service(
            mixin=TTSTracked,
            voice=cfg_env("tts", "pocket_voice", "LVI_TTS_POCKET_VOICE", "alba"))
        if tts is None:
            raise RuntimeError("tts.provider=pocket but the Pocket model failed to "
                               f"load — run: {tts_pocket.INSTALL_HINT}")
        logger.info("tts backend: pocket (local)")
    elif tts_provider == "deepgram":
        # Deepgram Aura-2: model name IS the voice (aura-2-thalia-en shape).
        # Verified against developers.deepgram.com/docs/tts-models. Key is
        # env-side forever (secrets never ride config.yaml).
        if DeepgramTTSService is None:
            raise RuntimeError("tts.provider=deepgram but this pipecat has no "
                               "pipecat.services.deepgram.tts — upgrade pipecat "
                               "or set tts.provider: elevenlabs")
        tts = TrackedDeepgramTTSService(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            voice=cfg_env("tts", "deepgram_voice", "LVI_TTS_VOICE",
                          "aura-2-thalia-en"),
        )
        logger.info("tts backend: deepgram aura-2")
    elif tts_provider == "deepgram-flux":
        # Deepgram Flux (preferred over Aura for voice quality) — the voice-agent-first
        # surface: conversation-aware, cross-turn voice consistency, /v2/speak
        # streaming. Same DEEPGRAM_API_KEY as Aura (one account, two model
        # families). Voice format flux-<name>-en; catalog at
        # developers.deepgram.com/docs/flux-tts/voices.
        if DeepgramFluxTTSService is None:
            raise RuntimeError("tts.provider=deepgram-flux but this pipecat "
                               "has no pipecat.services.deepgram.flux.tts — "
                               "upgrade pipecat or set tts.provider: elevenlabs")
        tts = TrackedDeepgramFluxTTSService(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            settings=DeepgramFluxTTSService.Settings(
                voice=cfg_env("tts", "deepgram_flux_voice", "LVI_TTS_FLUX_VOICE",
                              "flux-hannah-en"),
                # Speed: 0.5–1.5 in 0.05 steps (None = Flux default).
                # Expressivity: -2..2 calm→animated (None = default; fixed
                # per connection). Both optional — absent config = defaults.
                speed=(lambda v: float(v) if v not in (None, "") else None)(
                    cfg("tts", "deepgram_flux_speed")),
                expressivity=(lambda v: int(v) if v not in (None, "") else None)(
                    cfg("tts", "deepgram_flux_expressivity")),
            ),
        )
        logger.info("tts backend: deepgram flux")
    else:
        # ElevenLabs. Config precedence per house law: voice_id lives in
        # config.yaml (dashboard-editable) with the legacy LVI_TTS_VOICE_ID
        # env as fallback; model in config.yaml; api_key stays env-side
        # forever (secrets never ride config.yaml — the .env is gitignored,
        # config.yaml is not).
        # ONE provider, TWO lanes: the MODEL picks the lane —
        # eleven_v3*/eleven_v4* models ride the Text-to-Dialogue websocket
        # (the only lane that reaches them; the Realtime TTS WS rejects
        # them with HTTP 400 — commit fb0cf94 for v3, ElevenLabs docs for
        # v4); flash/v2 ride the Realtime TTS WS. The user just picks a
        # model; the lane is an implementation detail.
        el_model = cfg_env("tts", "model", "LVI_TTS_MODEL", "eleven_flash_v2_5")
        if el_model.startswith(("eleven_v3", "eleven_v4")):
            if ElevenLabsDialogueTTSService is None:
                raise RuntimeError(
                    f"tts.model {el_model!r} rides the Text-to-Dialogue "
                    "lane, which needs pipecat's ElevenLabsDialogueTTSService "
                    "(pipecat >= 1.10)")
            tts = TrackedElevenLabsDialogueTTSService(
                api_key=os.environ["ELEVENLABS_API_KEY"],
                settings=ElevenLabsDialogueTTSService.Settings(
                    model=el_model,
                    voice=cfg_env("tts", "voice_id", "LVI_TTS_VOICE_ID"),
                ),
            )
            logger.info(f"tts backend: elevenlabs {el_model} (text-to-dialogue)")
        else:
            tts = TrackedElevenLabsTTSService(
                api_key=os.environ["ELEVENLABS_API_KEY"],
                voice_id=cfg_env("tts", "voice_id", "LVI_TTS_VOICE_ID"),
                model=el_model,
                # Flash v2.5 voice-shaping knobs:
                # speed 0.7–1.2, stability 0.0–1.0 (lower = expressive,
                # higher = consistent). Absent config = None = ElevenLabs
                # default — never a made-up value. The v3/v4 dialogue lane
                # deliberately does NOT get these (ElevenLabs guidance: the
                # model drives expressivity there).
                settings=ElevenLabsTTSService.Settings(
                    speed=(lambda v: float(v) if v not in (None, "") else None)(
                        cfg("tts", "elevenlabs_speed")),
                    stability=(lambda v: float(v) if v not in (None, "") else None)(
                        cfg("tts", "elevenlabs_stability")),
                ),
            )
            logger.info(f"tts backend: elevenlabs {el_model}")
    # Hot standby (tts.fallback: pocket): the primary gets wrapped in
    # pipecat's failover switcher just before the pipeline is built. No
    # fallback key = today's pipeline, unchanged.
    tts_primary = tts
    _fallback = str(cfg("tts", "fallback") or "").strip().lower()
    if _fallback == "pocket" and tts_provider == "pocket":
        import tts_pocket
        tts_pocket.warn_once("fallback-ignored",
                             "tts.fallback: pocket ignored — Pocket is already the primary")
        _fallback = ""
    tts_pocket_standby = None
    if _fallback == "pocket":
        import tts_pocket
        tts_pocket_standby = (tts_pocket.make_service(
            mixin=TTSTracked,
            voice=cfg_env("tts", "pocket_voice", "LVI_TTS_POCKET_VOICE", "alba"))
            if tts_pocket.pocket_available() else None)
        if tts_pocket_standby is None:
            tts_pocket.warn_once("fallback-unavailable",
                                 "tts.fallback: pocket requested but Pocket can't load — "
                                 f"running WITHOUT a fallback ({tts_pocket.INSTALL_HINT})")
    gate = EchoGate()
    # Multi-node fix: EVERYTHING room-related resolves
    # through THIS pipeline's own pc_id — never the registry's global
    # "last connected" room. The offer tee has already correlated
    # {room, pc_id} by the time the pipeline runs (send-time tee), so
    # room_for_pc is exact here.
    # pc_id SOURCE FIX (measured 2026-10-03): SmallWebRTCTransport is a
    # facade — it holds ._client, not ._webrtc_connection, so the
    # original attribute walk always returned None and every wake since
    # 10-01 recorded room=null. The runner args carry the connection as
    # a public field; the facade walk stays as a defensive fallback.
    pc_id = getattr(getattr(runner_args, "webrtc_connection", None), "pc_id", None)
    if pc_id is None:
        pc_id = getattr(
            getattr(getattr(transport, "_client", None), "_webrtc_connection", None),
            "pc_id",
            None,
        )
    from dashboard import registry as dash_registry
    dash_registry.set_gate(gate)

    def _pipeline_room():
        # This pipeline's room — fall back to None (-> default zone) if
        # the tee hasn't correlated yet (ordering race, handled).
        return dash_registry.room_for_pc(pc_id) if pc_id else None

    def _reply_zone():
        room = _pipeline_room()
        if not room or room == "unknown":
            return None
        # mapped zone, else a speaker node's own speaker ("builtin"),
        # else None -> default zone fallback in the output backend.
        import node_speaker
        return node_speaker.resolve_zone(room, node_speaker._room_zones(),
                                         node_speaker._has_speaker(room))

    def _on_wake():
        dash_registry.note_wake(_pipeline_room(), heard_key=_room)

    # Session mode for THIS pipeline's room (fresh default; sticky rooms
    # keep conversation memory across reconnects/restarts — config
    # `session_modes`, dashboard checkbox 2026-10-03).
    _room = _pipeline_room()
    _session_mode = (cfg("session_modes") or {}).get(_room, "fresh")

    # TTS health tap: key this pipeline's TTS
    # service to its room so pipecat's usability verdicts land on the
    # dashboard — note_tts(True) here, then every set_usable().
    if isinstance(tts_primary, TTSTracked):
        tts_primary.bind_tts_room(_room)
    if tts_pocket_standby is not None:
        # Quiet bind: no initial "usable" note (that would overwrite the
        # primary's chip); it reports only if it dies while speaking.
        tts_pocket_standby._tts_room = _room

    # Intelligence dispatch: hermes AND openclaw both ride
    # IntelligenceAgent — the kind branch lives inside _ask_intelligence (envelope
    # only). A past bug: this ternary only
    # matched == "hermes", so kind: openclaw fell to the echo stub and
    # the operator got canned answers while the suite stayed green (9b11 tested
    # the agent directly, never the construction site).
    # Dead-TTS containment wire: the tracked
    # TTS service's is_usable rides into the agent so texts pushed to a
    # DEAD service are never armed as echo texts — they made no sound,
    # cannot echo back, and their words were eating real follow-ups.
    # Reads `tts` late: with a fallback armed that's the switcher, whose
    # is_usable means "some service can still speak".
    tts_usable_probe = ((lambda: getattr(tts, "is_usable", True))
                        if isinstance(tts_primary, TTSTracked) else None)
    intelligence = (IntelligenceAgent(echo_gate=gate, room=_room, session_mode=_session_mode,
                                tts_is_usable=tts_usable_probe)
             if cfg_env("intelligence", "kind", "LVI_INTELLIGENCE_KIND", "hermes") in ("hermes", "openclaw")
             else StubIntelligence(echo_gate=gate))

    # VAD tunables from config.yaml (vad: section) — SileroVADAnalyzer takes
    # VADParams directly; defaults match the old hardcodes.
    vad_cfg = cfg("vad") or {}
    vad_params = {}
    for k in ("confidence", "start_secs", "stop_secs", "min_volume"):
        if k in vad_cfg:
            vad_params[k] = float(vad_cfg[k])
    from pipecat.audio.vad.vad_analyzer import VADParams
    vad_analyzer = (SileroVADAnalyzer(params=VADParams(**vad_params)) if vad_params
                    else SileroVADAnalyzer())
    if vad_params:
        logger.info(f"vad params from config.yaml: {vad_params}")

    # LED ring goes on a DIRECT wire (led_state.py): pipeline frames stall
    # behind a blocked processor — IntelligenceAgent holds the queue 3-40s on
    # intelligence calls and every LED message used to arrive as one late burst
    # (measured 2026-09-30: the ring held stale purple through the whole
    # "On it." -> answer gap). One output transport, shared by the pipeline
    # and the LED wire; per-pipeline LedSender = per-node targeting free.
    from led_state import LedSender

    out = transport.output()
    led = LedSender(out)
    # Barge-in: the interrupt callback stops whichever
    # output is sounding. The outputs are built in the pipeline list below,
    # so the closure reaches them through this holder — set right after
    # construction, before any audio can flow.
    _outputs: list = []

    async def _on_interrupt() -> None:
        for o in _outputs:
            try:
                await o.stop_current_clip()
            except Exception:
                pass  # one backend's failure never blocks the other

    # Wake gate built once: the pipeline gets it conditionally, and
    # SonosOutput reads its state for the end-of-speech LED (purple vs
    # dark) and fires speaking LEDs from its real-time TTS frames.
    wake_filter = (WakeWordFilter(echo_gate=gate, on_wake=_on_wake, led=led,
                                  on_interrupt=_on_interrupt)
                   if cfg_env("wake", "enabled", "LVI_WAKE_WORD", "1") in (True, 1, "1", "true")
                   else None)

    # Barge-in holder fill: the interrupt callback's
    # closure reaches the backends through _outputs — set BEFORE the
    # pipeline task starts so the first interrupt can't race a None.
    sonos_out = SonosOutput(echo_gate=gate, zone_selector=_reply_zone,
                            wake_gate=wake_filter, led=led)
    wiim_out = WiiMOutput(echo_gate=gate, zone_selector=_reply_zone,
                          wake_gate=wake_filter, led=led)
    _outputs.extend([sonos_out, wiim_out])
    # echo gate for replies on a node's built-in speaker (the audio
    # itself already rides transport.output() to the node).
    from node_speaker_output import NodeSpeakerOutput
    node_spk_out = NodeSpeakerOutput(echo_gate=gate, zone_selector=_reply_zone)

    # Arm the hot standby: ServiceSwitcher([primary, pocket], Failover).
    # The recovery loop queues its switch-back frame through the task,
    # which doesn't exist yet: late-bound via _task_ref.
    _task_ref = {}
    if tts_pocket_standby is not None:
        from tts_fallback import wrap_with_fallback
        try:
            _cool = float(cfg_env("tts", "revive_cooldown_secs",
                                  "LVI_TTS_REVIVE_COOLDOWN", 30) or 30)
        except (TypeError, ValueError):
            _cool = 30.0

        async def _queue(frame):
            await _task_ref["task"].queue_frame(frame)

        tts = wrap_with_fallback(tts_primary, tts_pocket_standby, room=_room,
                                 queue_frame=_queue, cooldown=_cool)
        logger.info(f"tts fallback: pocket armed behind {tts_provider}")

    # Cloud STT: ServiceSwitcher([deepgram, whisper], Failover). Deepgram
    # marks itself down on a bad key / dead network and the session moves to
    # local Whisper; stt_fallback switches back when it answers again. A
    # missing key or SDK = Whisper with a warning, never a dead pipeline.
    import stt_catalog
    import stt_fallback
    import stt_local
    if stt_catalog.validate_provider(stt_provider):
        logger.warning(f"stt: unknown provider {stt_provider!r} — using local Whisper")
        stt_provider = "whisper"
    stt_fallback.set_provider(stt_provider)
    stt_timer = None
    if stt_provider == "deepgram":
        stt_local.set_latency_mode("e2e")
        stt_timer = stt_fallback.SttTimer()
        _dg, _dg_why = stt_fallback.make_deepgram(cfg_env("stt", "language", "LVI_STT_LANGUAGE"))
        stt_fallback.note_unavailable(_dg_why)
        if _dg is None:
            logger.warning(f"stt: provider deepgram unavailable ({_dg_why}) — local Whisper is listening")
        else:
            try:
                _stt_cool = float(cfg_env("stt", "revive_cooldown_secs", "LVI_STT_REVIVE_COOLDOWN", 30) or 30)
            except (TypeError, ValueError):
                _stt_cool = 30.0

            async def _stt_queue(frame):
                await _task_ref["task"].queue_frame(frame)

            try:
                stt = stt_fallback.wrap_stt_with_fallback(_dg, stt, room=_room, queue_frame=_stt_queue,
                                                          cooldown=_stt_cool)
                logger.info("stt: deepgram nova-3 (local Whisper standby)")
            except Exception as e:  # noqa: BLE001 — Whisper, not a dead pipeline
                stt_fallback.note_unavailable(f"Deepgram STT could not be wired ({type(e).__name__})")
                logger.warning(f"stt: deepgram wiring failed ({type(e).__name__}) — local Whisper is listening")
    else:
        stt_local.set_latency_mode("compute")

    task = PipelineWorker(
        Pipeline(
            [
                transport.input(),
                # wake gate AFTER the source (frames flow downstream from
                # transport.input()), BEFORE VAD/STT: gates audio in IDLE/LOCKED
                *([wake_filter] if wake_filter else []),
                VADProcessor(vad_analyzer=vad_analyzer),
                stt,
                *([stt_timer] if stt_timer else []),
                intelligence,
                tts,
                # Per-zone backend dispatch (2026-10-04): BOTH output
                # backends live in the pipeline; each reply is claimed by
                # whichever backend owns the zone at SPEECH time (the
                # other skips it — the same zone-ownership check that
                # always ran at reply time). The old build-time ternary
                # picked ONE backend when the pipeline was constructed,
                # but the offer tee correlates the room in the same
                # tick — a lost race there put a WiiM room on a
                # Sonos-only pipeline and every reply died silently
                # (a real bedroom outage). Building
                # both removes the race: no build-time room knowledge
                # is needed at all.
                sonos_out,
                wiim_out,
                node_spk_out,
                out,
            ]
        ),
        params=PipelineParams(
            allow_interruptions=False,  # no pipecat barge-in — ours is the wake gate
        ),
        # Always-on ESP32 node: a quiet house is NOT a dead session. This is a
        # PipelineTask kwarg (NOT a PipelineParams field — putting it there
        # silently did nothing and the 300s worker default killed every quiet
        # session at exactly 5 min). None = never idle-cancel; the node's STUN
        # keepalive + on_client_disconnected handle real disconnects.
        idle_timeout_secs=None,
    )
    _task_ref["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # client = SmallWebRTCConnection (pipecat smallwebrtc): tie it to
        # the room recorded by the offer tee; the dashboard registry
        # turns "a client connected" into "the bedroom node is online".
        from dashboard import registry
        registry.mark_connected(client.pc_id)
        # Register the OUTPUT TRANSPORT (direct data-channel wire),
        # not the pipeline task — node commands must not queue behind a
        # blocked intelligence. Same wire LedSender uses.
        registry.register_sender(client.pc_id, out)
        logger.info("client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        from dashboard import registry
        registry.mark_disconnected(client.pc_id)
        registry.drop_sender(client.pc_id)
        logger.info("client disconnected")
        if tts_pocket_standby is not None:
            # Stop recovery BEFORE teardown: a probe firing between the
            # cancel and the finally would open a socket on a dead session.
            from tts_fallback import close_fallback
            close_fallback(tts)
        if getattr(stt, "_lenoxvi_primary", None) is not None:
            stt_fallback.close_stt_fallback(stt)
        await task.cancel()

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        # Dashboard command replies + liveness probes (firmware 1.1.0).
        # NOTE: the smallwebrtc transport registers "on_app_message" —
        # NOT "on_client_message" (that name is the RTVI processor's;
        # using it silently never fires, measured 2026-09-29).
        # Signature is (message, sender) where sender IS the pc_id
        # string (transport.py:578 passes connection.pc_id directly) —
        # not a connection object.
        from dashboard import node_command
        node_command.on_message(message, sender)

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        if tts_pocket_standby is not None:
            from tts_fallback import close_fallback
            close_fallback(tts)
        if getattr(stt, "_lenoxvi_primary", None) is not None:
            stt_fallback.close_stt_fallback(stt)
        _reclaim_session()


def _reclaim_session() -> None:
    """Reclaim a finished session's pipeline graph NOW (per-session leak fix).

    Every session builds a fresh pipeline (wake model, Silero VAD, resamplers,
    WebRTC transport) that forms one big reference cycle, so refcounting can't
    free it — only the cyclic GC can. Python 3.14's incremental collector never
    reached those cycles on this bot: measured 2026-10-14/15, ~74 MB + ~4
    native threads per dead session, 110 sessions → 10.4 GB RSS / 406 threads,
    and one manual gc.collect() (injected via sys.remote_exec) freed 80 threads
    + 1.2 GB at once. The per-session native threads are NOT the openWakeWord
    model (its ONNX pool is process-wide — tested); whatever owns them, it is
    reachable only through the dead cycle, so collecting the cycle frees it.
    A full collection costs ~70-100 ms (measured), paid once per ENDED session
    (node reboot/reconnect) — the other live node's audio just buffers.
    """
    import gc
    import time

    t0 = time.perf_counter()
    freed = gc.collect()
    logger.info(f"session teardown: gc reclaimed {freed} objects in "
                f"{1000 * (time.perf_counter() - t0):.0f} ms; "
                f"threads={threading.active_count()} py-visible")


async def bot(runner_args: RunnerArguments):
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    # Node OTA delivery: /firmware/<track>/version + /firmware/<track>/<bin>
    # (array | flex | waveshare; see core/firmware_server.py — files in firmware/<track>/).
    from pipecat.runner.run import app as _runner_app
    from firmware_server import register_firmware_routes
    register_firmware_routes(_runner_app)

    # Web dashboard: /dashboard page + /api/dashboard/* endpoints
    # (mapping, nodes, zones, test clips). Same registration pattern
    # as firmware_server; see core/dashboard/.
    from dashboard import register_dashboard
    register_dashboard(_runner_app)

    # Serve HTTPS when certs exist: plain-HTTP LAN origins are insecure
    # contexts, so browsers hide getUserMedia/RTCPeerConnection and the
    # prebuilt client dies with "WebRTC not supported or suppressed".
    # The pipecat runner has no SSL args, so we wrap uvicorn.run to inject
    # the certfile/keyfile. Certs: scripts/make-cert.sh (self-signed, click
    # through the browser warning once).
    import uvicorn

    _cert = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    if os.path.exists(os.path.join(_cert, "cert.pem")):
        _orig_run = uvicorn.run

        def _tls_run(app, host=None, port=None, **kw):
            kw["ssl_certfile"] = os.path.join(_cert, "cert.pem")
            kw["ssl_keyfile"] = os.path.join(_cert, "key.pem")
            return _orig_run(app, host=host, port=port, **kw)

        uvicorn.run = _tls_run

    # Whisper at STARTUP, not on the first voice turn (2026-10-15, TODO "RSS
    # step"): same model/device/precision run_bot uses. Non-fatal — a failure
    # leaves the lazy load in LocalWhisperSTTService.ensure_model() in charge.
    try:
        from stt_local import preload_shared_model
        from config import cfg_env as _cfg_env

        preload_shared_model(
            _cfg_env("stt", "model", "LVI_STT_MODEL", "large-v3-turbo"),
            _cfg_env("stt", "device", "LVI_STT_DEVICE", "cuda"),
            _cfg_env("stt", "compute_type", "LVI_STT_COMPUTE", "float16"),
        )
    except Exception as e:
        logger.warning(f"whisper preload failed ({e}) — will load lazily on the first voice turn")

    # Pocket TTS at STARTUP when it's the provider or the fallback: one
    # model per process, so sessions never load weights mid-wake.
    # Non-fatal: a failure means primary-only (fallback) or a loud
    # construction error (pocket primary).
    try:
        from config import cfg as _cfg
        from config import cfg_env as _cfg_env

        _tp = str(_cfg_env("tts", "provider", "LVI_TTS_PROVIDER", "elevenlabs") or "")
        _fb = str(_cfg("tts", "fallback") or "").strip().lower()
        if _tp == "pocket" or _fb == "pocket":
            import tts_pocket
            if tts_pocket.pocket_available():
                tts_pocket.preload(tts_pocket.resolve_voice(
                    _cfg_env("tts", "pocket_voice", "LVI_TTS_POCKET_VOICE", "alba")))
            else:
                logger.warning(f"pocket tts not installed — {tts_pocket.INSTALL_HINT}")
    except Exception as e:
        logger.warning(f"pocket preload failed ({e}) — running without Pocket")

    main()