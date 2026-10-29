"""Text-to-speech choices: every TTS provider, its speech tier, and the
.env key it needs.

Pure data — no pipecat import, so routes, keys_catalog and tests stay light.
"""
from __future__ import annotations

# Re-exported: one copy of the tiers for both cards.
from stt_catalog import TIER_NAMES, TIERS  # noqa: F401

# Card order inside each tier = list order. ElevenLabs is one provider with
# two lanes picked by tts.model (Flash -> TTS websocket, v3/v4 -> Text-to-Dialogue).
PROVIDERS = [
    {"name": "kokoro", "label": "Kokoro", "tier": "embedded", "key_var": None},
    {"name": "localai", "label": "LocalAI", "tier": "local", "key_var": None},
    {"name": "elevenlabs", "label": "ElevenLabs", "tier": "cloud", "key_var": "ELEVENLABS_API_KEY"},
    {"name": "deepgram-flux", "label": "Deepgram Flux", "tier": "cloud", "key_var": "DEEPGRAM_API_KEY"},
    {"name": "deepgram", "label": "Deepgram Aura-2", "tier": "cloud", "key_var": "DEEPGRAM_API_KEY"},
]
PROVIDER_NAMES = [p["name"] for p in PROVIDERS]
DEFAULT_PROVIDER = "kokoro"


def resolve_provider(value) -> str:
    """What a configured tts.provider means: unset or unknown -> Embedded."""
    v = str(value or "").strip().lower()
    return v if v in PROVIDER_NAMES else DEFAULT_PROVIDER


def tier_of(provider) -> str | None:
    return next((p["tier"] for p in PROVIDERS if p["name"] == provider), None)


def is_cloud(provider) -> bool:
    return tier_of(provider) == "cloud"


def key_var(provider) -> str | None:
    """The .env key a TTS provider needs; None = keyless (Kokoro, LocalAI, unknown)."""
    return next((p["key_var"] for p in PROVIDERS if p["name"] == provider), None)
