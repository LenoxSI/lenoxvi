"""LocalAI client: speech-to-text and text-to-speech over LocalAI's OpenAI
audio API, plus model discovery and ONE up/down state shared by STT and TTS.

Imported only when a card picks the `localai` provider. Stdlib HTTP only.
The key (LVI_LOCALAI_KEY) is required: without it no request is sent at all.
It never appears in any error, log or status text.

What LocalAI actually does (checked against v4.12.1):
- /v1/audio/transcriptions answers verbose JSON; `text` has a leading space.
- /v1/audio/speech ignores response_format=pcm and always sends a WAV, and
  sends the whole clip at once (no streaming), so we ask for wav and read the
  sample rate from its header.
- /v1/models lists ids only; /api/models/config-json/<id> says what a model
  is for (known_usecases) and which backend runs it.
"""
from __future__ import annotations

import array
import http.client
import io
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import uuid

from loguru import logger

KEY_VAR = "LVI_LOCALAI_KEY"
CONNECT_TIMEOUT = 1.0
STT_TIMEOUT = 6.0
TTS_TIMEOUT = 5.0
PROBE_TIMEOUT = 3.0
NO_KEY = "no LocalAI key — add it on System · Keys"
_FLAGS = {"FLAG_TRANSCRIPT": "stt", "FLAG_TTS": "tts"}


