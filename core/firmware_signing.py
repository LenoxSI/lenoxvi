"""Firmware signing: the bot holds the key and signs every image it serves.

Nodes running 1.3+ firmware accept an over-the-air update only when it carries
a Secure Boot V2 signature (RSA-3072) made with the same key as the image they
are running (ESP-IDF "verify app signature on update", no hardware secure
boot). The key never leaves this machine and users never handle it:

- created on first use at paths.secrets_dir()/firmware-signing-key.pem (0600),
- every image the bot serves is signed here: dashboard uploads, images
  deploy_firmware.sh copies in (it runs scripts/sign_firmware.py on the host),
  and anything left unsigned in a track dir at startup,
- firmware_server refuses to serve an unsigned app image.

Signing uses Espressif's own tool (`python -m espsecure`, pip esptool), run
with this interpreter. The signature sector is the last 4096 bytes of a signed
image: block 0 starts 0xE7 0x02, CRC32 of its first 1196 bytes at 1196.

Losing the key doesn't break nodes: they keep running but refuse updates
signed by a new key until re-flashed over USB (Node setup card).
"""

from __future__ import annotations

import binascii
import os
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import paths
from loguru import logger

KEY_NAME = "firmware-signing-key.pem"
SECTOR = 4096
_BLOCK_CRC_SPAN = 1196
_lock = threading.Lock()
_fp_cache: dict[tuple, str] = {}


def key_path() -> Path:
    return paths.secrets_dir() / KEY_NAME


def _espsecure(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "espsecure", *args], capture_output=True,
                          text=True, timeout=timeout, check=False)


def ensure_key() -> Path:
    """The signing key, created (RSA-3072, 0600) if this bot has none yet."""
    k = key_path()
    with _lock:
        if k.is_file():
            return k
        k.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(k.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=k.parent, prefix=".key-", suffix=".pem")
        os.close(fd)
        os.unlink(tmp)  # espsecure refuses nothing, but wants to create the file itself
        old = os.umask(0o077)
        try:
            r = _espsecure("generate-signing-key", "--version", "2", "--scheme", "rsa3072", tmp)
        finally:
            os.umask(old)
        if r.returncode != 0 or not Path(tmp).is_file():
            Path(tmp).unlink(missing_ok=True)
            raise RuntimeError("couldn't create the firmware signing key (espsecure generate-signing-key failed)")
        os.chmod(tmp, 0o600)
        os.replace(tmp, k)
        logger.info(f"firmware_signing: created a new signing key at {k} (fingerprint {fingerprint()[:16]}…) — back it up")
        return k


def fingerprint() -> str:
    """SHA-256 of the public key in Secure Boot V2 form (hex) — the digest a
    node logs and compares; the same for every image this bot signs."""
    k = ensure_key() if not key_path().is_file() else key_path()
    st = k.stat()
    cache_key = (str(k), st.st_mtime_ns, st.st_size)
    if cache_key in _fp_cache:
        return _fp_cache[cache_key]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "digest.bin"
        r = _espsecure("digest-sbv2-public-key", "--keyfile", str(k), "--output", str(out))
        if r.returncode != 0 or not out.is_file():
            raise RuntimeError("couldn't read the signing key's fingerprint")
        fp = out.read_bytes().hex()
    _fp_cache.clear()
    _fp_cache[cache_key] = fp
    return fp


def has_signature(data: bytes) -> bool:
    """True when `data` ends in a well-formed Secure Boot V2 signature sector
    (structure + CRC only; the node does the RSA check)."""
    if len(data) < 2 * SECTOR or len(data) % SECTOR:
        return False
    blk = data[-SECTOR:]
    if blk[0] != 0xE7 or blk[1] != 0x02:
        return False
    crc = struct.unpack("<I", blk[_BLOCK_CRC_SPAN:_BLOCK_CRC_SPAN + 4])[0]
    return crc == (binascii.crc32(blk[:_BLOCK_CRC_SPAN]) & 0xFFFFFFFF)


def file_has_signature(path: Path) -> bool:
    size = path.stat().st_size
    if size < 2 * SECTOR or size % SECTOR:
        return False
    with open(path, "rb") as f:
        f.seek(size - SECTOR)
        tail = f.read(SECTOR)
    return has_signature(b"\x00" * SECTOR + tail)


def signer_digest(path: Path) -> str | None:
    """The public-key digest of signature block 0 (hex), via espsecure."""
    r = _espsecure("signature-info-v2", str(path))
    for line in r.stdout.splitlines():
        if line.startswith("Public key digest for block 0:"):
            return line.split(":", 1)[1].replace(" ", "").strip().lower()
    return None


def sign_file(path: Path) -> bool:
    """Sign `path` in place with this bot's key (temp file + rename). An image
    already signed by this key is left alone (returns False); one signed by
    another key is re-signed. Raises on failure, leaving `path` untouched."""
    path = Path(path)
    k = ensure_key()
    data = path.read_bytes()
    if has_signature(data):
        if signer_digest(path) == fingerprint():
            return False
        data = data[:-SECTOR]  # someone else's signature: drop it, sign the padded image
    fd, src = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".unsigned")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    out = Path(src).with_suffix(".signed")
    try:
        r = _espsecure("sign-data", "--version", "2", "--keyfile", str(k), "--output", str(out), src)
        if r.returncode != 0 or not out.is_file():
            raise RuntimeError(f"espsecure sign-data failed for {path.name}")
        if not file_has_signature(out) or signer_digest(out) != fingerprint():
            raise RuntimeError(f"signed copy of {path.name} didn't verify")
        os.chmod(out, path.stat().st_mode & 0o777)
        os.replace(out, path)
    finally:
        Path(src).unlink(missing_ok=True)
        out.unlink(missing_ok=True)
    logger.info(f"firmware_signing: signed {path.name} (key {fingerprint()[:16]}…)")
    return True


def sign_served_images(firmware_root: Path, tracks) -> list[str]:
    """Startup pass: sign any served app image that isn't signed yet (images
    copied in by hand or by an older deploy script). Returns what it signed."""
    signed = []
    for t in tracks:
        app = Path(firmware_root) / t / f"{t}_esp32.bin"
        if app.is_file() and not file_has_signature(app):
            try:
                if sign_file(app):
                    signed.append(f"{t}/{app.name}")
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:  # never block startup on this
                logger.warning(f"firmware_signing: couldn't sign {t}/{app.name}: {e}")
    return signed
