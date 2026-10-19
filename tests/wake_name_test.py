"""051 — wake_name module pins (host venv).

Patches wake_name._wake instead of touching config files.
Run: cd tests && ~/venvs/lenoxvi/bin/python wake_name_test.py <path-to-core>
"""
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import wake_name as wn  # noqa: E402

CFG = {"wake": {}}
wn._wake = lambda: CFG["wake"]

# the name comes ONLY from wake.name (dashboard text box or a hand edit)
CFG["wake"] = {}
check("wake name name: unset -> empty (never guessed)", wn.name() == "", repr(wn.name()))
CFG["wake"] = {"model": "models/Hey_Willow_1-0.onnx"}
check("wake name name: a model filename is never used as the name", wn.name() == "", repr(wn.name()))
CFG["wake"] = {"model": "hey_jarvis"}
check("wake name name: a stock model doesn't name the house either", wn.name() == "", repr(wn.name()))
CFG["wake"] = {"model": "models/Hey_Willow_1-0.onnx", "name": "  Bill  "}
check("wake name name: wake.name used, whitespace tidied", wn.name() == "Bill", repr(wn.name()))
check("wake name: no derivation helpers left", not hasattr(wn, "derive_name") and not hasattr(wn, "effective_name"))

# regex — identical behaviour for this rig's shape of config
CFG["wake"] = {"name": "Willow", "heard_as": ["Linux", "yellow", "linux"]}
check("wake name: aliases normalised + de-duplicated", wn.aliases() == ["linux", "yellow"], str(wn.aliases()))
for t in ["Hey Linux.", "hey willow", "Willow.", "ok willow!", "Hey Willow?", "okay yellow.", "hi willow",
          "Hey, Willow."]:
    check(f"wake name drop: {t!r}", wn.is_wake_phrase_only(t))
for t in ["Hey Willow turn on the lights", "what's the weather", "hey willow, kill the kitchen lights",
          "Willow is my assistant", "install Linux on my laptop", ""]:
    check(f"wake name keep: {t!r}", not wn.is_wake_phrase_only(t))

# Review Focus 1/2: metacharacters escaped; a common-word alias stays harmless
CFG["wake"] = {"name": "Ms Smith", "heard_as": ["the", "a.b"]}
check("wake name: multi-word name drops", wn.is_wake_phrase_only("hey ms  smith."))
check("wake name: metachar alias is literal (a.b != axb)",
      wn.is_wake_phrase_only("a.b") and not wn.is_wake_phrase_only("axb"))
check("wake name: common-word alias drops only a bare utterance",
      wn.is_wake_phrase_only("hey the") and not wn.is_wake_phrase_only("turn off the lights"))
check("wake name: valid_term charset",
      wn.valid_term("Ms Smith") and wn.valid_term("o'neil") and not wn.valid_term("R2 D2")
      and not wn.valid_term("Mr. X") and not wn.valid_term("") and not wn.valid_term("x" * 31))

# live reload: no caching inside the module
CFG["wake"] = {"name": "Jarvis"}
before = wn.is_wake_phrase_only("hey computer")
CFG["wake"] = {"name": "Computer"}
check("wake name: config change applies with no restart",
      before is False and wn.is_wake_phrase_only("hey computer"))

# model resolution
check("wake name: stock key resolves to the bundled file",
      wn.resolve_model("hey_jarvis").endswith("hey_jarvis_v0.1.onnx"), wn.resolve_model("hey_jarvis"))
check("wake name: custom path passes through", wn.resolve_model("models/x.onnx") == "models/x.onnx")
CFG["wake"] = {}
check("wake name: absent model -> hey_jarvis bundled", wn.resolve_model().endswith("hey_jarvis_v0.1.onnx"))

# no speech-to-text hint any more (removed: failed its accuracy bar; aliases do the job)
check("wake name: no spelling-hint helpers left",
      not any(hasattr(wn, x) for x in ("hotwords", "bias_enabled", "_stt_bias", "BIAS_DEFAULT")))
CFG["wake"] = {"heard_as": ["linux"]}
check("wake name: no name -> aliases still drop, nothing else does",
      wn.is_wake_phrase_only("hey linux") and not wn.is_wake_phrase_only("hey")
      and not wn.is_wake_phrase_only(""))
CFG["wake"] = {}
check("wake name: no name and no aliases -> the filter never drops",
      not wn.is_wake_phrase_only("hey") and not wn.is_wake_phrase_only("Hey Jarvis."))
# --- review fixes ---
check("wake name: accented names are valid; digits/punctuation still rejected",
      wn.valid_term("José") and wn.valid_term("Zoë") and wn.valid_term("Chloé")
      and not wn.valid_term("R2 D2") and not wn.valid_term("Mr. X") and not wn.valid_term("Zoë!"))
check("wake name: filler words are reserved (never aliases)",
      all(wn.reserved_term(t) for t in ["hey", "OK", "okay", "hi", "stop", "yes", "no", "cancel"])
      and not wn.reserved_term("linux") and not wn.reserved_term("yellow"))
CFG["wake"] = {"name": "Willow", "heard_as": ["linux"]}
check("wake name: a leading comma with no prefix still drops (', Willow.')",
      wn.is_wake_phrase_only(", Willow.") and wn.is_wake_phrase_only("Hey, Linux."))
CFG["wake"] = {"name": "Willow", "heard_as": ["okay", "linux"]}
check("wake name: a reserved word hand-edited into heard_as is ignored by the filter",
      not wn.is_wake_phrase_only("Okay.") and wn.is_wake_phrase_only("hey linux"))

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
