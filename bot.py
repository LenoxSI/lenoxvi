"""lenoxvi house-voice bot: wake gate -> VAD -> whisper STT -> brain -> TTS.

Pipeline (Phase 4): WebRTC in -> wake gate (openWakeWord "Hey Willow") ->
Silero VAD -> local faster-whisper STT -> brain (HermesAgent via config.yaml
brain.kind, production = hermes; stub = bench fallback) -> ElevenLabs TTS
(voice from .env LVI_TTS_VOICE_ID) -> Sonos audioClip in the room's zone.

Run on appliance-host from ~/projects/lenoxvi:
  ~/venvs/lenoxvi/bin/python bot.py
Then open https://<bot-host-LAN-IP>:7860/client in a LAN browser
(mic access needs the HTTPS secure context; certs via scripts/make-cert.sh).

Requires .env (see .env.example) with all three keys:
  ELEVENLABS_API_KEY, LVI_TTS_VOICE_ID, LVI_HERMES_KEY.
"""

import asyncio
import os
import sys

from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))

# Load .env (secrets live only on the host, gitignored) before any env reads.
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from brain_stub import StubBrain
from echo_gate import EchoGate
from hermes_agent import HermesAgent
from sonos_output import SonosOutput
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
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport


async def run_bot(transport: SmallWebRTCTransport, runner_args: RunnerArguments):
    from config import cfg, cfg_env

    stt = LocalWhisperSTTService(
        model=cfg_env("stt", "model", "LVI_STT_MODEL", "large-v3-turbo"),
        device=cfg_env("stt", "device", "LVI_STT_DEVICE", "cuda"),
        compute_type=cfg_env("stt", "compute_type", "LVI_STT_COMPUTE", "float16"),
        language=cfg_env("stt", "language", "LVI_STT_LANGUAGE") or None,
    )
    # TTS: ElevenLabs (api key + voice_id in .env; model in config.yaml).
    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        voice_id=os.getenv("LVI_TTS_VOICE_ID"),
        # Code default = the measured-good model. Do NOT default to eleven_v3:
        # the websocket handshake rejects v3 with HTTP 400 (config.yaml:32-34,
        # commit fb0cf94). A config.yaml without tts.model — the fresh-clone
        # state — must land on a model that actually speaks.
        model=cfg_env("tts", "model", "LVI_TTS_MODEL", "eleven_flash_v2_5"),
    )
    logger.info("tts backend: elevenlabs")
    gate = EchoGate()
    # Multi-node fix (Opus review H1): EVERYTHING room-related resolves
    # through THIS pipeline's own pc_id — never the registry's global
    # "last connected" room. The offer tee has already correlated
    # {room, pc_id} by the time the pipeline runs (send-time tee, note
    # 024), so room_for_pc is exact here.
    pc_id = getattr(getattr(transport, "_webrtc_connection", None), "pc_id", None)
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
        from config import cfg
        mapped = (cfg("room_zones") or {}).get(room)
        return mapped  # None -> default zone fallback in SonosOutput

    def _on_wake():
        dash_registry.note_wake(_pipeline_room())

    brain = (HermesAgent(echo_gate=gate) if cfg_env("brain", "kind", "LVI_BRAIN", "stub") == "hermes"
            else StubBrain(echo_gate=gate))

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
    # behind a blocked processor — HermesAgent holds the queue 3-40s on
    # brain calls and every LED message used to arrive as one late burst
    # (2026-09-30 12:23: the ring held stale purple through the whole
    # "On it." -> answer gap). One output transport, shared by the pipeline
    # and the LED wire; per-pipeline LedSender = per-node targeting free.
    from led_state import LedSender

    out = transport.output()
    led = LedSender(out)
    # Wake gate built once: the pipeline gets it conditionally, and
    # SonosOutput reads its state for the end-of-speech LED (purple vs
    # dark) and fires speaking LEDs from its real-time TTS frames.
    wake_filter = (WakeWordFilter(echo_gate=gate, on_wake=_on_wake, led=led)
                   if cfg_env("wake", "enabled", "LVI_WAKE_WORD", "1") in (True, 1, "1", "true")
                   else None)

    task = PipelineWorker(
        Pipeline(
            [
                transport.input(),
                # wake gate AFTER the source (frames flow downstream from
                # transport.input()), BEFORE VAD/STT: gates audio in IDLE/LOCKED
                *([wake_filter] if wake_filter else []),
                VADProcessor(vad_analyzer=vad_analyzer),
                stt,
                brain,
                tts,
                # reply in the wake room's zone when mapped; speaking LEDs
                # fire from its real-time TTS frames on the direct wire
                SonosOutput(echo_gate=gate, zone_selector=_reply_zone,
                            wake_gate=wake_filter, led=led),
                out,
            ]
        ),
        params=PipelineParams(
            allow_interruptions=False,  # no barge-in by design
        ),
        # Always-on ESP32 node: a quiet house is NOT a dead session. This is a
        # PipelineTask kwarg (NOT a PipelineParams field — putting it there
        # silently did nothing and the 300s worker default killed every quiet
        # session at exactly 5 min). None = never idle-cancel; the node's STUN
        # keepalive + on_client_disconnected handle real disconnects.
        idle_timeout_secs=None,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # client = SmallWebRTCConnection (pipecat smallwebrtc): tie it to
        # the room recorded by the offer tee; the dashboard registry
        # turns "a client connected" into "the bedroom node is online".
        from dashboard import registry
        registry.mark_connected(client.pc_id)
        # M6: register the OUTPUT TRANSPORT (direct data-channel wire),
        # not the pipeline task — node commands must not queue behind a
        # blocked brain. Same wire LedSender uses.
        registry.register_sender(client.pc_id, out)
        logger.info("client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        from dashboard import registry
        registry.mark_disconnected(client.pc_id)
        registry.drop_sender(client.pc_id)
        logger.info("client disconnected")
        await task.cancel()

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        # Dashboard command replies + liveness probes (firmware 1.1.0).
        # NOTE: the smallwebrtc transport registers "on_app_message" —
        # NOT "on_client_message" (that name is the RTVI processor's;
        # using it silently never fires, measured 2026-09-29 18:10).
        # Signature is (message, sender) where sender IS the pc_id
        # string (transport.py:578 passes connection.pc_id directly) —
        # not a connection object.
        from dashboard import node_command
        node_command.on_message(message, sender)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


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

    # Node OTA delivery: /firmware/version + /firmware/<bin> (see
    # core/firmware_server.py; files live in firmware/node/ in-repo).
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

    main()