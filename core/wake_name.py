"""The wake name: one owner for every rule that knows what the house is called.

The openWakeWord model is acoustic (it never sees spelling); the TEXT side does.
Speech-to-text mishears rare names, and the intelligence must drop a bare
"Hey <name>." that carries no question (saying the wake word again while the
room is already listening). Mis-spellings ("Jarvas", "Jervis" for "Jarvis") are
matched by sound-alike spelling, so nobody keeps a list. Everything here reads
config PER CALL: the config cache is cleared on every dashboard write, so a
name edit applies without a restart. Only the wake MODEL needs a restart (it
loads at startup).

The name comes ONLY from wake.name (the dashboard's Wake name card or a hand
edit) — never guessed from the model file. Unset = no name: nothing is
dropped. The name is never sent to speech-to-text.
"""
from __future__ import annotations

import os
import re

# Pre-trained wake models registered in openwakeword 0.4.0's own `models`
# table (bundled in its wheel; CC BY-NC-SA 4.0, non-commercial). LenoxVI never
# ships or hosts a model file. ("timer"/"weather" there are not wake words.)
STOCK_MODELS: tuple[str, ...] = ("hey_jarvis", "hey_mycroft", "alexa")
STOCK_LICENSE = "CC BY-NC-SA 4.0 (non-commercial)"
DEFAULT_MODEL = "hey_jarvis"

_PREFIX = r"(?:(?:hey|okay|ok|hi)\b\s*)?,?\s*"
# A letter (any script: José, Zoë), then letters, spaces, ' or -; max 30.
_TERM_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '\-]){0,29}$")
_LEADING = {"hey", "ok", "okay", "hi"}
# Words a sound-alike match must never drop: as a whole utterance they are a
# real one-word command ("Stop.").
RESERVED = _LEADING | {"stop", "yes", "no", "cancel"}
# The whole utterance: an optional "hey"/"ok", then one to three words.
_WHOLE = re.compile(r"^\s*" + _PREFIX + r"(?P<term>[^\W\d_]+(?:[ '\-]+[^\W\d_]+){0,2})\s*[.!?]?\s*$",
                    re.IGNORECASE)


def _wake() -> dict:
    from config import cfg
    return cfg("wake") or {}


def model_value() -> str:
    """The configured wake model: config > LVI_WAKE_MODEL env > stock default."""
    return str(_wake().get("model") or os.getenv("LVI_WAKE_MODEL") or DEFAULT_MODEL)


def resolve_model(value: str | None = None) -> str:
    """A loadable path: stock keys resolve to the file bundled with
    openwakeword; anything else is already a path."""
    v = str(value if value is not None else model_value())
    if v in STOCK_MODELS:
        import openwakeword
        return openwakeword.models[v]["model_path"]
    return v


def name() -> str:
    """wake.name, whitespace tidied; "" when unset."""
    return " ".join(str(_wake().get("name") or "").split())


def valid_term(s: str) -> bool:
    """Name/alias charset: a letter, then letters, spaces, ' or -; max 30."""
    return bool(_TERM_RE.match(str(s or "")))


def reserved_term(s: str) -> bool:
    return " ".join(str(s or "").split()).lower() in RESERVED


def _letters(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalpha())


def _distance(a: str, b: str) -> int:
    """Levenshtein edits between two short words."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def sounds_like(term: str, wake: str | None = None) -> bool:
    """`term` is the name or a spelling speech-to-text writes for it: the same
    letters within 2 edits for names of 5+ letters ("Jarvas", "Jervis" for
    "Jarvis"), 1 for 4 letters, exact below that (short names are too
    close to ordinary words)."""
    n = _letters(name() if wake is None else wake)
    t = _letters(term)
    if not n or not t or reserved_term(term):
        return False
    allowed = 2 if len(n) >= 5 else 1 if len(n) == 4 else 0
    return abs(len(t) - len(n)) <= allowed and _distance(t, n) <= allowed


def is_wake_phrase_only(text: str) -> bool:
    """The WHOLE utterance is the wake phrase ("Hey Jervis.") — never a real
    sentence that merely contains a similar word ("Jervis Bay is lovely")."""
    m = _WHOLE.match(text or "")
    return bool(m and sounds_like(m.group("term")))
