"""Speech-to-text choices: the three speech tiers (one copy, shared with
tts_catalog), the fixed Embedded Whisper model, common languages, and the
tier each provider sits in.

Pure data + rules — no Whisper import, so routes and tests stay light.
Hand-set config values outside these lists are left alone (shown as
"set in config"); the dashboard only ever writes these.
"""
from __future__ import annotations

# The three speech tiers, in display order — one copy for both cards
# (tts_catalog imports it). Embedded runs on this machine with no keys and
# no GPU; LocalAI is a speech server on the LAN; Cloud is an internet vendor.
TIERS = [
    {"name": "embedded", "label": "Embedded"},
    {"name": "localai", "label": "LocalAI"},
    {"name": "cloud", "label": "Cloud"},
]
TIER_NAMES = [t["name"] for t in TIERS]

# Embedded speech-to-text is fixed and CPU-only. Bigger models or a GPU
# belong to the LocalAI tier.
EMBEDDED_MODEL = "base"
EMBEDDED_DEVICE = "cpu"
EMBEDDED_COMPUTE = "int8"

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


# Who turns speech into text, in card order. Every provider sits in one
# tier; LocalAI and Cloud fall back to Embedded (Whisper base).
PROVIDERS = [
    {"name": "whisper", "label": "Whisper base", "tier": "embedded"},
    {"name": "localai", "label": "LocalAI", "tier": "localai"},
    {"name": "deepgram", "label": "Deepgram Nova-3", "tier": "cloud"},
    {"name": "elevenlabs", "label": "ElevenLabs Scribe v2", "tier": "cloud"},
]
PROVIDER_NAMES = [p["name"] for p in PROVIDERS]
DEFAULT_PROVIDER = "whisper"
DEEPGRAM_MODEL = "nova-3"
ELEVENLABS_MODEL = "scribe_v2_realtime"
# The .env key each cloud provider reads — shared with that vendor's TTS.
KEY_VARS = {"deepgram": "DEEPGRAM_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY"}


def validate_provider(provider):
    if provider not in PROVIDER_NAMES:
        return f"provider must be one of {PROVIDER_NAMES}"
    return None


def resolve_provider(value) -> str:
    """What a configured stt.provider means: unset or unknown -> Embedded."""
    v = str(value or "").strip().lower()
    return v if v in PROVIDER_NAMES else DEFAULT_PROVIDER


def tier_of(provider) -> str | None:
    return next((p["tier"] for p in PROVIDERS if p["name"] == provider), None)


def is_cloud(provider) -> bool:
    return tier_of(provider) == "cloud"


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


def language_value(code):
    """Config/Whisper value for a language code: auto or empty -> None."""
    return None if not code or code == "auto" else code


def validate_language(language):
    """An error message, or None when the language is allowed (None = no change)."""
    if language is not None and language not in LANGUAGE_CODES:
        return "language must be 'auto' or one of the listed codes"
    return None
