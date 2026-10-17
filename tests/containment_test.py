"""Headless test for intelligence_agent's echo-containment + tag-strip logic."""

import sys

sys.path.insert(0, "core")

from intelligence_agent import _normalize, _strip_tags

# Case 1: the live failure — her reply + filler blob transcribed back
echo_texts = [
    "Kitchen prep light is on. Anything else while I've got the butler warmed up?",
    "On it — one sec.",
]
heard = ("kitchen prep light is on anything else while i've got the butler "
         "warmed up on it. One sec.")

words = set(_normalize(heard).split())
hits = {w for t in echo_texts for w in words if w in set(_normalize(t).split())}
ratio = len(hits) / len(words) if words else 0
print(f"containment: {len(hits)}/{len(words)} words matched (ratio {ratio:.2f})")
assert ratio >= 0.6, "containment should have caught the echo blob"

# Case 2: a REAL user command — must NOT be caught
real = "can you turn on the kitchen prep light for me real quick"
rw = set(_normalize(real).split())
rhits = {w for t in echo_texts for w in rw if w in set(_normalize(t).split())}
rratio = len(rhits) / len(rw)
print(f"real-command: {len(rhits)}/{len(rw)} overlap (ratio {rratio:.2f})")
assert rratio < 0.6, "real command wrongly flagged as echo"

# Case 3: tags stripped. _strip_tags replaces [tags] with a space, so
# "Okay. [laughs] Either way, done." -> "Okay.  Either way, done." (double
# space, then strip). Normalize both sides to keep the assertion honest —
# the old `or True` here made this case structurally unable to fail.
_got = _strip_tags("Okay. [laughs] Either way, done.")
_expected = "Okay.  Either way, done."
assert " ".join(_got.split()) == " ".join(_expected.split()), f"tag strip: {_got!r}"
print("tag strip:", _got)

print("CONTAINMENT-LOGIC-OK")