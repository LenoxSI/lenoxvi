"""setup_wizard — the first-run setup wizard's server side (issue #59).

The marker file, the host facts, the pure step rules, the /setup/* routes
and the installer contract. Everything runs against a scratch config and
LVI_STATE_DIR (the tracked fixture config, copied); nothing touches the
repo's own state.
Run: cd tests && ../.venv/bin/python setup_wizard_test.py ../core
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

CORE = Path(sys.argv[1] if len(sys.argv) > 1 else "../core").resolve()
REPO = CORE.parent
sys.path.insert(0, str(CORE))
SCRATCH = Path(tempfile.mkdtemp(prefix="lvi-setup-test-"))
# A clean environment: no host token, key or LVI_* override decides a check.
for _k in list(os.environ):
    if _k.startswith("LVI_") or _k.endswith("_API_KEY"):
        os.environ.pop(_k)
os.environ["LVI_STATE_DIR"] = str(SCRATCH / "state")
(SCRATCH / "state").mkdir()
CONFIG = SCRATCH / "config.yaml"
shutil.copy2(REPO / "tests" / "fixtures" / "config.yaml", CONFIG)
os.environ["LVI_CONFIG"] = str(CONFIG)
ENV_FILE = SCRATCH / "state" / ".env"
ENV_FILE.write_text("# suite keys file\n")
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

# ── the /setup/* routes ──────────────────────────────────────────────
# Same rig as test_dashboard.py: the dashboard mounted on a plain FastAPI
# app, an IP-literal Host for the guard, the install shape pinned.
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dashboard  # noqa: E402
from dashboard import routes  # noqa: E402

app = FastAPI()
dashboard.register_dashboard(app)
client = TestClient(app, base_url="http://192.0.2.10", raise_server_exceptions=False)
PROBE = "/api/dashboard/setup/probe"
CHECK = "/api/dashboard/setup/check"
DONE = "/api/dashboard/setup/done"
_real_shape = routes._install_shape


def pin_shape(shape):
    async def _fake():
        return shape
    routes._install_shape = _fake


def disk():
    """config.yaml, .env and the marker, byte for byte."""
    m = paths.setup_marker()
    return CONFIG.read_bytes(), ENV_FILE.read_bytes(), m.read_bytes() if m.exists() else None


pin_shape("installer")
paths.setup_marker().unlink(missing_ok=True)
r = client.get(PROBE)
body = r.json() if r.status_code == 200 else {}
check("probe: no marker (an upgraded install) -> 200, pending false", r.status_code == 200 and body.get("pending") is False,
      f"status={r.status_code} body={r.text[:200]}")
TOP = {"pending", "token_required", "restart", "host", "restart_pending", "boot_time", "last_wake", "steps"}
check("probe: top-level keys exactly as designed", set(body) == TOP, str(sorted(body)))
check("probe: the eight step ids, in order", [s.get("id") for s in body.get("steps", [])] == IDS,
      str([s.get("id") for s in body.get("steps", [])]))
check("probe: host facts are the module's own", set(body.get("host") or {}) == set(facts), str(body.get("host")))
check("probe: installer shape -> restart auto", body.get("restart") == "auto", str(body.get("restart")))
check("probe: boot_time is the registry's", body.get("boot_time") == dashboard.registry.boot_time(),
      str(body.get("boot_time")))
check("probe: the fixture's zones count for the speakers step",
      next((s for s in body.get("steps", []) if s.get("id") == "speakers"), {}).get("done") is True, str(body.get("steps")))
check("probe: finish is done without the marker",
      next((s for s in body.get("steps", []) if s.get("id") == "finish"), {}).get("done") is True, str(body.get("steps")))
paths.setup_marker().touch()
r = client.get(PROBE)
check("probe: with the marker -> pending true, finish not done",
      r.status_code == 200 and r.json().get("pending") is True
      and next(s for s in r.json()["steps"] if s["id"] == "finish")["done"] is False, r.text[:200])
check("probe: no token configured -> token_required false", r.json().get("token_required") is False)
pin_shape("dev")
check("probe: dev shape -> restart auto", client.get(PROBE).json().get("restart") == "auto")
pin_shape("unknown")
check("probe: unknown shape -> restart manual", client.get(PROBE).json().get("restart") == "manual")
pin_shape("installer")

SENTINEL = "suite-sentinel-token-7f3a9c"
os.environ["LVI_DASHBOARD_TOKEN"] = SENTINEL
try:
    r = client.get(PROBE)
    check("probe: open read — a plain GET with no token answers 200 in token mode", r.status_code == 200, str(r.status_code))
    check("probe: token set -> token_required true", r.json().get("token_required") is True, r.text[:200])
    check("probe: the token value appears nowhere in the response", SENTINEL not in r.text)

    before = disk()
    r = client.post(CHECK)
    check("check: no token -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = client.post(CHECK, headers={"X-Dashboard-Token": "wrong-token-value"})
    check("check: wrong token -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = client.post(CHECK, headers={"X-Dashboard-Token": SENTINEL})
    check("check: right token -> 200 {ok: true}", r.status_code == 200 and r.json() == {"ok": True},
          f"{r.status_code} {r.text[:120]}")
    r = client.post(CHECK, headers={"X-Dashboard-Token": SENTINEL, "Origin": "https://evil.example"})
    check("check: a cross-origin request -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
    check("check: no side effects (config, .env and the marker unchanged)", disk() == before)
    r = client.post(DONE, json={}, headers={"X-Dashboard-Token": "wrong-token-value"})
    check("done: guarded — a wrong token is a 401 and the marker stays",
          r.status_code == 401 and paths.setup_marker().exists(), f"{r.status_code}")
finally:
    os.environ.pop("LVI_DASHBOARD_TOKEN", None)

cfg_before, env_before, _ = disk()
r = client.post(DONE, json={"skippd": True})
_locs = [e.get("loc") for e in (r.json().get("detail") if r.status_code == 422 else []) or []]
check("done: a misspelled field -> 422 naming it, marker kept",
      r.status_code == 422 and any("skippd" in (loc or []) for loc in _locs) and paths.setup_marker().exists(),
      f"{r.status_code} {r.text[:200]}")
r = client.post(DONE, json={"skipped": True})
check("done: removes the marker -> 200 {ok: true, pending: false}",
      r.status_code == 200 and r.json() == {"ok": True, "pending": False} and not paths.setup_marker().exists(),
      f"{r.status_code} {r.text[:120]}")
r = client.post(DONE, json={})
check("done: a second call (no marker) is still 200", r.status_code == 200 and r.json() == {"ok": True, "pending": False},
      f"{r.status_code} {r.text[:120]}")
check("done: config.yaml and .env bytes unchanged", disk()[:2] == (cfg_before, env_before))
check("probe: after done -> pending false", client.get(PROBE).json().get("pending") is False)

r = client.put("/api/dashboard/tts", json={"provider": "elevenlabs", "voice_id": "SuiteVoice0123456",
                                           "model": "eleven_flash_v2_5"})
p = client.get(PROBE).json()
voice = next((s for s in p.get("steps", []) if s.get("id") == "voice"), {})
check("probe: after PUT /tts to ElevenLabs with no key, voice is not done and tts waits for a restart",
      r.status_code == 200 and voice.get("done") is False and voice.get("tier") == "cloud"
      and "tts" in p.get("restart_pending", []), f"put={r.status_code} voice={voice} pending={p.get('restart_pending')}")
routes._install_shape = _real_shape

# ── repo hygiene ─────────────────────────────────────────────────────
_ignored = (REPO / ".gitignore").read_text().splitlines()
check(".gitignore lists setup.pending (a dev clone's state dir is the repo)",
      "setup.pending" in [ln.strip() for ln in _ignored], "")
# The installer writes the marker the server reads: a rename on either side
# must break this check, not a user's first visit.
import re  # noqa: E402

_state_sh = (REPO / "scripts" / "installer" / "state.sh").read_text()
_marker_writes = re.findall(r'write_file "\$LVI_STATE/([\w.-]+)" 0644 "\$LVI_USER:\$LVI_USER"', _state_sh)
check("install contract: state.sh writes exactly the marker paths.setup_marker() names",
      _marker_writes == [paths.setup_marker().name], str(_marker_writes))
_setup_src = (CORE / "dashboard" / "setup.py").read_text()
check("setup.py stays framework-free (no FastAPI import)", "fastapi" not in _setup_src.lower())

print()
if FAILS:
    print(f"{len(FAILS)} failed: {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
