"""firmware_server — OTA delivery for the reSpeaker node, served by bot.py.

The node's OTA client (lenoxvi-firmware/src/ota.cpp) does a plain GET of
{PIPECAT_SMALLWEBRTC_URL minus /api/offer}/firmware/version and expects:

    {"version": "1.2.3", "filename": "node_esp32.bin"}

then downloads /firmware/<filename> as the raw app image and flashes it
(esp_ota_ops, A/B partitions, rollback-safe). Versions compare numerically.

Files live in lenoxvi/firmware/node/ — gitignored on the host and
populated by scp from deploy_firmware.sh, not tracked in the repo:
    firmware/node/version.json    served at /firmware/version
    firmware/node/node_esp32.bin served at /firmware/node_esp32.bin

Deploy flow: scripts/deploy_firmware.sh (VM) builds + copies + bumps
version.json; the node picks it up within its idle poll window. (Future
work: a "check-ota" data-channel message from the bot — nothing sends one
today.)
"""

import json
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

FIRMWARE_DIR = Path(__file__).resolve().parent.parent / "firmware" / "node"
VERSION_FILE = FIRMWARE_DIR / "version.json"


def _read_manifest() -> dict:
    """Return the served version manifest, or a clear error."""
    if not VERSION_FILE.is_file():
        raise HTTPException(status_code=404, detail="no version.json — run deploy_firmware.sh")
    try:
        manifest = json.loads(VERSION_FILE.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"bad version.json: {e}")
    if "version" not in manifest:
        raise HTTPException(status_code=500, detail="version.json missing 'version'")
    return manifest


def register_firmware_routes(app) -> None:
    """Attach /firmware/* to the pipecat runner's FastAPI app (bot.py calls
    this before main()). Plain GETs; the node's TLS-skip handles the
    self-signed cert and it never sends secrets, so no auth on this path —
    it's read-only delivery of deployed firmware files (scp-populated by
    deploy_firmware.sh; gitignored, not in the repo)."""

    @app.get("/firmware/version")
    async def firmware_version():
        manifest = _read_manifest()
        # Only version + filename pass through: the manifest's md5 (if any) is
        # currently dropped, not served to the node.
        filename = manifest.get("filename", "node_esp32.bin")
        return JSONResponse({"version": manifest["version"], "filename": filename})

    @app.get("/firmware/{filename}")
    async def firmware_bin(filename: str):
        # Path traversal guard: only serve files that actually live in the dir
        target = (FIRMWARE_DIR / filename).resolve()
        if not str(target).startswith(str(FIRMWARE_DIR.resolve())) or not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        logger.info(f"firmware_server: serving {filename} ({target.stat().st_size} bytes)")
        return FileResponse(target, media_type="application/octet-stream")

    logger.info(
        f"firmware_server: /firmware/* routes live "
        f"({FIRMWARE_DIR}{'' if VERSION_FILE.is_file() else ' — no version.json yet'})"
    )