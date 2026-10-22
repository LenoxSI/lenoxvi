"""Speakable text for TTS: what the voice is asked to read out loud.

ElevenLabs Flash v2.5 does not normalise numbers by default (ElevenLabs keeps
it off on Flash for latency), and it garbles clock times: "It's 9:11 PM" came
out as "9-eleven p-a-e-e-e..." with seconds of silence, about half the time,
in an offline A/B. Two layers fix it:

1. `normalization_mode()` - the ElevenLabs `apply_text_normalization` option
   (tts.elevenlabs_text_normalization: auto | on | off; absent = ElevenLabs'
   own default). "on" spells out times, money, phone numbers; ~+0.15 s.
2. `ClockTimeFilter` - spells out clock times ourselves ("9:11 PM" ->
   "nine eleven PM") before any text reaches the provider. Spelled-out times
   were clean every time, cost nothing, and don't depend on a provider plan.
"""
import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

NORMALIZATION_MODES = ("auto", "on", "off")

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}

# H:MM or HH:MM, optional AM/PM in any common spelling ("PM", "pm", "p.m.",
# "p. m."). Not part of a longer number or a H:MM:SS duration, not a ratio
# with one-digit minutes ("2:1"), not glued to letters or digits.
_CLOCK = re.compile(
    r"(?<![\w:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:]|\.\d)"
    r"(?:(\s*)([AaPp])\.?\s?[Mm](\.?)(?![A-Za-z]))?")


def normalization_mode(value) -> str | None:
    """Config value -> ElevenLabs apply_text_normalization, or None (unset or
    unknown = ElevenLabs' own default; never a made-up value)."""
    v = str(value).strip().lower() if value is not None else ""
    return v if v in NORMALIZATION_MODES else None


def _words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def _speak(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = m.group(4)
    if ampm:
        suffix = " AM" if ampm in "Aa" else " PM"
    elif hour >= 13:            # 24-hour clock: say it the 12-hour way
        hour, suffix = hour - 12, " PM"
    elif hour == 0:
        hour, suffix = 12, " AM"
    else:
        suffix = ""
    if ampm and hour == 0:
        hour = 12
    if minute == 0:
        spoken = _words(hour) + (suffix or " o'clock")
    elif minute < 10:
        spoken = f"{_words(hour)} oh {_words(minute)}{suffix}"
    else:
        spoken = f"{_words(hour)} {_words(minute)}{suffix}"
    # "9:11 p.m." at the very end of a sentence: the dot was both the
    # abbreviation and the full stop - keep the full stop.
    if m.group(5) and m.end() == len(m.string):
        spoken += "."
    return spoken


def spell_clock_times(text: str) -> str:
    """'It's 9:11 PM' -> 'It's nine eleven PM'. Leaves everything else alone."""
    return _CLOCK.sub(_speak, text) if ":" in text else text


class ClockTimeFilter(BaseTextFilter):
    """pipecat text filter: runs on each aggregated sentence before TTS."""

    async def update_settings(self, settings):
        pass

    async def filter(self, text: str) -> str:
        return spell_clock_times(text)

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass
