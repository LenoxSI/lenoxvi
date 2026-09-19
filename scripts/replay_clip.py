"""Fire the exact 22:00:45 reply clip at the living room Sonos for ear verification."""

import asyncio
import sys
from pathlib import Path

import yaml
from sonos_websocket import SonosWebsocket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

CLIP = sys.argv[1] if len(sys.argv) > 1 else "reply-1789524045-1.mp3"
URL = f"http://203.0.113.185:8765/{CLIP}"


async def go():
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    z = cfg["sonos"]["zones"]["living_room"]
    s = SonosWebsocket(z["ip"])
    await s.connect()
    await s.play_clip(URL, volume=int(cfg["sonos"].get("clip_volume", 70)))
    print(f"PLAYED {URL}")
    await s.disconnect()


asyncio.run(go())