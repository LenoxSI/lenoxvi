"""lenoxvi Phase 1 bot: local whisper ears, stub brain, ElevenLabs TTS (sonos_output later).

Pipeline: WebRTC in -> VAD (Silero) -> local faster-whisper STT (gpu)
          -> StubBrain -> ElevenLabs TTS (Ivy) -> WebRTC out

Run on appliance-host from ~/projects/lenoxvi:
  ~/venvs/lenoxvi/bin/python bot.py
Then open http://203.0.113.185:7860/client in a LAN browser.
Requires .env (see .env.example) with ELEVENLABS_API_KEY and LVI_TTS_VOICE_ID.
"""

import asyncio
import os
import sys

from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))

# Load .env (secrets live only on the host, gitignored) before any env reads.
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# aiortc debug: RTP/SRTP drops inside aiortc are silent by default. This is
# the only way to see WHY frames never reach the track (decrypt failure,
# demux mismatch, wrong SSRC/PT routing). Remove once the node is stable.
import logging as _std_logging
_std_logging.getLogger("aiortc").setLevel(_std_logging.DEBUG)
_std_logging.basicConfig(level=_std_logging.WARNING)

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
from pipecat.pipeline.task import PipelineTask, PipelineParams
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
    # TTS backend swap (design note / 018): elevenlabs | chatterbox — one
    # config.yaml line. Chatterbox rides pipecat's OpenAI TTS client against
    # the local Chatterbox-TTS-Server's OpenAI-compatible endpoint.
    backend = cfg_env("tts", "backend", "LVI_TTS_BACKEND", "elevenlabs")
    if backend == "chatterbox":
        from pipecat.services.openai import tts as openai_tts
        # pipecat's OpenAITTSService validates the voice against OpenAI's
        # fixed dict (alloy/nova/...) and refuses anything else BEFORE the
        # request. Against our LOCAL Chatterbox server that gate is wrong —
        # voices are filenames (Emily.wav, or anything uploaded via the
        # server's Web UI). Widen the dict; we're the only client.
        _cb_voices = cfg("tts", "chatterbox", {}).get("voice", "Emily.wav")
        openai_tts.VALID_VOICES = {**openai_tts.VALID_VOICES, _cb_voices: _cb_voices}
        from pipecat.services.openai.tts import OpenAITTSService
        tts = OpenAITTSService(
            api_key="not-needed",  # local server has no auth
            base_url=cfg("tts", "chatterbox", {}).get("url", "http://localhost:4123/v1"),
            voice=_cb_voices,
            model=cfg("tts", "chatterbox", {}).get("model", "chatterbox-turbo"),
        )
        logger.info(f"tts backend: chatterbox (local, voice={_cb_voices})")
    else:
        tts = ElevenLabsTTSService(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            voice_id=os.getenv("LVI_TTS_VOICE_ID"),  # Ivy; set in .env
            model=cfg_env("tts", "model", "LVI_TTS_MODEL", "eleven_v3"),
        )
        logger.info(f"tts backend: elevenlabs")
    gate = EchoGate()
    brain = (HermesAgent(echo_gate=gate) if cfg_env("brain", "kind", "LVI_BRAIN", "stub") == "hermes"
            else StubBrain(echo_gate=gate))

    # VAD tunables from config.yaml (vad: section) — SileroVADAnalyzer takes
    # VADParams directly (pipecat 1.10); defaults match the old hardcodes.
    from config import cfg
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

    task = PipelineTask(
        Pipeline(
            [
                transport.input(),
                # wake gate AFTER the source (frames flow downstream from
                # transport.input()), BEFORE VAD/STT: gates audio in IDLE/LOCKED
                *( [WakeWordFilter(echo_gate=gate)] if cfg_env("wake", "enabled", "LVI_WAKE_WORD", "1") in (True, 1, "1", "true") else [] ),
                VADProcessor(vad_analyzer=vad_analyzer),
                stt,
                brain,
                tts,
                SonosOutput(echo_gate=gate),  # same reply to the zone's Sonos (audioClip overlay)
                transport.output(),
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
        logger.info("browser client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("browser client disconnected")
        await task.cancel()

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