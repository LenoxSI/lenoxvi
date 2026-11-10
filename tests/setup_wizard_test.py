"""setup_wizard — the first-run setup wizard's server side (issue #59).

The marker file, the host facts, the pure step rules, the /setup/* routes
and the installer contract. Everything runs against a scratch config and
LVI_STATE_DIR (the tracked fixture config, copied); nothing touches the
repo's own state. The browser harness's fixtures come from these routes:
--dump-fixtures rewrites dashboard-ui/e2e/fixtures/*.json, and a contract
check fails when a committed fixture's keys drift from the live responses.
Run: cd tests && ../.venv/bin/python setup_wizard_test.py ../core [--dump-fixtures]
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

DUMP = "--dump-fixtures" in sys.argv
_ARGS = [a for a in sys.argv[1:] if a != "--dump-fixtures"]
CORE = Path(_ARGS[0] if _ARGS else "../core").resolve()
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

# ── e2e fixtures: the fake API answers what these routes answer ──────
# Each scenario is a box state; its fixture holds the real responses to the
# GETs the page makes there, with machine facts swapped for fixed made-up
# values (key sets kept), so the fake API in dashboard-ui/e2e can't drift.
import ipaddress  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

import config as _config  # noqa: E402

E2E_FIXTURES = REPO / "dashboard-ui" / "e2e" / "fixtures"
E2E_GETS = ("setup/probe", "status", "mapping", "tts", "voice-tuning", "intelligence", "intelligence/lanes",
            "keys", "localai", "stt", "oww/models", "wake")
FAKE_HOST = {"arch": "x86_64", "cpus": 8, "ram_mb": 16000, "disk_free_gb": 200, "python": "3.12.3", "slow": False}
FAKE_BOOT = 1762700000.0
SCENARIOS = {  # name: (config source, marker, token)
    "fresh": ("installer", True, None),
    "upgraded": ("fixture", False, None),
    "token": ("installer", True, SENTINEL),
    "slow": ("installer", True, None),
    "manual": ("installer", True, None),
}
# Made-up machine facts per scenario, over FAKE_HOST: a slow box by the probe's
# own rule (aarch64), so the wizard's "Local or Cloud is faster" hints show.
SCENARIO_HOST = {"slow": {"arch": "aarch64", "cpus": 4, "slow": True}}
# The install shape per scenario (installer otherwise): one the bot can't
# restart itself, so the Restart step asks for a restart by hand.
SCENARIO_SHAPE = {"manual": "unknown"}


def _boot_scenario(source, marker, token):
    """A box as a fresh bot process sees it: the config, keys file, marker
    and token, with the boot snapshot taken from this config."""
    CONFIG.unlink(missing_ok=True)
    if source == "installer":  # exactly what the installer writes on a new box
        subprocess.run([sys.executable, str(REPO / "scripts" / "installer" / "write_config.py"),
                        str(REPO / "config.example.yaml"), str(CONFIG), "--host", "192.0.2.10"],
                       check=True, capture_output=True)
    else:
        shutil.copy2(REPO / "tests" / "fixtures" / "config.yaml", CONFIG)
    ENV_FILE.write_text("# suite keys file\n")
    _config._BOOT = None
    _config._load.cache_clear()
    if marker:
        paths.setup_marker().touch()
    else:
        paths.setup_marker().unlink(missing_ok=True)
    if token:
        os.environ["LVI_DASHBOARD_TOKEN"] = token
    else:
        os.environ.pop("LVI_DASHBOARD_TOKEN", None)


def _scrub(path, body):
    """Fixed made-up values for the facts of the machine running the suite:
    host, boot time, uptime, model paths and the stock wake keys (real
    phrases never reach the repo). Keys and types stay as the route sent them."""
    body = json.loads(json.dumps(body))
    if path == "setup/probe":
        body["host"] = {k: FAKE_HOST.get(k) for k in body.get("host") or {}}
        body["boot_time"] = FAKE_BOOT
    if path == "status":
        for k in ("uptime_secs", "boot_time"):
            if isinstance(body.get(k), (int, float)) and not isinstance(body.get(k), bool):
                body[k] = FAKE_BOOT if k == "boot_time" else 120
        if isinstance(body.get("version"), str):
            body["version"] = "v0.0.0-e2e"
    if path == "wake" and isinstance(body.get("name"), str):
        body["name"] = "Test Box"
    if path == "oww/models":
        n = 0
        for m in body.get("models") or []:
            if m.get("source") == "stock":
                n += 1
                m["name"] = f"hey_test_word_{n}"
            if "size" in m:
                m["size"] = 1000000
            if "path" in m:
                m["path"] = f"models/{m['name']}"
    return body


def scenario_responses(name):
    source, marker, token = SCENARIOS[name]
    _boot_scenario(source, marker, token)
    pin_shape(SCENARIO_SHAPE.get(name, "installer"))
    try:
        out = {}
        for p in E2E_GETS:
            r = client.get(f"/api/dashboard/{p}")
            out[f"/api/dashboard/{p}"] = _scrub(p, r.json()) if r.status_code == 200 else {"_status": r.status_code}
        _probe_body = out.get("/api/dashboard/setup/probe") or {}
        if name in SCENARIO_HOST and isinstance(_probe_body.get("host"), dict):
            _probe_body["host"].update({k: v for k, v in SCENARIO_HOST[name].items() if k in _probe_body["host"]})
        return out
    finally:
        os.environ.pop("LVI_DASHBOARD_TOKEN", None)
        pin_shape("installer")


def _shape(v):
    """Keys and JSON types, not values: a list is the set of its items' shapes."""
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return sorted({json.dumps(_shape(x), sort_keys=True) for x in v})
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    return "number" if isinstance(v, (int, float)) else type(v).__name__


