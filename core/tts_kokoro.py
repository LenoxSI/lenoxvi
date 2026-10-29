"""Kokoro TTS (82M, Apache-2.0, CPU via ONNX): a fully local TTS provider.

pipecat ships KokoroTTSService, but its __init__ downloads missing model
files from GitHub and builds a Kokoro model (325 MB) per instance, and
LenoxVI builds a TTS per room connection. This module loads ONE model
per process (warm(), in a worker thread) from a fixed cache directory and
hands it to every room's service. A room's service never loads or downloads
anything; scripts/fetch_kokoro_models.sh fetches the files once.

A process-wide lock serializes generation, so two rooms queue instead of
thrashing the CPU. Text gets the local-voice number pass (tts_numbers)
before synthesis.

Kokoro is the Embedded voice: kokoro-onnx is in the base install
(requirements.txt) and the bot preloads it at startup, as the main voice or
as the standby behind LocalAI and every cloud voice. Nothing here imports
kokoro-onnx or pipecat's Kokoro module at import time (the dashboard imports
this module); kokoro_available() answers without importing it.

Missing or wrong-size model files are retried on the next warm() (a cheap
check, so fetching the files then reconnecting a node works). A model that
fails to load is final for the process: a restart retries.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import threading
from pathlib import Path
from typing import Any

import tts_numbers
from loguru import logger

# English voices in voices-v1.0.bin. Prefix: a = American (en-us),
# b = British (en-gb); second letter f/m = female/male. Order = UI order.
KOKORO_VOICES: tuple[str, ...] = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
)
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0
SPEED_RANGE = (0.5, 1.5)
# File name -> exact size (scripts/fetch_kokoro_models.sh checks the sha256).
MODEL_FILES = {"kokoro-v1.0.onnx": 325_532_387, "voices-v1.0.bin": 28_214_398}
INSTALL_HINT = ("pip install -r requirements.txt && "
                "bash scripts/fetch_kokoro_models.sh")

GEN_LOCK = asyncio.Lock()
# Kokoro makes a chunk's audio in one go (no streaming inside a sentence), and
# pipecat ends a TTS context after stop_frame_timeout_s (default 3 s) without
# audio — a long reply then logged "completed with no audio" and briefly
# counted as finished. Sentences are spoken one at a time (first audio fast),
# and a slow one is tolerated this long. A normal reply still ends at once:
# its end is an explicit marker, not this timer.
STOP_FRAME_TIMEOUT_S = 10.0
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def sentences(text: str) -> list[str]:
    """`text` split after . ! or ? followed by a space; blanks dropped. A
    fragment under 12 characters ("Mr.", "Sure.") joins the next one."""
    out: list[str] = []
    carry = ""
    for p in _SENTENCE_END.split(text or ""):
        p = p.strip()
        if not p:
            continue
        p = f"{carry} {p}" if carry else p
        if len(p) < 12:
            carry = p
            continue
        out.append(p)
        carry = ""
    if carry:
        if out:
            out[-1] = f"{out[-1]} {carry}"   # a tiny tail joins the sentence before it
        else:
            out.append(carry)
    return out

_kokoro: Any = None
_load_failed = False
_service_cls: type | None = None
_warned: set[str] = set()
# Two rooms reconnecting at once both warm() in threads: load the model once.
_LOAD_LOCK = threading.Lock()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)


def kokoro_available() -> bool:
    """True when kokoro-onnx is installed. Never imports it."""
    try:
        return importlib.util.find_spec("kokoro_onnx") is not None
    except ValueError:
        # Already in sys.modules without a spec (e.g. a stub): importable.
        return True


def model_dir() -> Path:
    """Where the model files live: one fixed per-user cache directory (the
    fetch script writes the same path)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "lenoxvi" / "kokoro"


def resolve_voice(voice: str | None) -> str:
    """A catalog voice; anything else -> the default."""
    if voice in KOKORO_VOICES:
        return voice
    if voice:
        logger.warning(f"kokoro tts: unknown voice {voice!r}, using {DEFAULT_VOICE!r}")
    return DEFAULT_VOICE


def language_for(voice: str) -> str:
    """The espeak-ng language a voice speaks: en-gb for b*, else en-us."""
    return "en-gb" if resolve_voice(voice).startswith("b") else "en-us"


def _speed(value) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = None
    if speed is None or not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
        if value not in (None, ""):
            logger.warning(f"kokoro tts: speed {value!r} is outside 0.5–1.5, using {DEFAULT_SPEED}")
        return DEFAULT_SPEED
    return speed


def get_shared_model() -> Any:
    return _kokoro


