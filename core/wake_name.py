"""The wake name: one owner for every rule that knows what the house is called.

The openWakeWord model is acoustic (it never sees spelling); the TEXT side does.
Speech-to-text mishears rare names, and the intelligence must drop a bare
"Hey <name>." that carries no question. Everything here reads config PER CALL:
the config cache is cleared on every dashboard write, so name and alias edits
apply without a restart. Only the wake MODEL needs a restart (it loads at
startup).

The name comes ONLY from wake.name (the dashboard's Wake name card or a hand
edit) — never guessed from the model file. Unset = no name: the drop-filter
uses the heard-as aliases alone. Aliases are never sent to speech-to-text.
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

_PREFIX = r"(?:(?:hey|ok|okay|hi)\s*)?,?\s*"
# A letter (any script: José, Zoë), then letters, spaces, ' or -; max 30.
_TERM_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '\-]){0,29}$")
_LEADING = {"hey", "ok", "okay", "hi"}
# Words that must never become an alias: as a whole-utterance drop they would
# silently swallow a real one-word command ("Stop.").
RESERVED = _LEADING | {"stop", "yes", "no", "cancel"}


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


def normalise_aliases(items) -> list[str]:
    out: list[str] = []
    for a in items or []:
        a = " ".join(str(a).split()).lower()
        if a and a not in out:
            out.append(a)
    return out


def aliases() -> list[str]:
    raw = _wake().get("heard_as") or []
    return [a for a in normalise_aliases([raw] if isinstance(raw, str) else raw)
            if not reserved_term(a)]


def _pattern() -> re.Pattern | None:
    # Whole-utterance only: an alias can drop a BARE "Hey <alias>." and never
    # a real sentence that happens to contain the word.
    n = name().lower()
    terms = ([n] if n else []) + [a for a in aliases() if a != n]
    if not terms:
        return None                     # no name, no aliases: never drop
    alts = "|".join(r"\s+".join(re.escape(p) for p in t.split()) for t in terms)
    return re.compile(rf"^\s*{_PREFIX}(?:{alts})\s*[.!?]?\s*$", re.IGNORECASE)


def is_wake_phrase_only(text: str) -> bool:
    pat = _pattern()
    return bool(pat and pat.match(text or ""))
