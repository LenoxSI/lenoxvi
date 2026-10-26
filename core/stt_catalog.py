"""Speech-to-text choices the dashboard offers: four faster-whisper models
(benchmarked on a 12 GB GPU and a 4-core CPU), common languages, and the
GPU/CPU runtime each pick is written with.

Pure data + rules — no Whisper import, so routes and tests stay light.
Hand-set config values outside these lists are left alone (shown as
"set in config"); the dashboard only ever writes these.
"""
from __future__ import annotations

MODELS = [
    {"name": "large-v3-turbo", "label": "Turbo — best all-round", "size_gb": 1.6, "gpu_only": True,
     "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"},
    {"name": "medium", "label": "Medium — faster, solid in noise", "size_gb": 1.5, "gpu_only": True,
     "repo": "Systran/faster-whisper-medium"},
    {"name": "small", "label": "Small — snappy in quiet rooms", "size_gb": 0.5, "gpu_only": False,
     "repo": "Systran/faster-whisper-small"},
    {"name": "base", "label": "Base — no GPU / fastest", "size_gb": 0.15, "gpu_only": False,
     "repo": "Systran/faster-whisper-base"},
]
MODEL_NAMES = [m["name"] for m in MODELS]
# The files faster-whisper fetches for a model (same list as its
# download_model), so our own download leaves exactly what it would.
FILE_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def spec(name):
    return next((m for m in MODELS if m["name"] == name), None)

LANGUAGES = [
    {"code": "auto", "label": "Auto-detect"},
    {"code": "en", "label": "English"},
    {"code": "es", "label": "Spanish"},
    {"code": "fr", "label": "French"},
    {"code": "de", "label": "German"},
    {"code": "it", "label": "Italian"},
    {"code": "pt", "label": "Portuguese"},
    {"code": "nl", "label": "Dutch"},
    {"code": "pl", "label": "Polish"},
    {"code": "ru", "label": "Russian"},
    {"code": "uk", "label": "Ukrainian"},
    {"code": "tr", "label": "Turkish"},
    {"code": "ar", "label": "Arabic"},
    {"code": "hi", "label": "Hindi"},
    {"code": "ja", "label": "Japanese"},
    {"code": "ko", "label": "Korean"},
    {"code": "zh", "label": "Chinese"},
]
LANGUAGE_CODES = [lang["code"] for lang in LANGUAGES]


# Who turns speech into text. Whisper runs on this machine (private); a
# cloud provider streams post-wake speech out (quality on slow hardware),
# with the configured Whisper model as its fallback.
PROVIDERS = [
    {"name": "whisper", "label": "Whisper (local)", "cloud": False},
    # LocalAI on the LAN: audio stays in the house, so it is not cloud.
    {"name": "localai", "label": "LocalAI (host)", "cloud": False},
    {"name": "deepgram", "label": "Deepgram Nova-3 (cloud)", "cloud": True},
    {"name": "elevenlabs", "label": "ElevenLabs Scribe v2 (cloud)", "cloud": True},
]
PROVIDER_NAMES = [p["name"] for p in PROVIDERS]
DEEPGRAM_MODEL = "nova-3"
ELEVENLABS_MODEL = "scribe_v2_realtime"
# The .env key each cloud provider reads — shared with that vendor's TTS.
KEY_VARS = {"deepgram": "DEEPGRAM_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY"}


def validate_provider(provider):
    if provider not in PROVIDER_NAMES:
        return f"provider must be one of {PROVIDER_NAMES}"
    return None


def is_cloud(provider) -> bool:
    return any(p["name"] == provider and p["cloud"] for p in PROVIDERS)


def cloud_model(provider):
    return {"deepgram": DEEPGRAM_MODEL, "elevenlabs": ELEVENLABS_MODEL}.get(provider)


def deepgram_language(code):
    """Nova-3 language option: Auto -> 'multi' (code-switching); every card
    language is a Nova-3 monolingual code, so a code passes straight through."""
    return "multi" if not code or code == "auto" else code


def elevenlabs_language(code):
    """Scribe language option: Auto -> None (Scribe detects it); a code goes
    to pipecat, whose ElevenLabs table maps it (en -> eng) at build."""
    return language_value(code)


def runtime_for(has_gpu: bool) -> tuple[str, str]:
    """(device, compute_type) written with a model: never a mismatched pair
    (float16 on a CPU fails to load)."""
    return ("cuda", "float16") if has_gpu else ("cpu", "int8")


def validate(model, language, has_gpu: bool):
    """An error message, or None when the pick is allowed. `model=None` means
    a language-only change."""
    if model is not None:
        spec = next((m for m in MODELS if m["name"] == model), None)
        if spec is None:
            return f"model must be one of {MODEL_NAMES}"
        if spec["gpu_only"] and not has_gpu:
            return f"{model} needs a GPU (none detected) — pick small or base"
    if language is not None and language not in LANGUAGE_CODES:
        return "language must be 'auto' or one of the listed codes"
    return None


def language_value(code):
    """Config/Whisper value for a language code: auto or empty -> None."""
    return None if not code or code == "auto" else code
