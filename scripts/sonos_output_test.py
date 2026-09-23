"""Headless sonos_output test — drives _speak_to_sonos directly.

Exercises the real path: PCM -> MP3 (lameenc) -> clip file -> LAN HTTP serve
-> sonos-websocket play_clip on the configured zone (vol from config.yaml).
Plays one ~1s tone. Run from the repo root on appliance-host.
"""

import asyncio
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, "core")

from loguru import logger

from sonos_output import SonosOutput


def tone_pcm(seconds: float = 1.0, sr: int = 24000) -> bytes:
    import numpy as np

    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sig = 0.4 * np.sin(2 * np.pi * 440 * t)
    return (sig * 32767).astype(np.int16).tobytes()


async def main():
    out = SonosOutput("config.yaml")
    assert out._enabled, "sonos.enabled must be true in config.yaml"
    await out._clip_server.start()

    logger.info("calling _speak_to_sonos with 1s test tone ...")
    await out._speak_to_sonos(tone_pcm())

    clips = sorted(Path("clips").glob("*.mp3"))
    assert clips, "no clip file was written"
    clip = clips[-1]
    logger.info(f"clip on disk: {clip} ({clip.stat().st_size} bytes)")

    # header check: MP3 magic
    head = clip.read_bytes()[:3]
    assert head[0] == 0xFF, f"not an MP3 header: {head!r}"

    # the clip server serves it (fetch in a thread — a blocking fetch on this
    # loop would deadlock the very aiohttp server we're asking)
    url = out._clip_server.url_for(clip.name)

    def _fetch():
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.read()

    served = await asyncio.to_thread(_fetch)
    assert served == clip.read_bytes(), "served bytes differ from disk"
    logger.info(f"clip served over HTTP: {len(served)} bytes from {url}")

    await out._clip_server.stop()
    print("SONOS-OUTPUT-TEST-DONE")


asyncio.run(main())