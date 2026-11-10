"""setup_wizard — the first-run setup wizard's server side (issue #59).

The marker file, the host facts, the pure step rules, and the installer
contract. Everything runs against a scratch LVI_STATE_DIR; nothing touches
the repo's own state.
Run: cd tests && ../.venv/bin/python setup_wizard_test.py ../core
"""
import os
import sys
import tempfile
from pathlib import Path

CORE = Path(sys.argv[1] if len(sys.argv) > 1 else "../core").resolve()
REPO = CORE.parent
sys.path.insert(0, str(CORE))
SCRATCH = Path(tempfile.mkdtemp(prefix="lvi-setup-test-"))
os.environ["LVI_STATE_DIR"] = str(SCRATCH / "state")
(SCRATCH / "state").mkdir()
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import paths  # noqa: E402  (needs the path insert above)
import wake_name  # noqa: E402
from dashboard import setup  # noqa: E402

# ── the marker ───────────────────────────────────────────────────────
STATE = SCRATCH / "state"
check("setup marker: setup_marker() is <state dir>/setup.pending",
      paths.setup_marker() == STATE / "setup.pending", str(paths.setup_marker()))
check("setup marker: pending() is False with no file", setup.pending() is False)
paths.setup_marker().touch()
check("setup marker: pending() is True once the file exists", setup.pending() is True)
check("setup marker: clear() removes the file and says so", setup.clear() is True and not paths.setup_marker().exists())
try:
    _again = setup.clear()
    check("setup marker: a second clear() returns False and does not raise", _again is False, repr(_again))
except Exception as e:  # pragma: no cover — the failure path
    check("setup marker: a second clear() returns False and does not raise", False, repr(e))
_other = SCRATCH / "other-state"
_other.mkdir()
os.environ["LVI_STATE_DIR"] = str(_other)
check("setup marker: the path follows LVI_STATE_DIR at call time",
      paths.setup_marker() == _other / "setup.pending", str(paths.setup_marker()))
os.environ["LVI_STATE_DIR"] = str(STATE)

# ── host facts ───────────────────────────────────────────────────────
facts = setup.host_facts()
check("host facts: exactly arch, cpus, ram_mb, disk_free_gb, python, slow",
      set(facts) == {"arch", "cpus", "ram_mb", "disk_free_gb", "python", "slow"}, str(sorted(facts)))
check("host facts: no value carries a path", not any("/" in str(v) for v in facts.values()), str(facts))
check("host facts: slow is a bool and follows the arch/core-count rule",
      isinstance(facts.get("slow"), bool)
      and facts.get("slow") == (facts.get("arch") == "aarch64"
                                or (facts.get("cpus") is not None and facts["cpus"] < 4)), str(facts))

# ── step rules (pure) ────────────────────────────────────────────────
STT_OK = [{"name": "whisper", "label": "Whisper base", "tier": "embedded", "available": True},
          {"name": "deepgram", "label": "Deepgram Nova-3", "tier": "cloud", "available": False,
           "reason": "add the key on System · Keys"}]
TTS_OK = [{"name": "kokoro", "label": "Kokoro", "tier": "embedded", "available": True},
          {"name": "elevenlabs", "label": "ElevenLabs", "tier": "cloud", "available": False,
           "reason": "add the key on System · Keys"}]
NO_KEYS = {"hermes": False, "openclaw": False, "nanobot": False}


def states(**over):
    """A fresh keyless box (Whisper + Kokoro, echo, stock wake, nothing
    connected), with `over` replacing any input."""
    base = dict(stt_current={"provider": "whisper", "language": "auto", "custom_language": False},
                stt_providers=STT_OK, tts_provider="kokoro", tts_providers=TTS_OK,
                intelligence={"kind": "stub", "url": None, "label": "echo"}, keys_set=NO_KEYS,
                wake_file_ok=True, wake_custom="", zones={}, capable_rooms=[], rooms=[],
                pending=True, restart_pending=[])
    base.update(over)
    return {s["id"]: s for s in setup.step_states(**base)}


IDS = ["welcome", "listening", "voice", "brain", "wake", "speakers", "nodes", "finish"]
fresh_list = setup.step_states(
    stt_current={"provider": "whisper"}, stt_providers=STT_OK, tts_provider="kokoro", tts_providers=TTS_OK,
    intelligence={"kind": "stub", "url": None, "label": "echo"}, keys_set=NO_KEYS, wake_file_ok=True,
    zones={}, capable_rooms=[], rooms=[], pending=True, restart_pending=[])
check("steps: the eight ids, in order", [s["id"] for s in fresh_list] == IDS, str([s.get("id") for s in fresh_list]))
check("steps: each step has done (bool) and detail (str)",
      all(isinstance(s.get("done"), bool) and isinstance(s.get("detail"), str) for s in fresh_list), str(fresh_list))
fresh = {s["id"]: s for s in fresh_list}
check("steps fresh keyless box: welcome, listening, voice and wake are done",
      all(fresh[i]["done"] for i in ("welcome", "listening", "voice", "wake")), str(fresh))
check("steps fresh keyless box: brain, speakers, nodes and finish are not done",
      not any(fresh[i]["done"] for i in ("brain", "speakers", "nodes", "finish")), str(fresh))
check("steps fresh keyless box: listening/voice carry the provider label and tier",
      fresh["listening"]["detail"] == "Whisper base" and fresh["listening"].get("tier") == "embedded"
      and fresh["voice"]["detail"] == "Kokoro" and fresh["voice"].get("tier") == "embedded", str(fresh))
