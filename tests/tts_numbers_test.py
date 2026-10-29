"""Number pass for Kokoro and LocalAI's voices (Pocket among them): digits -> spoken words.

Pure Python, no dependencies: runs with any python3.

Run: cd tests && python3 tts_numbers_test.py <path-to-core>
"""
import os
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, os.path.abspath(CORE))

import tts_numbers

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else f" — {detail}"))
    fails += 0 if cond else 1


CASES = [
    # clock times
    ("9:06", "nine oh six"),
    ("9:00", "nine o'clock"),
    ("9:00 PM", "nine PM"),
    ("12:30", "twelve thirty"),
    ("9:11 PM", "nine eleven PM"),
    ("21:45", "twenty-one forty-five"),
    # money
    ("$12.50", "twelve dollars and fifty cents"),
    ("$1", "one dollar"),
    ("$0.99", "ninety-nine cents"),
    ("$1,200", "one thousand two hundred dollars"),
    ("$3.05", "three dollars and five cents"),
    # percent and degrees
    ("75%", "seventy-five percent"),
    ("72°", "seventy-two degrees"),
    ("72°F", "seventy-two degrees Fahrenheit"),
    ("-5°C", "minus five degrees Celsius"),
    # ordinals
    ("23rd", "twenty-third"),
    ("1st", "first"),
    ("2nd", "second"),
    ("11th", "eleventh"),
    # plain numbers
    ("449", "four hundred forty-nine"),
    ("1,234", "one thousand two hundred thirty-four"),
    ("3.5", "three point five"),
    ("0", "zero"),
    ("1000000", "one million"),
    # years standing alone (1100-2099)
    ("2026", "twenty twenty-six"),
    ("1999", "nineteen ninety-nine"),
    ("2000", "two thousand"),
    ("2005", "two thousand five"),
    # words around numbers stay as they are
    ("Version 2", "Version two"),
]
for text, want in CASES:
    got = tts_numbers.spell_numbers(text)
    check(f"spell_numbers({text!r}) -> {want!r}", got == want, f"got {got!r}")

# In a sentence: the words and punctuation around each number are kept.
for text, want in [
    ("It's 9:06 and the high is 72°F.", "It's nine oh six and the high is seventy-two degrees Fahrenheit."),
    ("Lunch was $12.50, about 75% of the budget.",
     "Lunch was twelve dollars and fifty cents, about seventy-five percent of the budget."),
    ("On October 23rd, 2026, at 9:00 PM.", "On October twenty-third, twenty twenty-six, at nine PM."),
    ("Timer set for 9:11 PM, see you then!", "Timer set for nine eleven PM, see you then!"),
    ("Room 449 has 3.5 chairs.", "Room four hundred forty-nine has three point five chairs."),
    ("It's 72° Fahrenheit at 7 am.", "It's seventy-two degrees Fahrenheit at seven AM."),
    ("Back in the 1990s, a 7:59 a.m. start.", "Back in the nineteen nineties, a seven fifty-nine AM start."),
]:
    got = tts_numbers.spell_numbers(text)
    check(f"spell_numbers keeps the sentence: {text!r}", got == want, f"got {got!r}")

# No digits: the very same string comes back (identity, nothing rewritten).
for text in ["", "Hello there.", "It's nine o'clock, Jarvis!", "Ünïcödé — dashes, 'quotes' & symbols: % $ °",
             "two thousand and five"]:
    got = tts_numbers.spell_numbers(text)
    check(f"spell_numbers leaves digit-free text alone: {text!r}", got is text, f"got {got!r}")

# Whatever comes in, no ASCII digit comes out (both voices read leftovers
# digit by digit).
for text in ["Call 555-0100 or 0800", "Pi is 3.14159", "The 2010s", "COVID-19 and MP3",
             "1/2 cup", "Score 3:2", "1:30:00", "$1.5 million", "12345678901234567890",
             "4K at 60fps", "-12", "100°", "7:59 a.m. sharp"]:
    got = tts_numbers.spell_numbers(text)
    check(f"spell_numbers leaves no digits behind: {text!r}",
          not any(c in "0123456789" for c in got), f"got {got!r}")

# Small helpers the pass is built on.
check("int_to_words: 0 -> zero", tts_numbers.int_to_words(0) == "zero")
check("int_to_words: 115 -> one hundred fifteen", tts_numbers.int_to_words(115) == "one hundred fifteen")
check("int_to_words: 2,000,001 -> two million one", tts_numbers.int_to_words(2_000_001) == "two million one")
check("int_to_words: -7 -> minus seven", tts_numbers.int_to_words(-7) == "minus seven")
check("ordinal_words: 100 -> one hundredth", tts_numbers.ordinal_words(100) == "one hundredth")
check("ordinal_words: 12 -> twelfth", tts_numbers.ordinal_words(12) == "twelfth")
check("ordinal_words: 40 -> fortieth", tts_numbers.ordinal_words(40) == "fortieth")

# Pure: no imports beyond the standard library's re.
with open(os.path.join(os.path.abspath(CORE), "tts_numbers.py")) as _fh:
    _src = _fh.read()
_imports = sorted({ln.split()[1].split(".")[0] for ln in _src.splitlines()
                   if ln.startswith(("import ", "from "))})
check("tts_numbers: standard library only", set(_imports) <= {"__future__", "re"}, f"imports {_imports}")

print(f"\n{'ALL CHECKS PASSED' if not fails else str(fails) + ' FAIL(S)'}")
sys.exit(1 if fails else 0)
