"""Speakable-text checks: clock times are spelled out, nothing else moves.

Run: cd tests && ~/venvs/lenoxvi/bin/python tts_text_test.py <path-to-core>
"""
import asyncio
import os
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, os.path.abspath(CORE))

import tts_text

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else f" — {detail}"))
    fails += 0 if cond else 1


SPOKEN = [
    # the incident sentence and its spellings
    ("It's 9:11 PM, Wednesday October 21st.", "It's nine eleven PM, Wednesday October 21st."),
    ("It's 9:11 p.m., Wednesday.", "It's nine eleven PM, Wednesday."),
    ("It's 9:11 pm on Wednesday", "It's nine eleven PM on Wednesday"),
    ("It's 9:11PM.", "It's nine eleven PM."),
    ("It's 9:11 p. m. now", "It's nine eleven PM now"),
    ("See you at 9:11 p.m.", "See you at nine eleven PM."),
    ("Wake up at 6:30 a.m. tomorrow", "Wake up at six thirty AM tomorrow"),
    # minutes: o'clock, oh-five, teens, hyphenated tens
    ("It's 9:00 PM.", "It's nine PM."),
    ("Dinner at 6:00.", "Dinner at six o'clock."),
    ("It's 9:05 AM", "It's nine oh five AM"),
    ("It's 9:45 PM", "It's nine forty-five PM"),
    ("It's 12:15 PM", "It's twelve fifteen PM"),
    ("It's 12:00 AM", "It's twelve AM"),
    # 24-hour clock said the 12-hour way
    ("The game starts at 21:30.", "The game starts at nine thirty PM."),
    ("Departure 00:45", "Departure twelve forty-five AM"),
    ("Meeting at 13:00", "Meeting at one PM"),
    # two times in one sentence, a range
    ("Open 9:30 AM to 5:15 PM", "Open nine thirty AM to five fifteen PM"),
]
UNCHANGED = [
    "It's 57 degrees with gusts to 44.",
    "The score was 2:1 at half time.",          # one-digit minutes = ratio
    "The run took 1:30:00 total.",              # H:MM:SS duration
    "Version 1.2:36 is out",                    # glued to a dot
    "Ratio 16:9 screens",                       # 9 is one digit
    "Call 555-0142 now",
    "Set it to 25:30",                          # not a clock hour
    "Room 9:61",                                # not a minute
    "Tomorrow at a meeting",                    # 'a m...' without a time
    "No colon here at all.",
    "",
]
for src, want in SPOKEN:
    got = tts_text.spell_clock_times(src)
    check(f"tts_text: {src!r}", got == want, f"got {got!r} want {want!r}")
for src in UNCHANGED:
    got = tts_text.spell_clock_times(src)
    check(f"tts_text: unchanged {src!r}", got == src, f"got {got!r}")
check("tts_text: 'at 9:30 a meeting' keeps the article",
      tts_text.spell_clock_times("at 9:30 a meeting") == "at nine thirty a meeting",
      tts_text.spell_clock_times("at 9:30 a meeting"))

# The pipecat filter is the same function on each aggregated sentence.
f = tts_text.ClockTimeFilter()
out = asyncio.run(f.filter("It's 9:11 PM."))
check("tts_text: ClockTimeFilter.filter spells the time", out == "It's nine eleven PM.", out)
check("tts_text: ClockTimeFilter is a pipecat BaseTextFilter",
      isinstance(f, __import__("pipecat.utils.text.base_text_filter",
                               fromlist=["BaseTextFilter"]).BaseTextFilter))

# Config value -> ElevenLabs apply_text_normalization
for v, want in [("on", "on"), ("ON", "on"), (" off ", "off"), ("auto", "auto"),
                (None, None), ("", None), ("yes", None), (True, None), (1, None)]:
    got = tts_text.normalization_mode(v)
    check(f"tts_text: normalization_mode({v!r}) -> {want!r}", got == want, f"got {got!r}")

print(f"\n{'ALL CHECKS PASSED' if not fails else str(fails) + ' FAIL(S)'}")
sys.exit(1 if fails else 0)
