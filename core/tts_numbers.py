"""Number pass for the local voices: digits -> the words a person would say.

Pocket (LocalAI's pocket-tts backend) reads digits one at a time ("4-4-9",
"2-3rd", "7-5%"), and Kokoro's espeak front-end reads "9:06" as "nine zero
six" and garbles "$12.50".
spell_numbers() rewrites clock times, money, percentages, degrees,
ordinals, years and plain numbers into words before either engine sees
the text. Phone numbers and bare runs of ten or more digits (no sign,
fraction or thousands commas) are ids, not quantities, and read digit by
digit; shorter runs and numbers written with thousands commas stay
quantities.

Local voices only. ElevenLabs and Deepgram normalise text themselves, and a
home-made time speller in front of ElevenLabs' normalizer once garbled
times (see tts_text.py), so the cloud services never get this pass.

Pure and standard-library only: text with no digit comes back unchanged.
"""
from __future__ import annotations

import re

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth",
            "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
# Past this many digits nothing is a quantity anyone says: digit by digit
# (commas, percentages and degrees included).
_MAX_DIGITS = 15
# A bare run this long (no sign, fraction or thousands commas) is a phone
# number or an id; shorter ones ("1500000 people") are amounts.
_MIN_ID_DIGITS = 10


def _under_1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    words = [f"{_ONES[hundreds]} hundred"] if hundreds else []
    if rest >= 20:
        tens, ones = divmod(rest, 10)
        words.append(_TENS[tens] + (f"-{_ONES[ones]}" if ones else ""))
    elif rest or not hundreds:
        words.append(_ONES[rest])
    return " ".join(words)


def int_to_words(n: int) -> str:
    """449 -> 'four hundred forty-nine' (American style, no 'and')."""
    if n < 0:
        return "minus " + int_to_words(-n)
    if n < 1000:
        return _under_1000(n)
    words = []
    for value, name in _SCALES:
        if n >= value:
            count, n = divmod(n, value)
            words.append(f"{int_to_words(count)} {name}")
    if n:
        words.append(_under_1000(n))
    return " ".join(words)


