"""ElevenLabs text normalization: the config value -> apply_text_normalization.

ElevenLabs Flash v2.5 does not normalise numbers by default (ElevenLabs keeps
it off on Flash for latency) and garbles clock times: "It's 9:11 PM" came out
as "9-eleven p-a-e-e-e..." with seconds of silence about half the time in an
offline A/B. `tts.elevenlabs_text_normalization: on` has ElevenLabs spell out
times, money and dates itself (clean in every trial, ~+0.15 s to first audio).
A home-made time speller was tried alongside and removed: the two collided
("nine oh six PM" through ElevenLabs' normalizer garbled ~3/8).
"""

NORMALIZATION_MODES = ("auto", "on", "off")


def normalization_mode(value) -> str | None:
    """Config value -> ElevenLabs apply_text_normalization, or None (unset or
    unknown = ElevenLabs' own default; never a made-up value).

    config.py loads YAML 1.1, where a bare `on`/`off` (also yes/no,
    true/false) parses as a boolean - the documented `on` arrives as True.
    Map booleans back to the words they were written as."""
    if isinstance(value, bool):
        return "on" if value else "off"
    v = str(value).strip().lower() if value is not None else ""
    return v if v in NORMALIZATION_MODES else None