def _model_paths() -> tuple[Path, Path]:
    d = model_dir()
    return d / "kokoro-v1.0.onnx", d / "voices-v1.0.bin"


def _files_problem() -> str | None:
    """Why the model files can't be loaded, or None when they look right."""
    for path in _model_paths():
        if not path.is_file():
            return f"{path} is missing"
        if path.stat().st_size != MODEL_FILES[path.name]:
            return f"{path} is incomplete or the wrong file"
    return None


def setup_problem() -> str | None:
    """Why Kokoro can't be used here (package or model files), or None.

    The dashboard offers and accepts Kokoro only when this is None: a
    provider saved without its model files makes every room fail to build
    its pipeline."""
    if not kokoro_available():
        return f"Kokoro is not installed — {INSTALL_HINT}"
    files = _files_problem()
    if files:
        return f"Kokoro model files: {files} — fetch them with: bash scripts/fetch_kokoro_models.sh"
    return None


def preload() -> bool:
    """Load the shared model (blocking). Never raises."""
    global _kokoro, _load_failed
    with _LOAD_LOCK:
        if _kokoro is not None:
            return True
        if _load_failed:
            return False
        problem = _files_problem()
        if problem:
            _warn_once(f"files:{problem}", f"kokoro tts: {problem}; fetch the model with: "
                                           "bash scripts/fetch_kokoro_models.sh")
            return False
        try:
            from kokoro_onnx import Kokoro
            model, voices = _model_paths()
            _kokoro = Kokoro(str(model), str(voices))
        except Exception as e:  # noqa: BLE001 — a bad model file must not crash the bot
            _kokoro = None
            _load_failed = True
            logger.warning(f"kokoro tts: model failed to load ({e}); "
                           f"install/fetch with: {INSTALL_HINT}")
            return False
        logger.info("kokoro tts: model loaded")
        return True


async def warm() -> bool:
    """preload() in a worker thread: session setup runs on the event loop,
    and a model load there stalls every room."""
    if _kokoro is not None:
        return True
    return await asyncio.to_thread(preload)


def _service_class(mixin: type) -> type:
    """Build (once) TrackedKokoroTTSService(mixin, KokoroTTSService)."""
    global _service_cls
    if _service_cls is not None and issubclass(_service_cls, mixin):
        return _service_cls
    from pipecat.audio.utils import create_stream_resampler
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.tts_service import TTSService
    from pipecat.transcriptions.language import Language

    languages = {"en-us": Language.EN_US, "en-gb": Language.EN_GB}

    class TrackedKokoroTTSService(mixin, KokoroTTSService):
        _tts_provider = "kokoro"

        def __init__(self, *, voice: str, speed: float, **kwargs):
            # Skip KokoroTTSService.__init__: it downloads missing files and
            # loads its own model. Do its TTSService half, then take the
            # shared model. Subclassing (not copying the class) keeps
            # pipecat's run_tts, which needs only _kokoro, _resampler and
            # the settings (the suite pins that).
            settings = KokoroTTSService.Settings(model=None, voice=voice, speed=speed,
                                                 language=languages[language_for(voice)])
            kwargs.setdefault("stop_frame_timeout_s", STOP_FRAME_TIMEOUT_S)
            TTSService.__init__(self, push_start_frame=True,
                                push_stop_frames=True, settings=settings, **kwargs)
            self._kokoro = _kokoro
            self._resampler = create_stream_resampler()

        async def run_tts(self, text: str, context_id: str):
            # One sentence at a time: its audio goes out while the next is
            # made, and another room can take its turn in between.
            for part in sentences(tts_numbers.spell_numbers(text)):
                async with GEN_LOCK:
                    async for frame in super().run_tts(part, context_id):
                        yield frame

        async def _try_revive(self) -> None:
            return None  # no websocket to revive

    _service_cls = TrackedKokoroTTSService
    return _service_cls


def make_service(*, mixin: type, voice: str | None, speed=DEFAULT_SPEED):
    """A per-session Kokoro service on the SHARED model, or None.

    Never loads: call warm() first. None when the model isn't loaded."""
    if _kokoro is None:
        return None
    voice = resolve_voice(voice)
    try:
        if voice not in _kokoro.voices:
            logger.warning(f"kokoro tts: voice {voice!r} is not in the voices file")
            return None
        return _service_class(mixin)(voice=voice, speed=_speed(speed))
    except Exception as e:  # noqa: BLE001 — None makes the caller raise a clear error
        logger.warning(f"kokoro tts: service construction failed ({e})")
        return None


def _reset_for_tests() -> None:
    global _kokoro, _service_cls, _load_failed
    _kokoro = None
    _service_cls = None
    _load_failed = False
    _warned.clear()
