"""051 — wake_name module pins (host venv).

Patches wake_name._wake / wake_name._stt_bias instead of touching config files.
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

CFG = {"wake": {}, "bias": None}
wn._wake = lambda: CFG["wake"]
wn._stt_bias = lambda: CFG["bias"]

# derivation
check("051 derive: stock hey_jarvis -> Jarvis", wn.derive_name("hey_jarvis") == "Jarvis")
check("051 derive: alexa -> Alexa", wn.derive_name("alexa") == "Alexa")
check("051 derive: custom Hey_Willow_1-0.onnx -> Willow",
      wn.derive_name("models/Hey_Willow_1-0.onnx") == "Willow")
check("051 derive: bundled path hey_jarvis_v0.1.onnx -> Jarvis",
      wn.derive_name("/x/hey_jarvis_v0.1.onnx") == "Jarvis")
check("051 derive: no alphabetic token -> None", wn.derive_name("1234.onnx") is None)

CFG["wake"] = {}
check("051 effective: nothing set -> Jarvis (from the stock default model)",
      wn.effective_name() == ("Jarvis", "model"), str(wn.effective_name()))
CFG["wake"] = {"model": "models/Hey_Willow_1-0.onnx"}
check("051 effective: custom model, no name -> derived",
      wn.effective_name() == ("Willow", "model"), str(wn.effective_name()))
CFG["wake"] = {"model": "models/Hey_Willow_1-0.onnx", "name": "Computer"}
check("051 effective: explicit name wins", wn.effective_name() == ("Computer", "set"))
CFG["wake"] = {"model": "9999.onnx"}
check("051 effective: underivable -> default", wn.effective_name() == ("Jarvis", "default"))

# regex — identical behaviour for this rig's shape of config
CFG["wake"] = {"model": "models/Hey_Willow_1-0.onnx", "heard_as": ["Linux", "yellow", "linux"]}
check("051 aliases normalised + de-duplicated", wn.aliases() == ["linux", "yellow"], str(wn.aliases()))
for t in ["Hey Linux.", "hey willow", "Willow.", "ok willow!", "Hey Willow?", "okay yellow.", "hi willow",
          "Hey, Willow."]:
    check(f"051 drop: {t!r}", wn.is_wake_phrase_only(t))
for t in ["Hey Willow turn on the lights", "what's the weather", "hey willow, kill the kitchen lights",
          "Willow is my assistant", "install Linux on my laptop", ""]:
    check(f"051 keep: {t!r}", not wn.is_wake_phrase_only(t))

# Review Focus 1/2: metacharacters escaped; a common-word alias stays harmless
CFG["wake"] = {"name": "Ms Smith", "heard_as": ["the", "a.b"]}
check("051 multi-word name drops", wn.is_wake_phrase_only("hey ms  smith."))
check("051 metachar alias is literal (a.b != axb)",
      wn.is_wake_phrase_only("a.b") and not wn.is_wake_phrase_only("axb"))
check("051 common-word alias drops only a bare utterance",
      wn.is_wake_phrase_only("hey the") and not wn.is_wake_phrase_only("turn off the lights"))
check("051 valid_term charset",
      wn.valid_term("Ms Smith") and wn.valid_term("o'neil") and not wn.valid_term("R2 D2")
      and not wn.valid_term("Mr. X") and not wn.valid_term("") and not wn.valid_term("x" * 31))

# live reload: no caching inside the module
CFG["wake"] = {"name": "Jarvis"}
before = wn.is_wake_phrase_only("hey computer")
CFG["wake"] = {"name": "Computer"}
check("051 config change applies with no restart",
      before is False and wn.is_wake_phrase_only("hey computer"))

# model resolution
check("051 stock key resolves to the bundled file",
      wn.resolve_model("hey_jarvis").endswith("hey_jarvis_v0.1.onnx"), wn.resolve_model("hey_jarvis"))
check("051 custom path passes through", wn.resolve_model("models/x.onnx") == "models/x.onnx")
CFG["wake"] = {}
check("051 absent model -> hey_jarvis bundled", wn.resolve_model().endswith("hey_jarvis_v0.1.onnx"))

# hotword = name only
CFG["wake"] = {"model": "models/Hey_Willow_1-0.onnx", "heard_as": ["linux"]}
check("051 hotword is the name only (never an alias)", wn.hotwords() == "Willow", wn.hotwords())
CFG["bias"] = None
check("051 bias default", wn.bias_enabled() is wn.BIAS_DEFAULT)
CFG["bias"] = False
check("051 bias off honoured", wn.bias_enabled() is False)
CFG["bias"] = True
check("051 bias on honoured", wn.bias_enabled() is True)

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