check("steps fresh keyless box: brain says echo, wake says stock model, nothing connected yet",
      fresh["brain"]["detail"] == "echo" and fresh["wake"]["detail"] == "stock model"
      and fresh["speakers"]["detail"] == "no zones" and fresh["nodes"]["detail"] == "0 online", str(fresh))

s = states(stt_current={"provider": "deepgram"})
check("steps: cloud STT picked without its key (not available) -> listening not done, tier cloud",
      not s["listening"]["done"] and s["listening"].get("tier") == "cloud", str(s["listening"]))
s = states(stt_current={"provider": "nope"})
check("steps: an STT provider missing from the list -> listening not done", not s["listening"]["done"])
s = states(tts_providers=[{**TTS_OK[0], "available": False, "reason": "model files missing"}, TTS_OK[1]])
check("steps: Kokoro model files missing -> voice not done", not s["voice"]["done"], str(s["voice"]))
s = states(tts_provider="elevenlabs")
check("steps: ElevenLabs picked without its key -> voice not done, tier cloud",
      not s["voice"]["done"] and s["voice"].get("tier") == "cloud" and s["voice"]["detail"] == "ElevenLabs",
      str(s["voice"]))

HERMES = {"kind": "hermes", "url": "http://192.0.2.20:8642/v1", "label": "Hermes"}
s = states(intelligence=HERMES, keys_set={**NO_KEYS, "hermes": True})
check("steps: hermes + URL + key -> brain done, detail is the lane label",
      s["brain"]["done"] and s["brain"]["detail"] == "Hermes", str(s["brain"]))
s = states(intelligence=HERMES)
check("steps: hermes + URL, no key -> brain not done", not s["brain"]["done"], str(s["brain"]))
s = states(intelligence={**HERMES, "url": None}, keys_set={**NO_KEYS, "hermes": True})
check("steps: hermes + key, no URL -> brain not done", not s["brain"]["done"], str(s["brain"]))
s = states(intelligence={"kind": "stub", "url": "http://192.0.2.20:8642/v1", "label": "echo"},
           keys_set={k: True for k in NO_KEYS})
check("steps: the stub lane is never done, whatever is saved", not s["brain"]["done"], str(s["brain"]))
s = states(intelligence={**HERMES, "kind": "openclaw"}, keys_set={**NO_KEYS, "hermes": True})
check("steps: the key must be the running kind's own (no cross-kind key)", not s["brain"]["done"], str(s["brain"]))

s = states(wake_file_ok=False)
check("steps: the wake model file missing -> wake not done", not s["wake"]["done"], str(s["wake"]))
s = states(wake_custom="custom_wake.onnx")
check("steps: a custom wake model shows its file name", s["wake"]["done"] and s["wake"]["detail"] == "custom_wake.onnx",
      str(s["wake"]))

s = states(zones={"Room 1": {"ip": "192.0.2.31", "backend": "sonos"}})
check("steps: one Sonos zone -> speakers done, '1 zone'",
      s["speakers"]["done"] and s["speakers"]["detail"] == "1 zone", str(s["speakers"]))
s = states(zones={"Room 1": {"ip": "192.0.2.31"}, "Room 2": {"ip": "192.0.2.32"}})
check("steps: two zones -> '2 zones'", s["speakers"]["detail"] == "2 zones", str(s["speakers"]))
ONLINE = [{"room": "Room 1", "online": True}]
s = states(capable_rooms=["Room 1"], rooms=ONLINE)
check("steps: a built-in-speaker room online, no zones -> speakers done",
      s["speakers"]["done"], str(s["speakers"]))
s = states(capable_rooms=["Room 1"], rooms=[{"room": "Room 1", "online": False}])
check("steps: a built-in-speaker room that is offline does not count", not s["speakers"]["done"], str(s["speakers"]))
s = states(rooms=ONLINE)
check("steps: one room online -> nodes done, '1 online'",
      s["nodes"]["done"] and s["nodes"]["detail"] == "1 online", str(s["nodes"]))
s = states(rooms=[{"room": "Room 1", "online": False}])
check("steps: a known room that is offline -> nodes not done", not s["nodes"]["done"], str(s["nodes"]))

s = states(pending=False)
check("steps: the marker gone -> finish done, 'ready'",
      s["finish"]["done"] and s["finish"]["detail"] == "ready", str(s["finish"]))
s = states(restart_pending=["tts"])
check("steps: saved settings waiting -> finish says restart needed",
      s["finish"]["detail"] == "restart needed", str(s["finish"]))

# The phrase is the user's own (or a stock model's): no detail ever says it.
_phrases = set()
for _k in wake_name.STOCK_MODELS:
    _phrases.add(_k.lower())
    _phrases.update(w for w in _k.lower().split("_") if w not in ("hey", "ok", "okay", "hi"))
_all_details = " ".join(st["detail"].lower() for st in fresh_list)
check("steps: no detail string carries a wake phrase",
      not any(p in _all_details for p in _phrases), _all_details)

# ── repo hygiene ─────────────────────────────────────────────────────
_ignored = (REPO / ".gitignore").read_text().splitlines()
check(".gitignore lists setup.pending (a dev clone's state dir is the repo)",
      "setup.pending" in [ln.strip() for ln in _ignored], "")
_setup_src = (CORE / "dashboard" / "setup.py").read_text()
check("setup.py stays framework-free (no FastAPI import)", "fastapi" not in _setup_src.lower())

print()
if FAILS:
    print(f"{len(FAILS)} failed: {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
