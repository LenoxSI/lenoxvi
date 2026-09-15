"""One-shot: generate a Roshni voice clip and play it on the living-room
Sonos over the owner's music (the ducking test). Key comes from .env on the host.
"""

import asyncio
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, "core")

from dotenv import load_dotenv

load_dotenv(".env")

import json

from sonos_output import SonosOutput


def gen_voice(text: str, voice_id: str, model: str) -> bytes:
    key = os.environ["ELEVENLABS_API_KEY"]
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps({"text": text, "model_id": model}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


async def main():
    cfg = SonosOutput("config.yaml")
    await cfg._clip_server.start()
    text = ("Hey the owner, this is your house voice. If you can hear me over the music, "
            "the Sonos overlay is working and ducking exactly as designed.")
    clip = Path("clips") / "voice-test.mp3"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(gen_voice(text, os.environ["LVI_TTS_VOICE_ID"], os.environ["LVI_TTS_MODEL"]))
    url = cfg._clip_server.url_for(clip.name)
    print(f"clip generated: {clip.stat().st_size} bytes")

    from sonos_websocket import SonosWebsocket

    sw = SonosWebsocket("203.0.113.222", player_id="RINCON_00000000000000000")
    await sw.connect()
    result = await sw.play_clip(url, volume=70)
    print("play_clip result:", result)
    await sw.close()
    await cfg._clip_server.stop()
    print("VOICE-TO-SONOS-DONE")


asyncio.run(main())