def ordinal_words(n: int) -> str:
    """23 -> 'twenty-third'."""
    words = int_to_words(n)
    head, sep, last = words.rpartition(" ")
    pre, dash, last = last.rpartition("-")
    if last in _ORDINAL:
        last = _ORDINAL[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + sep + pre + dash + last


def year_words(n: int) -> str:
    """2026 -> 'twenty twenty-six', 1905 -> 'nineteen oh five', 2005 -> 'two thousand five'."""
    if 2000 <= n <= 2009:
        return int_to_words(n)
    high, low = divmod(n, 100)
    if low == 0:
        return f"{int_to_words(high)} hundred"
    return f"{int_to_words(high)} {'oh ' if low < 10 else ''}{int_to_words(low)}"


def _digits(s: str) -> str:
    return " ".join(_ONES[int(c)] for c in s)


def _number(whole: str, frac: str | None = None) -> str:
    """'1,234' -> words; a leading zero or a very long run reads digit by digit;
    a fraction reads 'point' and then its digits."""
    plain = whole.replace(",", "")
    if len(plain) > _MAX_DIGITS or (len(plain) > 1 and plain[0] == "0" and "," not in whole):
        words = _digits(plain)
    else:
        words = int_to_words(int(plain))
    return words + (f" point {_digits(frac)}" if frac else "")


def _plural(words: str) -> str:
    return words[:-1] + "ies" if words.endswith("y") else words + "s"


# Building blocks. A number is "1,234" or "1234", optionally with a fraction.
_INT = r"(\d{1,3}(?:,\d{3})+|\d+)"
_NUM = _INT + r"(?:\.(\d+))?"
# A minus sign only where a number starts (not the dash in "5-10" or "COVID-19").
_SIGN = r"(?:(?<![\w.])([-−]))?"
# AM/PM: "PM", "pm", "p.m." (its last dot is kept when it ends a sentence).
_AMPM = r"([AaPp])(?:\.[Mm](?:\.(?=\s+[a-z]))?|[Mm])(?![A-Za-z])"

_MONEY = re.compile(_SIGN + r"\$\s?" + _NUM + r"(?:\s+(thousand|million|billion|trillion)\b)?(?!\d)")
_TIME = re.compile(r"(?<![\d:.])(\d{1,2}):([0-5]\d)(?![\d:])(?:\s?" + _AMPM + ")?")
_HOUR_AMPM = re.compile(r"(?<![\d:.,])(\d{1,2})\s?" + _AMPM)
_DEGREES = re.compile(_SIGN + _NUM + r"\s?°(?:\s?([FfCc])(?![A-Za-z]))?")
_PERCENT = re.compile(_SIGN + _NUM + r"\s?%")
_ORDINALS = re.compile(r"(?<![\d.])" + _INT + r"(st|nd|rd|th)(?![A-Za-z])", re.IGNORECASE)
# Decades: "1990s", "2010s", "'80s" (a bare "30s" may be seconds: left to the plain pass).
_DECADE = re.compile(r"(?<![\d.,])(?:['’](\d0)|(1[1-9]\d0|20\d0))s(?![A-Za-z])")
# Phone numbers: "555-123-4567", "555.123.4567", "(555) 123-4567",
# "+1 555 123 4567", "1-800-555-0199".
_PHONE = re.compile(
    r"(?<![\w.,$+\-−])(?:"
    r"(?:\+\d{1,3}[-. ]?)?\(\d{3}\)\s?\d{3}[-.]\d{4}"
    r"|\+\d{1,3}([-. ])\d{3}\1\d{3}\1\d{4}"
    r"|(?:1[-.])?\d{3}([-.])\d{3}\2\d{4}"
    r")(?![\w\-−]|[.,]\d)")
_ANY = re.compile(_SIGN + _NUM)
_HAS_DIGIT = re.compile(r"[0-9]")


def _minus(sign: str | None) -> str:
    return "minus " if sign else ""


def _phone(m: re.Match) -> str:
    # A comma between groups is the pause the local voices honour; a period
    # would split the number across sentences.
    groups = ", ".join(_digits(g) for g in re.findall(r"\d+", m.group(0)))
    return ("plus " if m.group(0).startswith("+") else "") + groups


def _money(m: re.Match) -> str:
    sign, whole, frac, scale = m.groups()
    if scale:                                   # "$1.5 million"
        return f"{_minus(sign)}{_number(whole, frac)} {scale} dollars"
    if frac and len(frac) > 2:                  # "$0.125": not cents
        return f"{_minus(sign)}{_number(whole, frac)} dollars"
    dollars = int(whole.replace(",", ""))
    cents = int(frac.ljust(2, "0")) if frac else 0
    parts = []
    if dollars or not cents:
        parts.append(f"{int_to_words(dollars)} dollar{'' if dollars == 1 else 's'}")
    if cents:
        parts.append(f"{int_to_words(cents)} cent{'' if cents == 1 else 's'}")
    return _minus(sign) + " and ".join(parts)


def _ampm(letter: str | None) -> str:
    return f" {letter.upper()}M" if letter else ""


def _time(m: re.Match) -> str:
    hour, minute, letter = int(m.group(1)), int(m.group(2)), m.group(3)
    if hour > 23:
        return m.group(0)                       # not a clock time; the plain pass reads it
    if minute == 0:
        if letter:
            said = int_to_words(hour)            # "9:00 PM" -> "nine PM"
        elif 1 <= hour <= 12:
            said = f"{int_to_words(hour)} o'clock"
        else:
            said = f"{int_to_words(hour)} hundred"   # "21:00" -> "twenty-one hundred"
    elif minute < 10:
        said = f"{int_to_words(hour)} oh {int_to_words(minute)}"
    else:
        said = f"{int_to_words(hour)} {int_to_words(minute)}"
    return said + _ampm(letter)


def _hour_ampm(m: re.Match) -> str:
    hour = int(m.group(1))
    if not 1 <= hour <= 12:
        return m.group(0)
    return int_to_words(hour) + _ampm(m.group(2))


def _degrees(m: re.Match) -> str:
    sign, whole, frac, scale = m.groups()
    one = whole == "1" and not frac
    unit = {"f": " Fahrenheit", "c": " Celsius"}.get((scale or "").lower(), "")
    return f"{_minus(sign)}{_number(whole, frac)} degree{'' if one else 's'}{unit}"


def _percent(m: re.Match) -> str:
    sign, whole, frac = m.groups()
    return f"{_minus(sign)}{_number(whole, frac)} percent"


def _ordinal(m: re.Match) -> str:
    return ordinal_words(int(m.group(1).replace(",", "")))


def _decade(m: re.Match) -> str:
    short, year = m.groups()
    if short:
        return _plural(int_to_words(int(short)))    # "'80s" -> "eighties"
    return _plural(year_words(int(year)))           # "1990s" -> "nineteen nineties"


def _any(m: re.Match) -> str:
    sign, whole, frac = m.groups()
    bare = not sign and not frac and "," not in whole
    if bare and len(whole) == 4 and 1100 <= int(whole) <= 2099:
        words = year_words(int(whole))
    elif bare and len(whole) >= _MIN_ID_DIGITS:
        words = _digits(whole)
    else:
        words = _minus(sign) + _number(whole, frac)
    # Keep a word glued to the number readable: "MP3" -> "MP three".
    text, start, end = m.string, m.start(), m.end()
    before = " " if start and text[start - 1].isalpha() else ""
    after = " " if end < len(text) and text[end].isalpha() else ""
    return before + words + after


# Most specific first. Each pass leaves words only, so a later pass never
# sees what an earlier one wrote.
_PASSES = ((_PHONE, _phone), (_MONEY, _money), (_TIME, _time), (_HOUR_AMPM, _hour_ampm),
           (_DEGREES, _degrees), (_PERCENT, _percent), (_ORDINALS, _ordinal), (_DECADE, _decade),
           (_ANY, _any))


def spell_numbers(text: str) -> str:
    """'It's 9:06, $12.50' -> "It's nine oh six, twelve dollars and fifty cents".

    Text without an ASCII digit is returned as is (the same object)."""
    if not text or not _HAS_DIGIT.search(text):
        return text
    for pattern, spell in _PASSES:
        text = pattern.sub(spell, text)
    return text
