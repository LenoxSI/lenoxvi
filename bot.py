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
    stt = LocalWhisperSTTService(
        model=os.getenv("LVI_STT_MODEL", "large-v3-turbo"),
        device=os.getenv("LVI_STT_DEVICE", "cuda"),
        compute_type=os.getenv("LVI_STT_COMPUTE", "float16"),
        language=os.getenv("LVI_STT_LANGUAGE") or None,
    )
    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        voice_id=os.getenv("LVI_TTS_VOICE_ID"),  # Ivy; set in .env
        model=os.getenv("LVI_TTS_MODEL", "eleven_v3"),
    )
    gate = EchoGate()
    brain = StubBrain(echo_gate=gate) if os.getenv("LVI_BRAIN", "stub") == "stub" else HermesAgent(echo_gate=gate)

    task = PipelineTask(
        Pipeline(
            [
                transport.input(),
                # wake gate AFTER the source (frames flow downstream from
                # transport.input()), BEFORE VAD/STT: gates audio in IDLE/LOCKED
                *( [WakeWordFilter(echo_gate=gate)] if os.getenv("LVI_WAKE_WORD", "1") == "1" else [] ),
                VADProcessor(vad_analyzer=SileroVADAnalyzer()),
                stt,
                brain,
                tts,
                SonosOutput(echo_gate=gate),  # same reply to the zone's Sonos (audioClip overlay)
                transport.output(),
            ]
        ),
        params=PipelineParams(allow_interruptions=False),  # no barge-in by design
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