"""Pocket TTS (Kyutai, CPU-native): the local TTS lane + fallback engine.

pipecat 1.11 ships PocketTTSService, but it loads the model in __init__
and LenoxVI builds a TTS per SESSION, so every wake would reload weights.
This module keeps ONE model per process (loaded at startup by preload())
and hands it to every session's service. A process-wide lock serializes
generation, so two rooms queue instead of thrashing the CPU.

Optional dependency: pocket-tts + CPU torch (requirements-pocket.txt).
Nothing here imports torch at module import time. pocket_available()
answers the question without importing it, so a cloud-only install
never pays for torch.

A failed load is final for the process: a load with no network retries
the weight host for ~24 s before falling back to the cache, and a load
that still fails must never be retried per session (it would freeze
every wake). A restart retries.
"""
from __future__ import annotations

import asyncio
import importlib.util
import threading
from typing import Any

from loguru import logger

# English presets shipped by pocket-tts 3.3.0 (README voice list; every
# name verified to load on a real install). Order = UI order.
POCKET_VOICES: tuple[str, ...] = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "eve", "fantine", "george", "jane", "jean",
    "javert", "marius", "mary", "michael", "paul", "peter_yearsley",
    "stuart_bell", "vera",
)
DEFAULT_VOICE = "alba"
INSTALL_HINT = ("pip install -r requirements-pocket.txt && "
                "bash scripts/fetch_pocket_weights.sh")

GEN_LOCK = asyncio.Lock()

_model: Any = None
_voice_states: dict[str, Any] = {}
_service_cls: type | None = None
_load_failed = False
_warned: set[str] = set()
# Two rooms reconnecting at once both warm() in threads: load the model once.
_LOAD_LOCK = threading.Lock()


def warn_once(key: str, msg: str) -> None:
    """A WARNING logged once per process (per-session repeats are noise)."""
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(msg)


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ValueError:
        # Already in sys.modules without a spec (e.g. a stub): importable.
        return True


def pocket_available() -> bool:
    """True when torch + pocket_tts are installed. Never imports them."""
    return _importable("torch") and _importable("pocket_tts")


def resolve_voice(voice: str | None) -> str:
    """A catalog voice or a .wav clone path; anything else -> the default."""
    if voice and (voice in POCKET_VOICES or str(voice).endswith(".wav")):
        return voice
    if voice:
        logger.warning(f"pocket tts: unknown voice {voice!r}, using {DEFAULT_VOICE!r}")
    return DEFAULT_VOICE


def _load_model() -> Any:
    from pocket_tts import TTSModel
    return TTSModel.load_model(language="english")


def get_shared_model() -> Any:
    return _model


def _voice_state(voice: str) -> Any:
    if voice not in _voice_states:
        _voice_states[voice] = _model.get_state_for_audio_prompt(voice)
    return _voice_states[voice]


def _usable_voice(voice: str) -> str | None:
    """`voice` if its state loads, else the default (warned once), else None.

    A bad clone voice (missing .wav, gated weights not logged in) must not
    take Pocket down: the model is fine, only that voice isn't."""
    for v in dict.fromkeys((voice, DEFAULT_VOICE)):
        try:
            _voice_state(v)
            return v
        except Exception as e:
            warn_once(f"voice:{v}", f"pocket tts: voice {v!r} failed to load ({e})"
                      + (f"; using {DEFAULT_VOICE!r}" if v != DEFAULT_VOICE else ""))
    return None


def preload(voice: str) -> bool:
    """Load the shared model + a usable voice state. Never raises."""
    with _LOAD_LOCK:
        return _preload_locked(voice)


async def warm(voice: str | None) -> bool:
    """preload() in a worker thread: session setup runs on the event loop,
    and a model or voice-state load there stalls every room."""
    return await asyncio.to_thread(preload, resolve_voice(voice))


def _preload_locked(voice: str) -> bool:
    global _model, _load_failed
    if _model is None:
        if _load_failed:
            return False
        try:
            _model = _load_model()
        except Exception as e:
            _model = None
            _load_failed = True
            logger.warning(f"pocket tts: model failed to load ({e}); "
                           f"install/fetch with: {INSTALL_HINT}")
            return False
        logger.info(f"pocket tts: model loaded (sr={_model.sample_rate})")
    return _usable_voice(voice) is not None


def _english():
    from pipecat.transcriptions.language import Language
    return Language.EN


def _service_class(mixin: type) -> type:
    """Build (once) TrackedPocketTTSService(mixin, PocketTTSService)."""
    global _service_cls
    if _service_cls is not None and issubclass(_service_cls, mixin):
        return _service_cls
    from pipecat.services.pocket_tts.tts import PocketTTSService
    from pipecat.services.tts_service import TTSService

    class TrackedPocketTTSService(mixin, PocketTTSService):
        _tts_provider = "pocket"

        def __init__(self, *, voice: str, **kwargs):
            # Skip PocketTTSService.__init__ (it loads weights). Mirror its
            # TTSService setup, then seed from the shared cache.
            settings = PocketTTSService.Settings(model=None, voice=voice,
                                                 language=_english())
            TTSService.__init__(self, push_start_frame=True,
                                push_stop_frames=True, settings=settings, **kwargs)
            self._model = _model
            self._voice_state = _voice_state(voice)

        async def run_tts(self, text: str, context_id: str):
            async with GEN_LOCK:
                async for frame in super().run_tts(text, context_id):
                    yield frame

        async def _try_revive(self) -> None:
            return None  # no websocket to revive; pipecat's gate handles it

    _service_cls = TrackedPocketTTSService
    return _service_cls


def make_service(*, mixin: type, voice: str, provider_label: str = "pocket"):
    """A per-session Pocket service on the SHARED model, or None."""
    voice = resolve_voice(voice)
    if _model is None and not preload(voice):
        return None
    voice = _usable_voice(voice)
    if voice is None:
        return None
    try:
        svc = _service_class(mixin)(voice=voice)
    except Exception as e:
        logger.warning(f"pocket tts: service construction failed ({e})")
        return None
    svc._tts_provider = provider_label
    return svc


def _reset_for_tests() -> None:
    global _model, _service_cls, _load_failed
    _model = None
    _service_cls = None
    _load_failed = False
    _voice_states.clear()
    _warned.clear()
