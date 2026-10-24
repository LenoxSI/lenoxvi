"""wake_name module pins (host venv).

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

# whole-utterance drop, the name only (no spellings list)
CFG["wake"] = {"name": "Willow"}
for t in ["Hey Yellow.", "hey willow", "Willow.", "ok willow!", "Hey Willow?", "okay wilow.", "hi willow",
          "Hey, Willow."]:
    check(f"wake name drop: {t!r}", wn.is_wake_phrase_only(t))
for t in ["Hey Willow turn on the lights", "what's the weather", "hey willow, kill the kitchen lights",
          "Willow is my assistant", "show me Yellow tonight", ""]:
    check(f"wake name keep: {t!r}", not wn.is_wake_phrase_only(t))

# sound-alike spellings speech-to-text writes for a rare name, matched with no list
CFG["wake"] = {"name": "Maple"}
for t in ["Hey Maple.", "Hey Mable.", "Hey Mapple!", "Hey, Mapel.", "ok maypl", "Mable."]:
    check(f"wake name sound-alike drop: {t!r}", wn.is_wake_phrase_only(t))
for t in ["Bake a maple pie for dinner.", "Hey Mable turn on the lights", "Stop.", "Next.", "Louder.",
          "Lights off.", "Hey Siri.", "Hey Alexa.", "Hey.", "Okay.", "Hey Maple Maple Maple Maple"]:
    check(f"wake name sound-alike keep: {t!r}", not wn.is_wake_phrase_only(t))
check("wake name: sounds_like allows 2 edits at 5+ letters, not 3",
      wn.sounds_like("Mapel", "Maple") and not wn.sounds_like("Mabel", "Maple")
      and not wn.sounds_like("Mapleton", "Maple"))
check("wake name: 4-letter names allow 1 edit, 3-letter names none",
      wn.sounds_like("Noba", "Nova") and not wn.sounds_like("Novel", "Nova")
      and wn.sounds_like("Max", "Max") and not wn.sounds_like("Mix", "Max"))
check("wake name: a filler word never matches, even as the name's neighbour",
      not wn.sounds_like("okay", "Okey") and not wn.sounds_like("stop", "Stoop"))

# multi-word names and literal matching (no regex built from config any more)
CFG["wake"] = {"name": "Ms Smith"}
check("wake name: multi-word name drops", wn.is_wake_phrase_only("hey ms  smith."))
check("wake name: punctuation in the utterance is not a pattern (a.b never matches)",
      not wn.is_wake_phrase_only("a.b") and not wn.is_wake_phrase_only("ms.smith"))
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
CFG["wake"] = {"heard_as": ["yellow"]}
check("wake name: no name -> the filter never drops (an old heard_as is ignored)",
      not wn.is_wake_phrase_only("hey yellow") and not wn.is_wake_phrase_only("hey")
      and not wn.is_wake_phrase_only(""))
CFG["wake"] = {}
check("wake name: no name -> the filter never drops",
      not wn.is_wake_phrase_only("hey") and not wn.is_wake_phrase_only("Hey Jarvis."))
check("wake name: no spellings-list helpers left",
      not any(hasattr(wn, x) for x in ("aliases", "normalise_aliases", "_pattern")))
# --- review fixes ---
check("wake name: accented names are valid; digits/punctuation still rejected",
      wn.valid_term("José") and wn.valid_term("Zoë") and wn.valid_term("Chloé")
      and not wn.valid_term("R2 D2") and not wn.valid_term("Mr. X") and not wn.valid_term("Zoë!"))
check("wake name: filler words are reserved (never dropped as a name)",
      all(wn.reserved_term(t) for t in ["hey", "OK", "okay", "hi", "stop", "yes", "no", "cancel"])
      and not wn.reserved_term("yellow") and not wn.reserved_term("wilow"))
CFG["wake"] = {"name": "Willow"}
check("wake name: a leading comma with no prefix still drops (', Willow.')",
      wn.is_wake_phrase_only(", Willow.") and wn.is_wake_phrase_only("Hey, Yellow."))
CFG["wake"] = {"name": "Willow", "heard_as": ["computer"]}
check("wake name: a heard_as left in config.yaml is ignored",
      not wn.is_wake_phrase_only("hey computer") and wn.is_wake_phrase_only("hey wilow"))
CFG["wake"] = {"name": "Hey Jarvis"}
check("wake name: a name saved with its 'hey' still drops the bare wake ('Hey Jarvis.', 'Jarvis.', 'Hey, Jervis!')",
      wn.is_wake_phrase_only("Hey Jarvis.") and wn.is_wake_phrase_only("Jarvis.")
      and wn.is_wake_phrase_only("Hey, Jervis!") and not wn.is_wake_phrase_only("Hey Jarvis, what time is it?"))
CFG["wake"] = {"name": "OK Computer"}
check("wake name: 'OK Computer' as the name drops 'Okay computer.' but keeps a sentence",
      wn.is_wake_phrase_only("Okay computer.") and not wn.is_wake_phrase_only("computer, turn on the lights"))
check("wake name: a one-word name that IS a leading word is left alone ('Hey' stays reserved)",
      wn._core_name("Hey") == "Hey" and wn._core_name("hey, Jarvis") == "Jarvis" and wn._core_name("Jarvis") == "Jarvis")

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