class LocalAIError(Exception):
    """A LocalAI call that didn't produce a usable answer. `reason` is short,
    human and key-free (it lands on the dashboard chip). `box_down` says
    whether it is about the whole server (unreachable, timed out, 5xx, the
    key, no URL) — callers mark LocalAI down for everyone only then; a
    per-request problem (unknown model, bad voice, a garbled answer) just
    falls back for that one call."""

    def __init__(self, reason: str, status: int | None = None, box_down: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.box_down = box_down


def redact(text, key: str | None = None) -> str:
    s = str(text)
    for k in filter(None, (key, os.environ.get(KEY_VAR))):
        s = s.replace(k, "***")
    return s[:160]


def normalize_url(raw) -> str | None:
    """`host:port` -> `http://host:port`; a trailing `/` or `/v1` is dropped
    (the paths below carry /v1 themselves)."""
    url = str(raw or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def _speed(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def settings() -> dict:
    from config import cfg_env
    return {
        "url": normalize_url(cfg_env("localai", "url", "LVI_LOCALAI_URL")),
        "model_stt": str(cfg_env("localai", "model_stt", "LVI_LOCALAI_MODEL_STT", "") or ""),
        "model_tts": str(cfg_env("localai", "model_tts", "LVI_LOCALAI_MODEL_TTS", "") or ""),
        "voice": str(cfg_env("localai", "voice", "LVI_LOCALAI_VOICE", "") or ""),
        "speed": _speed(cfg_env("localai", "speed", "LVI_LOCALAI_SPEED", 1.0)),
        # Per-request budgets: the defaults suit a GPU host; a slow CPU box
        # can raise them (a longer wait before the fallback takes over).
        "stt_timeout": _secs(cfg_env("localai", "stt_timeout", "LVI_LOCALAI_STT_TIMEOUT", STT_TIMEOUT), STT_TIMEOUT),
        "tts_timeout": _secs(cfg_env("localai", "tts_timeout", "LVI_LOCALAI_TTS_TIMEOUT", TTS_TIMEOUT), TTS_TIMEOUT),
    }


def _secs(value, default: float) -> float:
    """A timeout knob: 1–30 s; anything unreadable is the default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(30.0, max(1.0, v)) if v == v else default


def _request(method: str, path: str, *, body: bytes | None = None, ctype: str | None = None,
             total: float, url: str | None = None, key: str | None = None) -> bytes:
    """One HTTP call bounded by `total` seconds end to end (a timer shuts the
    socket at the deadline, so a server trickling bytes can't stretch it).
    Every failure is a LocalAIError — nothing else ever escapes."""
    url = normalize_url(url) if url else settings()["url"]
    if not url:
        raise LocalAIError("no LocalAI URL set")
    key = os.environ.get(KEY_VAR) if key is None else key
    if not key:
        raise LocalAIError(NO_KEY)
    try:
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname, u.port
        if not host:
            raise ValueError("no host")
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=CONNECT_TIMEOUT)
    except Exception:  # noqa: BLE001 — bad port, bad host, odd scheme
        raise LocalAIError("the LocalAI URL is not valid") from None
    deadline = time.monotonic() + total
    expired = threading.Event()

    def _cut():
        expired.set()
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(total, _cut)
    timer.daemon = True
    timer.start()
    try:
        try:
            conn.connect()
        except Exception as e:  # noqa: BLE001
            raise LocalAIError(f"LocalAI unreachable ({type(e).__name__})") from None
        headers = {"Accept": "*/*", "Authorization": f"Bearer {key}"}
        if ctype:
            headers["Content-Type"] = ctype
        try:
            conn.sock.settimeout(max(0.05, deadline - time.monotonic()))
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            chunks = []
            while True:
                if expired.is_set() or time.monotonic() > deadline:
                    raise TimeoutError
                part = resp.read1(65536)
                if not part:
                    break
                chunks.append(part)
            if expired.is_set():
                raise TimeoutError
            status = resp.status
        except Exception as e:  # noqa: BLE001 — timeouts, resets, protocol errors
            name = "TimeoutError" if expired.is_set() else type(e).__name__
            raise LocalAIError(f"LocalAI timed out or dropped the connection ({name})") from None
        data = b"".join(chunks)
        if status == 401:
            raise LocalAIError("LocalAI refused the key (check it on System · Keys)", 401)
        if status >= 400:
            msg = ""
            try:
                err = json.loads(data).get("error")
                msg = (err.get("message") if isinstance(err, dict) else err) or ""
            except Exception:  # noqa: BLE001 — a non-JSON error body is fine
                pass
            # 5xx = the server is in trouble; any other 4xx is about this request.
            raise LocalAIError(redact(f"LocalAI error {status}" + (f": {msg}" if msg else ""), key),
                               status, box_down=status >= 500)
        return data
    finally:
        timer.cancel()
        conn.close()


def _multipart(fields: dict, file_bytes: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    out.write(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
              "Content-Type: audio/wav\r\n\r\n".encode())
    out.write(file_bytes)
    out.write(f"\r\n--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"


def transcribe(wav: bytes, model: str, language: str | None, *, url=None, key=None, timeout=None) -> str:
    """Speech -> text. `language` None = let the server detect it."""
    fields = {"model": model}
    if language:
        fields["language"] = language
    body, ctype = _multipart(fields, wav)
    data = _request("POST", "/v1/audio/transcriptions", body=body, ctype=ctype,
                    total=timeout or STT_TIMEOUT, url=url, key=key)
    try:
        reply = json.loads(data)
        text = reply["text"]
    except Exception:  # noqa: BLE001 — not JSON, not a dict, or no text at all
        raise LocalAIError("LocalAI sent an unreadable transcript", box_down=False) from None
    return str(text or "").strip()


def speech(text: str, model: str, voice: str, speed: float, *, url=None, key=None,
           timeout=None) -> tuple[bytes, int]:
    """Text -> (16-bit mono PCM, sample rate). Mono 16-bit PCM passes through;
    mono 32-bit float (what LocalAI's Pocket backend sends) is converted.
    Anything else is an error, never noise."""
    body = json.dumps({"model": model, "input": text, "voice": voice, "speed": speed,
                       "response_format": "wav"}).encode()
    data = _request("POST", "/v1/audio/speech", body=body, ctype="application/json",
                    total=timeout or TTS_TIMEOUT, url=url, key=key)
    try:
        fmt, channels, rate, bits, raw = _parse_wav(data)
    except Exception:  # noqa: BLE001 — struct errors, missing chunks
        raise LocalAIError("LocalAI sent audio that isn't a WAV", box_down=False) from None
    if channels == 1 and fmt == _WAV_PCM and bits == 16:
        pcm = raw[:len(raw) - len(raw) % 2]
    elif channels == 1 and fmt == _WAV_FLOAT and bits == 32:
        pcm = _float32_to_pcm16(raw[:len(raw) - len(raw) % 4])
    else:
        kind = "float" if fmt == _WAV_FLOAT else "PCM" if fmt == _WAV_PCM else f"format {fmt}"
        raise LocalAIError(f"LocalAI audio is {channels}-channel {bits}-bit {kind}; need mono 16-bit PCM or "
                           "32-bit float", box_down=False)
    if not 8000 <= rate <= 48000:
        raise LocalAIError(f"LocalAI audio has an odd sample rate ({rate} Hz)", box_down=False)
    if not pcm or len(pcm) % 2:
        raise LocalAIError("LocalAI sent an empty or cut-off clip", box_down=False)
    return pcm, rate


_WAV_PCM, _WAV_FLOAT, _WAV_EXTENSIBLE = 1, 3, 0xFFFE


def _parse_wav(data: bytes) -> tuple[int, int, int, int, bytes]:
    """(format, channels, rate, bits, sample bytes) of a RIFF/WAVE clip.
    Reads the chunks itself: the stdlib `wave` module refuses float WAVs."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not RIFF/WAVE")
    fmt = channels = rate = bits = None
    i = 12
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + size]
        if cid == b"fmt ":
            fmt, channels, rate, _br, _ba, bits = struct.unpack("<HHIIHH", body[:16])
            if fmt == _WAV_EXTENSIBLE and len(body) >= 26:
                fmt = struct.unpack("<H", body[24:26])[0]  # the sub-format GUID's first 2 bytes
        elif cid == b"data":
            if fmt is None:
                raise ValueError("data before fmt")
            return fmt, channels, rate, bits, body
        i += 8 + size + (size & 1)
    raise ValueError("no data chunk")


def _float32_to_pcm16(raw: bytes) -> bytes:
    """Little-endian float32 samples (-1..1) -> 16-bit PCM, clipped; NaN = 0."""
    f = array.array("f")
    f.frombytes(raw)
    if sys.byteorder == "big":
        f.byteswap()
    out = array.array("h", (0 if math.isnan(x) else int(max(-1.0, min(1.0, x)) * 32767) for x in f))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def discover(*, url=None, key=None) -> dict:
    """{"stt": [id...], "tts": [{"id", "backend"}...]} — only models LocalAI
    says are for transcription or speech; anything unflagged is left out."""
    raw = json.loads(_request("GET", "/v1/models", total=PROBE_TIMEOUT, url=url, key=key))
    ids = [m.get("id") for m in (raw.get("data") or []) if isinstance(m, dict)]
    out: dict = {"stt": [], "tts": []}
    for mid in filter(None, ids):
        try:
            conf = json.loads(_request("GET", f"/api/models/config-json/{urllib.parse.quote(str(mid), safe='')}",
                                       total=PROBE_TIMEOUT, url=url, key=key))
        except (LocalAIError, ValueError):
            continue
        kinds = {_FLAGS.get(f) for f in (conf.get("known_usecases") or [])} - {None}
        if "stt" in kinds:
            out["stt"].append(mid)
        if "tts" in kinds:
            out["tts"].append({"id": mid, "backend": str(conf.get("backend") or "")})
    return out


def probe(*, url=None, key=None) -> tuple[bool, float | None, str | None]:
    """(answered, round-trip ms, reason when not)."""
    t0 = time.monotonic()
    try:
        _request("GET", "/v1/models", total=PROBE_TIMEOUT, url=url, key=key)
    except LocalAIError as e:
        return False, None, e.reason
    return True, (time.monotonic() - t0) * 1000, None


# ── one up/down state for the box: STT and TTS share it ──
_LOCK = threading.Lock()
_STATE: dict = {"down": None, "since": None, "last_probe": 0.0, "probing": False, "cooldown": 30.0,
                "url": None}


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(down=None, since=None, last_probe=0.0, probing=False, cooldown=30.0, url=None)


def bind(url: str | None) -> None:
    """The URL sessions were built with: the background probe checks THIS one
    (a URL saved since applies at the next session, like every setting)."""
    _STATE["url"] = normalize_url(url) if url else None


def set_cooldown(secs) -> None:
    try:
        _STATE["cooldown"] = max(0.1, float(secs))
    except (TypeError, ValueError):
        _STATE["cooldown"] = 30.0


def mark_down(reason: str) -> None:
    with _LOCK:
        if _STATE["down"] is None:
            logger.warning(f"localai: down ({reason}) — using the appliance fallback")
            _STATE["since"] = time.time()
            _STATE["last_probe"] = time.monotonic()
        _STATE["down"] = reason


def mark_up() -> None:
    with _LOCK:
        if _STATE["down"] is not None:
            logger.warning("localai: back — switching off the appliance fallback")
        _STATE.update(down=None, since=None)


def down_reason() -> str | None:
    return _STATE["down"]


def _probe_thread() -> None:
    try:
        ok, _ms, _why = probe(url=_STATE["url"])
        if ok:
            mark_up()
    except Exception as e:  # noqa: BLE001 — a probe must never kill anything
        # Type only: a traceback with locals could carry the key.
        logger.debug(f"localai: probe failed ({type(e).__name__})")
    finally:
        with _LOCK:
            _STATE["probing"] = False
            _STATE["last_probe"] = time.monotonic()


def should_try() -> bool:
    """True when LocalAI is up. When it's down, start at most ONE background
    probe per cooldown and answer False: a live turn never waits on it."""
    if _STATE["down"] is None:
        return True
    with _LOCK:
        due = time.monotonic() - _STATE["last_probe"] >= _STATE["cooldown"]
        if due and not _STATE["probing"]:
            _STATE["probing"] = True
            threading.Thread(target=_probe_thread, name="localai-probe", daemon=True).start()
    return False


def status() -> dict:
    try:
        url = settings()["url"]
    except Exception:  # noqa: BLE001 — status must never raise
        url = None
    return {"url": url, "down": _STATE["down"], "since": _STATE["since"]}
