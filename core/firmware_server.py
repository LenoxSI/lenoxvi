"""firmware_server — OTA delivery for the mic nodes, served by bot.py.

One directory per firmware line (track), named like everything else:

    firmware/array/version.json + array_esp32.bin   array line (XVF3800 mic-array)
    firmware/flex/version.json  + flex_esp32.bin    flex line (reSpeaker Flex)
    firmware/waveshare/version.json + waveshare_esp32.bin  waveshare line (Waveshare ESP32-S3-AUDIO)

A node's OTA client (lenoxvi-firmware/src/ota.cpp) GETs its track's manifest,
{PIPECAT_SMALLWEBRTC_URL minus /api/offer}/firmware/<track>/version:

    {"version": "1.2.9", "filename": "array_esp32.bin"}

then downloads /firmware/<track>/<filename> as the raw app image and flashes
it (esp_ota_ops, A/B partitions, rollback-safe). Versions compare
numerically. A track without a manifest 404s = "no update" (safe no-op).

LEGACY ALIAS (permanent): array images up to 1.2.8 have the track-less URL
compiled in — /firmware/version and /firmware/<filename>. Those routes serve
the ARRAY track, forever: any array node still on (or rolled back to) an old
image must keep finding its updates.

Files are gitignored, written only by the dashboard upload route or
scripts/deploy_firmware.sh — both refuse an image whose own identity
(core/firmware_image.py) doesn't match the track.
"""

import json
import os
from pathlib import Path

import firmware_image
import firmware_signing
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger
from paths import firmware_dir

# Track names only: the directories resolve per request via firmware_dir().
FIRMWARE_TRACKS = firmware_image.TRACKS
LEGACY_TRACK = "array"   # what the track-less legacy URLs serve (see docstring)


def _track_dir(track: str | None) -> Path:
    # Vocabulary only: the DIRECTORY resolves per request via
    # paths.firmware_dir() (repo/LVI_STATE_DIR), so the dashboard's uploads
    # (routes.py) and this server always agree — cwd anchoring only lined
    # up because systemd starts the bot in the repo.
    key = track or LEGACY_TRACK
    if key not in FIRMWARE_TRACKS:
        raise HTTPException(status_code=404, detail=f"unknown firmware track '{track}'")
    return firmware_dir() / key


def _read_manifest(track: str | None = None) -> dict:
    """Return the served version manifest (per-track), or a clear error."""
    version_file = _track_dir(track) / "version.json"
    if not version_file.is_file():
        raise HTTPException(status_code=404,
                             detail=f"no version.json (track '{track or LEGACY_TRACK}') — "
                                    f"upload firmware from the dashboard or run deploy_firmware.sh")
    try:
        manifest = json.loads(version_file.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"bad version.json: {e}")
    if "version" not in manifest:
        raise HTTPException(status_code=500, detail="version.json missing 'version'")
    return manifest


def _servable(d: Path, filename: str) -> Path:
    """The file a node may fetch from a track dir, or 404. Only files that
    really live in the dir (no traversal), and never a dashboard upload still
    being written: those land as `<name>.<random>.uploading` next to the live
    image and are renamed over it when complete."""
    target = (d / filename).resolve()
    if (not str(target).startswith(str(d.resolve())) or not target.is_file()
            or target.name.endswith(".uploading")):
        raise HTTPException(status_code=404, detail="not found")
    return target


def _refuse_unsigned_app(target: Path) -> None:
    """App images (<line>_esp32.bin) go out signed or not at all: signed nodes
    would refuse an unsigned one anyway, and nothing unsigned should reach a
    node by accident. The flash set's boot files aren't app images."""
    if target.name.endswith("_esp32.bin") and not firmware_signing.file_has_signature(target):
        logger.warning(f"firmware_server: refusing to serve unsigned {target.parent.name}/{target.name} "
                       f"— sign it (the bot signs uploads; deploy_firmware.sh runs scripts/sign_firmware.py)")
        raise HTTPException(status_code=409, detail="firmware image is not signed")


def register_firmware_routes(app) -> None:
    """Attach /firmware/* to the pipecat runner's FastAPI app (bot.py calls
    this before main()). Plain GETs; the node's TLS-skip handles the
    self-signed cert and it never sends secrets, so no auth on this path —
    it's read-only delivery of deployed firmware files (scp-populated by
    deploy_firmware.sh; gitignored, not in the repo)."""

    def _manifest_response(track: str | None) -> JSONResponse:
        manifest = _read_manifest(track)
        # Only version + filename pass through: the manifest's md5 (if any) is
        # currently dropped, not served to the node. A manifest always names
        # its file (both writers set it); the fallback is the pre-rename name.
        filename = manifest.get("filename", "node_esp32.bin")
        return JSONResponse({"version": manifest["version"], "filename": filename})

    @app.get("/firmware/version")
    async def firmware_version():
        # LEGACY ALIAS: array images <=1.2.8 ask here — serves the array track.
        return _manifest_response(None)

    @app.get("/firmware/{track}/version")
    async def firmware_track_version(track: str):
        # FastAPI matches in declaration order: the literal /firmware/version
        # above is declared first, so it never falls into this pattern.
        return _manifest_response(track)

    @app.get("/firmware/{track}/{filename}")
    async def firmware_track_bin(track: str, filename: str):
        d = _track_dir(track)
        target = _servable(d, filename)
        _refuse_unsigned_app(target)
        logger.info(f"firmware_server: serving {track}/{filename} ({target.stat().st_size} bytes)")
        return FileResponse(target, media_type="application/octet-stream")

    @app.get("/firmware/{filename}")
    async def firmware_bin(filename: str):
        # LEGACY ALIAS: array images <=1.2.8 download from here — array track.
        d = _track_dir(None)
        target = _servable(d, filename)
        _refuse_unsigned_app(target)
        logger.info(f"firmware_server: serving {LEGACY_TRACK}/{filename} via legacy URL "
                    f"({target.stat().st_size} bytes)")
        return FileResponse(target, media_type="application/octet-stream")

    root = firmware_dir()  # what requests will serve (per-request resolution)
    # Images copied in before signing existed (or by hand): sign them now, so
    # serving never has to refuse something this bot could have signed.
    signed = firmware_signing.sign_served_images(root, FIRMWARE_TRACKS)
    if signed:
        logger.info(f"firmware_server: signed at startup: {', '.join(signed)}")
    served = {t: (root / t / "version.json").is_file() for t in FIRMWARE_TRACKS}
    logger.info(f"firmware_server: /firmware/* routes live ({root}; manifests: {served})")