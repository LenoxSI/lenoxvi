"""Room recorder: a time-boxed capture of one room's raw mic audio.

Off unless asked for (dashboard API). One recording per room, capped at
MAX_SECONDS, written as a 16-bit WAV in the captures folder (repo
`captures/`, or LVI_CAPTURES_DIR) with owner-only permissions. Recordings
older than RETENTION_DAYS are pruned whenever a new one starts. Used for
diagnostics and for collecting voice samples.

The tap sits right after the transport input, so it hears exactly what
the wake gate hears: raw node audio, before any gating. The hot path is
one dict lookup per frame while nothing is recording. File writes run
off the event loop.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

MAX_SECONDS = 600
RETENTION_DAYS = 7
# The recorder's own name shape, <slug>-YYYYmmdd-HHMMSS[-N].wav: only these
# are listed, served, deleted or pruned, whatever else is in the folder.
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?-\d{8}-\d{6}(?:-\d+)?\.wav$")


@dataclass
class _Recording:
    room: str
    name: str
    seconds: int
    deadline: float  # time.monotonic()
    started: float  # time.time()
    chunks: list[bytes] = field(default_factory=list)
    nbytes: int = 0
    sample_rate: int = 0
    channels: int = 0
    done: bool = False
    timer: asyncio.TimerHandle | None = None


_active: dict[str, _Recording] = {}
_saving: set[str] = set()  # closed, file not written yet: the name is still taken


def captures_dir() -> Path:
    env = os.environ.get("LVI_CAPTURES_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "captures"


def _slug(room: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", room).strip("-") or "room"


def _files(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*.wav") if NAME_RE.match(p.name))


def _prune(d: Path) -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    for p in _files(d):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def is_recording(room: str | None = None) -> bool:
    return bool(_active) if room is None else room in _active


def start(room: str, seconds: int) -> dict:
    """Begin recording `room` for `seconds`. ValueError on a bad length,
    RuntimeError if the room is already recording."""
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be 1-{MAX_SECONDS}")
    if room in _active:
        raise RuntimeError(f"'{room}' is already recording")
    d = captures_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    _prune(d)
    # Names go down to the second; a stop and restart in the same second
    # (or two rooms with one slug) get -2, -3... instead of overwriting.
    base = f"{_slug(room)}-{time.strftime('%Y%m%d-%H%M%S')}"
    taken = {r.name for r in _active.values()} | _saving
    name, n = f"{base}.wav", 1
    while name in taken or (d / name).exists():
        n += 1
        name = f"{base}-{n}.wav"
    rec = _Recording(room=room, name=name, seconds=seconds,
                     deadline=time.monotonic() + seconds, started=time.time())
    _active[room] = rec
    try:
        # A node that drops mid-recording sends no more frames, so a timer
        # (not only the frame path) closes the file.
        rec.timer = asyncio.get_running_loop().call_later(seconds + 1, _finish, rec)
    except RuntimeError:
        rec.timer = None  # no loop (tests): the frame path or stop() closes it
    logger.info(f"recorder: {room} recording {seconds}s -> {name}")
    return {"room": room, "file": name, "seconds": seconds}


def feed(room: str | None, audio: bytes, sample_rate: int, channels: int) -> None:
    """Hot path: append one frame if `room` is recording."""
    rec = _active.get(room) if room else None
    if rec is None or rec.done:
        return
    if time.monotonic() >= rec.deadline:
        _finish(rec)
        return
    if not rec.sample_rate:
        rec.sample_rate, rec.channels = int(sample_rate), int(channels or 1)
    elif (int(sample_rate), int(channels or 1)) != (rec.sample_rate, rec.channels):
        return  # a WAV has one format; never splice a different one in
    rec.chunks.append(bytes(audio))
    rec.nbytes += len(audio)


async def stop(room: str) -> dict | None:
    """End `room`'s recording early and wait for the file to be written.
    None if it wasn't recording; OSError if the file could not be saved."""
    rec = _active.get(room)
    if rec is None:
        return None
    fut = _finish(rec)
    if fut is not None and not await fut:
        raise OSError(f"recorder: {rec.name} was not saved")
    return {"room": room, "file": rec.name}


def _finish(rec: _Recording):
    """Close a recording; returns the write future when a loop runs."""
    if rec.done:
        return None
    rec.done = True
    if rec.timer is not None:
        rec.timer.cancel()
    if _active.get(rec.room) is rec:
        del _active[rec.room]
    _saving.add(rec.name)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _write(rec)
        return None
    return loop.run_in_executor(None, _write, rec)


def _write(rec: _Recording) -> bool:
    """Write the WAV; False (logged) if it could not be saved. A timer or
    time-box close has no caller to tell, so only stop() reports it."""
    d = captures_dir()
    final = d / rec.name
    tmp = d / f".{rec.name}.part"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
            w.setnchannels(rec.channels or 1)
            w.setsampwidth(2)
            w.setframerate(rec.sample_rate or 16000)
            w.writeframes(b"".join(rec.chunks))
        os.replace(tmp, final)
        secs = rec.nbytes / (2 * (rec.channels or 1) * (rec.sample_rate or 16000))
        logger.info(f"recorder: {rec.room} saved {rec.name} ({secs:.1f}s of audio)")
        return True
    except OSError as e:
        logger.warning(f"recorder: {rec.room} could not save {rec.name} ({type(e).__name__})")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    finally:
        rec.chunks = []
        _saving.discard(rec.name)


def listing() -> dict:
    d = captures_dir()
    files = []
    if d.is_dir():
        for p in _files(d):
            st = p.stat()
            files.append({"name": p.name, "bytes": st.st_size, "mtime": int(st.st_mtime)})
    now = time.monotonic()
    active = [{"room": r.room, "file": r.name, "seconds_left": max(0, round(r.deadline - now))}
              for r in _active.values()]
    return {"recording": active, "files": files}


def path_for(name: str) -> Path | None:
    """The file for a listed recording name, or None. Names must have the
    recorder's own shape, so no path ever leaves the folder and no other
    file in it is ever served or deleted."""
    if not NAME_RE.match(name or ""):
        return None
    p = captures_dir() / name
    return p if p.is_file() else None


class RecorderTap(FrameProcessor):
    """Pass-through: hands raw input audio to the recorder for this
    pipeline's room while that room is recording."""

    def __init__(self, room_selector, **kwargs):
        super().__init__(**kwargs)
        self._room_selector = room_selector

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if _active and isinstance(frame, InputAudioRawFrame):
            try:
                feed(self._room_selector(), frame.audio, frame.sample_rate, frame.num_channels)
            except Exception as e:  # noqa: BLE001 — a recorder fault never stalls audio
                logger.warning(f"recorder: tap error ({type(e).__name__})")
        await self.push_frame(frame, direction)
