"""Wake-phrase-only filter with this rig's shape of config: drop bare wake
words, keep commands. The name comes from config (wake_name), never code."""

import sys

sys.path.insert(0, "core")

import wake_name  # noqa: E402

wake_name._wake = lambda: {"name": "Willow", "heard_as": ["linux", "yellow"]}

from intelligence_agent import _is_wake_phrase_only  # noqa: E402

should_drop = ["Hey Linux.", "hey willow", "Willow.", "ok willow!", "Hey Willow?",
               "okay yellow.", "hi willow"]
should_keep = ["Hey Willow turn on the lights", "what's the weather",
               "hey willow, kill the kitchen lights", "Willow is my assistant",
               "turn off the garage light"]

bad = []
for t in should_drop:
    if not _is_wake_phrase_only(t):
        bad.append(f"FAIL drop: {t!r}")
for t in should_keep:
    if _is_wake_phrase_only(t):
        bad.append(f"FAIL keep: {t!r}")

for b in bad:
    print(b)
assert not bad, "regex misbehaves"
print("WAKE-PHRASE-REGEX-OK")