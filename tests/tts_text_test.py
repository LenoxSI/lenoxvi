"""ElevenLabs text-normalization setting: config value -> API value.

Run: cd tests && ~/venvs/lenoxvi/bin/python tts_text_test.py <path-to-core>
"""
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


for v, want in [("on", "on"), ("ON", "on"), (" off ", "off"), ("auto", "auto"),
                (None, None), ("", None), ("yes", None), (True, "on"), (False, "off"),
                (1, None)]:
    got = tts_text.normalization_mode(v)
    check(f"tts_text: normalization_mode({v!r}) -> {want!r}", got == want, f"got {got!r}")

# Through the real config loader: a bare `on` in config.yaml (the documented
# spelling) must reach ElevenLabs as "on", not vanish as a YAML boolean.
import tempfile

for raw, want in [("on", "on"), ("off", "off"), ('"on"', "on"), ("auto", "auto")]:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w") as f:
            f.write(f"tts:\n  elevenlabs_text_normalization: {raw}\n")
        os.environ["LVI_CONFIG"] = path
        import importlib

        import config
        importlib.reload(config)
        got = tts_text.normalization_mode(config.cfg("tts", "elevenlabs_text_normalization"))
        check(f"tts_text: config.yaml `{raw}` -> {want!r} through cfg()", got == want, f"got {got!r}")
os.environ.pop("LVI_CONFIG", None)

# Native normalization only: no home-made speller rides alongside it (the two
# collided - ElevenLabs' normalizer garbled our spelled-out times).
check("tts_text: no custom clock-time speller remains",
      not any(hasattr(tts_text, n) for n in ("ClockTimeFilter", "clock_filters", "spell_clock_times")))

print(f"\n{'ALL CHECKS PASSED' if not fails else str(fails) + ' FAIL(S)'}")
sys.exit(1 if fails else 0)