def _public_leaks(text):
    """Machine paths, real wake phrases or non-documentation IPs in a fixture."""
    out = [s for s in (str(SCRATCH), str(REPO), str(Path.home()), sys.prefix) if s and s in text]
    out += sorted(p for p in _phrases if re.search(rf"\b{re.escape(p)}\b", text, re.I))
    for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a not in ipaddress.ip_network("192.0.2.0/24") and not a.is_loopback:
            out.append(ip)
    return out


pin_shape("installer")
_live = {name: scenario_responses(name) for name in SCENARIOS}
if DUMP:
    E2E_FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, resp in _live.items():
        doc = {"scenario": name, "generated_by": "tests/setup_wizard_test.py --dump-fixtures", "responses": resp}
        (E2E_FIXTURES / f"{name}.json").write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
        print(f"wrote dashboard-ui/e2e/fixtures/{name}.json")
_fx = {}
for name, resp in _live.items():
    check(f"e2e fixture {name}: every GET answers 200 on the live rig",
          not [p for p, b in resp.items() if "_status" in b], str([p for p, b in resp.items() if "_status" in b]))
    try:
        text = (E2E_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        got = json.loads(text).get("responses") or {}
    except (OSError, ValueError) as e:
        check(f"e2e fixture {name}: committed and readable", False, repr(e))
        continue
    _fx[name] = (text, got)
    check(f"e2e fixture {name}: the same GETs as the live rig", set(got) == set(resp), str(sorted(set(got) ^ set(resp))))
    drift = [p for p in resp if p in got and _shape(got[p]) != _shape(resp[p])]
    check(f"e2e fixture {name}: each response has the live keys and types (rerun with --dump-fixtures)",
          not drift, str(drift))
    check(f"e2e fixture {name}: no machine path, real wake phrase or private IP",
          not _public_leaks(text), str(_public_leaks(text)))
_probe = {n: (_fx[n][1].get("/api/dashboard/setup/probe") or {}) if n in _fx else {} for n in SCENARIOS}
check("e2e fixtures: fresh and token are marked, upgraded is not; only token needs a token",
      [_probe[n].get("pending") for n in ("fresh", "upgraded", "token")] == [True, False, True]
      and [_probe[n].get("token_required") for n in ("fresh", "upgraded", "token")] == [False, False, True],
      str({n: (p.get("pending"), p.get("token_required")) for n, p in _probe.items()}))
check("e2e fixtures: the token value never reaches a fixture", not any(SENTINEL in t for t, _ in _fx.values()))
_slow_host = _probe.get("slow", {}).get("host") or {}
check("e2e fixture slow: a marked box the probe's own rule calls slow (aarch64), the rest of fresh's shape",
      _slow_host.get("slow") is True and _slow_host.get("arch") == "aarch64" and _probe.get("slow", {}).get("pending") is True
      and all(_probe[n].get("host", {}).get("slow") is False for n in ("fresh", "upgraded", "token")), str(_slow_host))
check("e2e fixture manual: a marked box whose install shape can't restart itself (restart manual); the rest auto",
      _probe.get("manual", {}).get("restart") == "manual" and _probe.get("manual", {}).get("pending") is True
      and all(_probe[n].get("restart") == "auto" for n in ("fresh", "upgraded", "token", "slow")),
      str({n: p.get("restart") for n, p in _probe.items()}))
routes._install_shape = _real_shape

# ── repo hygiene ─────────────────────────────────────────────────────
_ignored = (REPO / ".gitignore").read_text().splitlines()
check(".gitignore lists setup.pending (a dev clone's state dir is the repo)",
      "setup.pending" in [ln.strip() for ln in _ignored], "")
# The installer writes the marker the server reads: a rename on either side
# must break this check, not a user's first visit.
_state_sh = (REPO / "scripts" / "installer" / "state.sh").read_text()
_marker_writes = re.findall(r'write_file "\$LVI_STATE/([\w.-]+)" 0644 "\$LVI_USER:\$LVI_USER"', _state_sh)
check("install contract: state.sh writes exactly the marker paths.setup_marker() names",
      _marker_writes == [paths.setup_marker().name], str(_marker_writes))
_setup_src = (CORE / "dashboard" / "setup.py").read_text()
check("setup.py stays framework-free (no FastAPI import)", "fastapi" not in _setup_src.lower())

# ── dashboard ui: the cards the wizard reuses stand alone ────────────
# Source pins (the host has no JS runtime for the React app): the TTS and
# Intelligence cards are their own components, and the shared cards take
# wizard props whose defaults change nothing on the tabs.
APP = (REPO / "dashboard-ui" / "src" / "App.jsx").read_text()


def fn(head):
    """A top-level component's source, up to the next top-level function."""
    i = APP.find(head)
    if i < 0:
        return ""
    j = re.search(r"\n(export default )?function ", APP[i + 1:])
    return APP[i:i + 1 + j.start()] if j else APP[i:]


def rendered(src):
    """The component tags a function's return renders, in order."""
    ret = src[src.find("\n  return ("):]
    return re.findall(r"<([A-Z]\w*)[\s/>]", ret)


TTS_HEAD = "function TtsCard({ tts, reloadAll }) {"
INT_HEAD = "function IntelligenceCard({ intelligence, setMsg, reloadAll }) {"
KEYS_HEAD = "function KeysCard({ setMsg, only }) {"
ROOMS_HEAD = "function Rooms({ status, mapping, setMsg, reloadAll, showZones = true }) {"
VOICE_HEAD = "function Voice({ tts, tuning, status, setMsg, reloadAll }) {"
SYSTEM_HEAD = "function System({ intelligence, status, setMsg, reloadAll, unsaved = 0 }) {"
_tts, _int, _keys, _rooms = fn(TTS_HEAD), fn(INT_HEAD), fn(KEYS_HEAD), fn(ROOMS_HEAD)
_voice, _system, _missing = fn(VOICE_HEAD), fn(SYSTEM_HEAD), fn("function MissingKey({ label }) {")
check("cards: TtsCard, IntelligenceCard, KeysCard(only) and Rooms(showZones) have their wizard signatures",
      all(APP.count(h) == 1 for h in (TTS_HEAD, INT_HEAD, KEYS_HEAD, ROOMS_HEAD)),
      str([h for h in (TTS_HEAD, INT_HEAD, KEYS_HEAD, ROOMS_HEAD) if APP.count(h) != 1]))
check("cards: Voice renders wake word, STT, TTS, tuning — in that order, nothing else",
      rendered(_voice) == ["OwwPanel", "SttCard", "TtsCard", "Tuning"]
      and "<TtsCard tts={tts} reloadAll={reloadAll} />" in _voice, str(rendered(_voice)))
check("cards: System renders Keys, LocalAI, Intelligence, Firmware, Node setup — in that order, nothing else",
      rendered(_system) == ["KeysCard", "LocalAICard", "IntelligenceCard", "FirmwarePanel", "NodeSetupPanel"]
      and "<IntelligenceCard intelligence={intelligence} setMsg={setMsg} reloadAll={reloadAll} />" in _system,
      str(rendered(_system)))
check("cards: Voice and System keep no settings state of their own (it moved with the cards)",
      bool(_voice) and bool(_system) and not any(s in _voice + _system for s in ("useDraft(", "useUnsaved(", "useState(")),
      "")
_reg_voice, _reg_int = "useUnsaved('voice'", "useUnsaved('intelligence'"
check("cards: the TTS and Intelligence unsaved keys register once each, inside their own card",
      APP.count(_reg_voice) == 1 and _reg_voice in _tts and APP.count(_reg_int) == 1 and _reg_int in _int,
      f"voice x{APP.count(_reg_voice)} intelligence x{APP.count(_reg_int)}")
check("cards: the TTS card keeps its section id and the Intelligence card its own, plus its discovery state",
      '<Section id="voice" title="TTS"' in _tts and '<Section id="intelligence" title="Intelligence"' in _int
      and "const [found, setFound] = useState({})" in _int and "const discover = async (kind, url) => {" in _int)
_order = re.search(r"const UNSAVED_ORDER = \[(.*?)\]", APP)
check("cards: UNSAVED_ORDER is unchanged",
      bool(_order) and re.findall(r"'([\w-]+)'", _order.group(1))
      == ["rooms", "wake-name-card", "wake-model", "stt", "voice", "tuning", "localai", "intelligence"],
      _order.group(0) if _order else "missing")
check("cards: KeysCard without `only` shows today's rows and Add box; with it, only those vars and no Add box",
      "const shownRows = (keys || []).filter(only ? r => only.includes(r.var) : shown)" in _keys
      and "{!keys || only ? null : (services.length === 0 && agents.length === 0) ? (" in _keys, "")
check("cards: Rooms mounts ZoneList unless showZones is false (the wizard's Speakers step is the one other mount)",
      "{showZones && <ZoneList mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} />}" in _rooms
      and APP.count("<ZoneList ") == 2, "")
check("cards: MissingKey keeps its #keys link outside the wizard; inside it, a button asks the wizard to show the keys",
      "const WizardCtx = createContext(null)" in APP and "const wizard = useContext(WizardCtx)" in _missing
      and 'href="#keys"' in _missing and "onClick={() => wizard.showKeys()}" in _missing
      and _missing.find("if (wizard)") < _missing.find('href="#keys"'), _missing[:300])

# ── the wizard shell: it replaces the tabs, never sits beside them ───
WIZ_JS = (REPO / "dashboard-ui" / "src" / "setupWizard.js").read_text()
_app = fn("export default function App() {")
_ret = _app[_app.find("\n  return ("):] if "\n  return (" in _app else ""
_head_at, _body_at = _ret.find("{setup || waiting ? null : ("), _ret.find("{setup ? (")
_else_at = _ret.find(") : waiting ? null : (", _body_at) if _body_at >= 0 else -1
_wiz_branch = _ret[_body_at:_else_at] if 0 <= _body_at < _else_at else ""
_tab_branch = _ret[_else_at:] if _wiz_branch else ""
check("shell: wizard mode renders SetupWizard; the tab bodies sit only in the branch that renders once the probe "
      "has answered and the wizard is not showing",
      "<SetupWizard status={status} mapping={mapping} tts={tts} intelligence={intelligence} setMsg={setMsg} "
      "reloadAll={reloadAll}" in _wiz_branch
      and not any(s in _wiz_branch for s in ("data-tab=", "<Overview", "<Rooms", "<Voice", "<System"))
      and all(f'data-tab="{t}"' in _tab_branch for t in ("rooms", "voice", "system")) and "<Overview" in _tab_branch,
      _ret[:200])
check("shell: the header's tab nav and Restart button render only outside the wizard",
      _head_at >= 0 and APP.count("<nav ") == 1 and APP.count("<RestartButton ") == 1
      and _ret.find("<nav ") > _head_at and _ret.find("<RestartButton ") > _head_at
      and _ret.find("<AppMsg") > _ret.find("<RestartButton "), "")
check("shell: the token never rides a URL or a log (no searchParams, no ?token=, no console call naming it)",
      not re.search(r"searchParams|URLSearchParams|[?&]token=", APP + WIZ_JS)
      and not re.search(r"console\.\w+\([^)]*token", APP + WIZ_JS, re.I), "")
check("shell: System ends with the quiet 'Run setup again' link to #setup",
      '<a href="#setup"' in _system and "Run setup again" in _system, _system[-400:])
_again = _system[_system.find('<a href="#setup"'):]
check("shell: 'Run setup again' over unsaved edits asks first (Discard N … and open setup?); Cancel stays put",
      "unsaved={unsaved}" in _tab_branch and "if (unsaved && !window.confirm(" in _again
      and "`Discard ${unsaved} unsaved change${unsaved === 1 ? '' : 's'} and open setup?`" in _again
      and "e.preventDefault()" in _again, _again[:400])

# ── the speech steps: the cards as they are, saved by their own save() ──
_wiz = fn("function SetupWizard({")
_speech = fn("function SpeechStep({")
_save_cards = fn("async function saveCards(cards, units, keys) {")
_bar = fn("function UnsavedBar({ cards, units, setMsg }) {")
check("speech steps: one saveCards() loop, shared by the unsaved bar and the wizard's Save and continue",
      bool(_save_cards) and "await cards.current[k]?.save(" in _save_cards
      and "await saveCards(cards, units, keys)" in _bar and "await cards.current[k]?.save(" not in _bar
      and "await saveCards(cards, units, keys)" in _wiz, _save_cards[:200])
check("speech steps: the wizard keeps its own unsaved-changes registry, so the page bar never lists its cards",
      "<UnsavedCtx.Provider value={putCard}>" in _wiz and "const cards = useRef({})" in _wiz
      and APP.count("<UnsavedCtx.Provider ") == 2, "")
check("speech steps: Listening hosts SttCard and Voice hosts TtsCard, each once, under the tier pick",
      "card={<SttCard status={status} setMsg={setMsg} />}" in _wiz
      and "card={<TtsCard tts={tts} reloadAll={reloadAll} />}" in _wiz
      and APP.count("<SttCard ") == 2 and APP.count("<TtsCard ") == 2, "")
check("speech steps: the tier pick writes nothing; it shows LocalAI + its key (Local) or the cloud keys (Cloud)",
      "const CLOUD_KEYS = ['DEEPGRAM_API_KEY', 'ELEVENLABS_API_KEY']" in APP and "const LOCAL_KEYS = ['LVI_LOCALAI_KEY']" in APP
      and "<KeysCard setMsg={setMsg} only={tier === 'cloud' ? CLOUD_KEYS : LOCAL_KEYS} />" in _speech
      and "{tier === 'local' && <LocalAICard setMsg={setMsg} />}" in _speech
      and "api(" not in _speech, _speech[:300])
check("speech steps: a 'No key' button inside a step scrolls to the step's key box, never to #keys on a tab",
      "<WizardCtx.Provider value={keysCtx}>" in _speech and "scrollToCard('keys')" in _speech, "")
check("speech steps: a slow machine gets the Local or Cloud hint", "Local or Cloud answer faster on this machine." in _speech)
check("speech steps: the narrow-screen wrapper is the wizard's own (cards keep their classes)",
      'className="wizard-cards' in _wiz
      and ".wizard-cards" in (REPO / "dashboard-ui" / "src" / "index.css").read_text(), "")

# ── the intelligence and wake steps ───────────────────────────────────
_intel_step = fn("function IntelligenceStep({")
_summary = fn("function StepSummary({")
check("intelligence step: IntelligenceCard as it is, once, plus only the key row of the lane on screen",
      "<IntelligenceCard intelligence={intelligence} setMsg={setMsg} reloadAll={reloadAll} />" in _intel_step
      and APP.count("<IntelligenceCard ") == 2 and "<KeysCard setMsg={setMsg} only={vars} />" in _intel_step
      and "e.target.id === 'int-kind'" in _intel_step and "<WizardCtx.Provider value={keysCtx}>" in _intel_step,
      _intel_step[:300])
check("intelligence step: its Skip reads 'Not yet' and writes nothing",
      "skipLabel={s.id === STEP_ID.Intelligence ? 'Not yet' : 'Skip'}" in _wiz, "")
check("wake step: OwwPanel as it is (wake name, model, stock licence line), once",
      "[STEP_ID['Wake word']]: <OwwPanel setMsg={setMsg} reloadAll={reloadAll} />" in _wiz
      and APP.count("<OwwPanel ") == 2, "")
check("finish summary: each step done or skipped by the probe, with a way back to it",
      "stepView(probe, s.id)" in _summary and "'skipped'" in _summary and "onGo(s.id)" in _summary
      and "<StepSummary probe={probe} onGo={go} />" in _wiz, _summary[:300])
check("the wizard re-reads the probe on every step, so the summary follows what was saved",
      "}, [step])" in _wiz and "api('/api/dashboard/setup/probe')" in _wiz, "")

# ── the speakers and nodes steps ──────────────────────────────────────
_nodes_step, _poll = fn("function NodesStep({"), fn("function useProbePoll(")
check("speakers step: ZoneList as it is, mounted by the wizard; the nodes step's Rooms leaves it out",
      "<ZoneList mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} />" in _wiz
      and "<Rooms status={status} mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} showZones={false} />"
      in _nodes_step and APP.count("<Rooms ") == 2, _nodes_step[:300])
check("speakers step: Sonos is discovered, a WiiM is added by its address (no WiiM discovery here)",
      "add a WiiM by its IP address" in _wiz, "")
check("nodes step: NodeSetupPanel as it is (its desktop-only message unchanged), once in the wizard",
      "<NodeSetupPanel setMsg={setMsg} />" in _nodes_step and APP.count("<NodeSetupPanel ") == 2
      and "Node setup needs Chrome or Edge on a desktop computer." in APP, "")
check("nodes step: the probe is read every 5 s only while the step is mounted (cleared on unmount)",
      "useProbePoll(p => {" in _nodes_step and "onProbe(p)" in _nodes_step and "setInterval(" in _poll and "}, 5000)" in _poll
      and "return () => clearInterval(t)" in _poll and "setInterval(" not in _wiz, _poll[:300])

# ── the restart-and-test step, Finish, and the leave-page warning ─────
_finish = fn("function FinishStep({")
check("restart step: 'Say …' is wakePhrase() of the selected model from GET /oww/models, never the wake name",
      "wakePhrase(models)" in _finish and "api('/api/dashboard/oww/models')" in _finish
      and "/api/dashboard/wake" not in _finish and ".name" not in _finish, _finish[:300])
check("restart step: 'Heard you in …' only for a wake after the current boot (heardSince over the polled probe)",
      "heardSince(probe)" in _finish and "useProbePoll(" in _finish, "")
check("restart step: restarts through restartBot(); a bot that can't restart itself gets a note and no POST",
      "restartBot(setMsg, setRebooting)" in _finish and "probe?.restart === 'manual'" in _finish
      and "Restart the bot yourself" in _finish and "/api/dashboard/restart" not in _finish, "")
check("restart step: the browser mic link keeps its trailing slash", 'href="/client/"' in _finish, "")
check("restart step: hosted by the wizard; Finish posts /setup/done {skipped: false} and lands on #rooms",
      "<FinishStep " in _wiz and "() => leave(false)" in _wiz and "json: { skipped }" in _wiz
      and "window.location.hash = 'rooms'" in _wiz, "")
check("leave-page warning: the one beforeunload listener also counts the wizard's unsaved edits",
      APP.count("addEventListener('beforeunload'") == 1 and "onUnsaved={setWizardUnsaved}" in _app
      and "const leaving = unsaved + wizardUnsaved" in _app and "onUnsaved(count)" in _wiz, "")

_titles = re.findall(r"\{ id: '\w+', title: '([^']+)'", WIZ_JS)
_dist = [p.read_text(errors="ignore") for p in (REPO / "dashboard-ui" / "dist" / "assets").glob("*.js")]
check("dist: a bundle carries 'Run setup again' and every setup step title (rebuild dist/)",
      len(_titles) == 8 and any("Run setup again" in s and all(t in s for t in _titles) for s in _dist), str(_titles))

# ── the browser harness: dev-side only, on its own port ──────────────
_pkg = json.loads((REPO / "dashboard-ui" / "package.json").read_text())
check("harness: @playwright/test is a devDependency pinned to exactly 1.65.0, run by `npm run e2e`",
      (_pkg.get("devDependencies") or {}).get("@playwright/test") == "1.65.0"
      and "@playwright/test" not in (_pkg.get("dependencies") or {})
      and (_pkg.get("scripts") or {}).get("e2e") == "playwright test", str(_pkg.get("devDependencies")))
_pw_cfg = REPO / "dashboard-ui" / "playwright.config.js"
_pw = _pw_cfg.read_text() if _pw_cfg.exists() else ""
check("harness: the preview server binds 127.0.0.1:4173 (strict, never reused), never a bot port",
      all(s in _pw for s in ("--host 127.0.0.1", "--port 4173", "--strictPort", "reuseExistingServer: false"))
      and not re.search(r"\b(7860|8642|9119)\b", _pw), "")
check("harness: projects desktop-chromium, phone-android and phone-ios (WebKit)",
      all(f"name: '{n}'" in _pw for n in ("desktop-chromium", "phone-android", "phone-ios"))
      and "devices['iPhone 13']" in _pw, "")
_npx = [str(p.relative_to(REPO)) for p in sorted((REPO / "scripts").rglob("*"))
        if p.is_file() and re.search(r"playwright|npx", p.read_text(errors="ignore"), re.I)]
check("users never get the harness: nothing under scripts/ mentions playwright or npx", not _npx, str(_npx))

print()
if FAILS:
    print(f"{len(FAILS)} failed: {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
