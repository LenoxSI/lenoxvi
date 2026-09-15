"""One-shot probe: does the ElevenLabs multi-stream websocket accept our key per model?"""
import asyncio
import os

import websockets


async def try_model(model: str) -> str:
    key = os.environ["ELEVENLABS_API_KEY"]
    voice = os.environ["LVI_TTS_VOICE_ID"]
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/multi-stream-input"
        f"?model_id={model}&output_format=pcm_24000&auto_mode=true"
    )
    try:
        async with websockets.connect(
            url, additional_headers={"xi-api-key": key}, max_size=16 * 1024 * 1024
        ):
            return f"{model}: CONNECTED OK"
    except Exception as e:
        return f"{model}: {type(e).__name__}: {str(e)[:250]}"


async def main():
    for m in ("eleven_v3", "eleven_flash_v2_5", "eleven_multilingual_v2"):
        print(await try_model(m))


asyncio.run(main())