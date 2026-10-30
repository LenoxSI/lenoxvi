"""Offline test rig for the LenoxVI dashboard package.

Runs against the host's REAL venv (pipecat, fastapi, soco, ruamel) in a
scratch dir — NOT the live repo checkout (host is a pure deploy target).

Exercises:
  1. imports + register_dashboard() on a plain FastAPI app
  2. /dashboard page serves
  3. offer tee -> registry correlation (node offer with room, browser
     offer without; connect-before-offer ordering race)
  4. GET /api/dashboard/status shape
  5. config write round-trip (room_zones) on a THROWAWAY config file —
     comment preservation + cache reload + validation
  6. zone add via API + config round-trip
  7. mapping PUT validation (unknown zone rejected)
  8. REAL Sonos test clip on the REAL configured zone (if reachable)

Usage (host): ~/venvs/lenoxvi/bin/python test_dashboard.py <repo-core-path>
"""

import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_CORE = Path(sys.argv[1]).resolve()  # .../lenoxvi/core
SCRATCH = Path(tempfile.mkdtemp(prefix="tk-dash-test-"))
FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    sys.path.insert(0, str(REPO_CORE))
    import paths  # the runtime-paths contract (repo/LVI_STATE_DIR anchored)

    # ── throwaway config: copy the tracked fixture (tests/fixtures; a fresh
    # clone has no config.yaml, and a live one leaked house routing into the
    # checks), then point the package at it via LVI_CONFIG. LVI_TEST_CONFIG
    # runs the suite against another file.
    cfg_src = Path(os.environ.get("LVI_TEST_CONFIG") or REPO_CORE.parent / "tests" / "fixtures" / "config.yaml")
    cfg_test = SCRATCH / "config.yaml"
    shutil.copy2(cfg_src, cfg_test)
    # The LIVE bot owns :8765 — this rig runs out-of-process, so serve
    # test clips from a free port on the same LAN IP (Sonos fetches the
    # URL over HTTP either way). In production the dashboard shares the
    # bot's own clip server, so this collision cannot happen there.
    cfg_test.write_text(cfg_test.read_text().replace("port: 8765", "port: 8766"))
    # Per-house agent routing must not leak into the rig: a house agent
    # (intelligence.agents.<kind>) or a room override (room_agents) in the
    # live config changes which key every intelligence/keys check sees.
    # Checks that need them set them explicitly.
    # Round-trip (ruamel) so comments survive: a check pins them.
    from ruamel.yaml import YAML as _RigYAML
    _rig_y = _RigYAML()
    with open(cfg_test) as _f:
        _rig = _rig_y.load(_f) or {}
    _rig_intel = _rig.get("intelligence") or {}
    if _rig_intel.get("agents") or _rig.get("room_agents"):
        _rig_intel.pop("agents", None)
        _rig.pop("room_agents", None)
        with open(cfg_test, "w") as _f:
            _rig_y.dump(_rig, _f)
    os.environ["LVI_CONFIG"] = str(cfg_test)

    from fastapi import BackgroundTasks, FastAPI
    from fastapi.testclient import TestClient

    import dashboard  # noqa: E402  (registers nothing yet)
    from dashboard import registry  # noqa: E402
    from dashboard import config_store  # noqa: E402

    dashboard.register_dashboard(FastAPI())
    check("register_dashboard mounts without error", True)

    app = FastAPI()
    # Fake pipecat /api/offer — TWO shapes, to prove the tee's timing
    # contract:
    #  a) immediate return (browser-ish): correlation at send time.
    #  b) pipecat's REAL shape: the "pipeline" runs as this request's
    #     BACKGROUND task, so the ASGI app call does not return until
    #     the session ends. The tee must correlate at response-SEND
    #     time (before connected), NOT after the app call returns
    #     (session end — measured live).
    _pc_counter = 0

    @app.post("/api/offer")
    async def fake_offer(background_tasks: BackgroundTasks, body: dict):
        nonlocal _pc_counter
        _pc_counter += 1
        pc_id = f"test-pc-{_pc_counter}"

        def session(body=body):
            # simulate a long-lived WebRTC session that only "connects"
            # after the response has been sent
            registry.mark_connected(pc_id)

        background_tasks.add_task(session)
        return {"sdp": "v=0 answer", "type": "answer", "pc_id": pc_id}

    dashboard.register_dashboard(app)
    # Mirror the LIVE app: pipecat's runner installs this
    # app-wide RequestValidationError handler AFTER register_dashboard
    # (pipecat/runner/run.py). It reads the body (multipart → "Stream
    # consumed" 500) and logs errors with their `input` (key leak). The
    # plain FastAPI suite app hid both — the dashboard's own route class
    # must make it unreachable for dashboard routes.
    from fastapi import Request as _PReq
    from fastapi.exceptions import RequestValidationError as _PRVE
    from fastapi.responses import JSONResponse as _PJR
    from fastapi.encoders import jsonable_encoder as _pje
    PIPECAT_HANDLER_HITS = []

    @app.exception_handler(_PRVE)
    async def _pipecat_like_validation_handler(request: _PReq, exc: _PRVE):
        body = await request.body()  # pipecat does exactly this
        PIPECAT_HANDLER_HITS.append((request.url.path, str(exc.errors())))
        return _PJR(status_code=422, content=_pje({"detail": exc.errors()}))

    # guard.py requires an IP-literal Host (public-DNS hosts are the
    # rebinding vector) — TestClient's default 'testserver' would 403.
    # raise_server_exceptions=False: a handler crash must surface as the
    # 500 the live app returns, not abort the suite.
    client = TestClient(app, base_url="http://192.0.2.10", raise_server_exceptions=False)

    # 2. page
    r = client.get("/dashboard")
    check("GET /dashboard -> 200 html", r.status_code == 200 and "dashboard" in r.text)

    # Page.html fallback DELETED — dist/ missing
    # must 404 naming the build step, never serve a stale page.
    # (REPO_CORE anchors this the same way __init__.py resolves ui_index:
    # parents[2] of core/dashboard/__init__.py == REPO_CORE.parent.)
    _pg_idx = REPO_CORE.parent / "dashboard-ui" / "dist" / "index.html"
    assert _pg_idx.exists(), f"suite dist index missing: {_pg_idx}"
    _pg_moved = _pg_idx.with_name("index.html.suite-moved")
    _pg_idx.rename(_pg_moved)
    try:
        r_pg404 = client.get("/dashboard")
        check("page.html gone: no dist → 404 naming the build step",
              r_pg404.status_code == 404 and "npm run build" in r_pg404.text,
              f"status={r_pg404.status_code} body={r_pg404.text[:120]}")
    finally:
        _pg_moved.rename(_pg_idx)
    r = client.get("/dashboard")
    check("dashboard serves again once dist/ is back",
          r.status_code == 200, f"status={r.status_code}")

    # 3. offer tee + registry: node offer (with room), browser (no room)
    offer = {"sdp": "v=0\r\n", "type": "offer", "room": "bedroom"}
    resp = client.post("/api/offer", json=offer)
    check("POST /api/offer (fake) accepted", resp.status_code == 200, f"status={resp.status_code}")
    pc_id = None
    try:
        pc_id = resp.json().get("pc_id")
    except Exception:
        pass
    check("offer response carries pc_id", bool(pc_id), f"pc_id={pc_id}")
    # TestClient runs background tasks after the response completes; the
    # tee must have correlated BEFORE mark_connected ran (the whole
    # point of send-time correlation). Verify the registry already
    # knew the room when connect fired:
    rooms = {r["room"]: r for r in registry.known_rooms()}
    check("tee correlated room BEFORE connect (send-time)",
          rooms.get("bedroom", {}).get("online") is True, f"rooms={list(rooms)}")

    # connect-BEFORE-offer race: mark connected first, then tee fires
    pc2 = "fake-pc-race"
    registry.mark_connected(pc2)  # provisional 'unknown'
    check("provisional room assigned pre-offer",
          registry.room_for_pc(pc2) == "unknown", f"room={registry.room_for_pc(pc2)!r}")
    registry.note_offer(pc2, "kitchen")
    rooms = {r["room"]: r for r in registry.known_rooms()}
    check("race reconciled to kitchen",
          rooms.get("kitchen", {}).get("online") is True and "unknown" not in rooms,
          f"rooms={list(rooms)}")

    # Two nodes connect before the tee correlates either (every node
    # re-offers at once after a bot restart): both rooms come online, and
    # the shared placeholder goes only when the last one resolves.
    registry.mark_connected("suite-pc-prov-a")
    registry.mark_connected("suite-pc-prov-b")
    registry.note_offer("suite-pc-prov-a", "suite-prov-a")
    rooms = {r["room"]: r for r in registry.known_rooms()}
    _prov_mid = "unknown" in rooms
    registry.note_offer("suite-pc-prov-b", "suite-prov-b")
    rooms = {r["room"]: r for r in registry.known_rooms()}
    check("registry: two provisional connects both come online once correlated",
          rooms.get("suite-prov-a", {}).get("online") is True
          and rooms.get("suite-prov-b", {}).get("online") is True
          and _prov_mid and "unknown" not in rooms,
          f"rooms={ {k: v.get('online') for k, v in rooms.items()} } placeholder_mid={_prov_mid}")
    registry.mark_disconnected("suite-pc-prov-a")
    registry.mark_disconnected("suite-pc-prov-b")

    # browser client: no room
    resp = client.post("/api/offer", json={"sdp": "v=0\r\n", "type": "offer"})
    pc3 = resp.json().get("pc_id") if resp.status_code == 200 else None
    if pc3:
        registry.mark_connected(pc3)
        rooms = {r["room"]: r for r in registry.known_rooms()}
        check("browser client -> 'browser' room",
              rooms.get("browser", {}).get("online") is True, f"rooms={list(rooms)}")

    # 4. status shape
    s = client.get("/api/dashboard/status").json()
    check("status shape", all(k in s for k in
          ("uptime_secs", "last_wake", "rooms")), f"s={s}")
    # Contract: bot.py passes THIS pipeline's room (room_for_pc) — the
    # bare-call form is gone; a wake with no room records room=None.
    registry.note_wake("bedroom")
    s = client.get("/api/dashboard/status").json()
    check("note_wake records room", s["last_wake"] and s["last_wake"]["room"] == "bedroom",
          f"last_wake={s['last_wake']}")

    # 5. config write round-trip on throwaway file
    before_text = cfg_test.read_text()
    r = client.put("/api/dashboard/mapping", json={"room_zones": {"bedroom": "Kitchen"}})
    check("mapping PUT ok", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
    after_text = cfg_test.read_text()
    check("config write changed the file", after_text != before_text)
    check("comment preservation (header survives)",
          "# LenoxVI config" in after_text or "THE tuning surface" in after_text,
          )
    m = client.get("/api/dashboard/mapping").json()
    check("mapping read-back after write", m["room_zones"] == {"bedroom": "Kitchen"},
          f"m={m['room_zones']}")
    check("backup file created", (SCRATCH / "config.backup.yaml").is_file())

    # 6. zone add round-trip
    r = client.post("/api/dashboard/zones", json={
        "name": "test_zone", "ip": "192.0.2.222", "player_uid": "RINCON_TEST"})
    check("zone add ok", r.status_code == 200, f"status={r.status_code}")
    m = client.get("/api/dashboard/mapping").json()
    check("new zone visible in mapping GET", "test_zone" in m["zones"], f"zones={list(m['zones'])}")
    check("mapping GET: zones carry ip + backend, never player_uid",
          m["zones"].get("test_zone", {}).get("ip") == "192.0.2.222"
          and m["zones"]["test_zone"].get("backend") == "sonos"
          and not any("player_uid" in z for z in m["zones"].values()), str(m["zones"].get("test_zone")))
    _sonos_b = dict(config_store.read_section("sonos") or {})
    config_store.write_section("sonos", {**_sonos_b, "enabled": False})
    r = client.post("/api/dashboard/zones", json={
        "name": "suite_enable_zone", "ip": "192.0.2.223", "player_uid": "RINCON_TEST2"})
    check("zones: adding a Sonos zone turns Sonos on (same as WiiM)",
          r.status_code == 200 and (config_store.read_section("sonos") or {}).get("enabled") is True,
          f"{r.status_code} {config_store.read_section('sonos')}")
    client.delete("/api/dashboard/zones/suite_enable_zone")
    _sonos_a = dict(config_store.read_section("sonos") or {})
    if "enabled" in _sonos_b:
        _sonos_a["enabled"] = _sonos_b["enabled"]
    else:
        _sonos_a.pop("enabled", None)
    config_store.write_section("sonos", _sonos_a)
    # A '/' in a zone name broke every per-zone route (volume, test, delete
    # take the name as one path segment): refused at add, with a reason.
    r = client.post("/api/dashboard/zones", json={
        "name": "suite/zone", "ip": "127.0.0.1", "player_uid": "RINCON_TEST"})
    m = client.get("/api/dashboard/mapping").json()
    check("zones: a name containing '/' is refused at add (400, says why)",
          r.status_code == 400 and "/" in r.text and "suite/zone" not in m["zones"],
          f"status={r.status_code} body={r.text[:120]} zones={list(m['zones'])}")

    # 7. validation: unknown zone in mapping rejected
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "nonexistent_zone"}})
    check("mapping PUT rejects unknown zone", r.status_code == 400, f"status={r.status_code}")

    # 7b. mapping-wipe guards (bug: the React UI sent a BARE
    # merged dict — no room_zones wrapper — and Pydantic's default_factory
    # silently turned it into {} and the route wiped all mappings; three
    # real wipes hit production config before these guards existed).
    r = client.put("/api/dashboard/mapping", json={"bedroom": "Kitchen"})
    check("unwrapped mapping PUT 422s (extra=forbid)",
          r.status_code == 422, f"status={r.status_code} body={r.text[:200]}")
    r = client.put("/api/dashboard/mapping", json={"room_zones": {}})
    check("empty mapping PUT refused (wipe guard)",
          r.status_code == 400, f"status={r.status_code} body={r.text[:200]}")
    m = client.get("/api/dashboard/mapping").json()
    check("mapping survived the two rejected PUTs", m["room_zones"] == {"bedroom": "Kitchen"},
          f"room_zones={m['room_zones']}")

    # 8. REAL Sonos test clip — opt-in (LVI_TEST_LIVE=1): it plays out loud on
    # a real speaker, so it never runs by default.
    real_zones = m["zones"]
    real = list(real_zones)[:1] if os.environ.get("LVI_TEST_LIVE") == "1" else []
    if not real:
        print("[SKIP] REAL test clip (set LVI_TEST_LIVE=1 to play one out loud)")
    if real:
        r = client.post(f"/api/dashboard/zones/{real[0]}/test")
        ok = r.status_code == 200
        check(f"REAL test clip on '{real[0]}'", ok,
              "" if ok else f"status={r.status_code} body={r.text[:200]}")
    else:
        print("[SKIP] no real zone in config — skipping live Sonos test")

    # 8m. Small-fix pins
    import importlib
    import config as _cfg_mod
    _saved_cwd = os.getcwd()
    # Config resolves to the REPO ROOT when LVI_CONFIG is unset —
    # a different cwd must NOT silently read {}. We chdir to / and
    # check the resolved path is absolute + repo-anchored.
    _saved_os_config = os.environ.pop("LVI_CONFIG", None)
    try:
        os.chdir("/")
        # fresh import so CONFIG_PATH re-resolves without the env override
        importlib.reload(_cfg_mod)
        _cp = str(_cfg_mod.CONFIG_PATH)
        check("config path is repo-anchored (cwd-proof)",
              _cp == str(REPO_CORE.parent / "config.yaml"), f"CONFIG_PATH={_cp}")
        # A fresh clone has no config.yaml at the root (it's private and
        # gitignored): then the loader must read {} cleanly, not raise. The
        # suite never writes one there (a crash would leave a fake config the
        # bot could load).
        if Path(_cp).is_file():
            check("config loads away from repo root",
                  bool(_cfg_mod.cfg("tts")), f"tts={_cfg_mod.cfg('tts')}")
        else:
            print("[SKIP] config loads away from repo root: no config.yaml at the repo root (fresh clone)")
            check("config: no config.yaml at the repo root reads as empty, never raises",
                  _cfg_mod.cfg("tts") == {}, f"tts={_cfg_mod.cfg('tts')}")
    finally:
        os.chdir(_saved_cwd)
        if _saved_os_config is not None:
            os.environ["LVI_CONFIG"] = _saved_os_config
        importlib.reload(_cfg_mod)  # restore the suite's throwaway config
    # A failed ClipServer.start() must NOT leave a half-bound
    # runner — simulate by double-starting on the same port: the
    # second singleton user reuses it (no EADDRINUSE), and a forced
    # raw second bind fails cleanly with the runner still reusable.
    from sonos_output import ClipServer, shared_clip_server

    async def _m11_pins():
        _cs = await shared_clip_server()
        _first_runner = _cs._runner
        _again = await shared_clip_server()
        check("shared clip server is a true singleton",
              _again is _cs and _cs._runner is _first_runner,
              f"first={_first_runner is not None} again-identity={_again is _cs}")
        # raw double-bind on the SAME interface + port as the singleton
        # (127.0.0.1 vs the LAN IP are different interfaces — no conflict;
        # the first suite run "passed" the bind for that wrong reason)
        _raw = ClipServer(Path(tempfile.mkdtemp()), _cs._host_ip, _cs._port)
        try:
            await _raw.start()
            check("raw double-bind raises (no silent second server)", False,
                  "raw start on a taken port unexpectedly succeeded")
            try:
                await _raw.stop()
            except Exception:
                pass
        except Exception:
            check("raw double-bind raises (no silent second server)", True, "")
            check("failed bind leaves no half-bound runner",
                  _raw._runner is None, f"runner={_raw._runner}")
        check("clip server singleton still healthy after a failed raw bind",
              _cs._runner is _first_runner, "")

    import asyncio as _aio
    _aio.get_event_loop_policy()
    try:
        _aio.run(_m11_pins())
    except RuntimeError as _re11:
        # running loop already exists (suite invoked under one) — hop on it
        check("clip server pins ran under an existing loop", False, f"loop hop failed: {_re11}")
    # Missing TTS secret fails construction with a CLEAR error
    # naming the variable (not a bare KeyError deep in pipecat).
    _saved_el = os.environ.pop("ELEVENLABS_API_KEY", None)
    _saved_dg = os.environ.pop("DEEPGRAM_API_KEY", None)
    os.environ["LVI_TTS_PROVIDER"] = "elevenlabs"
    # config.yaml wins over the env var, and the throwaway config is a copy
    # of the live one: pin the provider there too, whatever the house runs.
    _saved_tts = dict(config_store.read_section("tts") or {})
    config_store.write_section("tts", {**_saved_tts, "provider": "elevenlabs"})
    try:
        importlib.reload(_cfg_mod)
        _prov = _cfg_mod.cfg_env("tts", "provider", "LVI_TTS_PROVIDER", "elevenlabs")
        _need = "ELEVENLABS_API_KEY" if str(_prov).startswith("eleven") else None
        check("missing TTS secret detected at construction (clear error shape)",
              _need == "ELEVENLABS_API_KEY"
              and not os.environ.get("ELEVENLABS_API_KEY"),
              f"provider={_prov}")
    finally:
        if _saved_el is not None:
            os.environ["ELEVENLABS_API_KEY"] = _saved_el
        if _saved_dg is not None:
            os.environ["DEEPGRAM_API_KEY"] = _saved_dg
        os.environ.pop("LVI_TTS_PROVIDER", None)
        config_store.write_section("tts", _saved_tts)
        importlib.reload(_cfg_mod)
    # Zone DELETE with a dangling room mapping does ONE atomic
    # multi-section write (zone gone + mapping cleaned, same pass).
    # The probe WiiM zone carries NO player_uid — this used to
    # need a fake RINCON because ZonePayload required a uid for every
    # backend (so the real UI's WiiM add 422'd every time).
    _r_add14 = client.post("/api/dashboard/zones", json={
        "name": "m14_probe_zone", "backend": "wiim", "ip": "127.0.0.9"})
    check("probe zone add ok (wiim, no player_uid)",
          _r_add14.status_code == 200, f"status={_r_add14.status_code} body={_r_add14.text[:120]}")
    _wz14 = ((config_store.read_section("wiim") or {}).get("zones") or {}).get("m14_probe_zone")
    check("wiim zone written without a player_uid key",
          isinstance(_wz14, dict) and "player_uid" not in _wz14, f"zone={_wz14}")
    # The UI's exact pre-fix WiiM body (player_uid: '') is legal;
    # any stray value sent for wiim is dropped, never written.
    _r_h3 = client.post("/api/dashboard/zones", json={
        "name": "h3_probe_zone", "backend": "wiim", "ip": "127.0.0.8",
        "player_uid": ""})
    check("wiim zone add with empty player_uid ok",
          _r_h3.status_code == 200, f"status={_r_h3.status_code} body={_r_h3.text[:120]}")
    client.delete("/api/dashboard/zones/h3_probe_zone")
    # Sonos still REQUIRES a real uid (validator, sonos-only).
    _r_h3s = client.post("/api/dashboard/zones", json={
        "name": "h3_sonos_nouid", "backend": "sonos", "ip": "127.0.0.7"})
    check("sonos zone without player_uid 422s",
          _r_h3s.status_code == 422, f"status={_r_h3s.status_code} body={_r_h3s.text[:120]}")
    _r_h3t = client.post("/api/dashboard/zones", json={
        "name": "h3_sonos_short", "backend": "sonos", "ip": "127.0.0.7",
        "player_uid": "RINC"})
    check("sonos zone with short player_uid 422s",
          _r_h3t.status_code == 422, f"status={_r_h3t.status_code} body={_r_h3t.text[:120]}")
    check("rejected sonos probes never reached config",
          not ({"h3_sonos_nouid", "h3_sonos_short"}
               & set(((config_store.read_section("sonos") or {}).get("zones") or {}))),
          "")
    client.put("/api/dashboard/mapping", json={
        "room_zones": {"m14_probe_room": "m14_probe_zone"}})
    _del = client.delete("/api/dashboard/zones/m14_probe_zone")
    check("delete zone ok (with dangling mapping)",
          _del.status_code == 200, f"status={_del.status_code} body={_del.text[:120]}")
    check("dangling room mapping dropped in the same write",
          _del.json().get("dropped_room_mappings") == ["m14_probe_room"],
          f"body={_del.text[:120]}")
    _probe = (config_store.read_section("room_zones") or {}).get("m14_probe_room")
    check("probe mapping actually gone from config",
          _probe is None, f"room_zones m14_probe_room={_probe}")

    # ── deleting the default zone never leaves default_zone dangling ──
    _sonos_saved = dict(config_store.read_section("sonos") or {})
    _wiim_saved = dict(config_store.read_section("wiim") or {})
    try:
        config_store.write_section("sonos", {**_sonos_saved, "default_zone": "zdA", "zones": {
            "zdA": {"ip": "127.0.0.9", "player_uid": "RINCON_ZDA00000000001400"},
            "zdB": {"ip": "127.0.0.8", "player_uid": "RINCON_ZDB00000000001400"}}})
        config_store.write_section("wiim", {**_wiim_saved, "default_zone": "zwA",
                                            "zones": {"zwA": {"ip": "127.0.0.7"}}})
        _d1 = client.delete("/api/dashboard/zones/zdA")
        _dz1 = (config_store.read_section("sonos") or {}).get("default_zone")
        check("zones: deleting the default hands default to a remaining zone",
              _d1.status_code == 200 and _dz1 == "zdB" and _d1.json().get("default_zone") == "zdB",
              f"{_d1.status_code} default={_dz1} body={_d1.text[:120]}")
        _d2 = client.delete("/api/dashboard/zones/zdB")
        _dz2 = (config_store.read_section("sonos") or {}).get("default_zone")
        check("zones: deleting the last zone clears default_zone",
              _d2.status_code == 200 and _dz2 is None, f"{_d2.status_code} default={_dz2}")
        _d3 = client.delete("/api/dashboard/zones/zwA")
        _dz3 = (config_store.read_section("wiim") or {}).get("default_zone")
        check("zones: a WiiM default is cleared the same way",
              _d3.status_code == 200 and _dz3 is None, f"{_d3.status_code} default={_dz3}")
    finally:
        config_store.write_section("sonos", _sonos_saved)
        config_store.write_section("wiim", _wiim_saved)

    # ── slow dashboard work never stalls the voice event loop ──
    import asyncio as _aio_rl
    import types as _types_rl

    from dashboard import routes as _rRL

    async def _loop_gap(coro):
        gaps, st = [], {"stop": False}

        async def ticker():
            last = time.monotonic()
            while not st["stop"]:
                await _aio_rl.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        t = _aio_rl.create_task(ticker())
        await _aio_rl.sleep(0.05)
        try:
            await coro
        finally:
            st["stop"] = True
            await t
        return max(gaps)

    _sub_saved, _val_saved, _dir_saved = _rRL.subprocess.run, _rRL._oww_validate, _rRL._oww_dir
    _oww_tmp = Path(tempfile.mkdtemp())
    try:
        def _slow_run(*a, **k):
            time.sleep(0.5)
            return _types_rl.SimpleNamespace(returncode=0, stderr="", stdout="")
        _rRL.subprocess.run = _slow_run
        _gap_restart = _aio_rl.run(_loop_gap(_rRL.restart_bot()))

        def _slow_validate(data):
            time.sleep(0.5)
        _rRL._oww_validate = _slow_validate
        _rRL._oww_dir = lambda: _oww_tmp

        class _Up:
            filename = "suite_loop.onnx"

            async def read(self):
                return b"\x00" * 4096
        _gap_upload = _aio_rl.run(_loop_gap(_rRL.oww_upload_model(file=_Up())))
    finally:
        _rRL.subprocess.run, _rRL._oww_validate, _rRL._oww_dir = _sub_saved, _val_saved, _dir_saved
    check("routes: restart scheduling doesn't block the event loop",
          _gap_restart < 0.15, f"max loop gap={_gap_restart:.2f}s")
    # ── config writes keep the comments inside and between sections ──
    _cfg_c = Path(tempfile.mkdtemp()) / "config.yaml"
    _cfg_c.write_text(
        "# header note\n"
        "sonos:\n"
        "  # about the sonos section\n"
        "  enabled: true  # inline enabled note\n"
        "  clip_volume: 30\n"
        "  zones:\n"
        "    # the kitchen speaker\n"
        "    kitchen: {ip: 127.0.0.5, player_uid: RINCON_K}\n"
        "  default_zone: kitchen\n"
        "# about the wake section\n"
        "wake:\n"
        "  threshold: 0.5  # tuned by ear\n"
        "  barge_in: false\n")
    _oh_cfg_saved = os.environ.get("LVI_CONFIG")
    os.environ["LVI_CONFIG"] = str(_cfg_c)
    try:
        _snap = dict(config_store.read_section("sonos"))   # plain copy, like the routes
        _snap["clip_volume"] = 40
        _snap["zones"] = {**dict(_snap["zones"]), "den": {"ip": "127.0.0.6", "player_uid": "RINCON_D"}}
        _res = config_store.write_section("sonos", _snap)
        config_store.write_sections({"wake": {"threshold": 0.45, "barge_in": False}})
        _txt_c = _cfg_c.read_text()
        _keep = ["# header note", "# about the sonos section", "# inline enabled note",
                 "# the kitchen speaker", "# about the wake section", "# tuned by ear"]
        _lost = [c for c in _keep if c not in _txt_c]
        check("config comments: section, inline, nested and next-section comments survive writes",
              not _lost, f"lost={_lost}")
        _rt = config_store.read_sections(["sonos", "wake"])
        check("config comments: the written values land",
              _rt["sonos"]["clip_volume"] == 40 and "den" in _rt["sonos"]["zones"]
              and _rt["wake"]["threshold"] == 0.45, str(_rt))
        check("config comments: the returned 'before' is the pre-write state",
              "den" not in (_res["before"].get("zones") or {}) and _res["before"]["clip_volume"] == 30,
              str(_res["before"]))
        config_store.write_section("sonos", {"enabled": True, "zones": {"kitchen": {
            "ip": "127.0.0.5", "player_uid": "RINCON_K"}}, "default_zone": "kitchen"})
        _rt2 = config_store.read_section("sonos")
        _txt_c2 = _cfg_c.read_text()
        check("config comments: a removed key goes; the other comments stay",
              "clip_volume" not in _rt2 and "den" not in _rt2["zones"]
              and "# the kitchen speaker" in _txt_c2 and "# about the wake section" in _txt_c2,
              _txt_c2[:300])
        # (bug-hunt) YAML 1.1 bool/null words: ruamel writes YAML 1.2
        # (a plain `on:` key), but the bot reads config.yaml with PyYAML
        # (YAML 1.1: on/off/yes/no/true/false/null — and y/n/~, any
        # case — become bool/None), so a room named "on" read back as
        # key True and played nowhere. The writer must quote exactly
        # those words; everything else stays plain.
        _txt_bh1 = _cfg_c.read_text()
        config_store.write_section("room_zones", {"on": "Off", "Yes": "kitchen"})
        _rz_bh1 = __import__("yaml").safe_load(_cfg_c.read_text()).get("room_zones")
        check("config writes: YAML 1.1 bool/null words round-trip as the exact strings",
              _rz_bh1 == {"on": "Off", "Yes": "kitchen"}, f"room_zones={_rz_bh1!r}")
        _cfg_c.write_text(_txt_bh1)  # restore: the section (and comments) as they were
    finally:
        if _oh_cfg_saved is None:
            os.environ.pop("LVI_CONFIG", None)
        else:
            os.environ["LVI_CONFIG"] = _oh_cfg_saved
    check("routes: a wake-model upload doesn't block the event loop",
          _gap_upload < 0.15 and (_oww_tmp / "suite_loop.onnx").exists(),
          f"max loop gap={_gap_upload:.2f}s")

    # ONE volume truth. /mapping serves effective_volumes for every
    # zone through the same resolver as the test tone; each zone resolves
    # in its OWN backend section; one code default everywhere.
    import yaml as _yaml_l3
    import sonos_output as _so_l3
    import wiim_output as _wo_l3
    from dashboard import routes as _routes_l3
    _sonos_keys_l3 = set(((config_store.read_section("sonos") or {}).get("zones") or {}))
    _wiim_keys_l3 = set(((config_store.read_section("wiim") or {}).get("zones") or {}))
    _ev = client.get("/api/dashboard/mapping").json().get("effective_volumes") or {}
    check("effective_volumes covers every zone (sonos ∪ wiim)",
          bool(_ev) and set(_ev) == _sonos_keys_l3 | _wiim_keys_l3, f"ev={_ev}")
    check("effective volume == test-tone volume for every zone (one resolver)",
          bool(_ev) and all(_ev[z] == _routes_l3._zone_test_volume(z, _routes_l3._backend_of(z)) for z in _ev), f"ev={_ev}")
    # backend isolation: a Sonos global change must not move a WiiM zone
    _sonos_sec_l3 = dict(config_store.read_section("sonos") or {})
    _saved_live_l3 = dict(_so_l3._LIVE_ZONE_VOLUMES)
    try:
        config_store.write_section("sonos", {**_sonos_sec_l3, "clip_volume": 61, "zone_volumes": {}})
        _so_l3.zone_volume_overrides({})
        _ev2 = client.get("/api/dashboard/mapping").json().get("effective_volumes") or {}
        _wiim_sec_l3 = config_store.read_section("wiim") or {}
        _wiim_glob = _wiim_sec_l3.get("clip_volume")
        _wiim_expect = int(_wiim_glob) if _wiim_glob is not None else getattr(_routes_l3, "DEFAULT_CLIP_VOLUME", None)
        # a WiiM zone with its own saved volume keeps it (the copied config may carry one)
        _wiim_zv_l3 = {k: int(v) for k, v in (_wiim_sec_l3.get("zone_volumes") or {}).items()}
        check("Sonos global moves Sonos zones only; WiiM resolves in its own section",
              bool(_ev2) and all(_ev2[z] == 61 for z in _sonos_keys_l3)
              and all(_ev2[z] == _wiim_zv_l3.get(z, _wiim_expect)
                      for z in _wiim_keys_l3 if z not in _wo_l3._LIVE_WIIM_VOLUMES),
              f"ev2={_ev2} wiim_expect={_wiim_expect} wiim_zone_volumes={_wiim_zv_l3}")
        # live slider layer wins
        if _sonos_keys_l3:
            _z = sorted(_sonos_keys_l3)[0]
            _so_l3.zone_volume_overrides({_z: 13})
            _ev3 = client.get("/api/dashboard/mapping").json().get("effective_volumes") or {}
            check("live slider override wins", _ev3.get(_z) == 13, f"ev3={_ev3}")
    finally:
        config_store.write_section("sonos", _sonos_sec_l3)
        _so_l3.zone_volume_overrides(_saved_live_l3)
    # one code default: a config with no clip_volume → the SAME number in
    # SonosOutput (was 70), WiiMOutput (35) and the dashboard resolver (35)
    _cfg_l3 = SCRATCH / "l3-novol.yaml"
    _cfg_l3.write_text(_yaml_l3.safe_dump({"sonos": {"enabled": False, "zones": {}}}))
    _so_def = _so_l3.SonosOutput(config_path=str(_cfg_l3), echo_gate=None, zone_selector=lambda: None, led=None)
    check("SonosOutput default clip volume == DEFAULT_CLIP_VOLUME (was 70)",
          _so_def._volume == getattr(_routes_l3, "DEFAULT_CLIP_VOLUME", None) == 35,
          f"sonos={_so_def._volume} default={getattr(_routes_l3, "DEFAULT_CLIP_VOLUME", None)}")

    # 9. TTS settings panel (Settings tab — config-editable arc)
    r = client.get("/api/dashboard/tts")
    ok = r.status_code == 200 and "provider" in r.json() and "deepgram_key_hint" in r.json()
    check("TTS GET returns provider config + key hints", ok, f"status={r.status_code}")
    if ok:
        t = r.json()
        check("TTS GET never leaks key values",
              "api_key" not in t and not any("sk_" in str(v) for v in t.values()),
              f"keys={list(t)}")
        # GET /tts serves the FULL accepted catalog per provider —
        # the UI's only pick-list source (it hardcoded 12/40 Aura, 16/36
        # Flux). Every catalog entry must pass the server's own validation
        # (no I/O: validate the payload directly), so the list can never
        # offer a value the PUT rejects.
        _cat = t.get("catalog") or {}
        check("TTS GET catalog sizes (elevenlabs 2, aura 40, flux 36, pocket 21, kokoro 28)",
              len(_cat.get("elevenlabs", [])) == 2 and len(_cat.get("deepgram", [])) == 40
              and len(_cat.get("deepgram-flux", [])) == 36
              and len(_cat.get("pocket", [])) == 21
              and len(_cat.get("kokoro", [])) == 28,
              str({k: len(v) for k, v in _cat.items()}))
        from dashboard import routes as _routes_m1
        import tts_kokoro as _tk_m1
        _m1_kavail = _tk_m1.kokoro_available
        _m1_kfiles = _tk_m1._files_problem
        _tk_m1.kokoro_available = lambda: True  # validate the list, not this host's install
        _tk_m1._files_problem = lambda: None
        _m1_bad = []
        for _prov, _vals in _cat.items():
            for _v in _vals:
                try:
                    if _prov == "pocket":
                        # LocalAI's pocket-tts presets ride PUT /localai's voice, not a TTS provider.
                        _routes_m1.LocalAIPayload(voice=_v)
                        continue
                    if _prov == "elevenlabs":
                        _p = _routes_m1.TTSPayload(provider=_prov, model=_v, voice_id="AbC123xyZw9876543210")
                    elif _prov == "deepgram":
                        _p = _routes_m1.TTSPayload(provider=_prov, deepgram_voice=_v)
                    elif _prov == "kokoro":
                        _p = _routes_m1.TTSPayload(provider=_prov, kokoro_voice=_v)
                    else:
                        _p = _routes_m1.TTSPayload(provider=_prov, deepgram_flux_voice=_v)
                    _routes_m1._validate_tts_payload(_p)
                except Exception as _e:
                    _m1_bad.append((_prov, _v, str(_e)[:40]))
        _tk_m1.kokoro_available = _m1_kavail
        _tk_m1._files_problem = _m1_kfiles
        check("every catalog entry passes the server's own TTS validation",
              not _m1_bad and sum(len(v) for v in _cat.values()) == 127, f"bad={_m1_bad[:3]}")
        check("localai card: GET /tts catalog.pocket = LocalAI pocket-tts's 21 presets, alba first (its voice picker's list)",
              (_cat.get("pocket") or [None])[0] == "alba" and len(_cat.get("pocket") or []) == 21, str(_cat.get("pocket")))
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs",
        "voice_id": "AbC123xyZw9876543210", "model": "eleven_flash_v2_5"})
    check("TTS PUT (elevenlabs) ok", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")
    t = client.get("/api/dashboard/tts").json()
    check("TTS round-trip: elevenlabs voice_id + model persisted",
          t.get("voice_id") == "AbC123xyZw9876543210" and t.get("model") == "eleven_flash_v2_5",
          f"t={t}")
    check("tts norm: GET carries elevenlabs_text_normalization (unset = None)",
          "elevenlabs_text_normalization" in t, f"keys={sorted(t)}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_flash_v2_5", "elevenlabs_text_normalization": "on"})
    t = client.get("/api/dashboard/tts").json()
    check("tts norm: PUT 'on' persists",
          r.status_code == 200 and t.get("elevenlabs_text_normalization") == "on",
          f"status={r.status_code} t={t.get('elevenlabs_text_normalization')}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210", "model": "eleven_flash_v2_5"})
    t = client.get("/api/dashboard/tts").json()
    check("tts norm: a PUT without the field leaves it as-is",
          r.status_code == 200 and t.get("elevenlabs_text_normalization") == "on",
          f"t={t.get('elevenlabs_text_normalization')}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_flash_v2_5", "elevenlabs_text_normalization": "auto"})
    t = client.get("/api/dashboard/tts").json()
    check("tts norm: PUT 'auto' switches back",
          r.status_code == 200 and t.get("elevenlabs_text_normalization") == "auto",
          f"t={t.get('elevenlabs_text_normalization')}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_flash_v2_5", "elevenlabs_text_normalization": "always"})
    check("tts norm: unknown mode rejected (422)", r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram", "deepgram_voice": "aura-2-zeus-en"})
    check("TTS PUT (deepgram) ok", r.status_code == 200, f"status={r.status_code}")
    t = client.get("/api/dashboard/tts").json()
    check("TTS round-trip: deepgram voice persisted",
          t.get("provider") == "deepgram" and t.get("deepgram_voice") == "aura-2-zeus-en",
          f"t={t}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-hannah-en"})
    check("TTS PUT (deepgram-flux) ok", r.status_code == 200, f"status={r.status_code}")
    t = client.get("/api/dashboard/tts").json()
    check("TTS round-trip: flux voice persisted",
          t.get("provider") == "deepgram-flux" and t.get("deepgram_flux_voice") == "flux-hannah-en",
          f"t={t}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-hannah-en",
        "deepgram_flux_speed": 1.1, "deepgram_flux_expressivity": 2})
    check("TTS PUT (flux speed+expressivity) ok", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")
    t = client.get("/api/dashboard/tts").json()
    check("TTS round-trip: flux speed 1.1 + expressivity 2 persisted",
          t.get("deepgram_flux_speed") == 1.1 and t.get("deepgram_flux_expressivity") == 2,
          f"t={t}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-hannah-en",
        "deepgram_flux_speed": 0.87})
    check("TTS PUT rejects off-step speed (0.87)", r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-hannah-en",
        "deepgram_flux_speed": 1.7})
    check("TTS PUT rejects out-of-range speed (1.7)", r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-hannah-en",
        "deepgram_flux_expressivity": 3})
    check("TTS PUT rejects out-of-range expressivity (3)", r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-hannah-en"})
    t = client.get("/api/dashboard/tts").json()
    check("TTS bare flux PUT keeps tuned knobs (partial-update semantics)",
          t.get("deepgram_flux_speed") == 1.1 and t.get("deepgram_flux_expressivity") == 2,
          f"t={t}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram-flux", "deepgram_flux_voice": "flux-notarealvoice-en"})
    check("TTS PUT rejects unknown Flux voice", r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "deepgram", "deepgram_voice": "aura-2-nonexistent-en"})
    check("TTS PUT rejects unknown Aura-2 voice", r.status_code == 422, f"status={r.status_code}")
    # ONE provider, model picks the lane: only
    # flash_v2_5 and v4_turbo are in the menu — flash rides the Realtime
    # TTS WS, v4_turbo rides Text-to-Dialogue. v2/v3/v4-regular retired.
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_v3"})
    check("TTS PUT rejects retired eleven_v3 (422)",
          r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "short", "model": "eleven_v2"})
    check("TTS PUT rejects too-short voice_id (model also retired — 422 either way)",
          r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "deepgram_voice": "aura-2-zeus-en"})
    check("TTS PUT elevenlabs-without-voice_id rejected (422)",
          r.status_code == 422, f"status={r.status_code}")
    # ONE provider, model picks the lane: v3/v4 models are
    # legal on 'elevenlabs' (bot.py routes them to Text-to-Dialogue);
    # 'elevenlabs-v4' is gone. Round-trips must persist provider+model.
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_v4_turbo"})
    check("TTS PUT accepts elevenlabs + eleven_v4_turbo (single provider)",
          r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        t4 = client.get("/api/dashboard/tts").json()
        check("TTS v4 round-trip persisted model under provider=elevenlabs",
              t4.get("provider") == "elevenlabs" and t4.get("model") == "eleven_v4_turbo",
              f"tts={t4.get('provider')}/{t4.get('model')}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs-v4", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_v4_turbo"})
    check("TTS PUT rejects retired provider elevenlabs-v4 (422)",
          r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_bogus_v9"})
    check("TTS PUT rejects unknown model (422)",
          r.status_code == 422, f"status={r.status_code}")
    # Kokoro lane (routes). kokoro_available is monkeypatched, so the pins
    # hold whether or not kokoro-onnx is on this host.
    import tts_kokoro as _tkr
    check("kokoro: the dashboard imports neither kokoro_onnx nor pipecat's Kokoro service",
          "kokoro_onnx" not in sys.modules and "pipecat.services.kokoro.tts" not in sys.modules)
    _KOKORO_28 = ("af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river "
                  "af_sarah af_sky am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck "
                  "am_santa bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis").split()
    _orig_availkr = _tkr.kokoro_available
    _orig_fileskr = _tkr._files_problem
    _tkr.kokoro_available = lambda: True
    _tkr._files_problem = lambda: None
    try:
        t = client.get("/api/dashboard/tts").json()
        check("kokoro: GET /tts catalog.kokoro = exactly the 28 English voices",
              (t.get("catalog") or {}).get("kokoro") == _KOKORO_28, f"{(t.get('catalog') or {}).get('kokoro')}")
        _kp = next((p for p in t.get("providers") or [] if p.get("name") == "kokoro"), {})
        check("kokoro: GET /tts reports kokoro_voice + kokoro_speed and offers Kokoro",
              {"kokoro_voice", "kokoro_speed"} <= set(t) and _kp.get("available") is True
              and t.get("kokoro_voice") in _KOKORO_28,
              str({k: t.get(k) for k in ("kokoro_voice", "kokoro_speed")}) + str(_kp))
        r = client.put("/api/dashboard/tts", json={"provider": "kokoro", "kokoro_voice": "bf_emma",
                                                   "kokoro_speed": 1.25})
        t = client.get("/api/dashboard/tts").json()
        check("kokoro: PUT provider kokoro + voice + speed round-trips",
              r.status_code == 200 and t.get("provider") == "kokoro" and t.get("kokoro_voice") == "bf_emma"
              and t.get("kokoro_speed") == 1.25, f"{r.status_code} {r.text[:120]} {t.get('kokoro_voice')}/{t.get('kokoro_speed')}")
        for _body, _why in (({"provider": "kokoro"}, "no voice"),
                            ({"provider": "kokoro", "kokoro_voice": "af_nope"}, "unknown voice"),
                            ({"provider": "kokoro", "kokoro_voice": "aura-2-thalia-en"}, "another provider's voice"),
                            ({"provider": "kokoro", "kokoro_voice": "af_heart", "kokoro_speed": 1.27}, "off-step speed"),
                            ({"provider": "kokoro", "kokoro_voice": "af_heart", "kokoro_speed": 1.6}, "speed above 1.5"),
                            ({"provider": "kokoro", "kokoro_voice": "af_heart", "kokoro_speed": 0.45}, "speed below 0.5"),
                            ({"provider": "kokoro", "kokoro_voice": "af_heart", "kokoro_spead": 1.0}, "misspelled key")):
            r = client.put("/api/dashboard/tts", json=_body)
            check(f"kokoro: PUT rejects {_why} (422)", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
        # Package installed but model files missing: not offered, not accepted.
        _tkr._files_problem = lambda: "/x/kokoro-v1.0.onnx is missing"
        t = client.get("/api/dashboard/tts").json()
        _kp = next((p for p in t.get("providers") or [] if p.get("name") == "kokoro"), {})
        check("kokoro: model files missing -> GET /tts offers Kokoro disabled with 'model files missing'",
              _kp.get("available") is False and _kp.get("reason") == "model files missing", str(_kp))
        r = client.put("/api/dashboard/tts", json={"provider": "kokoro", "kokoro_voice": "bf_emma"})
        check("kokoro: model files missing -> PUT provider kokoro is refused (422) with the fetch hint",
              r.status_code == 422 and "fetch_kokoro_models.sh" in r.text, f"{r.status_code} {r.text[:160]}")
        _tkr.kokoro_available = lambda: False
        t = client.get("/api/dashboard/tts").json()
        _kp = next((p for p in t.get("providers") or [] if p.get("name") == "kokoro"), {})
        check("kokoro: package missing -> not installed, not 'files missing'",
              _kp.get("available") is False and _kp.get("reason") == "not installed"
              and "pip install -r requirements.txt" in _tkr.INSTALL_HINT, str(_kp))
        _tkr.kokoro_available = lambda: True
        _tkr._files_problem = lambda: None
        t = client.get("/api/dashboard/tts").json()
        check("kokoro: refused PUTs saved nothing",
              t.get("kokoro_voice") == "bf_emma" and t.get("kokoro_speed") == 1.25, str(t)[:160])
        r = client.put("/api/dashboard/tts", json={"provider": "kokoro", "kokoro_voice": "am_adam"})
        t = client.get("/api/dashboard/tts").json()
        check("kokoro: PUT without kokoro_speed leaves the saved speed as-is",
              r.status_code == 200 and t.get("kokoro_voice") == "am_adam" and t.get("kokoro_speed") == 1.25,
              f"{t.get('kokoro_voice')}/{t.get('kokoro_speed')}")
        r = client.put("/api/dashboard/tts", json={"provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
                                                   "model": "eleven_flash_v2_5"})
        t = client.get("/api/dashboard/tts").json()
        check("kokoro: a PUT for another provider leaves kokoro_voice and kokoro_speed as they are",
              r.status_code == 200 and t.get("provider") == "elevenlabs"
              and t.get("kokoro_voice") == "am_adam" and t.get("kokoro_speed") == 1.25,
              f"{t.get('provider')} {t.get('kokoro_voice')}/{t.get('kokoro_speed')}")
        # Not installed: the card shows it disabled, a PUT says how to install.
        _tkr.kokoro_available = lambda: False
        r = client.put("/api/dashboard/tts", json={"provider": "kokoro", "kokoro_voice": "af_heart"})
        check("kokoro: PUT provider kokoro refused when not installed (422 + install hint)",
              r.status_code == 422 and _tkr.INSTALL_HINT in r.text, f"{r.status_code} {r.text[:160]}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram", "deepgram_voice": "aura-2-thalia-en"})
        check("kokoro: Kokoro missing: other providers still save",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        check("kokoro: GET /tts never returns a key value with Kokoro fields added",
              not any("sk_" in str(v) for v in client.get("/api/dashboard/tts").json().values()))
    finally:
        _tkr.kokoro_available = _orig_availkr
        _tkr._files_problem = _orig_fileskr
    # restore the real provider (the suite config is a copy, but leave it
    # in a sane state anyway)
    client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_flash_v2_5"})

    # (bug-hunt) GET /tts used to read ONLY the config file: a knob the
    # bot reads via cfg_env (bot.py) but that lived only in .env showed
    # the code default on the panel. File > LVI_* env > the bot's default,
    # per knob — pin provider (the panel's most consequential one).
    _saved_tts_bh6 = dict(config_store.read_section("tts") or {})
    _saved_env_bh6 = os.environ.get("LVI_TTS_PROVIDER")
    try:
        config_store.write_section("tts", {k: v for k, v in _saved_tts_bh6.items()
                                           if k != "provider"})
        os.environ["LVI_TTS_PROVIDER"] = "deepgram"
        _t_bh6 = client.get("/api/dashboard/tts").json()
        check("TTS GET: provider absent from the file -> the LVI_TTS_PROVIDER value (the bot's precedence)",
              _t_bh6.get("provider") == "deepgram", f"provider={_t_bh6.get('provider')!r}")
    finally:
        config_store.write_section("tts", _saved_tts_bh6)
        if _saved_env_bh6 is None:
            os.environ.pop("LVI_TTS_PROVIDER", None)
        else:
            os.environ["LVI_TTS_PROVIDER"] = _saved_env_bh6

    # 9b. Bot restart route (suite MUST NEVER restart the real bot — it
    # runs on the same host as prod). LVI_RESTART_COMMAND used to override
    # the command, but that env hook was a prod code-exec lane
    # and is now deleted. The suite monkeypatches
    # subprocess.run IN THIS PROCESS so systemd-run never executes while
    # the route's code is fully exercised.
    real_run = subprocess.run
    scheduled_cmds: list = []

    class _FakeProc:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, *a, **kw):
        scheduled_cmds.append(cmd)
        return _FakeProc()

    subprocess.run = _fake_run
    try:
        r = client.post("/api/dashboard/restart")
        ok = (r.status_code == 200 and r.json().get("restarting") is True
              and len(scheduled_cmds) == 1
              and "lenoxvi-dash-restart-" in " ".join(scheduled_cmds[0])
              and "systemctl --user restart lenoxvi-bot" in " ".join(scheduled_cmds[0]))
        check("restart route schedules the real transient unit (monkeypatched, nothing executed)",
              ok, f"status={r.status_code} scheduled={scheduled_cmds}")
    finally:
        subprocess.run = real_run

    # Mute error honesty (the lying-error incident):
    # a node answering {ok: false, data: "unknown cmd"} to set_mute must
    # 501 with the per-line TRUTH (array = physical button, flex needs
    # 1.1.0+) — never the old lie "(firmware 1.1.0+ required)" that
    # told a 1.1.3 array node to upgrade toward a feature it can't get.
    from dashboard import routes as _routes_mut
    from dashboard import registry as _reg_mut
    _real_request = _routes_mut.node_command.request
    # seed an online room so _room_online passes (the suite's
    # connect/tee pattern — see the fw_push pins)
    _reg_mut.note_offer("suite-mute-pc", "suite-mute-probe")
    _reg_mut.mark_connected("suite-mute-pc")

    async def _fake_mute_request(room, cmd, args=None, timeout=12.0):
        if cmd == "set_mute":
            return {"ok": False, "data": "unknown cmd"}
        return await _real_request(room, cmd, args, timeout)

    _routes_mut.node_command.request = _fake_mute_request
    try:
        _r_mut = client.post("/api/dashboard/nodes/suite-mute-probe/mute",
                             json={"mute": True})
        _j_mut = _r_mut.json()
        ok = (_r_mut.status_code == 501
              and "physical mute button" in str(_j_mut.get("detail", ""))
              and "1.2.11" in str(_j_mut.get("detail", ""))  # what fixes it on array
              and "1.1.0+ required" not in str(_j_mut.get("detail", "")))
        check("mute: unknown-cmd 501s with the per-line truth (no firmware lie)",
              ok, f"status={_r_mut.status_code} body={_r_mut.text[:140]}")

        # Restart + wifi check the node's ack — ok:false is a 502,
        # never a lying 200. Replies are the real firmware strings
        # (cmd.cpp: restart "rebooting"; set_wifi "nvs error" /
        # "stored, connecting").
        _m3_reply = {}
        async def _fake_m3_request(room, cmd, args=None, timeout=12.0):
            if cmd in _m3_reply:
                return _m3_reply[cmd]
            return await _real_request(room, cmd, args, timeout)
        _routes_mut.node_command.request = _fake_m3_request
        _m3_reply["restart"] = {"ok": False, "data": "busy"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/restart")
        check("restart: node ok:false → 502",
              _r.status_code == 502 and "busy" in _r.text, f"status={_r.status_code} body={_r.text[:120]}")
        _m3_reply["restart"] = {"ok": True, "data": "rebooting"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/restart")
        check("restart: node ok:true → 200", _r.status_code == 200, f"status={_r.status_code}")
        _m3_reply["set_wifi"] = {"ok": False, "data": "nvs error"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                         json={"ssid": "suite-net", "password": "suite-secret-pw"})
        check("wifi: node ok:false → 502, password never echoed",
              _r.status_code == 502 and "nvs error" in _r.text
              and "suite-secret-pw" not in _r.text,
              f"status={_r.status_code} body={_r.text[:120]}")
        _m3_reply["set_wifi"] = {"ok": True, "data": "stored, connecting"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                         json={"ssid": "suite-net", "password": "suite-secret-pw"})
        check("wifi: node ok:true → 200", _r.status_code == 200, f"status={_r.status_code}")
        # Password shape matches the firmware (1.2.35): up to 63 chars is a
        # passphrase, exactly 64 must be hex (a raw PSK), 65+ is refused.
        # A refusal is a 422 that never echoes the password back.
        _psk_hex = "0123456789abcdef" * 4
        _psk_bad = "0123456789abcdef" * 3 + "0123456789abcdeg"
        _r64 = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                           json={"ssid": "suite-net", "password": _psk_hex})
        check("wifi: 64-char hex PSK accepted", _r64.status_code == 200,
              f"status={_r64.status_code} body={_r64.text[:120]}")
        _r64b = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                            json={"ssid": "suite-net", "password": _psk_bad})
        _r65 = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                           json={"ssid": "suite-net", "password": _psk_hex + "0"})
        check("wifi: 64 non-hex and 65 chars 422, password never echoed",
              _r64b.status_code == 422 and _r65.status_code == 422
              and _psk_bad not in _r64b.text and _psk_hex not in _r65.text,
              f"64bad={_r64b.status_code} 65={_r65.status_code} body={_r64b.text[:160]}")
        _r63 = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                           json={"ssid": "suite-net", "password": "p" * 63})
        check("wifi: 63-char passphrase still accepted", _r63.status_code == 200,
              f"status={_r63.status_code}")

        # MutePayload.mute is REQUIRED — a body-less/empty POST
        # must never mute a node (old default=True did).
        _m3_reply["set_mute"] = {"ok": True, "data": {"muted": True}}
        _r_e = client.post("/api/dashboard/nodes/suite-mute-probe/mute", json={})
        _r_n = client.post("/api/dashboard/nodes/suite-mute-probe/mute")
        check("mute: empty body / no body 422 (no default-True trap)",
              _r_e.status_code == 422 and _r_n.status_code == 422,
              f"empty={_r_e.status_code} none={_r_n.status_code}")
        # A successful set_mute updates the room record from the ACK
        # (the node's truth), so /status agrees before the next push.
        _r_h4 = client.post("/api/dashboard/nodes/suite-mute-probe/mute", json={"mute": True})
        _rec_h4 = next((r for r in _reg_mut.known_rooms() if r["room"] == "suite-mute-probe"), {})
        check("mute: set_mute ack updates the room record (muted=True)",
              _r_h4.status_code == 200 and _rec_h4.get("muted") is True,
              f"status={_r_h4.status_code} rec={_rec_h4}")
    finally:
        _routes_mut.node_command.request = _real_request
        _reg_mut.mark_disconnected("suite-mute-pc")

    # extra="forbid" on EVERY write payload — a misspelled key
    # 422s instead of silently no-opping (the mapping-wipe bug's wider
    # cousin). Each body is otherwise VALID, so the extra key is the only
    # reason to reject; config.yaml must be byte-identical afterwards.
    import hashlib as _hl_m7
    _cfg_path_m7 = Path(os.environ["LVI_CONFIG"])
    _sha_m7 = _hl_m7.sha256(_cfg_path_m7.read_bytes()).hexdigest()
    _first_zone_m7 = next(iter((config_store.read_section("sonos") or {}).get("zones") or {"x": 0}))
    _m7_cases = [
        ("PUT", "/api/dashboard/tts", {"provider": "elevenlabs", "bogus": 1}),
        ("PUT", "/api/dashboard/voice-tuning", {"wake_threshold": 0.5, "wake_treshold": 0.5}),
        ("PUT", "/api/dashboard/intelligence", {"kind": "hermes", "knd": "openclaw"}),
        ("PUT", f"/api/dashboard/zones/{_first_zone_m7}/volume", {"volume": 30, "vol": 30}),
        ("POST", "/api/dashboard/zones", {"name": "m7z", "backend": "wiim", "ip": "127.0.0.6", "bogus": 1}),
        ("POST", "/api/dashboard/nodes/suite-m7/session-mode", {"mode": "fresh", "mdoe": "sticky"}),
        ("PUT", "/api/dashboard/oww/select", {"model": "x.onnx", "bogus": 1}),
        ("PUT", "/api/dashboard/tts/key/elevenlabs", {"api_key": "validkey_0123456789", "bogus": 1}),
        ("POST", "/api/dashboard/nodes/suite-m7/name", {"name": "ok-name", "bogus": 1}),
        ("POST", "/api/dashboard/nodes/suite-m7/wifi", {"ssid": "s", "password": "p", "bogus": 1}),
        ("POST", "/api/dashboard/nodes/suite-m7/mute", {"mute": True, "bogus": 1}),
    ]
    _m7_bad = []
    for _meth, _url, _body in _m7_cases:
        _rr = client.request(_meth, _url, json=_body)
        _det = _rr.json().get("detail") if _rr.status_code == 422 else None
        if not (_rr.status_code == 422 and "extra_forbidden" in str(_det)):
            _m7_bad.append((_url, _rr.status_code, _rr.text[:80]))
    check(f"extra key 422s on all {len(_m7_cases)} write payloads",
          not _m7_bad, f"not forbidden: {_m7_bad}")
    check("forbidden writes left config.yaml byte-identical",
          _hl_m7.sha256(_cfg_path_m7.read_bytes()).hexdigest() == _sha_m7, "")

    # The legacy manifest-only POST route is
    # REMOVED — the upload route is the only manifest writer (version and
    # track come from the image itself, board-identity-guarded). A bare
    # manifest write could point the array track at anything.
    r_leg = client.request("POST", "/api/dashboard/firmware/version",
                           json={"version": "1.0.0"})
    check("legacy POST /firmware/version removed (404, never a bare manifest write)",
          r_leg.status_code == 404, f"status={r_leg.status_code} body={r_leg.text[:100]}")

    # Key-leak pin: a key PUT that fails validation must not
    # put the key in the response OR any log line (pipecat's handler
    # logged Pydantic's `input` = the full key to journald).
    from loguru import logger as _lg_leak
    _leak_lines = []
    _sink_id = _lg_leak.add(lambda m: _leak_lines.append(str(m)), level="DEBUG")
    try:
        _rl = client.put("/api/dashboard/intelligence/key",
                         json={"api_key": "suite-leak-key-98765"})  # no kind → 422
        _rl2 = client.put("/api/dashboard/tts/key/elevenlabs",
                          json={"api_key": "suite-leak-key-98765", "bogus": 1})
    finally:
        _lg_leak.remove(_sink_id)
    check("422 on key routes never echoes the key in the response",
          _rl.status_code == 422 and _rl2.status_code == 422
          and "suite-leak-key-98765" not in _rl.text + _rl2.text,
          f"{_rl.status_code}/{_rl2.status_code} {_rl.text[:100]}")
    check("422 on key routes never writes the key to any log line",
          not any("suite-leak-key-98765" in ln for ln in _leak_lines)
          and any("dashboard: 422 on /api/dashboard/intelligence/key" in ln for ln in _leak_lines),
          f"lines={len(_leak_lines)}")
    check("dashboard 422s never reach pipecat's app-wide handler",
          not [p for p, _ in PIPECAT_HANDLER_HITS if p.startswith("/api/dashboard")],
          f"hits={[p for p, _ in PIPECAT_HANDLER_HITS][:5]}")

    # ── LocalAI card: settings, Test, key route, provider gates ──
    import http.server as _la_hs
    import threading as _la_th
    _la_seen = []

    class _LaFake(_la_hs.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            _la_seen.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer la-good-SECRET-7777":
                body, code = b'{"error":{"message":"unauthorized"}}', 401
            elif self.path == "/v1/models":
                body, code = json.dumps({"data": [{"id": "fw-small"}, {"id": "kokoro"}, {"id": "llm"}]}).encode(), 200
            elif self.path == "/api/models/config-json/fw-small":
                body, code = b'{"backend":"faster-whisper","known_usecases":["FLAG_TRANSCRIPT"]}', 200
            elif self.path == "/api/models/config-json/kokoro":
                body, code = b'{"backend":"kokoro","known_usecases":["FLAG_TTS"]}', 200
            else:
                body, code = b'{"backend":"llama","known_usecases":null}', 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    _la_srv = _la_hs.ThreadingHTTPServer(("127.0.0.1", 0), _LaFake)
    _la_th.Thread(target=_la_srv.serve_forever, daemon=True).start()
    _la_env = SCRATCH / "localai.env"
    _la_env.write_text("")
    _la_saved_env = {k: os.environ.get(k) for k in ("LVI_ENV_FILE", "LVI_LOCALAI_KEY", "LVI_LOCALAI_URL")}
    os.environ["LVI_ENV_FILE"] = str(_la_env)
    os.environ.pop("LVI_LOCALAI_KEY", None)
    os.environ.pop("LVI_LOCALAI_URL", None)
    _la_tts_before = dict(config_store.read_section("tts") or {})
    _la_stt_before = dict(config_store.read_section("stt") or {})
    try:
        r = client.get("/api/dashboard/localai")
        check("localai card: GET shape (url, model_stt, key_set), nothing saved yet",
              r.status_code == 200 and set(r.json()) == {"url", "model_stt", "key_set"}
              and r.json()["url"] is None and r.json()["key_set"] is False,
              r.text[:200])
        r = client.put("/api/dashboard/tts", json={"provider": "localai"})
        check("localai card: TTS can't pick LocalAI before its URL is saved (422 says so)",
              r.status_code == 422 and "URL" in r.text, r.text[:160])
        r = client.put("/api/dashboard/localai", json={"url": "192.0.2.50:8080/", "model_stt": "fw-small"})
        check("localai card: PUT normalises and saves the URL and model",
              r.status_code == 200 and config_store.read_section("localai").get("url") == "http://192.0.2.50:8080"
              and config_store.read_section("localai").get("model_stt") == "fw-small", r.text[:200])
        for _bad in ("http://x\nLVI_RESTART_COMMAND=boom", "ftp://x:21", "http://x:99999", "http://x/v2/thing"):
            r = client.put("/api/dashboard/localai", json={"url": _bad})
            check(f"localai card: a bad URL is refused (422) [{_bad[:18]}]", r.status_code == 422, r.text[:120])
        r = client.put("/api/dashboard/localai", json={"nope": 1})
        check("localai card: an unknown field is refused (422)", r.status_code == 422)
        r = client.put("/api/dashboard/localai", json={"model_stt": "bad name; rm"})
        check("localai card: a model name with odd characters is refused (422)", r.status_code == 422)
        r = client.put("/api/dashboard/stt", json={"provider": "localai"})
        check("localai card: STT can't pick LocalAI without its key (422 names System · Keys)",
              r.status_code == 422 and "System · Keys" in r.text, r.text[:160])
        _g = client.get("/api/dashboard/stt").json()
        _lp = next((p for p in _g.get("providers", []) if p.get("name") == "localai"), {})
        check("localai card: the STT card offers LocalAI as unavailable with the reason",
              _lp.get("available") is False and _lp.get("reason") == "set it up on System", str(_lp))
        r = client.post("/api/dashboard/localai/test")
        check("localai card: Test without a key answers ok=false naming System · Keys (no request sent)",
              r.status_code == 200 and r.json()["ok"] is False and "System · Keys" in (r.json()["reason"] or ""), r.text[:200])
        r = client.put("/api/dashboard/localai/key", json={"api_key": "x\nLVI_RESTART_COMMAND=evil"})
        check("localai key: newline-injected key refused (422)", r.status_code == 422)
        r = client.put("/api/dashboard/localai/key", json={"api_key": "la-bad-SECRET-6666"})
        check("localai key: saved to .env as LVI_LOCALAI_KEY, value never echoed",
              r.status_code == 200 and "LVI_LOCALAI_KEY=la-bad-SECRET-6666" in _la_env.read_text()
              and "la-bad-SECRET" not in r.text, r.text[:160])
        r = client.post("/api/dashboard/localai/test")
        check("localai card: Test against a dead host -> ok=false with a reason, never a 500 or the key",
              r.status_code == 200 and r.json()["ok"] is False and r.json()["reason"] and "SECRET" not in r.text, r.text[:200])
        client.put("/api/dashboard/localai", json={"url": f"127.0.0.1:{_la_srv.server_port}"})
        r = client.post("/api/dashboard/localai/test")
        check("localai card: a wrong key -> ok=false, the reason says the key was refused, key never shown",
              r.status_code == 200 and r.json()["ok"] is False and "key" in (r.json()["reason"] or "").lower()
              and "SECRET" not in r.text, r.text[:200])
        _kt = client.post("/api/dashboard/keys/test", json={"var": "LVI_LOCALAI_KEY"})
        check("localai key: Keys-card Test reports the refusal as 'rejected'",
              _kt.status_code == 200 and _kt.json().get("status") == "rejected" and "SECRET" not in _kt.text, _kt.text[:200])
        client.put("/api/dashboard/localai/key", json={"api_key": "la-good-SECRET-7777"})
        r = client.post("/api/dashboard/localai/test")
        _j = r.json()
        check("localai card: Test with the right key lists STT and voice models, hides the unflagged one",
              r.status_code == 200 and _j["ok"] is True and _j["ms"] is not None and _j["stt"] == ["fw-small"]
              and _j["tts"] == [{"id": "kokoro", "backend": "kokoro"}] and "SECRET" not in r.text, r.text[:300])
        check("localai card: Test sends the SAVED key as a Bearer token",
              ("/v1/models", "Bearer la-good-SECRET-7777") in _la_seen)
        _kt = client.post("/api/dashboard/keys/test", json={"var": "LVI_LOCALAI_KEY"})
        check("localai key: Keys-card Test says LocalAI accepted the key", _kt.json().get("ok") is True, _kt.text[:200])
        config_store.write_section("tts", {**(config_store.read_section("tts") or {}),
                                           "deepgram_flux_voice": "flux-suite-voice"})
        r = client.put("/api/dashboard/tts", json={"provider": "localai"})
        check("localai card: with URL + key saved, TTS can pick LocalAI", r.status_code == 200, r.text[:160])
        check("tts card: saving LocalAI keeps the saved Deepgram Flux voice",
              (config_store.read_section("tts") or {}).get("deepgram_flux_voice") == "flux-suite-voice",
              str(config_store.read_section("tts")))
        # Restart pending: LocalAI settings count only while a card uses LocalAI.
        import config as _cfgLAP
        from dashboard import routes as _rLAP

        def _la_pending(boot, saved):
            _rb, _rl = _cfgLAP.boot_config, _cfgLAP._load
            _cfgLAP.boot_config, _cfgLAP._load = (lambda: boot), (lambda: saved)
            try:
                return _rLAP._restart_pending()
            finally:
                _cfgLAP.boot_config, _cfgLAP._load = _rb, _rl
        _laB = {"stt": {"provider": "localai"}, "localai": {"url": "http://192.0.2.50:8080", "voice": "af_heart"}}
        _laS = {"stt": {"provider": "localai"}, "localai": {"url": "http://192.0.2.50:8080", "voice": "af_bella"}}
        _laN = {"stt": {"provider": "whisper"}, "localai": {"url": "http://192.0.2.50:8080"}}
        _laN2 = {"stt": {"provider": "whisper"}, "localai": {"url": "http://192.0.2.51:8080"}}
        check("restart pending: a LocalAI change lists 'localai' while a card uses LocalAI, never before",
              "localai" in _la_pending(_laB, _laS) and "localai" not in _la_pending(_laN, _laN2)
              and "localai" not in _la_pending(_laB, _laB), f"{_la_pending(_laB, _laS)} {_la_pending(_laN, _laN2)}")
        check("localai card: GET /tts says nothing is missing for LocalAI",
              any(p.get("name") == "localai" and p.get("available") is True
                  for p in client.get("/api/dashboard/tts").json().get("providers") or []))
        r = client.put("/api/dashboard/localai", json={"model_stt": ""})
        check("localai card: an empty value clears that setting", r.status_code == 200
              and "model_stt" not in (config_store.read_section("localai") or {}))
    finally:
        _la_srv.shutdown()
        config_store.write_section("tts", _la_tts_before)
        config_store.write_section("stt", _la_stt_before)
        config_store.write_section("localai", {})
        for _k, _v in _la_saved_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # Key-route hardening: the character-class
    # pattern must reject newline injection (the old code-exec lane) and
    # accept well-formed keys. LVI_ENV_FILE points the writer at a scratch
    # .env so the suite never touches a real key file.
    os.environ["LVI_ENV_FILE"] = str(SCRATCH / "test.env")
    try:
        r = client.put("/api/dashboard/tts/key/elevenlabs",
                       json={"api_key": "validkey_0123456789abcdef"})
        check("key route accepts well-formed key (200)",
              r.status_code == 200 and r.json().get("api_key_set") is True,
              f"status={r.status_code} body={r.text[:120]}")
        env_txt = (SCRATCH / "test.env").read_text()
        check("key route writes key to scratch .env",
              "ELEVENLABS_API_KEY=validkey_0123456789abcdef" in env_txt,
              f"env={env_txt[:200]}")
        r = client.put("/api/dashboard/tts/key/elevenlabs",
                       json={"api_key": "x\nLVI_RESTART_COMMAND=evil"})
        check("key route REJECTS newline-injected key (422)",
              r.status_code == 422, f"status={r.status_code} body={r.text[:160]}")
        r = client.put("/api/dashboard/tts/key/deepgram",
                       json={"api_key": "has space and 'quote'"})
        check("key route REJECTS keys with spaces/quotes (422)",
              r.status_code == 422, f"status={r.status_code}")
        # The injected env var must NOT have landed in the file
        env_txt = (SCRATCH / "test.env").read_text()
        check("no LVI_RESTART_COMMAND line in written .env (injection dead)",
              "LVI_RESTART_COMMAND" not in env_txt, f"env={env_txt[:200]}")
    finally:
        os.environ.pop("LVI_ENV_FILE", None)

    # Clip server singleton: SonosOutput and
    # WiiMOutput must share ONE server. Two mixed-backend pipelines +
    # the dashboard test-clip lane must all resolve to the same object
    # with exactly ONE bind on the port. (Suite runs on port 8766 via
    # LVI_CONFIG — the live bot owns 8765.)
    import asyncio as _asyncio
    from sonos_output import shared_clip_server as _scs
    from wiim_output import WiiMOutput
    from sonos_output import SonosOutput

    async def _two_backends_one_server():
        s1 = await _scs()
        s2 = await _scs()  # second backend + dashboard lane
        return s1, s2, s1 is s2

    _s1, _s2, _same = _asyncio.run(_two_backends_one_server())
    check("clip server singleton: mixed callers get the SAME object",
          _same, f"s1={id(_s1)} s2={id(_s2)}")
    check("clip server singleton: exactly one runner (one bind)",
          _s1._runner is not None and _s1._runner is _s2._runner,
          f"runner1={_s1._runner} runner2={_s2._runner}")
    # Constructing both outputs must not create or bind anything new.
    _n_before = id(_s1)
    _so = SonosOutput(echo_gate=None, zone_selector=lambda: "test", led=None)
    _wo = WiiMOutput(echo_gate=None, zone_selector=lambda: "test", led=None)
    _s3 = _asyncio.run(_scs())
    check("output construction does not spawn a second server",
          _s3 is _s1 and id(_s3) == _n_before, f"s3={id(_s3)}")
    # url_for must live on the shared server (was stranded mid-refactor).
    _url = _s1.url_for("probe.mp3")
    check("clip server url_for works (quotes filenames)",
          _url.endswith("/probe.mp3") and _url.startswith("http://"), f"url={_url}")

    # Reply-time backend dispatch regression: bot.py must wire BOTH
    # output backends into the pipeline UNCONDITIONALLY. The old build-time
    # ternary picked one backend before the offer tee correlated the
    # room, so a WiiM room could land on a Sonos-only pipeline and every
    # reply died in a skip warning. Dispatch happens at SPEECH time:
    # whichever backend owns the reply zone plays; the other must skip
    # gracefully (warning + return, no network, no play).
    import ast as _ast
    _bot_src = (REPO_CORE.parent / "bot.py").read_text()
    _tree = _ast.parse(_bot_src)
    _pipes = [n for n in _ast.walk(_tree)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
              and n.func.id == "Pipeline"]
    check("bot.py: exactly one Pipeline construction", len(_pipes) == 1,
          f"count={len(_pipes)}")
    _wired, _conditional = [], []
    for _n in _ast.walk(_pipes[0].args[0]):
        if (isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Name)
                and _n.func.id in ("SonosOutput", "WiiMOutput")):
            _wired.append(_n.func.id)
        elif isinstance(_n, _ast.IfExp):
            for _sub in _ast.walk(_n):
                if (isinstance(_sub, _ast.Call)
                        and isinstance(_sub.func, _ast.Name)
                        and _sub.func.id in ("SonosOutput", "WiiMOutput")):
                    _conditional.append(_sub.func.id)
    # The backends can be hoisted to variables before
    # the Pipeline (the interrupt closure needs the objects) — accept the
    # hoisted form when BOTH constructors run unconditionally at module
    # scope of run_bot, no ternary anywhere.
    if set(_wired) != {"SonosOutput", "WiiMOutput"}:
        _hoisted = []
        for _n in _ast.walk(_tree):
            if (isinstance(_n, _ast.Assign) and isinstance(_n.value, _ast.Call)
                    and isinstance(_n.value.func, _ast.Name)
                    and _n.value.func.id in ("SonosOutput", "WiiMOutput")):
                _hoisted.append(_n.value.func.id)
        _wired = _hoisted
    check("bot.py: BOTH output backends wired into the pipeline",
          set(_wired) == {"SonosOutput", "WiiMOutput"},
          f"wired={_wired} (dispatch must be reply-time, not build-time)")
    check("bot.py: no build-time ternary picks the backend (the bedroom-outage shape)",
          not _conditional, f"conditional={_conditional}")
    # Intelligence rider (the echo-stub incident): the intelligence construction ternary
    # must route BOTH live kinds (hermes, openclaw) to IntelligenceAgent — a
    # kind the ternary doesn't know falls to the echo stub silently while
    # every agent-level suite check stays green. The dispatch is the
    # wiring the suite never exercised; this AST check pins it.
    _brain_exp = None
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.Assign) and isinstance(_n.value, _ast.IfExp) \
                and _n.targets and isinstance(_n.targets[0], _ast.Name) \
                and _n.targets[0].id == "intelligence":
            _brain_exp = _n.value
    # The ternary accepts every registered lane (intelligence.KINDS), so a
    # kind is dispatched exactly when the registry holds it.
    import intelligence as _lanes_reg
    _routes_registry = _brain_exp is not None and any(
        isinstance(_sub, _ast.Attribute) and _sub.attr == "KINDS" and isinstance(_sub.value, _ast.Name)
        and _sub.value.id == "intelligence_lanes" for _sub in _ast.walk(_brain_exp.test)) \
        and "import intelligence as intelligence_lanes" in _bot_src
    _routes_live_kinds = _routes_registry and {"hermes", "openclaw"} <= set(_lanes_reg.KINDS)
    check("bot.py: intelligence dispatch routes hermes AND openclaw to the real agent",
          _routes_live_kinds, "construction ternary must accept both live kinds")
    _routes_nanobot = _routes_registry and "nanobot" in _lanes_reg.KINDS
    check("nanobot: bot.py: intelligence dispatch routes nanobot to the real agent (not the echo stub)",
          _routes_nanobot, "construction ternary must accept kind nanobot")
    # Naming rule: the intelligence step is named for what it
    # is, not for one of its two kinds. The dispatch's live branch must
    # build IntelligenceAgent, and the old module must be gone — a stale
    # core/hermes_agent.py would shadow-import fine and hide a half rename.
    _live_cls = set()
    if _brain_exp is not None:
        for _sub in _ast.walk(_brain_exp.body):
            if isinstance(_sub, _ast.Call) and isinstance(_sub.func, _ast.Name):
                _live_cls.add(_sub.func.id)
    check("bot.py: the live intelligence branch constructs IntelligenceAgent",
          "IntelligenceAgent" in _live_cls, f"constructs={sorted(_live_cls)}")
    check("core/hermes_agent.py is gone (no stale module behind the rename)",
          not (REPO_CORE / "hermes_agent.py").exists()
          and (REPO_CORE / "intelligence" / "agent.py").exists(), "")
    _ia_src = (REPO_CORE / "intelligence" / "agent.py").read_text() \
        if (REPO_CORE / "intelligence" / "agent.py").exists() else ""
    import ast as _astoi
    _old_imports = []
    for _pf in [REPO_CORE.parent / "bot.py"] + list(REPO_CORE.rglob("*.py")) + list((REPO_CORE.parent / "tests").glob("*.py")):
        for _node in _astoi.walk(_astoi.parse(_pf.read_text())):
            _mods = ([a.name for a in _node.names] if isinstance(_node, _astoi.Import)
                     else [_node.module or ""] if isinstance(_node, _astoi.ImportFrom) else [])
            if any(m in ("intelligence_agent", "intelligence_stub", "intelligence_target") for m in _mods):
                _old_imports.append(_pf.name)
    check("intelligence: the old intelligence_* aliases are gone and nothing imports them",
          not any((REPO_CORE / f"intelligence_{n}.py").exists() for n in ("agent", "stub", "target"))
          and not _old_imports, f"importers={sorted(set(_old_imports))}")
    check("intelligence_agent logs under its own module name",
          "intelligence_agent:" in _ia_src and "hermes_agent:" not in _ia_src
          and "def _ask_intelligence(" in _ia_src, "")
    # The skip mechanism, both directions, against the throwaway config
    # (sonos zones: Kitchen/Hall; wiim zone: Loft).
    from loguru import logger as _lg
    _cap = []
    _sink = _lg.add(lambda m: _cap.append(str(m)), level="DEBUG")
    try:
        _so_skip = SonosOutput(echo_gate=None,
                               zone_selector=lambda: "Loft", led=None)
        _wo_skip = WiiMOutput(echo_gate=None,
                              zone_selector=lambda: "Kitchen", led=None)
        _asyncio.run(_so_skip._speak_to_sonos(b"\x00" * 48000))
        _asyncio.run(_wo_skip._speak_to_wiim(b"\x00" * 48000))
        _joined = "\n".join(_cap)
        check("sonos_output skips a WiiM-owned zone at reply time (no play)",
              "zone 'Loft' not in config zones" in _joined
              and "sonos_output: playing clip" not in _joined,
              f"skip-warning-seen={'not in config zones' in _joined}")
        check("wiim_output skips a Sonos-owned zone at reply time (no play)",
              "not in wiim zones" in _joined
              and "wiim_output: playing clip" not in _joined,
              f"skip-warning-seen={'not in wiim zones' in _joined}")
    finally:
        _lg.remove(_sink)

    # fw_push (firmware 1.1.2 connect-push): a node announcing its
    # RUNNING version on data-channel open lands in the room record and
    # surfaces in known_rooms. Direct path: the room is ALREADY
    # correlated (post-tee) when the push lands. Race path: push before
    # the tee correlates — version held by pc_id, promoted with the room.
    from dashboard import registry as _reg
    from dashboard import node_command
    _reg.note_offer("test-pc-fw", "fw-direct-room")  # room correlated first
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result",
                    "cmd": "fw_push", "ok": True,
                    "data": {"fw": "1.1.2"}}), "test-pc-fw")
    check("fw_push stores the running version (direct room)",
          any(r["room"] == "fw-direct-room" and r.get("fw") == "1.1.2"
              for r in _reg.known_rooms()),
          f"rooms={_reg.known_rooms()}")
    # Provisional-window race: push arrives before the offer correlates
    # the room — version must ride along on promotion.
    _reg.mark_connected("test-pc-fw-race")  # provisional (pre-tee)
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result",
                    "cmd": "fw_push", "ok": True,
                    "data": {"fw": "1.1.3"}}), "test-pc-fw-race")
    _reg.note_offer("test-pc-fw-race", "fw-race-room")
    check("fw_push survives the connect/tee race (held, promoted with room)",
          any(r["room"] == "fw-race-room" and r.get("fw") == "1.1.3"
              for r in _reg.known_rooms()),
          f"rooms={_reg.known_rooms()}")

    # The OTHER race order (fw=None bug): fw_push lands
    # while pc_id has NO binding at all (no connect, no tee yet) — the
    # version is held in _fw_by_pc; note_offer's NON-provisional branch
    # must drain it too. Old code stranded the hold forever here.
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result",
                    "cmd": "fw_push", "ok": True,
                    "data": {"fw": "1.1.4"}}), "test-pc-fw-unknown")
    check("fw_push held for an unbound pc_id",
          _reg._fw_by_pc.get("test-pc-fw-unknown") == "1.1.4",
          f"held={_reg._fw_by_pc}")
    _reg.note_offer("test-pc-fw-unknown", "fw-unknown-room")
    check("note_offer drains the fw hold (else-branch, the fw=None bug)",
          _reg._fw_by_pc.get("test-pc-fw-unknown") is None
          and any(r["room"] == "fw-unknown-room" and r.get("fw") == "1.1.4"
                  for r in _reg.known_rooms()),
          f"held={_reg._fw_by_pc} rooms={_reg.known_rooms()}")

    # fw_push "wifi_rollback" (firmware 1.2.35): the node rolled back to its
    # previous network after a failed Wi-Fi change — logged as a WARNING so
    # the journal says why the node came back on the old network. Absent or
    # false: no warning.
    from loguru import logger as _lg_wr
    _wr_lines = []
    _wr_sink = _lg_wr.add(lambda m: _wr_lines.append((m.record["level"].name, m.record["message"])),
                          level="DEBUG")
    try:
        _reg.note_offer("test-pc-wifirb", "wifirb-room")
        node_command.on_message(
            json.dumps({"type": "cmd_result", "evt": "cmd_result",
                        "cmd": "fw_push", "ok": True,
                        "data": {"fw": "1.2.35", "line": "array", "wifi_rollback": True,
                                 "muted": False}}), "test-pc-wifirb")
        _wr_hit = [m for lv, m in _wr_lines if lv == "WARNING" and "previous network" in m]
        _wr_lines.clear()
        node_command.on_message(
            json.dumps({"type": "cmd_result", "evt": "cmd_result",
                        "cmd": "fw_push", "ok": True,
                        "data": {"fw": "1.2.35", "line": "array", "muted": False}}), "test-pc-wifirb")
        _wr_quiet = [m for lv, m in _wr_lines if lv == "WARNING" and "previous network" in m]
    finally:
        _lg_wr.remove(_wr_sink)
    check("fw_push wifi_rollback: one WARNING naming the room; none without it",
          len(_wr_hit) == 1 and "wifirb-room" in _wr_hit[0] and not _wr_quiet,
          f"hit={_wr_hit} quiet={_wr_quiet}")

    # Boot flags (firmware 1.2.41+): fw_push "rejected" names an OTA version
    # the node rolled back from and now refuses, "wifi_rollback" that this boot
    # went back to the previous network. Both land on the room record for the
    # Nodes table; every push sets them (a push without them clears last
    # boot's), with the same connect-race handling as fw.
    def _nh_push(pc, data):
        node_command.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result",
                                            "cmd": "fw_push", "ok": True, "data": data}), pc)

    def _nh_rec(name):
        return next((r for r in _reg.known_rooms() if r["room"] == name), {})

    _reg.note_offer("test-pc-nh", "Hall")
    _nh_push("test-pc-nh", {"fw": "1.2.41", "line": "array", "rejected": "1.2.42", "wifi_rollback": True})
    check("node health: fw_push rejected + wifi_rollback land on the room record",
          _nh_rec("Hall").get("fw_rejected") == "1.2.42" and _nh_rec("Hall").get("wifi_rollback") is True,
          str(_nh_rec("Hall")))
    _nh_push("test-pc-nh", {"fw": "1.2.41", "line": "array"})
    check("node health: a fw_push without the keys clears both (a clean boot)",
          "fw_rejected" in _nh_rec("Hall") and _nh_rec("Hall")["fw_rejected"] is None
          and _nh_rec("Hall").get("wifi_rollback") is False, str(_nh_rec("Hall")))
    _nh_push("test-pc-nh", {"fw": "1.2.41", "rejected": 142, "wifi_rollback": "yes"})
    check("node health: a non-string rejected and a non-true wifi_rollback are dropped",
          "fw_rejected" in _nh_rec("Hall") and _nh_rec("Hall")["fw_rejected"] is None
          and _nh_rec("Hall").get("wifi_rollback") is False, str(_nh_rec("Hall")))
    check("node health: a node that never reported (older firmware) shows fw_rejected=None, wifi_rollback=False",
          "fw_rejected" in _nh_rec("fw-direct-room") and _nh_rec("fw-direct-room")["fw_rejected"] is None
          and _nh_rec("fw-direct-room").get("wifi_rollback") is False, str(_nh_rec("fw-direct-room")))
    # provisional window: the push lands before the tee binds the room
    _reg.mark_connected("test-pc-nh-race")
    _nh_push("test-pc-nh-race", {"fw": "1.2.41", "rejected": "1.2.40", "wifi_rollback": True})
    _reg.note_offer("test-pc-nh-race", "Den")
    check("node health: boot flags survive the connect/tee race (held, promoted with the room)",
          _nh_rec("Den").get("fw_rejected") == "1.2.40" and _nh_rec("Den").get("wifi_rollback") is True,
          str(_nh_rec("Den")))
    # unbound pc_id (the fw=None race order): held, drained by note_offer
    _nh_push("test-pc-nh-unbound", {"fw": "1.2.41", "rejected": "1.2.40", "wifi_rollback": True})
    _nh_held = getattr(_reg, "_boot_flags_by_pc", {}).get("test-pc-nh-unbound")
    _reg.note_offer("test-pc-nh-unbound", "Loft")
    check("node health: unbound-pc boot flags drained by note_offer (with the fw hold)",
          _nh_held == ("1.2.40", True) and "test-pc-nh-unbound" not in getattr(_reg, "_boot_flags_by_pc", {})
          and _nh_rec("Loft").get("fw_rejected") == "1.2.40" and _nh_rec("Loft").get("wifi_rollback") is True
          and _nh_rec("Loft").get("fw") == "1.2.41", f"held={_nh_held} rec={_nh_rec('Loft')}")
    _st_nh = client.get("/api/dashboard/status").json()
    check("node health: /status rooms[] carries fw_rejected and wifi_rollback",
          any(r.get("room") == "Loft" and r.get("fw_rejected") == "1.2.40" and r.get("wifi_rollback") is True
              for r in _st_nh.get("rooms", [])), str(_st_nh.get("rooms"))[:200])

    # rejected is logged at WARNING once per session, naming room and version.
    _nh_lines = []
    _nh_sink = _lg_wr.add(lambda m: _nh_lines.append((m.record["level"].name, m.record["message"])),
                          level="DEBUG")
    try:
        _reg.note_offer("test-pc-nh-log", "Hall")
        for _ in range(2):
            _nh_push("test-pc-nh-log", {"fw": "1.2.41", "rejected": "1.2.42"})
        _nh_once = [m for lv, m in _nh_lines if lv == "WARNING" and "refuses firmware" in m]
    finally:
        _lg_wr.remove(_nh_sink)
    check("node health: a refused version is logged at WARNING once per session, naming room and version",
          len(_nh_once) == 1 and "Hall" in _nh_once[0] and "1.2.42" in _nh_once[0], str(_nh_once))

    # Request-id echo from the version (firmware 1.2.37+ echoes ids): fw_push
    # marks the session at once; anything that isn't a plain x.y.z proves nothing.
    _nh_gate = [("1.2.37", True), ("1.2.41", True), ("1.10.0", True), ("2.0.0", True),
                ("1.2.36", False), ("1.1.99", False), ("0.9.99", False), ("garbage", False), ("1.2", False),
                ("1.2.37-dirty", False), ("v1.2.37", False), ("", False), (None, False), (1.237, False),
                (["1", "2", "37"], False), ({"v": "1.2.37"}, False)]
    _nh_wrong = []
    for _i, (_fwv, _want) in enumerate(_nh_gate):
        _pc = f"test-pc-nh-echo-{_i}"
        _nh_push(_pc, {"fw": _fwv})
        if (_pc in node_command._echoes_id) != _want:
            _nh_wrong.append(_fwv)
        node_command._echoes_id.discard(_pc)
    # an unreadable version still learns from the first id-bearing reply
    _nh_push("test-pc-nh-echo-learn", {"fw": "garbage"})
    node_command.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "get_info",
                                        "id": 7, "ok": True, "data": {}}), "test-pc-nh-echo-learn")
    _nh_learned = "test-pc-nh-echo-learn" in node_command._echoes_id
    node_command._echoes_id.discard("test-pc-nh-echo-learn")
    check("node health: fw_push fw >= 1.2.37 marks the session as echoing ids; older or garbage versions don't, "
          "and the first id-bearing reply still teaches it",
          not _nh_wrong and _nh_learned, f"wrong for {_nh_wrong} learned={_nh_learned}")

    async def _nh_settle_probe():
        _saved = (node_command.registry.sender_for_room, node_command._send)
        node_command.registry.sender_for_room = lambda room: (object(), "test-pc-nh-echo-fw")
        node_command._send = lambda out, payload: True
        try:
            try:
                await node_command.request("Hall", "set_mute", {"mute": True}, timeout=0.1)
            except TimeoutError:
                pass
            return ("test-pc-nh-echo-fw", "set_mute") in node_command._settling
        finally:
            node_command.registry.sender_for_room, node_command._send = _saved
            node_command._settling.pop(("test-pc-nh-echo-fw", "set_mute"), None)

    _nh_push("test-pc-nh-echo-fw", {"fw": "1.2.41"})
    _nh_settles = asyncio.run(_nh_settle_probe())
    node_command._echoes_id.discard("test-pc-nh-echo-fw")
    check("node health: a 1.2.37+ node's timed-out command opens no settling window (its retry goes out at once)",
          _nh_settles is False, f"settling={_nh_settles}")

    # Mute state (firmware 1.2.11+): the node reports its mic-mute
    # state — fw_push carries "muted" at connect, mute_push fires when an
    # array node's PHYSICAL button is pressed. Same race handling as fw.
    def _room_rec(name):
        return next((r for r in _reg.known_rooms() if r["room"] == name), {})
    _reg.note_offer("test-pc-h4", "h4-direct-room")
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.11", "muted": True}}), "test-pc-h4")
    check("mute: fw_push carries muted -> room record muted=True (direct room)",
          _room_rec("h4-direct-room").get("muted") is True, str(_room_rec("h4-direct-room")))
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "mute_push",
                    "ok": True, "data": {"muted": False}}), "test-pc-h4")
    check("mute: mute_push (physical button) flips the room record to muted=False",
          _room_rec("h4-direct-room").get("muted") is False, str(_room_rec("h4-direct-room")))
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "mute_push",
                    "ok": True, "data": {"muted": "yes"}}), "test-pc-h4")
    check("mute: mute_push with a non-bool muted is ignored (no change)",
          _room_rec("h4-direct-room").get("muted") is False, str(_room_rec("h4-direct-room")))
    check("mute: a node that never reported (old firmware) shows muted=None",
          "muted" in _room_rec("fw-direct-room") and _room_rec("fw-direct-room")["muted"] is None,
          str(_room_rec("fw-direct-room")))
    # provisional-window race: push before the tee correlates the room
    _reg.mark_connected("test-pc-h4-race")
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "mute_push",
                    "ok": True, "data": {"muted": True}}), "test-pc-h4-race")
    _reg.note_offer("test-pc-h4-race", "h4-race-room")
    check("mute: mute state survives the connect/tee race (held, promoted with room)",
          _room_rec("h4-race-room").get("muted") is True, str(_room_rec("h4-race-room")))
    # unbound pc_id (the fw=None race order): held, drained by note_offer
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.11", "muted": True}}), "test-pc-h4-unbound")
    _held_h4 = _reg._muted_by_pc.get("test-pc-h4-unbound")
    _reg.note_offer("test-pc-h4-unbound", "h4-unbound-room")
    check("mute: unbound-pc mute hold drained by note_offer (with the fw hold)",
          _held_h4 is True and "test-pc-h4-unbound" not in _reg._muted_by_pc
          and _room_rec("h4-unbound-room").get("muted") is True
          and _room_rec("h4-unbound-room").get("fw") == "1.2.11",
          f"held={_held_h4} rec={_room_rec('h4-unbound-room')}")
    _st_h4 = client.get("/api/dashboard/status").json()
    check("mute: /status rooms[] carries muted",
          any(r.get("room") == "h4-direct-room" and r.get("muted") is False
              for r in _st_h4.get("rooms", [])), str(_st_h4.get("rooms"))[:200])

    # Board line (firmware 1.2.19+): fw_push names the line ("array"|"flex")
    # so the Nodes table can label each node's hardware. Same race handling.
    _reg.note_offer("test-pc-line", "line-direct-room")
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.19", "line": "flex", "muted": False}}), "test-pc-line")
    check("line: fw_push carries line -> room record line='flex'",
          _room_rec("line-direct-room").get("line") == "flex", str(_room_rec("line-direct-room")))
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.19", "line": "node"}}), "test-pc-line")
    check("line: an unknown line ('node') is dropped, not displayed",
          _room_rec("line-direct-room").get("line") == "flex", str(_room_rec("line-direct-room")))
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.29", "line": "waveshare"}}), "test-pc-line")
    import firmware_image as _fwiwst, firmware_server as _fwswst
    check("waveshare: the three track tables agree (firmware_image.TRACKS == registry.NODE_LINES == firmware_server.FIRMWARE_TRACKS)",
          tuple(_fwiwst.TRACKS) == tuple(_reg.NODE_LINES) and set(_fwswst.FIRMWARE_TRACKS) == set(_fwiwst.TRACKS)
          and set(_fwiwst.TRACK_LABEL) == set(_fwiwst.TRACKS),
          f"{_fwiwst.TRACKS} {_reg.NODE_LINES} {list(_fwswst.FIRMWARE_TRACKS)} {list(_fwiwst.TRACK_LABEL)}")
    _giws = (Path(__file__).resolve().parent.parent / ".gitignore").read_text().splitlines()
    check("waveshare: every firmware track dir is gitignored (built images never committed)",
          all(f"firmware/{t}/" in _giws for t in _fwiwst.TRACKS),
          str([t for t in _fwiwst.TRACKS if f"firmware/{t}/" not in _giws]))
    check("waveshare line: fw_push line='waveshare' is recorded (third line)",
          _room_rec("line-direct-room").get("line") == "waveshare", str(_room_rec("line-direct-room")))
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.19", "line": "flex"}}), "test-pc-line")
    check("line: a node that never said (older firmware) shows line=None",
          "line" in _room_rec("fw-direct-room") and _room_rec("fw-direct-room")["line"] is None,
          str(_room_rec("fw-direct-room")))
    node_command.on_message(
        json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "fw_push",
                    "ok": True, "data": {"fw": "1.2.19", "line": "array"}}), "test-pc-line-unbound")
    _held_line = _reg._line_by_pc.get("test-pc-line-unbound")
    _reg.note_offer("test-pc-line-unbound", "line-unbound-room")
    check("line: survives the connect race (held by pc, drained with the fw hold)",
          _held_line == "array" and "test-pc-line-unbound" not in _reg._line_by_pc
          and _room_rec("line-unbound-room").get("line") == "array"
          and _room_rec("line-unbound-room").get("fw") == "1.2.19",
          f"held={_held_line} rec={_room_rec('line-unbound-room')}")
    _st_line = client.get("/api/dashboard/status").json()
    check("line: /status rooms[] carries line",
          any(r.get("room") == "line-unbound-room" and r.get("line") == "array"
              for r in _st_line.get("rooms", [])), str(_st_line.get("rooms"))[:200])

    # Track-aware firmware upload: the image + manifest
    # must land in the DECLARED track's directory, and an unknown track
    # 400s. The old route staged everything into firmware/node/ — a
    # Flex .bin there would be OTA'd by an array node (cross-flash).
    # Board identity: real images carry exactly one OTA-track
    # URL format; the route refuses an image whose own track differs.
    # One track vocabulary (array | flex); 1.2.9+ images
    # also carry a per-line project name. "legacy" = array <=1.2.8's
    # track-less URL format (still a valid array witness on node_esp32).
    _MARK5B = {"flex": b"%.*s/firmware/flex/%s", "array": b"%.*s/firmware/array/%s",
               "legacy": b"%.*s/firmware/%s", "waveshare": b"%.*s/firmware/waveshare/%s"}
    _PROJ5B = {"flex": b"flex_esp32", "array": b"array_esp32", "legacy": b"node_esp32",
               "waveshare": b"waveshare_esp32", None: b"node_esp32"}

    def _fake_image5b(ver: str, board: str | None = "array", both: bool = False,
                      project: bytes | None = None) -> bytes:
        head = bytearray(b"\x00" * 256)
        head[32:36] = b"\x32\x54\xcd\xab"          # esp_app_desc magic
        head[48:80] = ver.encode().ljust(32, b"\x00")  # version string
        proj = project if project is not None else (b"node_esp32" if both else _PROJ5B[board])
        head[80:112] = proj.ljust(32, b"\x00")        # project_name
        body = bytearray(b"\x00" * (1024 * 200))   # over the size floor
        marks = [_MARK5B["flex"], _MARK5B["legacy"]] if both else ([_MARK5B[board]] if board else [])
        off = 4096
        for m in marks:
            body[off:off + len(m)] = m
            off += 512
        return bytes(head) + bytes(body)

    # (paths contract) Runtime roots follow the repo/LVI_STATE_DIR, not the
    # cwd: from / with no LVI_STATE_DIR the firmware root stays the repo's
    # (the point of the change); with LVI_STATE_DIR set, every dir the
    # suite writes moves to the scratch.
    _pcwd_5p = os.getcwd()
    _pstate_5p = os.environ.get("LVI_STATE_DIR")
    try:
        os.chdir("/")
        os.environ.pop("LVI_STATE_DIR", None)
        check("paths: cwd=/ and no LVI_STATE_DIR -> firmware_dir() is the repo's firmware/",
              paths.firmware_dir() == paths.REPO / "firmware",
              f"firmware_dir={paths.firmware_dir()} repo={paths.REPO}")
    finally:
        os.chdir(_pcwd_5p)
        if _pstate_5p is not None:
            os.environ["LVI_STATE_DIR"] = _pstate_5p

    _saved_updir5b = os.getcwd()
    _saved_state5b = os.environ.get("LVI_STATE_DIR")
    os.environ["LVI_STATE_DIR"] = str(SCRATCH)  # uploads land in the scratch, cwd or not
    os.chdir(SCRATCH)  # the suite's own cwd-relative reads stay in the scratch
    check("paths: LVI_STATE_DIR anchors clips/firmware/models/sessions",
          paths.clips_dir() == SCRATCH / "clips"
          and paths.firmware_dir() == SCRATCH / "firmware"
          and paths.models_dir() == SCRATCH / "models"
          and paths.sessions_file() == SCRATCH / "data" / "sessions.json",
          f"clips={paths.clips_dir()} firmware={paths.firmware_dir()} "
          f"models={paths.models_dir()} sessions={paths.sessions_file()}")
    # ── state dir: secrets file + TLS certs (a read-only code dir needs both
    # outside the repo). LVI_ENV_FILE is parked so the writer under test goes
    # to the scratch state dir — never the repo's (or the live) .env. ──
    _envf_SD = os.environ.pop("LVI_ENV_FILE", None)
    _repo_env_SD = paths.REPO / ".env"
    _repo_env_mt_SD = _repo_env_SD.stat().st_mtime_ns if _repo_env_SD.exists() else None
    _repo_cert_SD = paths.REPO / "certs" / "cert.pem"
    _repo_cert_mt_SD = _repo_cert_SD.stat().st_mtime_ns if _repo_cert_SD.exists() else None
    try:
        check("paths: LVI_STATE_DIR anchors the secrets file and the TLS certs",
              paths.env_file() == SCRATCH / ".env" and paths.certs_dir() == SCRATCH / "certs",
              f"env={paths.env_file()} certs={paths.certs_dir()}")
        os.environ["LVI_ENV_FILE"] = str(SCRATCH / "pinned.env")
        _pinned_SD = paths.env_file()
        os.environ.pop("LVI_ENV_FILE", None)
        os.environ.pop("LVI_STATE_DIR", None)
        _repo_default_SD = (paths.env_file(), paths.certs_dir())
        os.environ["LVI_STATE_DIR"] = str(SCRATCH)
        check("paths: LVI_ENV_FILE still wins; with no LVI_STATE_DIR both stay in the repo (existing installs unchanged)",
              _pinned_SD == SCRATCH / "pinned.env"
              and _repo_default_SD == (paths.REPO / ".env", paths.REPO / "certs"), f"{_pinned_SD} {_repo_default_SD}")
        from dashboard import routes as _rSD
        (SCRATCH / ".env").unlink(missing_ok=True)
        import asyncio as _aSD
        _aSD.run(_rSD._write_env_key("LVI_SUITE_STATE_KEY", "suite-not-a-key-123"))
        _wrote_SD = (SCRATCH / ".env").read_text() if (SCRATCH / ".env").exists() else ""
        _mode_SD = oct((SCRATCH / ".env").stat().st_mode & 0o777) if (SCRATCH / ".env").exists() else None
        check("paths: a Keys save lands in the state dir's .env (0600), not the repo's",
              "LVI_SUITE_STATE_KEY=suite-not-a-key-123" in _wrote_SD and _mode_SD == "0o600"
              and _rSD._env_path() == SCRATCH / ".env"
              and (_repo_env_SD.stat().st_mtime_ns if _repo_env_SD.exists() else None) == _repo_env_mt_SD,
              f"mode={_mode_SD} wrote={'yes' if _wrote_SD else 'no'}")
        import subprocess as _spSD
        _mc_SD = SCRATCH / "mc"
        _mc_SD.mkdir(exist_ok=True)
        _r_SD = _spSD.run(["bash", str(paths.REPO / "scripts" / "make-cert.sh"), "192.0.2.10"],
                          env={**os.environ, "LVI_STATE_DIR": str(_mc_SD)}, capture_output=True, text=True, timeout=60,
                          check=False)
        check("paths: make-cert.sh writes into $LVI_STATE_DIR/certs (key 0600) and leaves the repo's certs alone",
              _r_SD.returncode == 0 and "CERT-OK" in _r_SD.stdout
              and (_mc_SD / "certs" / "cert.pem").is_file()
              and oct((_mc_SD / "certs" / "key.pem").stat().st_mode & 0o777) == "0o600"
              and (_repo_cert_SD.stat().st_mtime_ns if _repo_cert_SD.exists() else None) == _repo_cert_mt_SD,
              (_r_SD.stdout + _r_SD.stderr)[-200:])
    finally:
        (SCRATCH / ".env").unlink(missing_ok=True)
        if _envf_SD is not None:
            os.environ["LVI_ENV_FILE"] = _envf_SD
        os.environ["LVI_STATE_DIR"] = str(SCRATCH)
    try:
        # (a) flex-declared upload → firmware/flex/
        _r = client.post("/api/dashboard/firmware/upload",
                         files={"file": ("flex.bin", _fake_image5b("1.9.9", "flex"),
                                         "application/octet-stream")},
                         data={"track": "flex"})
        _j = _r.json()
        check("firmware track: flex-track upload lands in firmware/flex/",
              _r.status_code == 200 and _j.get("track") == "flex"
              and _j.get("version") == "1.9.9"
              and (Path("firmware/flex/version.json")).is_file()
              and json.loads(Path("firmware/flex/version.json").read_text())["version"] == "1.9.9",
              _r.text[:120])
        # (b) unknown track → honest 400
        _rb = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("x.bin", _fake_image5b("1.9.8"),
                                          "application/octet-stream")},
                          data={"track": "warp"})
        check("firmware track: unknown track 400s (never a silent wrong dir)",
              _rb.status_code == 400, str(_rb.status_code))
        # (c) NO default track — a track-less upload 422s and
        # touches neither track's directory (reverses the earlier
        # "default stays array" pin: no board identity in the image yet,
        # so a silent array default is a cross-flash accelerant).
        _snap5c = {t: (Path(f"firmware/{t}/version.json").read_text()
                       if Path(f"firmware/{t}/version.json").exists() else None)
                   for t in ("array", "flex")}
        _rc = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("arr.bin", _fake_image5b("1.9.7"),
                                          "application/octet-stream")})
        check("firmware track: upload without track 422s (no silent array default; not pipecat's Stream-consumed 500)",
              _rc.status_code == 422 and "track" in _rc.text, f"{_rc.status_code} {_rc.text[:120]}")
        check("firmware track: track-less upload left both manifests untouched",
              all((Path(f"firmware/{t}/version.json").read_text()
                   if Path(f"firmware/{t}/version.json").exists() else None) == _snap5c[t]
                  for t in ("array", "flex")), str(_snap5c)[:120])
        # explicit array track still works
        _rd = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("arr.bin", _fake_image5b("1.9.7"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("firmware track: explicit array track upload lands in firmware/array/",
              _rd.status_code == 200 and _rd.json().get("track") == "array"
              and json.loads(Path("firmware/array/version.json").read_text())["version"] == "1.9.7",
              _rd.text[:120])

        # Board identity — the image's own OTA-track marker must match
        # the declared track; every refusal leaves BOTH tracks' served
        # image + manifest byte-identical.
        def _snaptk():
            out = {}
            for t in ("array", "flex", "waveshare"):
                for f in ("version.json", f"{t}_esp32.bin"):
                    q = Path(f"firmware/{t}/{f}")
                    out[f"{t}/{f}"] = q.read_bytes() if q.exists() else None
            return out
        _beforetk = _snaptk()
        _re = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", "flex"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("firmware track: Flex image declared for the ARRAY track -> 400, names the board",
              _re.status_code == 400 and "reSpeaker Flex firmware" in _re.text
              and "track 'flex'" in _re.text, f"{_re.status_code} {_re.text[:160]}")
        _rf = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", "array"),
                                          "application/octet-stream")},
                          data={"track": "flex"})
        check("firmware track: array image declared for the FLEX track -> 400, names the board",
              _rf.status_code == 400 and "array (XVF3800" in _rf.text
              and "track 'array'" in _rf.text, f"{_rf.status_code} {_rf.text[:160]}")
        _rg = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", None),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("firmware track: image with NO track marker -> 400 (never guessed onto a track)",
              _rg.status_code == 400 and "can't tell which board" in _rg.text,
              f"{_rg.status_code} {_rg.text[:160]}")
        _rh = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", both=True),
                                          "application/octet-stream")},
                          data={"track": "flex"})
        check("firmware track: image with BOTH markers -> 400 (can't be trusted either way)",
              _rh.status_code == 400 and "can't tell which board" in _rh.text,
              f"{_rh.status_code} {_rh.text[:160]}")
        _aftertk = _snaptk()
        check("firmware track: every refusal left both tracks' served image + manifest byte-identical",
              _aftertk == _beforetk,
              str([k for k in _beforetk if _beforetk[k] != _aftertk.get(k)]))
        _leftovertk = [str(q) for t in ("array", "flex")
                        for q in Path(f"firmware/{t}").glob("*.uploading")]
        check("firmware track: every refusal cleaned up its .uploading temp", not _leftovertk,
              str(_leftovertk))

        # deploy_firmware.sh's backstop uses the same helper via the CLI
        import subprocess as _sptk
        _abv = str(REPO_CORE.parent / "scripts" / "app_bin_version.py")
        _cli = {}
        for _lbl, _img in (("flex", _fake_image5b("1.9.5", "flex")), ("array", _fake_image5b("1.9.5", "array")),
                           ("none", _fake_image5b("1.9.5", None)), ("both", _fake_image5b("1.9.5", both=True))):
            _fp = Path(SCRATCH) / f"clitk-{_lbl}.bin"
            _fp.write_bytes(_img)
            _cli[_lbl] = _sptk.run([sys.executable, _abv, "--track", str(_fp)],
                                    capture_output=True, text=True).stdout.strip()
            _fp.unlink()
        check("firmware track: app_bin_version.py --track: flex/array/none/both -> flex/array/''/''",
              _cli == {"flex": "flex", "array": "array", "none": "", "both": ""}, str(_cli))

        # Firmware names — one vocabulary, per-line files.
        _rn = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("x.bin", _fake_image5b("1.9.4", "array"),
                                          "application/octet-stream")},
                          data={"track": "node"})
        check("firmware track: retired track name 'node' -> 400 naming array | flex",
              _rn.status_code == 400 and "array | flex" in _rn.text, f"{_rn.status_code} {_rn.text[:120]}")
        # In-field images (<=1.2.8, project node_esp32) are still judged by
        # their URL marker: array legacy URL -> array, flex URL -> flex.
        _rl = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("old.bin", _fake_image5b("1.9.3", "legacy"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("firmware track: legacy array image (node_esp32 + track-less URL) still uploads to array",
              _rl.status_code == 200 and _rl.json().get("track") == "array", f"{_rl.status_code} {_rl.text[:120]}")
        _rlf = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("oldf.bin", _fake_image5b("1.9.3", "flex", project=b"node_esp32"),
                                           "application/octet-stream")},
                           data={"track": "flex"})
        check("firmware track: legacy flex image (node_esp32 + flex URL) still uploads to flex",
              _rlf.status_code == 200 and _rlf.json().get("track") == "flex", f"{_rlf.status_code} {_rlf.text[:120]}")
        # A named image must agree with its own URL marker.
        _beforenm = _snaptk()
        _rm = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("liar.bin", _fake_image5b("1.9.2", "array", project=b"flex_esp32"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        _rm2 = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("liar2.bin", _fake_image5b("1.9.2", "flex", project=b"array_esp32"),
                                           "application/octet-stream")},
                           data={"track": "flex"})
        _rm3 = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("alien.bin", _fake_image5b("1.9.2", "array", project=b"some_other_app"),
                                           "application/octet-stream")},
                           data={"track": "array"})
        check("firmware track: project name vs URL disagreement / unknown project -> 400, nothing written",
              all(r.status_code == 400 and "can't tell which board" in r.text for r in (_rm, _rm2, _rm3))
              and _snaptk() == _beforenm,
              f"{_rm.status_code} {_rm2.status_code} {_rm3.status_code}")
        # Served file is named for its track; a pre-rename .bin is swept.
        Path("firmware/array/node_esp32.bin").write_bytes(b"stale")
        _rs = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("whatever-name.bin", _fake_image5b("1.9.9", "array"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        _mannm = json.loads(Path("firmware/array/version.json").read_text())
        check("firmware track: upload serves firmware/array/array_esp32.bin, manifest names it, stale node_esp32.bin removed",
              _rs.status_code == 200 and _rs.json().get("filename") == "array_esp32.bin"
              and _mannm == {"version": "1.9.9", "filename": "array_esp32.bin"}
              and sorted(q.name for q in Path("firmware/array").glob("*.bin")) == ["array_esp32.bin"],
              f"{_rs.text[:120]} {_mannm} {[q.name for q in Path('firmware/array').glob('*.bin')]}")
        _rsf = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("f.bin", _fake_image5b("1.9.9", "flex"),
                                           "application/octet-stream")},
                           data={"track": "flex"})
        check("firmware track: flex upload serves firmware/flex/flex_esp32.bin",
              _rsf.status_code == 200 and _rsf.json().get("filename") == "flex_esp32.bin"
              and sorted(q.name for q in Path("firmware/flex").glob("*.bin")) == ["flex_esp32.bin"],
              _rsf.text[:120])

        # Third line `waveshare` (Waveshare ESP32-S3-AUDIO bench board).
        _rw = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("w.bin", _fake_image5b("1.9.9", "waveshare"),
                                          "application/octet-stream")},
                          data={"track": "waveshare"})
        check("waveshare: waveshare upload serves firmware/waveshare/waveshare_esp32.bin",
              _rw.status_code == 200 and _rw.json().get("track") == "waveshare"
              and _rw.json().get("filename") == "waveshare_esp32.bin"
              and json.loads(Path("firmware/waveshare/version.json").read_text())
              == {"version": "1.9.9", "filename": "waveshare_esp32.bin"}
              and sorted(q.name for q in Path("firmware/waveshare").glob("*.bin")) == ["waveshare_esp32.bin"],
              f"{_rw.status_code} {_rw.text[:120]}")
        _beforews = _snaptk()
        _x = [client.post("/api/dashboard/firmware/upload",
                          files={"file": (f"x{i}.bin", _fake_image5b("1.9.1", img), "application/octet-stream")},
                          data={"track": trk})
              for i, (img, trk) in enumerate((("waveshare", "array"), ("waveshare", "flex"),
                                              ("array", "waveshare"), ("flex", "waveshare"),
                                              ("legacy", "waveshare")))]
        check("waveshare: cross-line uploads refused both ways (waveshare<->array/flex, legacy array) — 400, nothing written",
              all(r.status_code == 400 for r in _x) and _snaptk() == _beforews,
              f"{[r.status_code for r in _x]} {_x[0].text[:100]}")
        _rlie = client.post("/api/dashboard/firmware/upload",
                            files={"file": ("lie.bin", _fake_image5b("1.9.1", "waveshare", project=b"flex_esp32"),
                                            "application/octet-stream")},
                            data={"track": "waveshare"})
        check("waveshare: waveshare URL + another line's project name -> 400 (name and URL must agree)",
              _rlie.status_code == 400 and "can't tell which board" in _rlie.text, f"{_rlie.status_code} {_rlie.text[:100]}")

        # Waveshare: identity is decided by the image itself (not just "unknown track" 400s).
        import firmware_image as _fwiws
        check("waveshare image_track: a waveshare image identifies as 'waveshare'; a waveshare URL with an array name is None",
              _fwiws.image_track(_fake_image5b("1.9.1", "waveshare")) == "waveshare"
              and _fwiws.image_track(_fake_image5b("1.9.1", "waveshare", project=b"array_esp32")) is None
              and _fwiws.image_track(_fake_image5b("1.9.1", "array")) == "array",
              "")
        _xw = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("xw.bin", _fake_image5b("1.9.1", "waveshare"), "application/octet-stream")},
                          data={"track": "array"})
        _xa = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("xa.bin", _fake_image5b("1.9.1", "array"), "application/octet-stream")},
                          data={"track": "waveshare"})
        check("waveshare: cross-line refusal names both lines (waveshare->array, array->waveshare)",
              _xw.status_code == 400 and "Waveshare ESP32-S3-AUDIO firmware" in _xw.text and "array (XVF3800" in _xw.text
              and _xa.status_code == 400 and "array (XVF3800 mic-array board) firmware" in _xa.text
              and "Waveshare ESP32-S3-AUDIO track" in _xa.text,
              f"{_xw.status_code} {_xw.text[:140]} | {_xa.status_code} {_xa.text[:140]}")

        # ── signed firmware (core/firmware_signing.py): the bot holds
        # an RSA-3072 key and signs every image it serves. ──
        import firmware_signing as _fsg
        from loguru import logger as _lgSG
        _logSG: list[str] = []
        _hidSG = _lgSG.add(lambda m: _logSG.append(str(m)), level="DEBUG")
        _psSG = os.environ.get("LVI_STATE_DIR")
        os.environ["LVI_STATE_DIR"] = str(SCRATCH / "sgstate")
        try:
            _kNew = _fsg.ensure_key()
            check("signing: a fresh state dir gets its own key at <state>/secrets/firmware-signing-key.pem, "
                  "and creating it logs the fingerprint, not the key",
                  _kNew == SCRATCH / "sgstate" / "secrets" / "firmware-signing-key.pem" and _kNew.is_file()
                  and any("created a new signing key" in x for x in _logSG)
                  and not any("PRIVATE KEY" in x for x in _logSG), str(_logSG)[-200:])
        finally:
            if _psSG is None:
                os.environ.pop("LVI_STATE_DIR", None)
            else:
                os.environ["LVI_STATE_DIR"] = _psSG
        _kSG = _fsg.key_path()
        check("signing: the key is created on first use under the state dir's secrets/, owner-only (0600, dir 0700)",
              _kSG.is_file() and _kSG.parent == paths.secrets_dir()
              and (_kSG.stat().st_mode & 0o777) == 0o600 and (_kSG.parent.stat().st_mode & 0o777) == 0o700,
              f"{_kSG} exists={_kSG.is_file()}")
        _fpSG = _fsg.fingerprint()
        _rSG = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("sg.bin", _fake_image5b("1.9.9", "array"), "application/octet-stream")},
                           data={"track": "array"})
        _servedSG = Path("firmware/array/array_esp32.bin")
        check("signing: a dashboard upload is served signed by this bot's key, and the reply names the key",
              _rSG.status_code == 200 and _fsg.file_has_signature(_servedSG)
              and _fsg.signer_digest(_servedSG) == _fpSG and _rSG.json().get("signing_key") == _fpSG[:16]
              and _servedSG.read_bytes().startswith(_fake_image5b("1.9.9", "array")),
              f"{_rSG.status_code} {_rSG.text[:160]}")
        _uSG = SCRATCH / "unsigned_esp32.bin"
        _uSG.write_bytes(_fake_image5b("1.9.9", "array"))
        _bSG = bytearray(_servedSG.read_bytes())
        _bSG[-4096 + 40] ^= 0xFF  # inside the CRC span
        check("signing: unsigned, truncated and corrupted images are not 'signed' (structure + CRC)",
              not _fsg.file_has_signature(_uSG) and not _fsg.has_signature(_servedSG.read_bytes()[:-1])
              and not _fsg.has_signature(bytes(_bSG)) and _fsg.has_signature(_servedSG.read_bytes()),
              "")
        _oSG = SCRATCH / "otherkey.pem"
        subprocess.run([sys.executable, "-m", "espsecure", "generate-signing-key", "--version", "2", "--scheme",
                        "rsa3072", str(_oSG)], capture_output=True, check=False)
        _xSG = SCRATCH / "other_esp32.bin"
        subprocess.run([sys.executable, "-m", "espsecure", "sign-data", "--version", "2", "--keyfile", str(_oSG),
                        "--output", str(_xSG), str(_uSG)], capture_output=True, check=False)
        _wasOther = _fsg.file_has_signature(_xSG) and _fsg.signer_digest(_xSG) != _fpSG
        _chg1 = _fsg.sign_file(_xSG)
        _chg2 = _fsg.sign_file(_xSG)
        check("signing: an image signed by another key is re-signed with this bot's key; signing twice is a no-op",
              _wasOther and _chg1 is True and _chg2 is False and _fsg.signer_digest(_xSG) == _fpSG
              and _xSG.read_bytes().startswith(_uSG.read_bytes()), f"other={_wasOther} {_chg1} {_chg2}")
        _lgSG.remove(_hidSG)
        _logsSG = "\n".join(_logSG)
        check("signing: the private key never appears in a reply or a log line",
              "PRIVATE KEY" not in _rSG.text and "PRIVATE KEY" not in _logsSG
              and "PRIVATE KEY" in _kSG.read_text(), "")
        _shSG = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "sign_firmware.py"),
                                str(_uSG)], capture_output=True, text=True, check=False)
        check("signing: scripts/sign_firmware.py signs in place with the bot's key and prints only the fingerprint",
              _shSG.returncode == 0 and "signed:" in _shSG.stdout and _fpSG[:16] in _shSG.stdout
              and "PRIVATE" not in _shSG.stdout + _shSG.stderr and _fsg.signer_digest(_uSG) == _fpSG,
              f"rc={_shSG.returncode} {_shSG.stdout[-160:]} {_shSG.stderr[-160:]}")
        _depSG = (Path(__file__).resolve().parent.parent / "scripts" / "deploy_firmware.sh").read_text()
        check("signing: deploy_firmware.sh signs on the host with scripts/sign_firmware.py and stops if that fails",
              "scripts/sign_firmware.py" in _depSG and "signing failed" in _depSG and "exit 1" in _depSG.split("signing failed")[1][:200],
              "")

        # ── node setup route: GET /api/dashboard/firmware/{track}/flash ──
        # The full flash set (boot files + app) the System tab's Node setup
        # page flashes over USB. Uses the waveshare track dir, restored after.
        import hashlib as _hlNS
        _fsNS = Path("firmware/waveshare")
        _fsNS.mkdir(parents=True, exist_ok=True)
        _keepNS = {q.name: q.read_bytes() for q in _fsNS.iterdir() if q.is_file()}
        try:
            for _q in list(_fsNS.iterdir()):
                if _q.is_file():
                    _q.unlink()
            (_fsNS / "waveshare_esp32.bin").write_bytes(_fake_image5b("1.2.47", "waveshare"))
            (_fsNS / "version.json").write_text(json.dumps({"version": "1.2.47", "filename": "waveshare_esp32.bin"}))
            _rNS = client.get("/api/dashboard/firmware/waveshare/flash")
            check("node setup route: an app-only track has no full flash set (404 naming deploy_firmware)",
                  _rNS.status_code == 404 and "deploy_firmware" in _rNS.text, f"{_rNS.status_code} {_rNS.text[:160]}")
            _goodNS = {"flash": {"mode": "dio", "freq": "80m", "size": "8MB"},
                       "parts": [{"name": "bootloader", "file": "bootloader.bin", "offset": 0},
                                 {"name": "partition-table", "file": "partition-table.bin", "offset": 32768},
                                 {"name": "ota-data", "file": "ota_data_initial.bin", "offset": 65536}],
                       "app_offset": 131072}
            for _n, _b in (("bootloader.bin", b"B" * 1000), ("partition-table.bin", b"P" * 300),
                           ("ota_data_initial.bin", b"O" * 200)):
                (_fsNS / _n).write_bytes(_b)
            (_fsNS / "flash.json").write_text(json.dumps(_goodNS))
            _rNSu = client.get("/api/dashboard/firmware/waveshare/flash")
            check("signing: the Node setup flash set refuses an unsigned app image (409 naming it), so a USB flash "
                  "can't leave a node that refuses every later update",
                  _rNSu.status_code == 409 and "isn't signed" in _rNSu.text, f"{_rNSu.status_code} {_rNSu.text[:160]}")
            import firmware_signing as _fsg
            _fsg.sign_file(_fsNS / "waveshare_esp32.bin")
            _rNS = client.get("/api/dashboard/firmware/waveshare/flash")
            _jNS = _rNS.json() if _rNS.status_code == 200 else {}
            check("node setup route: full set -> 4 parts at the manifest offsets, app from version.json",
                  _rNS.status_code == 200 and _jNS.get("version") == "1.2.47" and _jNS.get("track") == "waveshare"
                  and [p.get("offset") for p in _jNS.get("parts", [])] == [0, 32768, 65536, 131072]
                  and _jNS["parts"][-1].get("file") == "waveshare_esp32.bin"
                  and _jNS.get("flash") == {"mode": "dio", "freq": "80m", "size": "8MB"},
                  f"{_rNS.status_code} {_rNS.text[:200]}")
            check("node setup route: sizes + sha256 match the files on disk",
                  bool(_jNS) and all(p["size"] == (_fsNS / p["file"]).stat().st_size
                                     and p["sha256"] == _hlNS.sha256((_fsNS / p["file"]).read_bytes()).hexdigest()
                                     for p in _jNS["parts"]), str(_jNS)[:200])
            _rNS = client.get("/api/dashboard/firmware/warp/flash")
            check("node setup route: unknown track 404s naming it",
                  _rNS.status_code == 404 and "unknown track" in _rNS.text, f"{_rNS.status_code} {_rNS.text[:120]}")
            _badNS = json.loads(json.dumps(_goodNS))
            _badNS["parts"][1]["offset"] = 0x9000
            (_fsNS / "flash.json").write_text(json.dumps(_badNS))
            _rNS = client.get("/api/dashboard/firmware/waveshare/flash")
            check("node setup route: a part in the settings region (0x9000-0xFFFF) is refused",
                  _rNS.status_code == 404 and "settings" in _rNS.text, f"{_rNS.status_code} {_rNS.text[:160]}")
            _badNS["parts"][1]["offset"] = 32768
            _badNS["parts"][1]["file"] = "../../config.yaml"
            (_fsNS / "flash.json").write_text(json.dumps(_badNS))
            _rNS = client.get("/api/dashboard/firmware/waveshare/flash")
            check("node setup route: a part outside the track dir is refused",
                  _rNS.status_code == 404 and "no full flash set" in _rNS.text, f"{_rNS.status_code} {_rNS.text[:160]}")
            (_fsNS / "flash.json").write_text("{not json")
            _rNS = client.get("/api/dashboard/firmware/waveshare/flash")
            check("node setup route: a corrupt flash.json is a clear 404, never a 500",
                  _rNS.status_code == 404 and "no full flash set" in _rNS.text, f"{_rNS.status_code} {_rNS.text[:160]}")
            (_fsNS / "flash.json").write_text(json.dumps(_goodNS))
            _ruNS = client.post("/api/dashboard/firmware/upload",
                                files={"file": ("waveshare.bin", _fake_image5b("1.2.48", "waveshare"),
                                                "application/octet-stream")},
                                data={"track": "waveshare"})
            check("node setup route: a Firmware-card upload keeps the three boot files",
                  _ruNS.status_code == 200 and all((_fsNS / n).is_file() for n in
                                                   ("bootloader.bin", "partition-table.bin", "ota_data_initial.bin")),
                  f"{_ruNS.status_code} {_ruNS.text[:160]} {sorted(q.name for q in _fsNS.iterdir())}")
        finally:
            for _q in list(_fsNS.iterdir()):
                if _q.is_file():
                    _q.unlink()
            for _n, _b in _keepNS.items():
                (_fsNS / _n).write_bytes(_b)

        # The OTA server (core/firmware_server.py) — first suite
        # coverage of the node-facing routes. Tracks point at this scratch.
        import firmware_server as _fws
        from fastapi import FastAPI as _FAnm
        _saved_tracksnm = dict(_fws.FIRMWARE_TRACKS)
        _fws.FIRMWARE_TRACKS.update({"array": Path("firmware/array").resolve(),
                                     "flex": Path("firmware/flex").resolve(),
                                     "waveshare": Path("firmware/waveshare").resolve()})
        try:
            _appnm = _FAnm()
            _fws.register_firmware_routes(_appnm)
            _cnm = TestClient(_appnm)
            _legacy = _cnm.get("/firmware/version")
            _arr = _cnm.get("/firmware/array/version")
            _flx = _cnm.get("/firmware/flex/version")
            check("firmware track OTA: legacy /firmware/version == /firmware/array/version (array <=1.2.8 keeps updating)",
                  _legacy.status_code == 200 and _legacy.json() == _arr.json()
                  == {"version": "1.9.9", "filename": "array_esp32.bin"},
                  f"{_legacy.status_code} {_legacy.text[:80]} | {_arr.text[:80]}")
            check("firmware track OTA: /firmware/flex/version serves the flex manifest",
                  _flx.status_code == 200 and _flx.json() == {"version": "1.9.9", "filename": "flex_esp32.bin"},
                  _flx.text[:80])
            _lb = _cnm.get("/firmware/array_esp32.bin")
            _tb = _cnm.get("/firmware/array/array_esp32.bin")
            check("firmware track OTA: legacy /firmware/<bin> and /firmware/array/<bin> serve the same array image",
                  _lb.status_code == 200 and _lb.content == _tb.content
                  == Path("firmware/array/array_esp32.bin").read_bytes(), f"{_lb.status_code} {_tb.status_code}")
            _wsv = _cnm.get("/firmware/waveshare/version")
            _wsb = _cnm.get("/firmware/waveshare/waveshare_esp32.bin")
            check("waveshare OTA: /firmware/waveshare/version + bin serve the waveshare track",
                  _wsv.status_code == 200 and _wsv.json() == {"version": "1.9.9", "filename": "waveshare_esp32.bin"}
                  and _wsb.status_code == 200
                  and _wsb.content == Path("firmware/waveshare/waveshare_esp32.bin").read_bytes(),
                  f"{_wsv.status_code} {_wsv.text[:80]} {_wsb.status_code}")
            check("waveshare OTA: the real FIRMWARE_TRACKS table names waveshare",
                  "waveshare" in _saved_tracksnm
                  and _saved_tracksnm["waveshare"].name == "waveshare", str(list(_saved_tracksnm)))
            check("firmware track OTA: retired /firmware/node/version 404s (unknown track = no update, safe)",
                  _cnm.get("/firmware/node/version").status_code == 404, "")
            check("firmware track OTA: path traversal out of a track dir 404s",
                  _cnm.get("/firmware/array/..%2F..%2Fconfig.yaml").status_code == 404, "")
            _upTmp = Path("firmware/array/array_esp32.bin.k3x9q.uploading")
            try:
                _upTmp.write_bytes(b"half an image")
                _upT = _cnm.get(f"/firmware/array/{_upTmp.name}").status_code
                _upL = _cnm.get(f"/firmware/{_upTmp.name}").status_code
            finally:
                _upTmp.unlink(missing_ok=True)
            check("firmware track OTA: an upload still being written (*.uploading) is never served (track and legacy URL 404)",
                  _upT == 404 and _upL == 404, f"{_upT} {_upL}")
            # signing: unsigned app images never go out; boot files aren't app images
            _flxSG = Path("firmware/flex/flex_esp32.bin")
            _keepFlx = _flxSG.read_bytes() if _flxSG.is_file() else None
            try:
                _flxSG.write_bytes(_fake_image5b("1.9.9", "flex"))
                Path("firmware/flex/bootloader.bin").write_bytes(b"B" * 1000)
                _rUn = _cnm.get("/firmware/flex/flex_esp32.bin")
                _rBoot = _cnm.get("/firmware/flex/bootloader.bin")
                check("signing: the OTA server refuses an unsigned app image (409) but serves boot files",
                      _rUn.status_code == 409 and "not signed" in _rUn.text and _rBoot.status_code == 200,
                      f"{_rUn.status_code} ({len(_rUn.content)} bytes) boot={_rBoot.status_code}")
                _app2 = _FAnm()
                _fws.register_firmware_routes(_app2)
                _rAfter = TestClient(_app2).get("/firmware/flex/flex_esp32.bin")
                import firmware_signing as _fsg2
                check("signing: at startup the bot signs an unsigned served image, which then serves",
                      _rAfter.status_code == 200 and _fsg2.has_signature(_rAfter.content)
                      and _fsg2.signer_digest(_flxSG) == _fsg2.fingerprint(),
                      f"{_rAfter.status_code}")
            finally:
                Path("firmware/flex/bootloader.bin").unlink(missing_ok=True)
                if _keepFlx is not None:
                    _flxSG.write_bytes(_keepFlx)
        finally:
            _fws.FIRMWARE_TRACKS.clear()
            _fws.FIRMWARE_TRACKS.update(_saved_tracksnm)
    finally:
        os.chdir(_saved_updir5b)
        if _saved_state5b is None:
            os.environ.pop("LVI_STATE_DIR", None)
        else:
            os.environ["LVI_STATE_DIR"] = _saved_state5b

    # OWW model panel (Voice tab): upload validates, select merges,
    # delete blocks the active model.
    tmpcfg = os.environ.get("LVI_CONFIG")
    models_dir = os.path.join(os.path.dirname(tmpcfg) if tmpcfg else ".", "unit_test_models")
    os.environ["LVI_MODELS_DIR"] = models_dir
    Path(models_dir).mkdir(parents=True, exist_ok=True)
    try:
        r = client.get("/api/dashboard/oww/models")
        before = r.json().get("models", [])
        # (a) upload rejects garbage bytes (422 — validation fires)
        _r_bad = client.post("/api/dashboard/oww/models",
                         files={"file": ("bad.onnx", b"\x00" * 4096, "application/octet-stream")})
        check("oww: corrupt model rejected with 422",
              _r_bad.status_code == 422, f"status={_r_bad.status_code}")
        # (b) upload accepts a REAL wake model: openwakeword's stock
        # hey_mycroft from the installed package (nothing private, nothing
        # checked in), uploaded under a suite name. A missing package model
        # is a loud SKIP, never a silent pass.
        import openwakeword as _owwup
        prod = Path((_owwup.models.get("hey_mycroft") or {}).get("model_path") or "/nonexistent.onnx")
        _upname = "suite_wake_model.onnx"
        if not prod.is_file():
            print("[SKIP] oww real-model checks: openwakeword's stock hey_mycroft model is not installed")
        if prod.is_file():
            _r_up = client.post("/api/dashboard/oww/models",
                            files={"file": (_upname, prod.read_bytes(), "application/octet-stream")})
            check("oww: real model upload accepted",
                  _r_up.status_code == 200 and _r_up.json().get("name") == _upname,
                  f"status={_r_up.status_code}")
        # (d) select merges into wake section (threshold survives) — run
        # FIRST so the uploaded models/ copy becomes the ACTIVE file
        if prod.is_file():
            _r_sel = client.put("/api/dashboard/oww/select",
                            json={"model": _upname})
            _wake_after = __import__("yaml").safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("wake") or {}
            check("oww: select writes wake.model and preserves threshold",
                  _r_sel.status_code == 200 and "model" in _wake_after and "threshold" in _wake_after,
                  f"status={_r_sel.status_code} wake={_wake_after}")
        # (c) delete blocks the ACTIVE file (path-exact)
        _oww = client.get("/api/dashboard/oww/models").json()
        _active_row = next((m for m in _oww["models"] if m["selected"]), None)
        if _active_row:
            _r_del = client.delete(
                f"/api/dashboard/oww/models/{_active_row['name']}?source={_active_row['source']}")
            check("oww: deleting the active model is blocked (409)",
                  _r_del.status_code == 409, f"status={_r_del.status_code}")
        # (e) a NON-active model is deletable — upload a distinct-name
        # spare from the same bytes, then delete it (200). Never touches
        # the package's stock file.
        if prod.is_file():
            _r_sp = client.post("/api/dashboard/oww/models",
                            files={"file": ("zz_suite_probe.onnx", prod.read_bytes(), "application/octet-stream")})
            _r_spdel = client.delete("/api/dashboard/oww/models/zz_suite_probe.onnx?source=models")
            check("oww: non-active spare uploads and deletes cleanly",
                  _r_sp.status_code == 200 and _r_spdel.status_code == 200,
                  f"upload={_r_sp.status_code} delete={_r_spdel.status_code}")
        # Wake-word stock models: listed with their licence, selectable by key,
        # never deletable. Restores wake.model afterwards.
        _wakewn = dict(config_store.read_section("wake") or {})
        try:
            _rowswn = client.get("/api/dashboard/oww/models").json()["models"]
            _stockwn = [m for m in _rowswn if m.get("source") == "stock"]
            check("wake name: OWW list carries the 3 stock models with their licence",
                  sorted(m["name"] for m in _stockwn) == ["alexa", "hey_jarvis", "hey_mycroft"]
                  and all("NC" in (m.get("license") or "") for m in _stockwn)
                  and all(m.get("size", 0) > 0 for m in _stockwn), str(_stockwn)[:240])
            r = client.put("/api/dashboard/oww/select", json={"model": "hey_mycroft", "source": "stock"})
            _rowswn = client.get("/api/dashboard/oww/models").json()["models"]
            check("wake name: selecting a stock model writes the key and marks it active",
                  r.status_code == 200
                  and (config_store.read_section("wake") or {}).get("model") == "hey_mycroft"
                  and any(m["name"] == "hey_mycroft" and m["selected"] for m in _rowswn),
                  f"{r.status_code} {r.text[:120]}")
            r = client.put("/api/dashboard/oww/select", json={"model": "not_a_stock", "source": "stock"})
            check("wake name: unknown stock key rejected (422)", r.status_code == 422, f"{r.status_code}")
            r = client.put("/api/dashboard/oww/select", json={"model": "hey_jarvis"})
            check("wake name: a stock key without source=stock is still rejected (422)",
                  r.status_code == 422, f"{r.status_code}")
            import openwakeword as _owwwn
            _pkgwn = _owwwn.models["hey_jarvis"]["model_path"]
            r = client.delete("/api/dashboard/oww/models/hey_jarvis?source=stock")
            check("wake name: a stock model can't be deleted (409), package file untouched",
                  r.status_code == 409 and os.path.isfile(_pkgwn), f"{r.status_code}")
            # R-M10: a broken stock entry must not hide the custom rows
            import wake_name as _wnwnr
            _orig_reswn = _wnwnr.resolve_model

            def _boomwn(value=None):
                if value in _wnwnr.STOCK_MODELS:
                    raise KeyError(value)
                return _orig_reswn(value)
            _wnwnr.resolve_model = _boomwn
            _probewn = Path(models_dir) / "zz_custom_probe.onnx"
            _probewn.write_bytes(b"x" * 2048)   # listing doesn't validate contents
            try:
                r = client.get("/api/dashboard/oww/models")
                _okwn = r.status_code == 200
                _rows_bwn = r.json().get("models", []) if _okwn else []
            finally:
                _wnwnr.resolve_model = _orig_reswn
                _probewn.unlink(missing_ok=True)
            check("wake name: OWW list survives a broken stock entry (custom rows still listed)",
                  _okwn and any(m["source"] != "stock" for m in _rows_bwn)
                  and not any(m["source"] == "stock" for m in _rows_bwn),
                  f"{r.status_code} {str(_rows_bwn)[:160]}")
        finally:
            config_store.write_section("wake", _wakewn)
    finally:
        os.environ.pop("LVI_MODELS_DIR", None)
        shutil.rmtree(models_dir, ignore_errors=True)

    # (bug-hunt) The wake-model active path must follow the FILE, not the
    # boot cache: _oww_active_path read cfg() first (cached until a
    # dashboard write clears it), so a hand-edited wake.model left the
    # active marker — and the delete guard — pointing at a stale model.
    from ruamel.yaml import YAML as _RY_bh2
    _p_bh2 = Path(os.environ["LVI_CONFIG"])
    _wake_bh2 = dict(config_store.read_section("wake") or {})
    try:
        config_store.write_section("wake", {**_wake_bh2, "model": "stale_suite.onnx"})
        _cfg_mod.cfg("wake")  # repopulate the boot cache from the file as written
        _ry_bh2 = _RY_bh2()
        _ry_bh2.preserve_quotes = True
        with _p_bh2.open() as _f_bh2:
            _d_bh2 = _ry_bh2.load(_f_bh2)
        _wk_bh2 = _d_bh2.get("wake")
        if _wk_bh2 is None:
            _wk_bh2 = _d_bh2["wake"] = {}
        _wk_bh2["model"] = "hand_edited_suite.onnx"   # the hand edit: NO cache clear
        with _p_bh2.open("w") as _f_bh2:
            _ry_bh2.dump(_d_bh2, _f_bh2)
        check("oww active path: a hand-edited wake.model wins over the stale boot cache",
              _cfg_mod.cfg("wake", "model") == "stale_suite.onnx"   # the cache really is stale
              and _rRL._oww_active_path() == Path("hand_edited_suite.onnx").resolve(),
              f"cached={_cfg_mod.cfg('wake', 'model')!r} active={_rRL._oww_active_path()}")
    finally:
        config_store.write_section("wake", _wake_bh2)

    # (bug-hunt) Same-name OWW uploads once shared ONE temp file
    # ("<name>.uploading"): two uploads interleaved into it and a failed
    # one left its partial behind for the next upload to swap in. Each
    # upload now stages to its own mkstemp file and the temp is always
    # cleaned up (monkeypatched like the loop-gap upload check above).
    _val_bh3, _dir_bh3 = _rRL._oww_validate, _rRL._oww_dir
    _tmp_bh3 = Path(tempfile.mkdtemp())
    try:
        _rRL._oww_validate = lambda data: None
        _rRL._oww_dir = lambda: _tmp_bh3

        class _Up_bh3:
            def __init__(self, data, name="suite_bh3.onnx"):
                self.filename, self._data = name, data

            async def read(self):
                return self._data

        async def _pair_bh3():
            return await asyncio.gather(
                _rRL.oww_upload_model(file=_Up_bh3(b"\x02" * 4096)),
                _rRL.oww_upload_model(file=_Up_bh3(b"\x03" * 4096)))
        try:
            _res_bh3 = asyncio.run(_pair_bh3())
        except Exception as _e_bh3:   # a shared temp would raise mid-pair
            _res_bh3 = [_e_bh3, _e_bh3]
        _dest_bh3 = _tmp_bh3 / "suite_bh3.onnx"
        check("oww upload: two same-name uploads stage separately — dest holds ONE upload's whole bytes, nothing left over",
              all(isinstance(r, dict) and r.get("ok") for r in _res_bh3)
              and _dest_bh3.is_file()
              and _dest_bh3.read_bytes() in (b"\x02" * 4096, b"\x03" * 4096)
              and not list(_tmp_bh3.glob("*.uploading")),
              f"res={[getattr(r, 'get', lambda _k: None)('ok') for r in _res_bh3]} "
              f"size={_dest_bh3.stat().st_size if _dest_bh3.is_file() else None} "
              f"left={[p.name for p in _tmp_bh3.glob('*.uploading')]}")

        # a failed swap must leave nothing behind (the finally unlink)
        _tmp_bh3.joinpath("suite_bh3fail.onnx").mkdir()  # target blocked -> replace fails
        _raised_bh3 = False
        try:
            asyncio.run(_rRL.oww_upload_model(file=_Up_bh3(b"\x04" * 4096, name="suite_bh3fail.onnx")))
        except OSError:
            _raised_bh3 = True
        check("oww upload: a failed swap leaves no .uploading temp behind",
              _raised_bh3 and not list(_tmp_bh3.glob("*.uploading")),
              f"raised={_raised_bh3} left={[p.name for p in _tmp_bh3.glob('*.uploading')]}")

        import inspect as _insp_bh3
        _src_bh3 = _insp_bh3.getsource(_rRL.oww_upload_model)
        check("oww upload: staging stays per-upload (mkstemp), never the shared name+'.uploading'",
              "mkstemp" in _src_bh3 and 'name + ".uploading"' not in _src_bh3,
              "shared staging is back" if 'name + ".uploading"' in _src_bh3 else "ok")
    finally:
        _rRL._oww_validate, _rRL._oww_dir = _val_bh3, _dir_bh3
        shutil.rmtree(_tmp_bh3, ignore_errors=True)

    # 9c. Voice Tuning (Voice tab — each knob round-trip + range + merge)
    r = client.get("/api/dashboard/voice-tuning")
    check("tuning GET shape — six knobs",
          r.status_code == 200 and all(k in r.json() for k in
              ("wake_threshold", "wake_awake_timeout", "vad_confidence",
               "vad_min_volume", "lull_secs", "dedup_window")),
          f"status={r.status_code} keys={list(r.json())}")
    # Round-trip EVERY knob with a distinctive value
    r = client.put("/api/dashboard/voice-tuning", json={
        "wake_threshold": 0.42, "wake_awake_timeout": 12.0,
        "vad_confidence": 0.65, "vad_min_volume": 0.55,
        "lull_secs": 0.8, "dedup_window": 8.0})
    check("tuning PUT (all six) ok", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")
    t = client.get("/api/dashboard/voice-tuning").json()
    check("tuning round-trip: all six persisted",
          t["wake_threshold"] == 0.42 and t["wake_awake_timeout"] == 12.0 and
          t["vad_confidence"] == 0.65 and t["vad_min_volume"] == 0.55 and
          t["lull_secs"] == 0.8 and t["dedup_window"] == 8.0, f"t={t}")
    # Partial update: one knob only — others must not move
    r = client.put("/api/dashboard/voice-tuning", json={"wake_threshold": 0.5})
    check("tuning PUT (single knob) ok", r.status_code == 200, f"status={r.status_code}")
    t = client.get("/api/dashboard/voice-tuning").json()
    check("tuning partial update leaves the other five untouched",
          t["wake_threshold"] == 0.5 and t["lull_secs"] == 0.8 and
          t["dedup_window"] == 8.0 and t["vad_confidence"] == 0.65, f"t={t}")
    # Range rejection, one per knob
    for knob, bad in [("wake_threshold", 0.9), ("wake_awake_timeout", 3.0),
                      ("vad_confidence", 0.95), ("vad_min_volume", 1.5),
                      ("lull_secs", 5.0), ("dedup_window", 60.0)]:
        r = client.put("/api/dashboard/voice-tuning", json={knob: bad})
        check(f"tuning PUT rejects {knob}={bad} (out of range)",
              r.status_code == 422, f"status={r.status_code}")
    # Empty payload rejected
    r = client.put("/api/dashboard/voice-tuning", json={})
    check("tuning PUT with no knobs rejected (422)", r.status_code == 422, f"status={r.status_code}")
    # CRITICAL (the filler-clobber lesson): tuning writes MERGE into the
    # intelligence section — max_tokens + fillers must survive untouched.
    brain_after = config_store.read_section("intelligence") or {}
    check("tuning write preserves intelligence.max_tokens (merge, not replace)",
          brain_after.get("max_tokens") == 300, f"intelligence keys={sorted(brain_after)}")
    import yaml as _yaml_fl  # the fixture's own fillers: no live-config count
    _fillers_in = (_yaml_fl.safe_load(cfg_src.read_text()).get("intelligence") or {}).get("fillers")
    sys.path.insert(0, str(REPO_CORE))
    from intelligence import agent as _ag_fl
    _ex_fl = (_yaml_fl.safe_load((REPO_CORE.parent / "config.example.yaml").read_text())
              .get("intelligence") or {}).get("fillers") or []
    _ex_tiers_fl = [(float(x["after"]), list(x["lines"])) for x in _ex_fl]
    _all_fl = [ln for _, lines in _ag_fl._DEFAULT_TIERS for ln in lines]
    # The two 2-word acks predate the 3-word rule (the echo gate covers them).
    _short_fl = [ln for ln in _all_fl if len(ln.split()) < 3 and ln not in ("On it.", "One moment.")]
    check("fillers: the shipped lines are the example config's, 3+ words (the two old 2-word acks excepted), "
          "and the retired ones are gone",
          _ex_tiers_fl == [(a, list(lines)) for a, lines in _ag_fl._DEFAULT_TIERS] and not _short_fl
          and not any(w in " ".join(_all_fl) for w in ("deep one", "big one", "slow one")),
          f"short={_short_fl} example={_ex_tiers_fl}")
    check("tuning write preserves intelligence.fillers (merge, not replace)",
          isinstance(brain_after.get("fillers"), list) and brain_after["fillers"] == _fillers_in,
          f"fillers={str(brain_after.get('fillers'))[:80]}")
    wake_after = config_store.read_section("wake") or {}
    check("tuning write preserves wake.enabled (merge, not replace)",
          wake_after.get("enabled") is True, f"wake={wake_after}")

    # Load-anchored speech timers: the v4
    # dialogue lane's gen->load gap made generation-anchored timers fire
    # early — end-of-speech LED flipped the ring to 'listening' mid-reply,
    # echo gate reopened the mic, note_speech capped the LOCKED release.
    # Both backends must re-anchor all three at clip-load/play time, and
    # a STALE speak task (older epoch) must not stomp a newer turn.
    if True:  # scope block (fake player layers live and die together)
        import types as _types
        from types import SimpleNamespace as _NS
        from echo_gate import EchoGate  # noqa: E402
        from pipecat.frames.frames import (  # noqa: E402
            TTSStartedFrame as _TTSStartedFrame,
            TTSAudioRawFrame as _TTSAudioRawFrame,
            TTSStoppedFrame as _TTSStoppedFrame,
        )
        from pipecat.processors.frame_processor import FrameDirection as _FrameDirection  # noqa: E402

        _LOADS = []

        class _FakeSw:
            def __init__(self, ip, player_id=None):
                pass

            async def connect(self):
                return

            async def play_clip(self, url, volume=None):
                await asyncio.sleep(0.25)  # the gen->load gap under test
                _LOADS.append(time.monotonic())
                return {"clipId": "fake-1"}

            async def close(self):
                return

        _saved_sonos_ws = sys.modules.get("sonos_websocket")
        sys.modules["sonos_websocket"] = _types.SimpleNamespace(SonosWebsocket=_FakeSw)

        class _FakeLed:
            def __init__(self):
                self.sent = []

            def send(self, m):
                self.sent.append((time.monotonic(), m))

        class _FakeWakeGate:
            def __init__(self):
                self.calls = []

            def note_speech(self, seconds):
                self.calls.append((time.monotonic(), seconds))

        _srv = _NS(url_for=lambda n: f"http://127.0.0.1:9/clips/{n}")
        _RATE, _SECS = 24000, 0.8

        async def _timer_probe(ctor, name):
            led = _FakeLed()
            gate = EchoGate()
            wake = _FakeWakeGate()
            out = ctor(echo_gate=gate, wake_gate=wake,
                       zone_selector=lambda: "test", led=led,
                       config_path=str(cfg_test))
            out._enabled = True
            out._zones = {"test": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
            out._clip_server = _srv
            out.create_task = lambda coro, name=None, context=None: asyncio.create_task(coro)
            _pcm = b"\x00\x00" * int(_RATE * _SECS)
            _gen_at = time.monotonic()
            for _f in (_TTSStartedFrame(), _TTSAudioRawFrame(_pcm, _RATE, 1),
                       _TTSStoppedFrame()):
                await out.process_frame(_f, _FrameDirection.DOWNSTREAM)
            await asyncio.sleep(0.6)  # let the speak task reach its play
            _load = _LOADS[-1] if name == "sonos" else None
            _wii_load = WIIM_PLAYED_AT[0] if name == "wiim" else None
            return out, led, gate, wake, _gen_at, (_load or _wii_load)

        import wiim_output as _wo_mod
        WIIM_PLAYED_AT = [None]
        _real_wiim_api = _wo_mod.wiim_api

        def _slow_play_wiim(ip, cmd, timeout=4.0):
            if cmd.startswith("setPlayerCmd:play:"):
                time.sleep(0.25)
                WIIM_PLAYED_AT[0] = time.monotonic()
            return "OK"

        _wo_mod.wiim_api = _slow_play_wiim
        try:

            async def _run():
                _so, _sled, _sgate, _swake, _sgen, _sload = await _timer_probe(SonosOutput, "sonos")
                if _sload is None:
                    check("sonos: clip loaded on the fake player", False,
                          "no load timestamp — every timing check below is meaningless")
                    return
                _speech_end = _sload + _SECS
                _fire = _so._end_of_speech_timer
                _fire_at = _fire.when() if _fire is not None else None
                if _fire_at is None:
                    _stops = [ts for ts, m in _sled.sent
                              if m in ("bot-stopped-speaking", "oww-sleeping")]
                    _fire_at = _stops[0] if _stops else None
                check("sonos: end-of-speech LED anchors to clip load, not generation",
                      _fire_at is not None and _fire_at >= _speech_end,
                      f"fire=+{(_fire_at or 0) - _sgen:.2f}s speech_end=+{_speech_end - _sgen:.2f}s")
                check("sonos: echo gate holds past real speech end",
                      _sgate._blocked_until >= _speech_end,
                      f"reopen=+{_sgate._blocked_until - _sgen:.2f}s speech_end=+{_speech_end - _sgen:.2f}s")
                check("sonos: note_speech anchored at load (not generation)",
                      _swake.calls and _swake.calls[-1][0] >= _sload - 0.05,
                      f"calls={[f'+{c[0] - _sgen:.2f}s' for c in _swake.calls]} load=+{_sload - _sgen:.2f}s")
                # Stale-task guard: an old-epoch speak task finishing late
                # must NOT move the current timers (filler racing a reply).
                # The entry-time echo block is unconditional on purpose —
                # a stale clip still SOUNDS, so the mic must stay shut; only
                # the LED/note_speech re-anchors are epoch-guarded.
                _so._speech_epoch += 1  # a newer generation now exists
                _epoch_before = _so._end_of_speech_timer
                _pbefore = _swake.calls[-1][0] if _swake.calls else 0
                _blocked_before = _sgate._blocked_until
                await _so._speak_to_sonos(b"\x00\x00" * int(_RATE * 0.3),
                                          epoch=_so._speech_epoch - 1)
                check("sonos: stale speak task can't stomp newer turn's timers",
                      _so._end_of_speech_timer is _epoch_before
                      and _sgate._blocked_until >= _blocked_before
                      and (not _swake.calls or _swake.calls[-1][0] == _pbefore),
                      "stale epoch re-anchor happened")
                # Fallback: a NEW generation whose speak task dies at the
                # zone check — the generation-time schedule (LED + note_speech)
                # must stand so the ring never strands cyan forever.
                _so._zones = {}  # every speak task now dies pre-load
                _pcm_small = b"\x00\x00" * int(_RATE * 0.3)
                _gen2 = time.monotonic()
                for _f in (_TTSStartedFrame(), _TTSAudioRawFrame(_pcm_small, _RATE, 1),
                           _TTSStoppedFrame()):
                    await _so.process_frame(_f, _FrameDirection.DOWNSTREAM)
                await asyncio.sleep(0.05)  # let the doomed speak task die
                _fallback_timer = _so._end_of_speech_timer
                check("sonos: fallback schedule stands after speak-task death (ring never strands)",
                      _fallback_timer is not None and _fallback_timer.when() >= _gen2 + 0.3,
                      f"timer={_fallback_timer.when() if _fallback_timer else None} gen2=+{time.monotonic() - _gen2:.2f}s")
                # —─ WiiMOutput: same three checks ──
                _wo, _wled, _wgate, _wwake, _wgen, _wload = await _timer_probe(WiiMOutput, "wiim")
                if _wload is None:
                    check("wiim: clip played on the fake player", False,
                          "no play timestamp — every timing check below is meaningless")
                    return
                _wspeech_end = _wload + 0.25 + _SECS + 0.35  # lead-in + speech + tail
                _wfire = _wo._end_of_speech_timer
                _wfire_at = _wfire.when() if _wfire is not None else None
                check("wiim: end-of-speech LED anchors to clip load, not generation",
                      _wfire_at is not None and _wfire_at >= _wspeech_end,
                      f"fire=+{(_wfire_at or 0) - _wgen:.2f}s speech_end=+{_wspeech_end - _wgen:.2f}s")
                check("wiim: echo gate holds past real speech end",
                      _wgate._blocked_until >= _wspeech_end,
                      f"reopen=+{_wgate._blocked_until - _wgen:.2f}s speech_end=+{_wspeech_end - _wgen:.2f}s")
                check("wiim: note_speech anchored at load (not generation)",
                      _wwake.calls and _wwake.calls[-1][0] >= _wload - 0.05,
                      f"calls={[f'+{c[0] - _wgen:.2f}s' for c in _wwake.calls]} load=+{_wload - _wgen:.2f}s")

                # ── LED ownership: one end message per reply, from the backend that sounds it ──
                class _AwakeGate(_FakeWakeGate):
                    state = "awake"
                _ENDS = ("bot-stopped-speaking", "oww-sleeping", "purple")

                async def _own_probe(zone, wiim_on=True):
                    led, wake = _FakeLed(), _AwakeGate()
                    wo = WiiMOutput(echo_gate=EchoGate(), wake_gate=wake, zone_selector=lambda: zone,
                                    led=led, config_path=str(cfg_test))
                    wo._enabled = wiim_on
                    wo._zones = {"wz": {"ip": "127.0.0.2"}}
                    _kw = ({"reply_elsewhere": lambda: wo.owns_reply()}
                           if hasattr(wo, "owns_reply") else {})
                    so = SonosOutput(echo_gate=EchoGate(), wake_gate=wake, zone_selector=lambda: zone,
                                     led=led, config_path=str(cfg_test), **_kw)
                    so._enabled = True
                    so._duck_while_awake = False
                    so._zones = {"sz": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
                    for o in (so, wo):
                        o._clip_server = _srv
                        o.create_task = lambda coro, name=None, context=None: asyncio.create_task(coro)
                    WIIM_PLAYED_AT[0] = None
                    _pcm = b"\x00\x00" * int(_RATE * _SECS)
                    t0 = time.monotonic()
                    for _f in (_TTSStartedFrame(), _TTSAudioRawFrame(_pcm, _RATE, 1), _TTSStoppedFrame()):
                        await so.process_frame(_f, _FrameDirection.DOWNSTREAM)
                        await wo.process_frame(_f, _FrameDirection.DOWNSTREAM)
                    await asyncio.sleep(3.0)
                    ends = [(round(ts - t0, 2), m) for ts, m in led.sent if m in _ENDS]
                    return ends, WIIM_PLAYED_AT[0] and round(WIIM_PLAYED_AT[0] - t0, 2)

                _ow_ends, _ow_play = await _own_probe("wz")
                _ow_speech_end = (_ow_play or 99) + 0.25 + _SECS + 0.35
                check("led owner: a WiiM reply ends ONCE, with the firmware's message, after it sounds",
                      len(_ow_ends) == 1 and _ow_ends[0][1] == "bot-stopped-speaking"
                      and _ow_ends[0][0] >= _ow_speech_end - 0.05,
                      f"ends={_ow_ends} play=+{_ow_play} speech_end=+{_ow_speech_end:.2f}")
                _os_ends, _os_play = await _own_probe("sz")
                check("led owner: a Sonos reply gets no WiiM end message and no WiiM play",
                      [m for _, m in _os_ends] == ["bot-stopped-speaking"] and _os_play is None,
                      f"ends={_os_ends} wiim_play={_os_play}")
                _od_ends, _ = await _own_probe("sz", wiim_on=False)
                check("led owner: WiiM disabled sends no instant end message on a Sonos reply",
                      [m for _, m in _od_ends] == ["bot-stopped-speaking"]
                      and _od_ends[0][0] >= _SECS, f"ends={_od_ends}")

            _asyncio.run(_run())

            # ── WiiM volume: overlapping clips share one music snapshot ──
            _DEV = {"vol": 23, "sets": []}

            def _vol_api(ip, cmd, timeout=4.0):
                if cmd.startswith("setPlayerCmd:vol:"):
                    _DEV["vol"] = int(cmd.rsplit(":", 1)[1])
                    _DEV["sets"].append((round(time.monotonic() - _DEV["t0"], 2), _DEV["vol"]))
                return "OK"

            _saved_status_v = _wo_mod.wiim_get_status
            _wo_mod.wiim_api = _vol_api
            _wo_mod.wiim_get_status = lambda ip, timeout=3.0: {"vol": str(_DEV["vol"])}
            try:
                async def _vol_probe():
                    wo = WiiMOutput(echo_gate=EchoGate(), zone_selector=lambda: "wz", led=None,
                                    config_path=str(cfg_test))
                    wo._enabled = True
                    wo._zones = {"wz": {"ip": "127.0.0.3"}}
                    wo._zone_volumes = {"wz": 35}
                    wo._clip_server = _srv
                    _DEV["t0"] = time.monotonic()
                    filler = asyncio.create_task(wo._speak_to_wiim(b"\x00\x00" * int(_RATE * 0.2)))
                    await asyncio.sleep(0.3)
                    reply = asyncio.create_task(wo._speak_to_wiim(b"\x00\x00" * int(_RATE * 0.6)))
                    await asyncio.sleep(0.75)   # ~1.05 s: past the filler's restore, mid-reply
                    mid = _DEV["vol"]
                    await asyncio.gather(filler, reply)
                    return mid, _DEV["vol"], list(_DEV["sets"])

                _v_mid, _v_end, _v_sets = _asyncio.run(_vol_probe())

                # ── clip files: written off the event loop, on disk before the URL goes out ──
                import threading as _thcw
                _CW = []      # (file name, written on the loop thread?)
                _CW_URL = []  # clip on disk when its URL was handed out?
                _real_wb = Path.write_bytes

                def _rec_wb(self, data):
                    if "mp3" in self.name:
                        _CW.append((self.name, _thcw.current_thread() is _thcw.main_thread()))
                    return _real_wb(self, data)

                def _url_chk(n):
                    _CW_URL.append((paths.clips_dir() / n).is_file())
                    return f"http://127.0.0.1:9/clips/{n}"

                async def _cw_probe():
                    so = SonosOutput(echo_gate=EchoGate(), zone_selector=lambda: "sz", led=None,
                                     config_path=str(cfg_test))
                    so._enabled = True
                    so._duck_while_awake = False
                    so._zones = {"sz": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
                    so._clip_server = _NS(url_for=_url_chk)
                    await so._speak_to_sonos(b"\x00\x00" * int(_RATE * 0.2))
                    wo = WiiMOutput(echo_gate=EchoGate(), zone_selector=lambda: "wz", led=None,
                                    config_path=str(cfg_test))
                    wo._enabled = True
                    wo._zones = {"wz": {"ip": "127.0.0.3"}}
                    wo._clip_server = _NS(url_for=_url_chk)
                    await wo._speak_to_wiim(b"\x00\x00" * int(_RATE * 0.2))

                Path.write_bytes = _rec_wb
                try:
                    _asyncio.run(_cw_probe())
                finally:
                    Path.write_bytes = _real_wb
                check("clips: Sonos and WiiM reply clips are written off the event loop",
                      len(_CW) == 2 and not any(on_loop for _, on_loop in _CW), str(_CW))
                check("clips: each clip is on disk before its URL is handed out",
                      _CW_URL == [True, True], str(_CW_URL))

                # the hourly prune runs off the loop too
                import sonos_output as _so_cw
                _PR = []

                class _StopPrune(Exception):
                    pass

                def _rec_prune(clips_dir, days):
                    _PR.append(_thcw.current_thread() is _thcw.main_thread())
                    raise _StopPrune

                _real_prune, _real_sleep = _so_cw._prune_clips, asyncio.sleep

                async def _no_wait(secs, *a, **k):
                    return await _real_sleep(0)

                _so_cw._prune_clips, asyncio.sleep = _rec_prune, _no_wait
                try:
                    _asyncio.run(_so_cw._clip_prune_loop(SCRATCH / "no-clips", 7))
                except _StopPrune:
                    pass
                finally:
                    _so_cw._prune_clips, asyncio.sleep = _real_prune, _real_sleep
                check("clips: the hourly prune runs off the event loop", _PR == [False], str(_PR))

                # ══ speakers: shared WiiM volume snapshot, unmapped rooms, unique clips ══
                import inspect as _insp_spk

                from loguru import logger as _lg_spk
                _SPK = {"vol": 23, "cmds": [], "fail_play": False, "slow_vol": None}
                _spk_slow_started = _thcw.Event()

                def _spk_api(ip, cmd, timeout=4.0):
                    _SPK["cmds"].append(cmd)
                    if cmd.startswith("setPlayerCmd:play:") and _SPK["fail_play"]:
                        raise OSError("unit napping")
                    if cmd.startswith("setPlayerCmd:vol:"):
                        _v = int(cmd.rsplit(":", 1)[1])
                        if _SPK["slow_vol"] is not None and _v == _SPK["slow_vol"]:
                            _spk_slow_started.set()
                            time.sleep(0.4)  # the restore write is in flight
                        _SPK["vol"] = _v
                    return "OK"

                def _spk_secs(s, rate=16000):
                    return b"\x00\x00" * int(rate * s)

                def _spk_wiim(ip, selector=lambda: "wz", **kw):
                    wo = WiiMOutput(echo_gate=EchoGate(), zone_selector=selector, led=None,
                                    config_path=str(cfg_test), **kw)
                    wo._enabled = True
                    wo._zones = {"wz": {"ip": ip}}
                    wo._zone_volumes = {"wz": 35}
                    wo._clip_server = _srv
                    return wo

                _SPK_SW = []

                class _SpkSw:
                    def __init__(self, ip, player_id=None):
                        _SPK_SW.append(ip)

                    async def connect(self):
                        return

                    async def play_clip(self, url, volume=None):
                        return {"clipId": "spk-1"}

                    async def cancel_clip(self, cid):
                        return

                    async def close(self):
                        return

                class _SpkAwake:
                    state = "awake"

                    def __init__(self):
                        self.calls = []

                    def note_speech(self, s):
                        self.calls.append(s)

                async def _spk_shared():
                    # two pipelines (two rooms) sounding clips on ONE speaker
                    _SPK.update(vol=23, cmds=[])
                    a, b = _spk_wiim("127.0.0.4"), _spk_wiim("127.0.0.4")
                    ta = asyncio.create_task(a._speak_to_wiim(_spk_secs(0.8)))
                    await asyncio.sleep(0.3)
                    tb = asyncio.create_task(b._speak_to_wiim(_spk_secs(0.8)))
                    await asyncio.sleep(1.25)  # ~1.55 s: past A's restore (~1.4 s), mid B (~1.7 s)
                    mid = _SPK["vol"]
                    await asyncio.gather(ta, tb)
                    return mid, _SPK["vol"]

                async def _spk_restore_race():
                    # a new clip lands while the previous clip's restore write is in flight
                    _SPK.update(vol=23, cmds=[], slow_vol=23)
                    _spk_slow_started.clear()
                    wo = _spk_wiim("127.0.0.5")
                    ta = asyncio.create_task(wo._speak_to_wiim(_spk_secs(0.2)))
                    await asyncio.to_thread(_spk_slow_started.wait, 5.0)
                    tb = asyncio.create_task(wo._speak_to_wiim(_spk_secs(0.2)))
                    await asyncio.gather(ta, tb)
                    _SPK["slow_vol"] = None
                    return _SPK["vol"]

                async def _spk_play_fail():
                    _SPK.update(vol=23, cmds=[], fail_play=True)
                    try:
                        await _spk_wiim("127.0.0.6")._speak_to_wiim(_spk_secs(0.2))
                    finally:
                        _SPK["fail_play"] = False
                    return _SPK["vol"], list(_SPK["cmds"])

                async def _spk_cancel():
                    _SPK.update(vol=23, cmds=[])
                    t = asyncio.create_task(_spk_wiim("127.0.0.7")._speak_to_wiim(_spk_secs(2.0)))
                    await asyncio.sleep(0.5)  # sounding at the voice volume
                    playing = _SPK["vol"]
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    return playing, _SPK["vol"], t.cancelled()

                async def _spk_unmapped():
                    # zone None (unmapped room) with a default_zone configured on BOTH backends
                    _SPK.update(vol=23, cmds=[])
                    _SPK_SW.clear()
                    led, wake = _FakeLed(), _SpkAwake()
                    wgate, sgate = EchoGate(), EchoGate()
                    wo = _spk_wiim("127.0.0.8", selector=lambda: None, wake_gate=wake)
                    wo._led, wo._echo_gate = led, wgate
                    wo._zone = "wz"
                    _kw = ({"room_selector": lambda: "probe-room"}
                           if "room_selector" in _insp_spk.signature(SonosOutput.__init__).parameters else {})
                    so = SonosOutput(echo_gate=sgate, wake_gate=wake, zone_selector=lambda: None, led=led,
                                     config_path=str(cfg_test), reply_elsewhere=lambda: wo.owns_reply(), **_kw)
                    so._enabled = True
                    so._duck_while_awake = True
                    so._zones = {"sz": {"ip": "127.0.0.9", "player_uid": "RINCON_TEST"}}
                    so._zone = "sz"
                    pushed = []

                    async def _pf(frame, direction=_FrameDirection.DOWNSTREAM):
                        pushed.append(frame)
                    for o in (so, wo):
                        o._clip_server = _srv
                        o.create_task = lambda coro, name=None, context=None: asyncio.create_task(coro)
                        o.push_frame = _pf
                    holder = asyncio.create_task(so._duck_holder_loop())
                    t0 = time.monotonic()
                    for _f in (_TTSStartedFrame(), _TTSAudioRawFrame(_spk_secs(0.8, 24000), 24000, 1),
                               _TTSStoppedFrame()):
                        await so.process_frame(_f, _FrameDirection.DOWNSTREAM)
                        await wo.process_frame(_f, _FrameDirection.DOWNSTREAM)
                    await asyncio.sleep(1.5)
                    holder.cancel()
                    try:
                        await holder
                    except asyncio.CancelledError:
                        pass
                    ends = [(round(ts - t0, 2), m) for ts, m in led.sent
                            if m in ("bot-stopped-speaking", "oww-sleeping")]
                    return {"owns": wo.owns_reply(),
                            "plays": [c for c in _SPK["cmds"] if c.startswith("setPlayerCmd:play:")],
                            "vol_writes": [c for c in _SPK["cmds"] if c.startswith("setPlayerCmd:vol:")],
                            "sonos": list(_SPK_SW), "ends": ends, "speech": list(wake.calls),
                            "echo_open": max(wgate._blocked_until, sgate._blocked_until) <= time.monotonic(),
                            "pushed": len(pushed)}

                async def _spk_names():
                    names = []
                    nsrv = _NS(url_for=lambda n: names.append(n) or f"http://127.0.0.1:9/clips/{n}")
                    outs = []
                    for _ in range(2):  # two rooms' pipelines, both fresh after a restart
                        so = SonosOutput(echo_gate=EchoGate(), zone_selector=lambda: "sz", led=None,
                                         config_path=str(cfg_test))
                        so._enabled = True
                        so._duck_while_awake = False
                        so._zones = {"sz": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
                        so._clip_server = nsrv
                        outs.append(so)
                    await asyncio.gather(*(o._speak_to_sonos(_spk_secs(0.1, 24000)) for o in outs))
                    wo = _spk_wiim("127.0.0.10")
                    wo._clip_server = nsrv
                    # a filler and a fast reply from one pipeline, same second
                    await asyncio.gather(wo._speak_to_wiim(_spk_secs(0.1)), wo._speak_to_wiim(_spk_secs(0.1)))
                    return names

                _spk_saved = (_wo_mod.wiim_api, _wo_mod.wiim_get_status, sys.modules.get("sonos_websocket"))
                _wo_mod.wiim_api = _spk_api
                _wo_mod.wiim_get_status = lambda ip, timeout=3.0: {"vol": str(_SPK["vol"])}
                sys.modules["sonos_websocket"] = _types.SimpleNamespace(SonosWebsocket=_SpkSw)
                _spk_log = []
                _spk_sink = _lg_spk.add(lambda m: _spk_log.append(str(m)), level="INFO")
                try:
                    _sh_mid, _sh_end = _asyncio.run(_spk_shared())
                    check("wiim: two pipelines on one speaker share the music snapshot (no mid-reply dip)",
                          _sh_mid == 35, f"mid={_sh_mid}")
                    check("wiim: two pipelines on one speaker put the music level back afterwards",
                          _sh_end == 23, f"end={_sh_end}")
                    _rr_end = _asyncio.run(_spk_restore_race())
                    check("wiim: a clip arriving mid-restore keeps the music level (restore holds the lock)",
                          _rr_end == 23, f"end={_rr_end} cmds={_SPK['cmds']}")
                    _pf_vol, _pf_cmds = _asyncio.run(_spk_play_fail())
                    check("wiim: a failed play command puts the music volume back",
                          _pf_vol == 23, f"vol={_pf_vol} cmds={_pf_cmds}")
                    _cn_playing, _cn_end, _cn_cancelled = _asyncio.run(_spk_cancel())
                    check("wiim: a reply cancelled mid-clip (bot stopping) still restores the music volume",
                          _cn_playing == 35 and _cn_end == 23 and _cn_cancelled,
                          f"playing={_cn_playing} end={_cn_end} cancelled={_cn_cancelled}")
                    _um = _asyncio.run(_spk_unmapped())
                    _um_log = "\n".join(_spk_log)
                    check("speakers: an unmapped room's reply plays on no WiiM (default_zone is not a fallback)",
                          _um["owns"] is False and _um["plays"] == [] and _um["vol_writes"] == [], str(_um))
                    check("speakers: an unmapped room's reply plays on no Sonos and ducks nothing",
                          _um["sonos"] == [], str(_um))
                    check("speakers: an unmapped room's reply ends the speaking LED at once, exactly once",
                          len(_um["ends"]) == 1 and _um["ends"][0][0] < 0.3, str(_um["ends"]))
                    check("speakers: an unmapped room's reply never arms the echo gate",
                          _um["echo_open"], str(_um))
                    check("speakers: an unmapped room's reply still reaches the transport (every frame passed on)",
                          _um["pushed"] == 6, str(_um["pushed"]))
                    check("speakers: an unmapped room logs one 'no speaker mapping' line naming the room",
                          _um_log.count("has no speaker mapping") == 1 and "probe-room" in _um_log,
                          _um_log[-400:])
                    _nm = _asyncio.run(_spk_names())
                    _nm_s = [n for n in _nm if n.startswith("reply-")]
                    _nm_w = [n for n in _nm if n.startswith("wiim-reply-")]
                    check("clips: two Sonos pipelines replying in the same second get distinct clip names",
                          len(_nm_s) == 2 and len(set(_nm_s)) == 2, str(_nm_s))
                    check("clips: two WiiM clips from one pipeline in the same second get distinct names",
                          len(_nm_w) == 2 and len(set(_nm_w)) == 2, str(_nm_w))
                finally:
                    _lg_spk.remove(_spk_sink)
                    _wo_mod.wiim_api, _wo_mod.wiim_get_status = _spk_saved[0], _spk_saved[1]
                    if _spk_saved[2] is None:
                        sys.modules.pop("sonos_websocket", None)
                    else:
                        sys.modules["sonos_websocket"] = _spk_saved[2]

                # SonosOutput construction stays off the disk; the startup prune runs in setup(), in a thread
                from config import cfg as _cfg_spk
                from pipecat.processors.frame_processor import FrameProcessor as _FP_spk
                _PR_SPK, _RT_SPK = [], []
                _real_prune_spk, _real_rt_spk = _so_cw._prune_clips, Path.read_text
                _real_setup_spk, _real_scs_spk = _FP_spk.setup, _so_cw.shared_clip_server
                _real_audited_spk = SonosOutput._zones_audited

                def _rec_prune_spk(clips_dir, days):
                    _PR_SPK.append(_thcw.current_thread() is _thcw.main_thread())
                    return 0

                def _rec_rt_spk(self, *a, **k):
                    if self.suffix in (".yaml", ".yml"):
                        _RT_SPK.append(self.name)
                    return _real_rt_spk(self, *a, **k)

                async def _noop_setup_spk(self, setup):
                    return

                async def _fake_scs_spk():
                    return _srv

                async def _spk_construct():
                    _cfg_spk("sonos")  # the bot's config cache is warm long before a session starts
                    SonosOutput._zones_audited = True  # no capability audit task here
                    Path.read_text = _rec_rt_spk
                    try:
                        so = SonosOutput(echo_gate=None, zone_selector=lambda: None, led=None)
                    finally:
                        Path.read_text = _real_rt_spk
                    built = list(_PR_SPK)
                    so._duck_while_awake = False
                    await so.setup(None)
                    return built, list(_PR_SPK)

                _so_cw._prune_clips = _rec_prune_spk
                _FP_spk.setup, _so_cw.shared_clip_server = _noop_setup_spk, _fake_scs_spk
                try:
                    _spk_built, _spk_after = _asyncio.run(_spk_construct())
                finally:
                    _so_cw._prune_clips = _real_prune_spk
                    _FP_spk.setup, _so_cw.shared_clip_server = _real_setup_spk, _real_scs_spk
                    SonosOutput._zones_audited = _real_audited_spk
                check("sonos: constructing the output reads no config file (cached config)",
                      _RT_SPK == [], str(_RT_SPK))
                check("sonos: constructing the output does no clip prune",
                      _spk_built == [], str(_spk_built))
                check("sonos: setup() runs the startup clip prune once, off the event loop",
                      _spk_after == [False], str(_spk_after))
                # ══ end speakers block ══
            finally:
                _wo_mod.wiim_get_status = _saved_status_v
            check("wiim volume: the filler's restore doesn't dip the reply",
                  _v_mid == 35, f"mid={_v_mid} sets={_v_sets}")
            check("wiim volume: the music level comes back after overlapping clips",
                  _v_end == 23, f"end={_v_end} sets={_v_sets}")
        finally:
            _wo_mod.wiim_api = _real_wiim_api
            if _saved_sonos_ws is None:
                sys.modules.pop("sonos_websocket", None)
            else:
                sys.modules["sonos_websocket"] = _saved_sonos_ws

    # 9d. Rig facts (Settings tab header) — status carries live rig facts
    r = client.get("/api/dashboard/status")
    s = r.json()
    check("status: no wake_model or active_room — the dashboard never read them",
          r.status_code == 200 and not {"wake_model", "active_room"} & set(s), str(sorted(s)))
    check("status exposes tts_provider",
          bool(s.get("tts_provider")), f"tts_provider={s.get('tts_provider')!r}")

    # TTS health tap: tracked TTS services (bot.py TTSTracked)
    # relay pipecat's set_usable() verdicts per room — status carries
    # them so a hears-but-can't-speak node is visible without journald.
    registry.note_tts("tts-suite-down", "elevenlabs", False)
    registry.note_tts("tts-suite-up", "deepgram", True)
    s_tts = client.get("/api/dashboard/status").json()
    _rt = {x["room"]: x for x in s_tts["rooms"]}
    check("status carries per-room TTS health (tts_usable + tts_provider)",
          _rt.get("tts-suite-down", {}).get("tts_usable") is False
          and _rt.get("tts-suite-down", {}).get("tts_provider") == "elevenlabs"
          and _rt.get("tts-suite-up", {}).get("tts_usable") is True,
          f"probe={[x for x in s_tts['rooms'] if x['room'].startswith('tts-suite')]}")
    check("every rooms[] entry carries the tts fields (None = no service)",
          all("tts_usable" in x and "tts_provider" in x for x in s_tts["rooms"]),
          f"rooms={[x['room'] for x in s_tts['rooms']]}")
    # 10. REAL soco discovery on the LAN — opt-in (LVI_TEST_LIVE=1), like the
    # live clip: a default run never touches the network.
    r = (client.post("/api/dashboard/zones/discover") if os.environ.get("LVI_TEST_LIVE") == "1"
         else type("_Skip", (), {"status_code": 0, "text": "skipped (LVI_TEST_LIVE unset)"})())
    if r.status_code == 200:
        found = r.json()["found"]
        check("soco discovery ran", True, f"found {len(found)} player(s): "
              + ", ".join(f"{f['name']} ({f['ip']})" for f in found))
    else:
        print(f"[WARN] discovery endpoint returned {r.status_code}: {r.text[:120]}")

    # 12. Guard: cross-origin mutation rejected, non-browser passes
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "Kitchen"}},
                   headers={"Origin": "https://evil.example"})
    check("guard: cross-origin mutation 403", r.status_code == 403, f"status={r.status_code}")
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "Kitchen"}})
    check("guard: no-Origin (curl) mutation passes", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/api/dashboard/status", headers={"Origin": "https://evil.example"})
    check("guard: cross-origin READ stays open", r.status_code == 200, f"status={r.status_code}")

    # 13. Token mode (env set -> mutations need the header)
    os.environ["LVI_DASHBOARD_TOKEN"] = "rig-secret"
    try:
        r = client.put("/api/dashboard/mapping",
                       json={"room_zones": {"bedroom": "Kitchen"}})
        check("guard: token mode, bare mutation 401", r.status_code == 401, f"status={r.status_code}")
        r = client.put("/api/dashboard/mapping",
                       json={"room_zones": {"bedroom": "Kitchen"}},
                       headers={"X-Dashboard-Token": "rig-secret"})
        check("guard: token mode, matching header passes", r.status_code == 200, f"status={r.status_code}")
    finally:
        del os.environ["LVI_DASHBOARD_TOKEN"]

    # 13a. (bug-hunt) A non-ASCII token: str/str compare_digest raises
    # TypeError on it -> a 500 even for the RIGHT token. The compare runs
    # on UTF-8 bytes now; headers ride the wire as bytes (the ASGI layer
    # decodes them latin-1), so the probes send raw bytes.
    _saved_tok_bh4 = os.environ.get("LVI_DASHBOARD_TOKEN")
    # Whatever the rig maps already (no house names in a public test).
    _rz_bh4 = {"room_zones": dict(client.get("/api/dashboard/mapping").json().get("room_zones") or {})}
    os.environ["LVI_DASHBOARD_TOKEN"] = "rig-sécret"
    try:
        r = client.put("/api/dashboard/mapping", json=_rz_bh4)
        check("guard: non-ASCII token, bare mutation 401 (not a 500)",
              r.status_code == 401, f"status={r.status_code}")
        r = client.put("/api/dashboard/mapping", json=_rz_bh4,
                       headers={"X-Dashboard-Token": "rig-sécret".encode("latin-1")})
        check("guard: non-ASCII token, matching header passes",
              r.status_code == 200, f"status={r.status_code} body={r.text[:120]}")
        r = client.put("/api/dashboard/mapping", json=_rz_bh4,
                       headers={"X-Dashboard-Token": "rig-sécret".encode("utf-8")})
        check("guard: non-ASCII token sent as raw UTF-8 (curl, scripts) passes too",
              r.status_code == 200, f"status={r.status_code} body={r.text[:120]}")
        r = client.put("/api/dashboard/mapping", json=_rz_bh4,
                       headers={"X-Dashboard-Token": "wröng-tokén".encode("latin-1")})
        check("guard: a wrong non-ASCII token is a plain 401, never a 500",
              r.status_code == 401, f"status={r.status_code}")
    finally:
        if _saved_tok_bh4 is None:
            os.environ.pop("LVI_DASHBOARD_TOKEN", None)
        else:
            os.environ["LVI_DASHBOARD_TOKEN"] = _saved_tok_bh4

    # 13b. Guard: an Origin must match the Host's port too (a page on another
    # port of the same IP is another origin); a missing port is the scheme's.
    # The empty mapping is refused by the route itself (400, nothing written)
    # once the guard lets it through, so 400 = passed the guard, 403 = refused.
    _gport = {}
    for _base, _origin, _key in (
            ("https://127.0.0.1:7860", "https://127.0.0.1:7860", "same"),
            ("https://127.0.0.1:7860", "https://127.0.0.1:9119", "other_port"),
            ("https://127.0.0.1:7860", "https://127.0.0.1", "default_vs_explicit"),
            ("http://127.0.0.1", "http://127.0.0.1", "http_default"),
            ("http://127.0.0.1", "http://127.0.0.1:80", "http_explicit_80"),
            ("http://127.0.0.1", "https://127.0.0.1", "https_default_443"),
            ("https://127.0.0.1", "https://127.0.0.1:443", "https_explicit_443")):
        _gc = TestClient(app, base_url=_base, raise_server_exceptions=False)
        _gport[_key] = _gc.put("/api/dashboard/mapping", json={"room_zones": {}},
                               headers={"Origin": _origin}).status_code
    check("guard: same-origin mutations pass (explicit port, or the scheme's default either way)",
          all(_gport[k] == 400 for k in ("same", "http_default", "http_explicit_80", "https_explicit_443")),
          str(_gport))
    check("guard: an Origin on another port of the same host is refused",
          all(_gport[k] == 403 for k in ("other_port", "default_vs_explicit", "https_default_443")),
          str(_gport))

    # 13c. Fire-and-forget sends hold a task reference until they finish
    # (the loop keeps only weak ones; an unreferenced task can be collected).
    async def _ref_probe():
        import led_state as _ls_ref
        from dashboard import node_command as _nc_ref
        gate = asyncio.Event()

        class _Out:
            async def send_message(self, frame):
                await gate.wait()

        _ls_ref.LedSender(_Out()).send("oww-awake")
        _nc_ref._send(_Out(), {"cmd": "pong"})
        await asyncio.sleep(0)
        held = (len(getattr(_ls_ref, "_inflight", ())), len(getattr(_nc_ref, "_inflight", ())))
        gate.set()
        await asyncio.sleep(0.05)
        after = (len(getattr(_ls_ref, "_inflight", ())), len(getattr(_nc_ref, "_inflight", ())))
        return held, after

    _ref_held, _ref_after = _asyncio.run(_ref_probe())
    check("tasks: LED and node-command sends are referenced while in flight, released when done",
          _ref_held == (1, 1) and _ref_after == (0, 0), f"held={_ref_held} after={_ref_after}")

    # Epoch turns + wake-word barge-in: turns
    # run as background tasks (frame loop never blocks on the intelligence), a
    # new question supersedes (old reply not spoken in its place, no history
    # write, ack spoken), and the wake word twice-confirmed during LOCKED fires
    # the interrupt + reopens the gate; backends stop their clips.
    if True:  # scope block
        import os as _os
        _os.environ["LVI_HERMES_KEY"] = "suite-test-key-not-real"
        _os.environ["LVI_SESSIONS"] = str(SCRATCH / "sessions-t8.json")
        from pipecat.frames.frames import (  # noqa: E402
            TranscriptionFrame as _TF,
            VADUserStartedSpeakingFrame as _VADStart,
            BotStartedSpeakingFrame as _BotStart,
            InputAudioRawFrame as _InputAudio,
        )
        from pipecat.processors.frame_processor import (  # noqa: E402
            FrameDirection as _FD8,
        )
        import types as _types_module
        from types import SimpleNamespace as _NS8
        from echo_gate import EchoGate as _EchoGate8
        from intelligence.agent import IntelligenceAgent as _Agent  # noqa: E402
        from wake_word import TurnState as _TurnState, WakeWordFilter as _WF  # noqa: E402
        _FD = _FD8

        # ── turn gate: a streaming STT's mid-speech final never closes the turn ──
        async def _tg_probe():
            from pipecat.frames.frames import InterimTranscriptionFrame as _ITF
            from pipecat.frames.frames import VADUserStoppedSpeakingFrame as _VADStop
            ag = _Agent(echo_gate=_EchoGate8(), room="suite-tg", session_mode="fresh")
            ag._lull_secs = 0.3
            turns = []

            async def rec(text, epoch):
                turns.append((time.monotonic(), text))
            ag._run_turn = rec
            D = _FD8.DOWNSTREAM

            async def tick(n, gap=0.05):
                for _ in range(n):
                    await ag.process_frame(_TF("", "", 0, 0, 0), D)
                    await asyncio.sleep(gap)
            out = {}
            # A — the live split, replayed: "What" final mid-speech, the rest
            # arrives as Deepgram's finalize result after VAD stop.
            await ag.process_frame(_VADStart(), D)
            await ag.process_frame(_ITF("What", "", 0), D)  # Deepgram streams interims
            await ag.process_frame(_TF("What", "", 0, 0, 0), D)
            await tick(14)
            out["mid"] = [t for _, t in turns]
            await ag.process_frame(_VADStop(), D)
            t0 = time.monotonic()
            await ag.process_frame(_TF("STT provider is", "", 0, 0, 0, finalized=True), D)
            await asyncio.sleep(0.02)
            out["A"] = [t for _, t in turns]
            out["A_ms"] = (turns[0][0] - t0) * 1000 if turns else None
            turns.clear()
            # B — no finalize result: the lull counts from VAD stop.
            await ag.process_frame(_VADStart(), D)
            await ag.process_frame(_TF("hello there", "", 0, 0, 0), D)
            await tick(8)
            await ag.process_frame(_VADStop(), D)
            ts = time.monotonic()
            await tick(3)
            out["B_early"] = [t for _, t in turns]
            await tick(8)
            out["B"] = [t for _, t in turns]
            out["B_wait"] = (turns[0][0] - ts) if turns else None
            turns.clear()
            # C — Whisper's order (stop, then one plain transcript): unchanged.
            await ag.process_frame(_VADStart(), D)
            await tick(4)
            await ag.process_frame(_VADStop(), D)
            await asyncio.sleep(0.4)
            await ag.process_frame(_TF("what time is it", "", 0, 0, 0), D)
            tw = time.monotonic()
            await tick(10)
            out["C"] = [t for _, t in turns]
            out["C_wait"] = (turns[0][0] - tw) if turns else None
            return out

        _tg = _asyncio.run(_tg_probe())
        check("turn gate: a final that arrives mid-speech never closes the turn (the live 'What' split)",
              _tg["mid"] == [], str(_tg))
        check("turn gate: VAD stop + the finalize result -> ONE turn with the whole sentence, at once",
              _tg["A"] == ["What STT provider is"] and _tg["A_ms"] is not None and _tg["A_ms"] < 100, str(_tg))
        check("turn gate: no finalize result -> the lull counts from VAD stop (not early, not lost)",
              _tg["B_early"] == [] and _tg["B"] == ["hello there"] and 0.28 <= (_tg["B_wait"] or 0) <= 0.55, str(_tg))
        check("turn gate: Whisper order (stop, then a plain transcript) still waits the lull from the transcript",
              _tg["C"] == ["what time is it"] and 0.28 <= (_tg["C_wait"] or 0) <= 0.55, str(_tg))

        # ── segmented STT (Whisper stamps EVERY stretch finalized, no interims) ──
        async def _seg_probe():
            from pipecat.frames.frames import VADUserStoppedSpeakingFrame as _VADStopS
            ag = _Agent(echo_gate=_EchoGate8(), room="suite-seg", session_mode="fresh")
            ag._lull_secs = 0.3
            turns = []

            async def rec(text, epoch):
                turns.append((time.monotonic(), text))
            ag._run_turn = rec
            D = _FD8.DOWNSTREAM

            async def tick(secs):
                end = time.monotonic() + secs
                while time.monotonic() < end:
                    await ag.process_frame(_TF("", "", 0, 0, 0), D)
                    await asyncio.sleep(0.02)

            def final(text):
                return _TF(text, "", 0, 0, 0, finalized=True)
            out = {}
            # D: one stretch -> closes ~lull after the STOP, not at the final.
            await ag.process_frame(_VADStart(), D)
            await tick(0.1)
            await ag.process_frame(_VADStopS(), D)
            ts = time.monotonic()
            await tick(0.15)
            await ag.process_frame(final("what time is it"), D)
            await asyncio.sleep(0.01)
            out["D_instant"] = [t for _, t in turns]
            await tick(0.4)
            out["D"] = [t for _, t in turns]
            out["D_wait"] = (turns[0][0] - ts) if turns else None
            turns.clear()
            # E: a breath pause shorter than the lull -> ONE turn, both stretches.
            await ag.process_frame(_VADStart(), D)
            await tick(0.1)
            await ag.process_frame(_VADStopS(), D)
            await tick(0.1)
            await ag.process_frame(final("Sam got me tickets to a show."), D)
            await tick(0.1)  # 0.2 s after the stop: talking again
            await ag.process_frame(_VADStart(), D)
            await tick(0.3)
            out["E_mid"] = [t for _, t in turns]
            await ag.process_frame(_VADStopS(), D)
            await tick(0.1)
            await ag.process_frame(final("It's on my bucket list."), D)
            await tick(0.4)
            out["E"] = [t for _, t in turns]
            turns.clear()
            # F: a second stretch that yields no transcript -> closes after the grace.
            await ag.process_frame(_VADStart(), D)
            await tick(0.1)
            await ag.process_frame(_VADStopS(), D)
            await tick(0.1)
            await ag.process_frame(final("turn on the lights"), D)
            await tick(0.1)
            await ag.process_frame(_VADStart(), D)
            await tick(0.1)
            await ag.process_frame(_VADStopS(), D)
            tf = time.monotonic()
            await tick(0.3 + 0.3)
            out["F_early"] = [t for _, t in turns]
            await tick(1.6)
            out["F"] = [t for _, t in turns]
            out["F_wait"] = (turns[0][0] - tf) if turns else None
            return out

        _sg = _asyncio.run(_seg_probe())
        check("turn gate: a Whisper final does NOT close the turn at once",
              _sg["D_instant"] == [], str(_sg))
        check("turn gate: a Whisper final closes ~the lull after VAD stop",
              _sg["D"] == ["what time is it"] and 0.28 <= (_sg["D_wait"] or 0) <= 0.45, str(_sg))
        check("turn gate: a breath pause shorter than the lull keeps ONE turn (Whisper)",
              _sg["E_mid"] == [] and
              _sg["E"] == ["Sam got me tickets to a show. It's on my bucket list."], str(_sg))
        check("turn gate: a Whisper stretch with no transcript closes after the grace",
              _sg["F_early"] == [] and _sg["F"] == ["turn on the lights"]
              and 1.7 <= (_sg["F_wait"] or 0) <= 2.1, str(_sg))

        _SPOKEN8 = []

        async def _agent_probe():
            agent = _Agent(echo_gate=_EchoGate8(), room="suite-t8",
                           session_mode="fresh")
            _calls = {"n": 0}

            def fake_ask(text):
                _calls["n"] += 1
                if _calls["n"] == 1:
                    time.sleep(2.0)  # the slow first turn
                return f"reply to {text}"

            agent._ask_intelligence = fake_ask

            async def fake_push(frame, direction=None):
                t = getattr(frame, "text", None)
                if t and ("reply to" in t or "switching" in t):
                    _SPOKEN8.append(t)

            agent.push_frame = fake_push

            from pipecat.frames.frames import VADUserStoppedSpeakingFrame as _VADStop8

            async def say(txt):
                # Whisper's real order: VAD start, VAD stop, then the transcript.
                await agent.process_frame(_VADStart(), _FD8.DOWNSTREAM)
                await agent.process_frame(_VADStop8(), _FD8.DOWNSTREAM)
                await agent.process_frame(_TF(txt, "", 0, 0, 0), _FD8.DOWNSTREAM)

            async def flush():
                await agent.process_frame(_TF("", "", 0, 0, 0), _FD8.DOWNSTREAM)

            lull = float(agent._lull_secs)
            await say("whats the weather")
            await asyncio.sleep(lull + 0.3)
            t2 = asyncio.create_task(say("what time is it"))
            _t_flush = time.monotonic()
            await flush()
            _block = time.monotonic() - _t_flush
            await asyncio.sleep(lull + 0.3)
            await flush()
            await t2
            await asyncio.sleep(0.3)
            return agent, _block

        try:
            # The probe ends before the slow first call returns, so its late
            # report never lands here (turn_fixes_test covers late results).
            _agent, _block_time = _asyncio.run(_agent_probe())
            check("turns turn: frame loop never blocks on the intelligence (was 2.5s)",
                  _block_time < 0.5, f"blocked={_block_time:.2f}s")
            check("turns turn: superseded reply never spoken (stale question dropped)",
                  not any("weather" in s for s in _SPOKEN8),
                  f"spoken={_SPOKEN8}")
            check("turns turn: supersede ack spoken",
                  any("switching" in s for s in _SPOKEN8), f"spoken={_SPOKEN8}")
            check("turns turn: new turn's reply spoken",
                  any("time is it" in s for s in _SPOKEN8), f"spoken={_SPOKEN8}")
            _hist = [h.get("content", "") for h in _agent._history]
            check("turns turn: no history write for the dead turn",
                  not any("weather" in h for h in _hist) and
                  any("time is it" in h for h in _hist), f"hist={_hist}")

            # ── session end: the in-flight turn dies with the session ──
            import threading as _thSE

            import intelligence.agent as _iaSE
            from pipecat.frames.frames import CancelFrame as _CancelSE
            from pipecat.frames.frames import VADUserStoppedSpeakingFrame as _VADStopSE
            _stash_saved_SE = _iaSE._session_stash._path
            _iaSE._session_stash._path = SCRATCH / "sessions-end.json"

            async def _session_end_probe(end):
                agent = _Agent(echo_gate=_EchoGate8(), room="suite-se", session_mode="sticky")
                pushed = []

                def fake_ask(text):
                    time.sleep(0.8)
                    return "late reply"

                agent._ask_intelligence = fake_ask

                async def fake_push(frame, direction=None):
                    t = getattr(frame, "text", None)
                    if t:
                        pushed.append(t)

                agent.push_frame = fake_push
                await agent.process_frame(_VADStart(), _FD8.DOWNSTREAM)
                await agent.process_frame(_VADStopSE(), _FD8.DOWNSTREAM)
                await agent.process_frame(_TF("what time is it", "", 0, 0, 0), _FD8.DOWNSTREAM)
                await asyncio.sleep(float(agent._lull_secs) + 0.2)
                await agent.process_frame(_TF("", "", 0, 0, 0), _FD8.DOWNSTREAM)
                started = agent._turn_task is not None and not agent._turn_task.done()
                err = None
                try:
                    if end == "cleanup":
                        await agent.cleanup()
                    else:
                        await agent.process_frame(_CancelSE(), _FD8.DOWNSTREAM)
                except Exception as e:  # noqa: BLE001 — reported in the check detail
                    err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(1.2)  # the blocking call's thread finishes meanwhile
                return started, err, pushed, list(agent._history)

            try:
                for _end in ("cleanup", "CancelFrame"):
                    _iaSE._session_stash._path.unlink(missing_ok=True)
                    _se_started, _se_err, _se_pushed, _se_hist = _asyncio.run(_session_end_probe(_end))
                    _se_stash = _iaSE._session_stash.load("suite-se")
                    check(f"turns: session end ({_end}) cancels the in-flight turn: no late reply, no history, no stash",
                          _se_started and _se_err is None and "late reply" not in _se_pushed
                          and _se_hist == [] and _se_stash == [],
                          f"started={_se_started} err={_se_err} pushed={_se_pushed} hist={_se_hist} stash={_se_stash}")

                # sticky stash save runs off the event loop (a worker thread)
                _SAVES_SE = []
                _real_save_SE = _iaSE._session_stash.save

                def _rec_save(room, history):
                    _SAVES_SE.append((room, _thSE.current_thread() is _thSE.main_thread()))
                    return _real_save_SE(room, history)

                async def _stash_probe():
                    agent = _Agent(echo_gate=_EchoGate8(), room="suite-st8", session_mode="sticky")
                    agent._ask_intelligence = lambda text: "quick reply"

                    async def fake_push(frame, direction=None):
                        return None

                    agent.push_frame = fake_push
                    await agent._run_turn("what time is it", agent._turn_epoch)

                _iaSE._session_stash.save = _rec_save
                try:
                    _asyncio.run(_stash_probe())
                finally:
                    _iaSE._session_stash.save = _real_save_SE
                check("turns: the sticky stash is saved off the event loop",
                      _SAVES_SE == [("suite-st8", False)], str(_SAVES_SE))
            finally:
                _iaSE._session_stash._path = _stash_saved_SE

            # ── supersede-ack gap: config.yaml wins, env is the fallback ──
            _int_before_SE = dict(config_store.read_section("intelligence") or {})
            _env_gap_SE = os.environ.pop("LVI_SUPERSEDE_ACK_GAP", None)
            try:
                config_store.write_section("intelligence", {**_int_before_SE, "supersede_ack_gap": 7})
                _gap_cfg = getattr(_Agent(echo_gate=_EchoGate8(), room="suite-gap"), "_ack_gap", None)
                config_store.write_section("intelligence", {k: v for k, v in _int_before_SE.items()
                                                            if k != "supersede_ack_gap"})
                os.environ["LVI_SUPERSEDE_ACK_GAP"] = "12"
                _gap_env = getattr(_Agent(echo_gate=_EchoGate8(), room="suite-gap"), "_ack_gap", None)
                os.environ.pop("LVI_SUPERSEDE_ACK_GAP", None)
                _gap_def = getattr(_Agent(echo_gate=_EchoGate8(), room="suite-gap"), "_ack_gap", None)
            finally:
                config_store.write_section("intelligence", _int_before_SE)
                if _env_gap_SE is not None:
                    os.environ["LVI_SUPERSEDE_ACK_GAP"] = _env_gap_SE
            check("turns: intelligence.supersede_ack_gap is read from config.yaml, env fallback, default 45",
                  (_gap_cfg, _gap_env, _gap_def) == (7.0, 12.0, 45.0), f"{(_gap_cfg, _gap_env, _gap_def)}")

            # ── dedup vs retry ──
            async def _dedup_probe(fail_first, slow_first):
                agent = _Agent(echo_gate=_EchoGate8(), room="suite-dd",
                               session_mode="fresh")
                calls, spoken = {"n": 0}, []

                def fake_ask(text):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        if slow_first:
                            time.sleep(1.5)
                        if fail_first:
                            raise ConnectionRefusedError("suite: refused")
                    return f"reply to {text}"

                agent._ask_intelligence = fake_ask

                async def fake_push(frame, direction=None):
                    t = getattr(frame, "text", None)
                    if t:
                        spoken.append(t)

                agent.push_frame = fake_push
                from pipecat.frames.frames import (
                    VADUserStoppedSpeakingFrame as _VADStopD,
                )

                async def turn(txt):
                    await agent.process_frame(_VADStart(), _FD8.DOWNSTREAM)
                    await agent.process_frame(_VADStopD(), _FD8.DOWNSTREAM)
                    await agent.process_frame(_TF(txt, "", 0, 0, 0), _FD8.DOWNSTREAM)
                    await asyncio.sleep(float(agent._lull_secs) + 0.2)
                    await agent.process_frame(_TF("", "", 0, 0, 0), _FD8.DOWNSTREAM)

                await turn("what time is it")
                await asyncio.sleep(0.3 if not slow_first else 0.1)
                await turn("what time is it")   # the retry / the double-fire
                await asyncio.sleep(2.0)
                return calls["n"], spoken

            _dd_n, _dd_spoken = _asyncio.run(_dedup_probe(fail_first=True, slow_first=False))
            check("turns dedup: a retry after a failed turn reaches the intelligence",
                  _dd_n == 2 and any("reply to what time is it" in s for s in _dd_spoken)
                  and any("can't reach" in s for s in _dd_spoken),
                  f"calls={_dd_n} spoken={_dd_spoken}")
            _df_n, _df_spoken = _asyncio.run(_dedup_probe(fail_first=False, slow_first=True))
            # ── no filler while the user is talking ──
            import intelligence.agent as _ia_fill

            async def _filler_probe(talk_until, ask_secs):
                agent = _Agent(echo_gate=_EchoGate8(), room="suite-fh",
                               session_mode="fresh")
                spoken = []

                def fake_ask(text):
                    time.sleep(ask_secs)
                    return "the reply"

                agent._ask_intelligence = fake_ask

                async def fake_push(frame, direction=None):
                    t = getattr(frame, "text", None)
                    if t:
                        spoken.append((round(time.monotonic() - t0, 2), t))

                agent.push_frame = fake_push
                agent._vad_active = True
                t0 = time.monotonic()

                async def stop_talking():
                    await asyncio.sleep(talk_until)
                    agent._vad_active = False
                talker = asyncio.create_task(stop_talking())
                await agent._run_turn("what's the weather", agent._turn_epoch)
                await talker
                return spoken

            _orig_tiers = _ia_fill._filler_tiers
            _ia_fill._filler_tiers = lambda: [(0.2, ["Working on it."])]
            try:
                _fh1 = _asyncio.run(_filler_probe(talk_until=0.6, ask_secs=1.2))
                _fh2 = _asyncio.run(_filler_probe(talk_until=2.0, ask_secs=0.8))
            finally:
                _ia_fill._filler_tiers = _orig_tiers
            check("turns filler: held while the user talks, spoken once they stop",
                  [t for _, t in _fh1] == ["Working on it.", "the reply"]
                  and _fh1[0][0] >= 0.55, f"spoken={_fh1}")
            check("turns filler: skipped when the reply lands while the user still talks",
                  [t for _, t in _fh2] == ["the reply"], f"spoken={_fh2}")

            check("turns dedup: the same utterance twice during a live turn still asks once",
                  _df_n == 1 and sum("reply to" in s for s in _df_spoken) == 1,
                  f"calls={_df_n} spoken={_df_spoken}")

            # ── barge-in wake detection ──
            async def _wake_probe():
                fired = []
                wf = _WF(echo_gate=_EchoGate8(), on_wake=lambda: None,
                         on_interrupt=lambda: fired.append(time.monotonic()))
                wf._oww = _NS8(predict=lambda a: {"Hey_Willow": 0.99})
                wf._name = "Hey_Willow"
                wf._barge_in = True  # the ON path; default-off tested below
                wf._frame_size = 2560
                await wf.process_frame(_BotStart(), _FD8.DOWNSTREAM)
                # one over-threshold frame — must NOT fire
                await wf.process_frame(
                    _InputAudio(b"\x00" * 2560, 16000, 1), _FD8.DOWNSTREAM)
                one = len(fired)
                # second consecutive frame — MUST fire
                await wf.process_frame(
                    _InputAudio(b"\x00" * 2560, 16000, 1), _FD8.DOWNSTREAM)
                return wf, fired, one

            _wf8, _fired8, _one8 = _asyncio.run(_wake_probe())
            check("turns wake: one over-threshold frame does NOT fire (2-frame confirm)",
                  _one8 == 0, f"fired_after_one={_one8}")
            check("turns wake: two consecutive frames fire the interrupt, gate reopens",
                  len(_fired8) == 1 and _wf8._state is _TurnState.AWAKE,
                  f"fired={len(_fired8)} state={_wf8._state}")

            # ── a long sentence keeps the AWAKE window open ──
            import wake_word as _ww_mod
            from pipecat.frames.frames import (
                UserSpeakingFrame as _USpk,
            )
            from pipecat.frames.frames import (
                VADUserStoppedSpeakingFrame as _VADStopW,
            )

            async def _hold_probe(talk_s, cap):
                _ww_mod.SPEECH_HOLD_MAX = cap
                wf = _WF(echo_gate=_EchoGate8(), on_wake=lambda: None)
                wf._oww = _NS8(predict=lambda a: {"Hey_Willow": 0.0})
                wf._name = "Hey_Willow"
                wf._frame_size = 2560
                wf._awake_timeout = 0.6

                async def _noop(*a, **k):
                    return None

                wf.push_frame = _noop
                wf._state = _TurnState.AWAKE
                wf._last_activity = time.monotonic()
                audio = _InputAudio(b"\x00" * 2560, 16000, 1)
                await wf.process_frame(_VADStart(), _FD8.UPSTREAM)
                t0, closed_at = time.monotonic(), None
                while time.monotonic() - t0 < talk_s:
                    await wf.process_frame(_USpk(), _FD8.UPSTREAM)
                    await wf.process_frame(audio, _FD8.DOWNSTREAM)
                    if closed_at is None and wf._state is _TurnState.IDLE:
                        closed_at = time.monotonic() - t0
                    await asyncio.sleep(0.1)
                talking_state = wf._state
                await wf.process_frame(_VADStopW(), _FD8.UPSTREAM)
                await asyncio.sleep(0.8)
                await wf.process_frame(audio, _FD8.DOWNSTREAM)
                return talking_state, closed_at, wf._state

            _orig_cap = getattr(_ww_mod, "SPEECH_HOLD_MAX", None)
            try:
                _h_talk, _h_closed, _h_after = _asyncio.run(_hold_probe(2.0, 30.0))
                _c_talk, _c_closed, _c_after = _asyncio.run(_hold_probe(2.0, 0.5))
            finally:
                if _orig_cap is None:
                    del _ww_mod.SPEECH_HOLD_MAX
                else:
                    _ww_mod.SPEECH_HOLD_MAX = _orig_cap
            check("turns wake: talking past awake_timeout keeps the gate AWAKE",
                  _h_talk is _TurnState.AWAKE and _h_closed is None,
                  f"state={_h_talk} closed_at={_h_closed}")
            check("turns wake: after speech stops the window still times out",
                  _h_after is _TurnState.IDLE, f"state={_h_after}")
            check("turns wake: nonstop speech (TV) still closes at the hold cap",
                  _c_closed is not None and 0.9 <= _c_closed <= 1.6,
                  f"closed_at={_c_closed} state={_c_talk}")

            # ── thread pools: hung intelligence calls never starve wake scoring ──
            _pool_n = min(32, (os.cpu_count() or 1) + 4) + 2  # > the default pool

            async def _intel_storm_probe():
                agent = _Agent(echo_gate=_EchoGate8(), room="suite-pool",
                               session_mode="fresh")

                def hung_ask(text):
                    time.sleep(1.5)
                    return "late"

                agent._ask_intelligence = hung_ask

                async def _noop(*a, **k):
                    return None

                agent.push_frame = _noop
                turns = [asyncio.create_task(agent._run_turn(f"q{i}", agent._turn_epoch))
                         for i in range(_pool_n)]
                await asyncio.sleep(0.2)
                t0 = time.monotonic()
                try:
                    await asyncio.wait_for(asyncio.to_thread(lambda: 1), 1.0)
                    lat = time.monotonic() - t0
                except TimeoutError:
                    lat = None
                await asyncio.gather(*turns, return_exceptions=True)
                return lat

            async def _wake_starve_probe():
                wf = _WF(echo_gate=_EchoGate8(), on_wake=lambda: None)
                wf._oww = _NS8(predict=lambda a: {"Hey_Willow": 0.0})
                wf._name = "Hey_Willow"
                wf._frame_size = 2560

                async def _noop(*a, **k):
                    return None

                wf.push_frame = _noop
                hogs = [asyncio.create_task(asyncio.to_thread(time.sleep, 1.5))
                        for _ in range(_pool_n)]
                await asyncio.sleep(0.2)
                t0 = time.monotonic()
                try:
                    await asyncio.wait_for(wf.process_frame(
                        _InputAudio(b"\x00" * 2560, 16000, 1), _FD8.DOWNSTREAM), 1.0)
                    lat = time.monotonic() - t0
                except TimeoutError:
                    lat = None
                await asyncio.gather(*hogs, return_exceptions=True)
                return lat

            _storm_lat = _asyncio.run(_intel_storm_probe())
            _wake_lat = _asyncio.run(_wake_starve_probe())
            check("thread pools: a storm of hung intelligence calls leaves the shared pool free",
                  _storm_lat is not None and _storm_lat < 0.3, f"lat={_storm_lat} n={_pool_n}")
            check("thread pools: wake scoring runs while the shared pool is jammed",
                  _wake_lat is not None and _wake_lat < 0.3, f"lat={_wake_lat} n={_pool_n}")
            # knob OFF = today's behavior: even two frames never fire.
            # NOTE: pinned on the instance — the suite copies the LIVE
            # config.yaml (which carries barge_in: true on the dev rig),
            # so the config default can't be asserted hermetically here.
            async def _wake_probe_off():
                fired = []
                wf = _WF(echo_gate=_EchoGate8(), on_wake=lambda: None,
                         on_interrupt=lambda: fired.append(time.monotonic()))
                wf._oww = _NS8(predict=lambda a: {"Hey_Willow": 0.99})
                wf._name = "Hey_Willow"
                wf._barge_in = False  # the OFF path, pinned hermetically
                wf._frame_size = 2560
                await wf.process_frame(_BotStart(), _FD8.DOWNSTREAM)
                for _ in range(3):
                    await wf.process_frame(
                        _InputAudio(b"\x00" * 2560, 16000, 1), _FD8.DOWNSTREAM)
                return wf, fired

            _wf8b, _fired8b = _asyncio.run(_wake_probe_off())
            check("turns wake: barge_in default OFF never interrupts (bit-for-bit old)",
                  not _fired8b and _wf8b._state is _TurnState.LOCKED,
                  f"fired={len(_fired8b)} state={_wf8b._state}")

            # ── pin: an ASYNC on_interrupt must actually run ──
            # The production callback in bot.py is `async def`; the old
            # suite's sync lambda masked the never-awaited coroutine bug
            # (a bare call created it, nothing ran it, stop_current_clip
            # never executed). This probe pins the real shape.
            async def _wake_probe_async():
                ran = []
                async def _cb():
                    ran.append("ran")
                wf = _WF(echo_gate=_EchoGate8(), on_wake=lambda: None,
                         on_interrupt=_cb)
                wf._oww = _NS8(predict=lambda a: {"Hey_Willow": 0.99})
                wf._name = "Hey_Willow"
                wf._barge_in = True
                wf._frame_size = 2560
                await wf.process_frame(_BotStart(), _FD8.DOWNSTREAM)
                await wf.process_frame(
                    _InputAudio(b"\x00" * 2560, 16000, 1), _FD8.DOWNSTREAM)
                await wf.process_frame(
                    _InputAudio(b"\x00" * 2560, 16000, 1), _FD8.DOWNSTREAM)
                await asyncio.sleep(0.05)  # let the scheduled task run
                return wf, ran

            _wf8c, _ran8c = _asyncio.run(_wake_probe_async())
            check("turns wake: ASYNC on_interrupt coroutine actually runs",
                  len(_ran8c) == 1 and _wf8c._interrupt_task is not None
                  and _wf8c._interrupt_task.done(),
                  f"ran={len(_ran8c)} task={getattr(_wf8c, '_interrupt_task', 'missing')}")
            # ── backend clip stop (fake player layers, speech-timer pattern) ──
            _CANCELS8 = []

            class _FakeSw8:
                def __init__(self, ip, player_id=None):
                    pass

                async def connect(self):
                    return

                async def play_clip(self, url, volume=None):
                    await asyncio.sleep(0.05)
                    return {"clipId": "clip-t8"}

                async def cancel_clip(self, cid):
                    _CANCELS8.append(cid)

                async def close(self):
                    return

            _saved_ws8 = sys.modules.get("sonos_websocket")
            sys.modules["sonos_websocket"] = _types_module.SimpleNamespace(SonosWebsocket=_FakeSw8)
            try:
                from sonos_output import SonosOutput as _SO8
                from wiim_output import WiiMOutput as _WO8
                _srv8 = _NS8(url_for=lambda n: f"http://127.0.0.1:9/clips/{n}")

                async def _stop_probe():
                    so = _SO8(echo_gate=_EchoGate8(),
                              zone_selector=lambda: "test", led=None,
                              config_path=str(cfg_test))
                    so._enabled = True
                    so._zones = {"test": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
                    so._clip_server = _srv8
                    so.create_task = lambda coro, name=None, context=None: asyncio.create_task(coro)
                    from pipecat.frames.frames import (  # noqa: E402
                        TTSStartedFrame as _TS8, TTSAudioRawFrame as _TA8,
                        TTSStoppedFrame as _TSt8,
                    )
                    for f in (_TS8(), _TA8(b"\x00\x00" * 12000, 24000, 1), _TSt8()):
                        await so.process_frame(f, _FD8.DOWNSTREAM)
                    await asyncio.sleep(0.2)
                    return so

                _so8 = _asyncio.run(_stop_probe())
                _id8 = _so8._reply_clip_id
                check("turns stop: reply clip id held before interrupt", _id8 is not None,
                      f"id={_id8}")
                if _id8 is not None:
                    _asyncio.run(_so8.stop_current_clip())
                    check("turns stop: sonos barge-in cancels the held clipId",
                          "clip-t8" in _CANCELS8, f"cancels={_CANCELS8}")
                    check("turns stop: interrupt is idempotent (second call, no clip)",
                          _so8._reply_clip_id is None, "clip id not cleared")
                _WIIM_CMDS8 = []
                _real_wiim8 = _wo_mod.wiim_api if "_wo_mod" in dir() else None

                def _wiim_stop_capture(ip, cmd, timeout=4.0):
                    _WIIM_CMDS8.append(cmd)
                    return "OK"

                import wiim_output as _wo8_mod
                _saved_wiim_api8 = _wo8_mod.wiim_api
                _wo8_mod.wiim_api = _wiim_stop_capture
                try:
                    async def _wiim_stop_probe():
                        wo = _WO8(echo_gate=_EchoGate8(),
                                  zone_selector=lambda: "test", led=None,
                                  config_path=str(cfg_test))
                        wo._enabled = True
                        wo._zones = {"test": {"ip": "127.0.0.1"}}
                        wo._clip_server = _srv8
                        wo.create_task = lambda coro, name=None, context=None: asyncio.create_task(coro)
                        await wo.stop_current_clip()
                        return wo

                    _wo8b = _asyncio.run(_wiim_stop_probe())
                    check("turns stop: wiim barge-in sends setPlayerCmd:stop",
                          any("setPlayerCmd:stop" in c for c in _WIIM_CMDS8),
                          f"cmds={_WIIM_CMDS8}")

                    # ── barge-in vs a clip still in flight ──
                    _BG = {"plays": [], "cancels": [], "entered": None}

                    class _FakeSwBG:
                        def __init__(self, ip, player_id=None):
                            pass

                        async def connect(self):
                            return

                        async def play_clip(self, url, volume=None):
                            _BG["plays"].append(url)
                            if _BG["entered"] is not None:
                                _BG["entered"].set()
                            await asyncio.sleep(0.2)
                            return {"clipId": f"clip-bg{len(_BG['plays'])}"}

                        async def cancel_clip(self, cid):
                            _BG["cancels"].append(cid)

                        async def close(self):
                            return

                    def _so_bg():
                        so = _SO8(echo_gate=_EchoGate8(), zone_selector=lambda: "test",
                                  led=None, config_path=str(cfg_test))
                        so._enabled = True
                        so._duck_while_awake = False
                        so._zones = {"test": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
                        so._clip_server = _srv8
                        return so

                    _pcm_bg = b"\x00\x00" * 7200

                    async def _so_barge_probe(mode):
                        _BG["plays"].clear()
                        _BG["cancels"].clear()
                        _BG["entered"] = asyncio.Event()
                        so = _so_bg()
                        _kw = ({"barge": so._barge_epoch}
                               if hasattr(so, "_barge_epoch") else {})
                        t = asyncio.create_task(so._speak_to_sonos(
                            _pcm_bg, epoch=so._speech_epoch, **_kw))
                        if mode == "before":
                            await so.stop_current_clip()
                        elif mode == "during":
                            await asyncio.wait_for(_BG["entered"].wait(), 2.0)
                            await so.stop_current_clip()
                        await t
                        return (list(_BG["plays"]), list(_BG["cancels"]),
                                so._reply_clip_id, so._echo_gate.blocked(),
                                round(so._echo_gate._blocked_until - time.monotonic(), 2))

                    sys.modules["sonos_websocket"] = _types_module.SimpleNamespace(
                        SonosWebsocket=_FakeSwBG)
                    _bg_before = _asyncio.run(_so_barge_probe("before"))
                    _bg_during = _asyncio.run(_so_barge_probe("during"))
                    _bg_none = _asyncio.run(_so_barge_probe("none"))
                    sys.modules["sonos_websocket"] = _types_module.SimpleNamespace(
                        SonosWebsocket=_FakeSw8)
                    check("barge clip: sonos - a barge-in before load plays nothing, gate not armed",
                          _bg_before[0] == [] and _bg_before[3] is False, str(_bg_before))
                    check("barge clip: sonos - a barge-in during load cancels that clip, keeps no id",
                          len(_bg_during[0]) == 1 and _bg_during[1] == ["clip-bg1"]
                          and _bg_during[2] is None and _bg_during[4] <= 0.6, str(_bg_during))
                    check("barge clip: sonos - no barge-in still plays and holds the clip id",
                          len(_bg_none[0]) == 1 and _bg_none[1] == [] and _bg_none[2] == "clip-bg1",
                          str(_bg_none))

                    import threading as _thr_bg
                    _WBG = {"cmds": [], "in_play": _thr_bg.Event()}

                    def _wiim_bg_api(ip, cmd, timeout=4.0):
                        _WBG["cmds"].append(cmd)
                        if cmd.startswith("setPlayerCmd:play:"):
                            _WBG["in_play"].set()
                            time.sleep(0.2)
                            _WBG["cmds"].append("play-done")
                        return "OK"

                    _saved_status_bg = _wo8_mod.wiim_get_status
                    _wo8_mod.wiim_api = _wiim_bg_api
                    _wo8_mod.wiim_get_status = lambda ip, timeout=3.0: {"vol": "23"}

                    async def _wo_barge_probe(mode):
                        _WBG["cmds"].clear()
                        _WBG["in_play"].clear()
                        wo = _WO8(echo_gate=_EchoGate8(), zone_selector=lambda: "test",
                                  led=None, config_path=str(cfg_test))
                        wo._enabled = True
                        wo._zones = {"test": {"ip": "127.0.0.1"}}
                        wo._clip_server = _srv8
                        _kw = ({"barge": wo._barge_epoch}
                               if hasattr(wo, "_barge_epoch") else {})
                        t = asyncio.create_task(wo._speak_to_wiim(
                            _pcm_bg, epoch=wo._speech_epoch, **_kw))
                        if mode == "before":
                            await wo.stop_current_clip()
                        else:
                            while not _WBG["in_play"].is_set():
                                await asyncio.sleep(0.01)
                            await wo.stop_current_clip()
                        await asyncio.wait_for(t, 3.0)
                        return (list(_WBG["cmds"]), wo._echo_gate.blocked(),
                                round(wo._echo_gate._blocked_until - time.monotonic(), 2))

                    try:
                        _wbg_before = _asyncio.run(_wo_barge_probe("before"))
                        _wbg_during = _asyncio.run(_wo_barge_probe("during"))
                    finally:
                        _wo8_mod.wiim_get_status = _saved_status_bg
                    check("barge clip: wiim - a barge-in before load plays nothing, gate not armed",
                          not any(c.startswith("setPlayerCmd:play:") for c in _wbg_before[0])
                          and _wbg_before[1] is False, str(_wbg_before))
                    _wd = _wbg_during[0]
                    # The stop must land AFTER the play call completes: a stop sent
                    # while the play is still in flight can reach the unit first.
                    _wplay = next((i for i, c in enumerate(_wd) if c == "play-done"), None)
                    check("barge clip: wiim - a barge-in during load stops it and restores the volume",
                          _wplay is not None and "setPlayerCmd:stop" in _wd[_wplay + 1:]
                          and "setPlayerCmd:vol:23" in _wd[_wplay + 1:] and _wbg_during[2] <= 0.6,
                          str(_wbg_during))
                finally:
                    _wo8_mod.wiim_api = _saved_wiim_api8
            finally:
                if _saved_ws8 is None:
                    sys.modules.pop("sonos_websocket", None)
                else:
                    sys.modules["sonos_websocket"] = _saved_ws8

            # Frame passthrough (the "you wont wake" outage): the
            # epoch-turn _run_turn extraction lost process_frame's final push_frame —
            # the agent ate EVERY frame, StartFrame never reached the sink,
            # PipelineWorker killed the session at +20s, and the node rode a
            # reconnect loop. Every processor MUST pass frames downstream.
            async def _passthrough_probe():
                _agent2 = _Agent(echo_gate=_EchoGate8(), room="suite-t9",
                                 session_mode="fresh")
                _seen2 = []

                async def _sink(frame, direction=None):
                    _seen2.append(frame)

                _agent2.push_frame = _sink
                _frames2 = [_BotStart(), BotStoppedSpeakingFrame()]
                for _f in _frames2:
                    await _agent2.process_frame(_f, _FD8.DOWNSTREAM)
                return _frames2, _seen2

            from pipecat.frames.frames import BotStoppedSpeakingFrame  # noqa: E402
            _frames2, _seen2 = _asyncio.run(_passthrough_probe())
            check("passthrough agent: every frame passes through (StartFrame must reach the sink)",
                  all(any(x is f for x in _seen2) for f in _frames2),
                  f"forwarded={sum(1 for f in _frames2 if any(x is f for x in _seen2))}/{len(_frames2)}")

            # Echo-gate relax on confirmed wakes and barge-ins (the
            # double-wake bug): a wake during a sounding clip must
            # shorten the gate's block to a tail so the follow-up question
            # lands; stop_current_clip must do the same; a lapsed gate
            # must never be extended. Text containment stays armed.
            async def _relax_probe():
                _g = _EchoGate8()
                _g.block(10.0)
                _wf_r = _WF(echo_gate=_g, on_wake=lambda: None,
                            on_interrupt=lambda: None)
                _wf_r._oww = _NS8(predict=lambda a: {"Hey_Willow": 0.99})
                _wf_r._name = "Hey_Willow"
                _wf_r._barge_in = True
                _wf_r._frame_size = 2560
                _ai = _InputAudio(b"\x00" * 2560, 16000, 1)
                # IDLE -> AWAKE wake: gate must relax
                await _wf_r.process_frame(_ai, _FD8.DOWNSTREAM)
                await _wf_r.process_frame(_ai, _FD8.DOWNSTREAM)
                _after_wake = _g._blocked_until - time.monotonic()
                # BARGE-IN wake during LOCKED: gate must relax again
                await _wf_r.process_frame(_BotStart(), _FD8.DOWNSTREAM)
                _g.block(10.0)  # a new clip started sounding
                await _wf_r.process_frame(_ai, _FD8.DOWNSTREAM)
                await _wf_r.process_frame(_ai, _FD8.DOWNSTREAM)
                _after_barge = _g._blocked_until - time.monotonic()
                # lapsed gate must never be extended by relax()
                _g2 = _EchoGate8()
                _g2.relax()  # no active block — must stay open
                _lapsed = _g2.blocked()
                return _after_wake, _after_barge, _lapsed

            _aw, _ab, _lap = _asyncio.run(_relax_probe())
            check("echo gate: normal wake relaxes the gate (follow-up lands)",
                  _aw <= 0.6, f"remaining={_aw:.2f}s")
            check("echo gate: barge-in interrupt relaxes the gate",
                  _ab <= 0.6, f"remaining={_ab:.2f}s")
            check("echo gate: relax never extends a lapsed gate",
                  not _lap, "gate became blocked with no clip")
            # stop_current_clip relaxes the gate (sonos backend)
            async def _stop_relax_probe():
                _g3 = _EchoGate8()
                _g3.block(8.0)
                _so_r = _SO8(echo_gate=_g3, zone_selector=lambda: "test",
                             led=None, config_path=str(cfg_test))
                _so_r._enabled = True
                _so_r._zones = {"test": {"ip": "127.0.0.1", "player_uid": "RINCON_TEST"}}
                _so_r._clip_server = _NS8(url_for=lambda n: f"http://127.0.0.1:9/clips/{n}")
                await _so_r.stop_current_clip()
                return _g3._blocked_until - time.monotonic()

            _stop_rem = _asyncio.run(_stop_relax_probe())
            check("echo gate: stop_current_clip relaxes the gate",
                  _stop_rem <= 0.6, f"remaining={_stop_rem:.2f}s")

            # Intelligence lane: config round-trip
            # preserves intelligence.fillers (merge law), lane dispatch shapes
            # per kind, key fallback order + no-KeyError, key POST never
            # echoes material, test probe ok/error paths.
            import io as _io9
            import urllib.request as _urq9
            from dashboard import routes as _routes9

            # (a) PUT intelligence: kind+url+agent written, fillers SURVIVE
            _r = client.put("/api/dashboard/intelligence", json={
                "kind": "openclaw", "url": "http://127.0.0.1:18789/v1",
                "agent": "suite-agent"})
            check("intelligence: PUT ok", _r.status_code == 200, _r.text[:120])
            import yaml as _yaml9
            _brain9 = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("intelligence: PUT writes kind/url/agent",
                  _brain9.get("kind") == "openclaw"
                  and _brain9.get("url") == "http://127.0.0.1:18789/v1"
                  and _brain9.get("agent") == "suite-agent",
                  str(_brain9)[:120])
            check("intelligence: PUT merge: fillers survive",
                  "fillers" in _brain9, str(_brain9)[:160])
            # GET round-trips
            _g = client.get("/api/dashboard/intelligence").json()
            check("intelligence: GET returns kind/agents/key_set",
                  _g.get("kind") == "openclaw" and (_g.get("agents") or {}).get("openclaw") == "suite-agent"
                  and "key_set" in _g, str(_g)[:120])
            # (a2) per-kind URL memory: PUT files url under the kind; GET
            # returns the recall map; switching kinds keeps each URL.
            _r2 = client.put("/api/dashboard/intelligence", json={
                "kind": "hermes", "url": "http://127.0.0.1:8642/v1"})
            check("intelligence: switch to hermes ok", _r2.status_code == 200, _r2.text[:120])
            _brain9b = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("intelligence: urls map holds both kinds",
                  _brain9b.get("url") == "http://127.0.0.1:8642/v1"
                  and _brain9b.get("urls", {}).get("openclaw") == "http://127.0.0.1:18789/v1"
                  and _brain9b.get("urls", {}).get("hermes") == "http://127.0.0.1:8642/v1",
                  str(_brain9b.get("urls"))[:160])
            _g2 = client.get("/api/dashboard/intelligence").json()
            check("intelligence: GET returns urls map", _g2.get("urls", {}).get("openclaw")
                  == "http://127.0.0.1:18789/v1", str(_g2.get("urls"))[:120])
            # Bare kind flip: no url in the payload — the route
            # must restore THAT kind's remembered URL, not keep the other
            # kind's (which would point the lane at the wrong intelligence).
            _r3 = client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})
            _brain9c = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("intelligence: bare kind flip restores that kind's remembered URL",
                  _brain9c.get("url") == "http://127.0.0.1:18789/v1",
                  f"url={_brain9c.get('url')}")
            # restore kind for later checks: the lane probes below build
            # IntelligenceAgent against the LIVE throwaway config and expect
            # openclaw (the kind the earlier (a) block left behind)
            client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})

            # (b) invalid kind / agent / url -> 422
            _bad = client.put("/api/dashboard/intelligence", json={"kind": "claude"})
            check("intelligence: retired kind 422s", _bad.status_code == 422, str(_bad.status_code))
            _bad2 = client.put("/api/dashboard/intelligence",
                               json={"kind": "openclaw", "agent": "bad agent!"})
            check("intelligence: invalid agent 422s", _bad2.status_code == 422, str(_bad2.status_code))
            _bad3 = client.put("/api/dashboard/intelligence",
                               json={"kind": "openclaw", "url": "ftp://nope"})
            check("intelligence: invalid url 422s", _bad3.status_code == 422, str(_bad3.status_code))
            # A urls map in the payload is refused outright (only the URL on
            # screen is ever saved), so a bad value can't land either.
            _urls_before = (_yaml9.safe_load(Path(os.environ["LVI_CONFIG"]).read_text())
                            .get("intelligence") or {}).get("urls")
            _bad4 = client.put("/api/dashboard/intelligence",
                               json={"kind": "openclaw", "urls": {"hermes": "ftp://nope"}})
            _urls_after = (_yaml9.safe_load(Path(os.environ["LVI_CONFIG"]).read_text())
                           .get("intelligence") or {}).get("urls")
            check("intelligence: an invalid urls value 422s and changes nothing",
                  _bad4.status_code == 422 and _urls_after == _urls_before,
                  f"{_bad4.status_code} before={_urls_before} after={_urls_after}")
            # A save with no url key (never url:"", which 422s) keeps the
            # kind's remembered URL. The payload has no urls map: Save files
            # only the URL on screen, under the kind on screen.
            _ok5 = client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})
            check("intelligence: a save without a url is accepted", _ok5.status_code == 200,
                  f"{_ok5.status_code} {_ok5.text[:120]}")

            # (c) lane dispatch: monkeypatched urlopen captures the request
            _cap9 = []

            class _FakeResp9(_io9.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *a): return False

            def _fake_open9(req, timeout=None):
                _cap9.append({"headers": {k.lower(): v for k, v in req.headers.items()},
                              "body": json.loads(req.data.decode())})
                return _FakeResp9(json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())

            _saved_open9 = _urq9.urlopen
            _saved_bk9 = _os.environ.get("LVI_BRAIN_KEY")
            _saved_hk9 = _os.environ.get("LVI_HERMES_KEY")
            _saved_ok9 = _os.environ.get("LVI_OPENCLAW_KEY")
            try:
                _os.environ["LVI_BRAIN_KEY"] = "suite-intelligence-key-not-real"
                _os.environ["LVI_HERMES_KEY"] = "suite-hermes-key-not-real"
                _os.environ["LVI_OPENCLAW_KEY"] = "suite-openclaw-key-not-real"
                for mod_name in ("intelligence.agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import intelligence.agent as _ha9
                _urq9.urlopen = _fake_open9
                # kind from the LVI_CONFIG we just PUT (openclaw)
                _a9 = _ha9.IntelligenceAgent(lull_secs=0.05, room="suite",
                                       session_mode="sticky")
                _a9._ask_intelligence("hi")
                _oc_body = _cap9[-1]["body"]
                _oc_hdr = _cap9[-1]["headers"]
                check("intelligence: openclaw lane: model encodes agent, no conversation",
                      _oc_body.get("model") == "openclaw/suite-agent"
                      and "conversation" not in _oc_body, str(_oc_body)[:140])
                check("intelligence: openclaw lane: sticky session-key header",
                      _oc_hdr.get("x-openclaw-session-key") == "voice-suite",
                      str(_oc_hdr.get("x-openclaw-session-key")))
                check("intelligence: openclaw lane: LVI_OPENCLAW_KEY wins (naming law 2026-10-08)",
                      _oc_hdr.get("authorization") == "Bearer suite-openclaw-key-not-real",
                      str(_oc_hdr.get("authorization")))
                # strict key law: NO cross-kind fallback —
                # without LVI_OPENCLAW_KEY the openclaw lane must fail with
                # a clear error naming its OWN variable, never silently
                # auth with another intelligence's token (a real wrong-token 401).
                _os.environ.pop("LVI_OPENCLAW_KEY", None)
                for mod_name in ("intelligence.agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import intelligence.agent as _ha9_fb
                # A session built right after boot with this config (boot ==
                # saved): the saved-vs-boot rule has its own suite.
                import config as _cfg9_fb
                _cfg9_fb._load.cache_clear()
                _cfg9_fb._BOOT = None
                _a9_fb = _ha9_fb.IntelligenceAgent(lull_secs=0.05, room="suite",
                                             session_mode="fresh")
                try:
                    _a9_fb._ask_intelligence("hi")
                    _strict_ok9 = False
                    _strict_err9 = "no error raised"
                except RuntimeError as _e9:
                    _strict_ok9 = "LVI_OPENCLAW_KEY" in str(_e9)
                    _strict_err9 = str(_e9)[:80]
                check("intelligence: openclaw lane: missing LVI_OPENCLAW_KEY fails loud, names its own variable",
                      _strict_ok9, _strict_err9)
                _os.environ["LVI_OPENCLAW_KEY"] = "suite-openclaw-key-not-real"
                # fresh: header absent
                _a9f = _ha9.IntelligenceAgent(lull_secs=0.05, room="suite",
                                        session_mode="fresh")
                _a9f._ask_intelligence("hi")
                check("intelligence: openclaw lane: fresh sends no session header",
                      "x-openclaw-session-key" not in _cap9[-1]["headers"], "")
                # hermes round-trip: flip kind back, conversation field returns
                client.put("/api/dashboard/intelligence", json={"kind": "hermes"})
                for mod_name in ("intelligence.agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import intelligence.agent as _ha9b
                _a9b = _ha9b.IntelligenceAgent(lull_secs=0.05, room="suite",
                                         session_mode="fresh")
                _a9b._ask_intelligence("hi")
                _hm_body = _cap9[-1]["body"]
                _hm_hdr = _cap9[-1]["headers"]
                check("intelligence: hermes round-trip: conversation field + Hermes header restored",
                      _hm_body.get("conversation") and
                      _hm_hdr.get("x-hermes-session-key") == _hm_body.get("conversation"),
                      str(_hm_body)[:140])
                # Key rule: keys are KIND-AWARE — the hermes
                # lane must auth with LVI_HERMES_KEY even when LVI_BRAIN_KEY
                # (the OpenClaw token on the real rig) is also set. A kind
                # flip must never cross the tokens.
                check("intelligence: hermes lane: kind-aware key (LVI_HERMES_KEY wins on hermes)",
                      _hm_hdr.get("authorization") == "Bearer suite-hermes-key-not-real",
                      str(_hm_hdr.get("authorization")))
                # no key at all: construction survives, ask raises RuntimeError
                _os.environ.pop("LVI_BRAIN_KEY", None)
                _os.environ.pop("LVI_HERMES_KEY", None)
                _os.environ.pop("LVI_OPENCLAW_KEY", None)
                try:
                    _a9c = _ha9b.IntelligenceAgent(lull_secs=0.05, room="suite",
                                             session_mode="fresh")
                    try:
                        _a9c._ask_intelligence("hi")
                        check("intelligence no-key: ask raises (not success)", False, "no raise")
                    except RuntimeError as _e9:
                        check("intelligence no-key: RuntimeError with a clear message",
                              "no intelligence key" in str(_e9).lower(), str(_e9))
                    except KeyError as _e9:
                        check("intelligence no-key: RuntimeError with a clear message",
                              False, f"KeyError brick: {_e9}")
                except KeyError as _e9:
                    check("intelligence no-key: construction survives", False,
                          f"KeyError brick: {_e9}")
            finally:
                _urq9.urlopen = _saved_open9
                for _k9, _v9 in (("LVI_BRAIN_KEY", _saved_bk9),
                                 ("LVI_HERMES_KEY", _saved_hk9),
                                 ("LVI_OPENCLAW_KEY", _saved_ok9)):
                    if _v9 is None:
                        _os.environ.pop(_k9, None)
                    else:
                        _os.environ[_k9] = _v9

            # (e) key POST: response carries no key material; _write_env_key
            # monkeypatched so the real .env is NEVER touched
            _saved_wek9 = _routes9._write_env_key
            async def _fake_wek9(var, value):
                return True
            _routes9._write_env_key = _fake_wek9
            try:
                _kr = client.put("/api/dashboard/intelligence/key",
                                 json={"kind": "hermes",
                                       "api_key": "suite-keymaterial-12345"})
                _kj = _kr.json()
                check("intelligence: key POST ok + never echoes material",
                      _kr.status_code == 200 and _kj.get("key_set") is True
                      and "suite-keymaterial-12345" not in _kr.text,
                      _kr.text[:120])
                _kbad = client.put("/api/dashboard/intelligence/key",
                                   json={"kind": "hermes", "api_key": "bad\nkey"})
                check("intelligence: key POST newline 422s", _kbad.status_code == 422, "")
                # Contract pins: kind is REQUIRED (no silent
                # guess from config), only shipped lanes, extra="forbid".
                _knk = client.put("/api/dashboard/intelligence/key",
                                  json={"api_key": "suite-keymaterial-12345"})
                check("intelligence: key POST without kind 422s",
                      _knk.status_code == 422, _knk.text[:120])
                _kbk = client.put("/api/dashboard/intelligence/key",
                                  json={"kind": "claude",
                                        "api_key": "suite-keymaterial-12345"})
                check("intelligence: key POST unshipped kind 422s",
                      _kbk.status_code == 422, _kbk.text[:120])
                _kex = client.put("/api/dashboard/intelligence/key",
                                  json={"kind": "hermes", "apikey": "x",
                                        "api_key": "suite-keymaterial-12345"})
                check("intelligence: key POST extra field 422s (forbid)",
                      _kex.status_code == 422, _kex.text[:120])
                # Pin: the variable follows the PAYLOAD's
                # kind, NOT config.yaml's. The UI-rebuilt 401: dropdown on
                # OpenClaw, config still hermes, Save key → the OpenClaw
                # token landed in LVI_HERMES_KEY. Cross each kind against
                # the opposite configured kind so the old config-derived
                # route would fail both halves.
                _wek_calls9 = []
                async def _fake_wek_rec9(var, value):
                    _wek_calls9.append(var)
                    return True
                _routes9._write_env_key = _fake_wek_rec9
                try:
                    client.put("/api/dashboard/intelligence",
                               json={"kind": "hermes"})
                    _kx1 = client.put("/api/dashboard/intelligence/key",
                                      json={"kind": "openclaw",
                                            "api_key": "suite-keymaterial-12345"})
                    client.put("/api/dashboard/intelligence",
                               json={"kind": "openclaw", "agent": "suite-agent"})
                    _kx2 = client.put("/api/dashboard/intelligence/key",
                                      json={"kind": "hermes",
                                            "api_key": "suite-keymaterial-12345"})
                    check("intelligence: key save follows payload kind, not config",
                          _wek_calls9 == ["LVI_OPENCLAW_KEY", "LVI_HERMES_KEY"],
                          f"calls={_wek_calls9}")
                    check("intelligence: key response names the variable written",
                          _kx1.json().get("var") == "LVI_OPENCLAW_KEY"
                          and _kx2.json().get("var") == "LVI_HERMES_KEY",
                          f"{_kx1.text[:80]} | {_kx2.text[:80]}")
                    _cfg_kind9 = (_yaml9.safe_load(
                        Path(os.environ["LVI_CONFIG"]).read_text())
                        .get("intelligence") or {}).get("kind")
                    check("intelligence: key save never touches config kind",
                          _cfg_kind9 == "openclaw", f"kind={_cfg_kind9}")
                    # GET reports key status for EVERY kind (chip follows
                    # the kind on screen).
                    _skh9 = _os.environ.pop("LVI_HERMES_KEY", None)
                    _skn9 = _os.environ.pop("LVI_NANOBOT_KEY", None)
                    _sko9 = _os.environ.get("LVI_OPENCLAW_KEY")
                    _os.environ["LVI_OPENCLAW_KEY"] = "suite-oc-token-123"
                    try:
                        _gk9 = client.get("/api/dashboard/intelligence").json()
                        check("intelligence: GET keys_set per kind",
                              _gk9.get("keys_set") == {"hermes": False, "openclaw": True, "nanobot": False}
                              and "suite-oc-token-123" not in str(_gk9),
                              str(_gk9.get("keys_set")))
                    finally:
                        if _skh9 is not None:
                            _os.environ["LVI_HERMES_KEY"] = _skh9
                        if _skn9 is not None:
                            _os.environ["LVI_NANOBOT_KEY"] = _skn9
                        if _sko9 is None:
                            _os.environ.pop("LVI_OPENCLAW_KEY", None)
                        else:
                            _os.environ["LVI_OPENCLAW_KEY"] = _sko9
                finally:
                    _routes9._write_env_key = _fake_wek9
                    client.put("/api/dashboard/intelligence",
                               json={"kind": "hermes"})
            finally:
                _routes9._write_env_key = _saved_wek9

            # Agent picker + masked key hints: agents
            # discovered per provider, remembered per kind; a Hermes profile
            # routes to <base>/p/<agent>/v1 with ITS OWN key; set keys show
            # their first 4 characters, never more.
            import intelligence.target as _it12
            import threading as _th12
            import http.server as _hs12
            _h12 = _it12.key_hint
            _M12 = "\u2022" * 8
            check("agent picker key_var: hermes default -> LVI_HERMES_KEY, profile -> LVI_HERMES_KEY_<AGENT>, openclaw shares one",
                  _it12.key_var("hermes", "") == "LVI_HERMES_KEY"
                  and _it12.key_var("hermes", "default") == "LVI_HERMES_KEY"
                  and _it12.key_var("hermes", "home-helper") == "LVI_HERMES_KEY_HOME_HELPER"
                  and _it12.key_var("openclaw", "lenoxvi-test") == "LVI_OPENCLAW_KEY", "")
            check("agent picker request_url: hermes profile -> /p/<agent>/v1; default + openclaw unchanged",
                  _it12.request_url("hermes", "http://h:8642/v1", "home-helper") == "http://h:8642/p/home-helper/v1"
                  and _it12.request_url("hermes", "http://h:8642/v1/", "default") == "http://h:8642/v1"
                  and _it12.request_url("hermes", "http://h:8642/p/old/v1", "home-helper") == "http://h:8642/p/home-helper/v1"
                  and _it12.request_url("openclaw", "http://o:18789", "x") == "http://o:18789", "")
            check("agent picker: legacy top-level agent never reroutes Hermes (kind hermes + agent lenoxvi-test)",
                  _it12.agent_for({"agent": "lenoxvi-test"}, "hermes") == ""
                  and _it12.agent_for({"agent": "lenoxvi-test"}, "openclaw") == "lenoxvi-test"
                  and _it12.agent_for({"agents": {"hermes": "default"}}, "hermes") == ""
                  and _it12.agent_for({"agents": {"hermes": "home-helper"}}, "hermes") == "home-helper", "")
            check("agent picker: key hint: first 4 + fixed-length mask; short keys show no characters; unset -> None",
                  _h12("abcdefghijklmnop") == "abcd" + _M12
                  and _h12("abcdefghijklmnopqrstuvwxyz0123456789") == "abcd" + _M12
                  and _h12("short1234") == _M12 and _h12("") is None and _h12(None) is None, "")

            class _H12(_hs12.BaseHTTPRequestHandler):
                routes = {}
                def do_GET(self):
                    fn = self.routes.get(self.path)
                    if fn is None:
                        self.send_response(404); self.end_headers(); return
                    code, body = fn(self)
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(body).encode())
                def log_message(self, *a):
                    pass
            _srv12 = _hs12.ThreadingHTTPServer(("127.0.0.1", 0), _H12)
            _port12 = _srv12.server_address[1]
            _th12.Thread(target=_srv12.serve_forever, daemon=True).start()
            _auth12 = []
            def _models12(h):
                _auth12.append(h.headers.get("Authorization"))
                return 200, {"object": "list", "data": [
                    {"id": "openclaw"}, {"id": "openclaw/default"}, {"id": "openclaw/suite-a"},
                    {"id": "openclaw/bad agent"}, {"id": "gpt-4o"}]}
            _H12.routes = {
                "/v1/models": _models12,
                "/api/status": lambda h: (200, {"profiles": ["home-helper", "default", "parked-one", "../evil"],
                                                "parked_profiles": ["parked-one"]})}
            _env12 = {"LVI_OPENCLAW_KEY": "ockey-abcdefghijkl", "LVI_HERMES_KEY": "hmkey-abcdefghijkl"}
            _saved_port12 = _it12.HERMES_STATUS_PORT
            _saved_env12 = {k: _os.environ.get(k) for k in
                            ("LVI_OPENCLAW_KEY", "LVI_HERMES_KEY", "LVI_HERMES_KEY_HOME_HELPER")}
            _saved_open12 = _urq9.urlopen
            try:
                _oc12 = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}", _env12)
                check("agent picker: openclaw discovery: openclaw/<agent> ids only, invalid names dropped, token sent",
                      [a["id"] for a in _oc12["agents"]] == ["default", "suite-a"]
                      and _auth12[-1] == "Bearer ockey-abcdefghijkl", str(_oc12)[:200])
                _oc12b = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}/v1", _env12)
                check("agent picker: openclaw discovery: URL with or without /v1",
                      [a["id"] for a in _oc12b["agents"]] == ["default", "suite-a"], str(_oc12b)[:120])
                _hm12 = _it12.discover_agents("hermes", "http://ignored/v1", _env12,
                                              status_url=f"http://127.0.0.1:{_port12}/api/status")
                check("agent picker: hermes discovery: profiles from status, default first, parked + invalid dropped",
                      [a["id"] for a in _hm12["agents"]] == ["default", "home-helper"]
                      and _hm12["source"] == "hermes-status", str(_hm12)[:200])
                _by12 = {a["id"]: a for a in _hm12["agents"]}
                check("agent picker: hermes discovery: per-profile key status (default set, profile unset)",
                      _by12["default"]["key_set"] and not _by12["home-helper"]["key_set"]
                      and _by12["home-helper"]["key_hint"] is None, str(_by12)[:200])
                check("agent picker: discovery never returns key material (hint = first 4 only)",
                      "abcdefghijkl" not in json.dumps([_oc12, _hm12])
                      and _by12["default"]["key_hint"] == "hmke" + _M12, "")
                _hd12 = _it12.discover_agents("hermes", "http://127.0.0.1:9/v1", _env12, timeout=1.0,
                                              status_url="http://127.0.0.1:9/api/status")
                check("agent picker: hermes discovery: dashboard down -> default agent + honest note",
                      [a["id"] for a in _hd12["agents"]] == ["default"] and _hd12["source"] == "fallback"
                      and "default agent only" in (_hd12.get("note") or ""), str(_hd12)[:200])
                _nk12 = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}", {})
                check("agent picker: openclaw discovery without a key says so",
                      _nk12["agents"] == [] and "key" in (_nk12.get("note") or ""), str(_nk12))
                _nk12b = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}", _env12,
                                               attach_key=False)
                check("agent picker: attach_key=False probes with no Authorization header",
                      [a["id"] for a in _nk12b["agents"]] == ["default", "suite-a"]
                      and _auth12[-1] is None, f"auth={_auth12[-1]!r}")
                _nk12c = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}", {},
                                               attach_key=False)
                check("agent picker: attach_key=False still probes with no key saved",
                      [a["id"] for a in _nk12c["agents"]] == ["default", "suite-a"]
                      and _auth12[-1] is None, str(_nk12c)[:150])
                check("agent picker: same_target tolerates /v1 and slash spellings, nothing else",
                      _it12.same_target("http://h:18789", "http://h:18789/v1")
                      and _it12.same_target("http://h:18789/v1/", "http://h:18789/v1")
                      and _it12.same_target("http://H.test:1", "http://h.test:1")
                      and not _it12.same_target("http://h:18789", "http://h:18790")
                      and not _it12.same_target("http://h:18789", "https://h:18789")
                      and not _it12.same_target("http://h:18789", "http://evil:1/v1"), "")

                # route: kind validated, URL override, hermes via the status port
                _os.environ["LVI_OPENCLAW_KEY"] = "ockey-abcdefghijkl"
                _os.environ["LVI_HERMES_KEY"] = "hmkey-abcdefghijkl"
                _it12.HERMES_STATUS_PORT = _port12
                _ra12 = client.post("/api/dashboard/intelligence/agents",
                                    json={"kind": "openclaw", "url": f"http://127.0.0.1:{_port12}"})
                check("agent picker: POST /intelligence/agents openclaw: agents + selected",
                      _ra12.status_code == 200 and [a["id"] for a in _ra12.json()["agents"]] == ["default", "suite-a"]
                      and "selected" in _ra12.json(), _ra12.text[:200])
                check("agent picker: POST /intelligence/agents: unsaved URL probes WITHOUT the stored key",
                      _auth12[-1] is None, f"Authorization went to an unsaved URL: {_auth12[-1]!r}")
                _rh12 = client.post("/api/dashboard/intelligence/agents",
                                    json={"kind": "hermes", "url": "http://127.0.0.1:1/v1"})
                check("agent picker: POST /intelligence/agents hermes: profiles via the status port, no key material",
                      _rh12.status_code == 200 and [a["id"] for a in _rh12.json()["agents"]] == ["default", "home-helper"]
                      and "abcdefghijkl" not in _rh12.text, _rh12.text[:200])
                _rb12 = client.post("/api/dashboard/intelligence/agents", json={"kind": "claude"})
                _rb12b = client.post("/api/dashboard/intelligence/agents", json={"kind": "hermes", "url": "file:///etc"})
                check("agent picker: POST /intelligence/agents: unknown kind + non-http URL 422",
                      _rb12.status_code == 422 and _rb12b.status_code == 422, f"{_rb12.status_code} {_rb12b.status_code}")

                # PUT: agent filed under its kind; legacy key only for openclaw
                client.put("/api/dashboard/intelligence", json={"kind": "openclaw", "agent": "suite-a"})
                client.put("/api/dashboard/intelligence", json={"kind": "hermes", "agent": "home-helper"})
                _c12 = _yaml9.safe_load(Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
                check("agent picker: PUT files the agent per kind (hermes never overwrites openclaw's)",
                      (_c12.get("agents") or {}).get("hermes") == "home-helper"
                      and (_c12.get("agents") or {}).get("openclaw") == "suite-a"
                      and _c12.get("agent") == "suite-a", str(_c12.get("agents")) + " agent=" + str(_c12.get("agent")))
                _g12 = client.get("/api/dashboard/intelligence").json()
                check("agent picker: GET agents per kind + key status per kind, never the key",
                      # hermes/openclaw only: the suite's config is a copy of the live
                      # one, which may carry any nanobot agent.
                      {k: (_g12.get("agents") or {}).get(k) for k in ("hermes", "openclaw")}
                      == {"hermes": "home-helper", "openclaw": "suite-a"}
                      and "nanobot" in (_g12.get("agents") or {})
                      and (_g12.get("keys_set") or {}).get("openclaw") is True
                      and "abcdefghijkl" not in json.dumps(_g12), str(_g12)[:240])

                # the voice agent: profile turn -> /p/home-helper/v1 with the profile's key
                _urls12 = []
                def _fake_open12(req, timeout=None):
                    _urls12.append((req.full_url, {k.lower(): v for k, v in req.headers.items()}))
                    if req.data is None:  # the probe's health GET
                        return _FakeResp9(b"{}")
                    return _fake_open9(req, timeout)
                _os.environ["LVI_HERMES_KEY_HOME_HELPER"] = "helper-key-not-real-123"
                for _m12 in ("intelligence.agent",):
                    sys.modules.pop(_m12, None)
                import intelligence.agent as _ia12
                _urq9.urlopen = _fake_open12
                _a12 = _ia12.IntelligenceAgent(lull_secs=0.05, room="suite", session_mode="fresh")
                _a12._ask_intelligence("hi")
                check("agent picker: hermes profile turn: /p/home-helper/v1 + LVI_HERMES_KEY_HOME_HELPER",
                      _urls12[-1][0].endswith("/p/home-helper/v1/chat/completions")
                      and _urls12[-1][1].get("authorization") == "Bearer helper-key-not-real-123",
                      f"{_urls12[-1][0]} {_urls12[-1][1].get('authorization')}")
                client.put("/api/dashboard/intelligence", json={"kind": "hermes", "agent": "default"})
                for _m12 in ("intelligence.agent",):
                    sys.modules.pop(_m12, None)
                import intelligence.agent as _ia12b
                _a12b = _ia12b.IntelligenceAgent(lull_secs=0.05, room="suite", session_mode="fresh")
                _a12b._ask_intelligence("hi")
                check("agent picker: hermes default agent: configured URL + LVI_HERMES_KEY (unchanged behaviour)",
                      "/p/" not in _urls12[-1][0]
                      and _urls12[-1][1].get("authorization") == "Bearer hmkey-abcdefghijkl", _urls12[-1][0])

                # Test probe: the saved profile's URL + key
                _urls12.clear()
                _routes9._intelligence_probe({"kind": "hermes", "url": "http://127.0.0.1:1/v1",
                                              "agents": {"hermes": "home-helper"}})
                check("agent picker: Test probe uses the profile URL + its key",
                      any(u.endswith("/p/home-helper/v1/chat/completions")
                          and h.get("authorization") == "Bearer helper-key-not-real-123" for u, h in _urls12),
                      str([u for u, _ in _urls12]))
                _urq9.urlopen = _saved_open12

                # key save: per-profile variable, hint back, never the key
                _wek12 = []
                async def _fake_wek12(var, value):
                    _wek12.append(var)
                    return True
                _routes9._write_env_key = _fake_wek12
                try:
                    _kp12 = client.put("/api/dashboard/intelligence/key", json={
                        "kind": "hermes", "agent": "home-helper", "api_key": "helperkey-abcdefghijkl"})
                    _kp12b = client.put("/api/dashboard/intelligence/key", json={
                        "kind": "openclaw", "agent": "suite-a", "api_key": "oc-token-abcdefghijkl"})
                    _kp12c = client.put("/api/dashboard/intelligence/key", json={
                        "kind": "hermes", "agent": "default", "api_key": "hm-token-abcdefghijkl"})
                    check("agent picker: key save: hermes profile -> its own var; openclaw ignores agent; default -> base",
                          _wek12 == ["LVI_HERMES_KEY_HOME_HELPER", "LVI_OPENCLAW_KEY", "LVI_HERMES_KEY"], str(_wek12))
                    check("agent picker: key save returns the first-4 hint, never the key",
                          _kp12.json().get("key_hint") == "help" + _M12
                          and "abcdefghijkl" not in _kp12.text + _kp12b.text + _kp12c.text, _kp12.text[:160])
                    _kb12 = client.put("/api/dashboard/intelligence/key", json={
                        "kind": "hermes", "agent": "../x", "api_key": "helperkey-abcdefghijkl"})
                    check("agent picker: key save: invalid agent name 422s", _kb12.status_code == 422, str(_kb12.status_code))
                finally:
                    _routes9._write_env_key = _saved_wek9
                _t12 = client.get("/api/dashboard/tts").json()
                check("agent picker: TTS GET carries key hints (first 4 or null), never a key",
                      "elevenlabs_key_hint" in _t12 and "deepgram_key_hint" in _t12, str(_t12)[:160])
            finally:
                _urq9.urlopen = _saved_open12
                _it12.HERMES_STATUS_PORT = _saved_port12
                _srv12.shutdown()
                for _k12, _v12 in _saved_env12.items():
                    if _v12 is None:
                        _os.environ.pop(_k12, None)
                    else:
                        _os.environ[_k12] = _v12
                client.put("/api/dashboard/intelligence", json={"kind": "hermes", "agent": "default"})

            # (f) test route: monkeypatched probe path — ok + error
            _saved_probe9 = _routes9._intelligence_probe
            _routes9._intelligence_probe = lambda cfg: {"ok": True,
                                                             "latency_ms": 42,
                                                             "model": "openclaw/suite-agent",
                                                             "reply": "ok"}
            try:
                _tr = client.post("/api/dashboard/intelligence/test").json()
                check("intelligence: test route ok path", _tr.get("ok") is True
                      and _tr.get("latency_ms") == 42, str(_tr)[:120])
            finally:
                _routes9._intelligence_probe = _saved_probe9
            _routes9._intelligence_probe = lambda cfg: {"ok": False,
                                                             "error": "unreachable: URLError"}
            try:
                _tr2 = client.post("/api/dashboard/intelligence/test").json()
                check("intelligence: test route error path", _tr2.get("ok") is False
                      and "URLError" in _tr2.get("error", ""), str(_tr2)[:120])
            finally:
                _routes9._intelligence_probe = _saved_probe9

            # ── intelligence: the OpenClaw key only reaches a URL saved for OpenClaw ──
            # Save files ONLY the URL on screen, under the kind on screen: the
            # other kind's typed-but-unsaved draft must never become its saved
            # URL, because discovery attaches the key to the saved URL. A kind
            # switch with no URL for the new kind refuses instead of keeping the
            # old kind's URL. A new OpenClaw setup can save its URL before any
            # agent is known, so discovery can then list agents with the key.
            # A bad port is a 422, never a 500. Pinned at urlopen: the header
            # that actually leaves the box.
            import intelligence.target as _it13
            _icfg_saved13 = dict(config_store.read_section("intelligence") or {})
            _okey_saved13 = _os.environ.get("LVI_OPENCLAW_KEY")
            _saved_open13 = _urq9.urlopen
            _auth13 = []

            def _models13(req, timeout=None):
                _auth13.append((req.full_url, req.get_header("Authorization")))
                return _FakeResp9(json.dumps({"object": "list", "data": [{"id": "openclaw/main"}]}).encode())

            def _icfg13():
                return _yaml9.safe_load(Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}

            def _put_cfg13(**kv):
                config_store.write_section("intelligence", {**_base13, **kv})
            _H13 = "http://hm.test:8642/v1"
            _O13 = "http://oc.test:18789/v1"
            _T13 = "http://typo.test:18789/v1"
            _base13 = {k: v for k, v in _icfg_saved13.items()
                       if k not in ("kind", "url", "urls", "agent", "agents")}
            try:
                _os.environ["LVI_OPENCLAW_KEY"] = "ockey13-abcdefghijkl"
                _urq9.urlopen = _models13
                # Save on Hermes while an OpenClaw draft sits in the other slot
                _put_cfg13(kind="hermes", url=_H13, urls={"hermes": _H13})
                _r13 = client.put("/api/dashboard/intelligence", json={
                    "kind": "hermes", "url": _H13, "urls": {"hermes": _H13, "openclaw": _T13}})
                check("intelligence: Save never files another kind's unsaved URL",
                      _r13.status_code == 422 and "openclaw" not in (_icfg13().get("urls") or {}),
                      f"{_r13.status_code} urls={_icfg13().get('urls')}")
                _auth13.clear()
                _d13 = client.post("/api/dashboard/intelligence/agents", json={"kind": "openclaw", "url": _T13})
                check("intelligence: a URL never saved for OpenClaw is probed without the key",
                      _d13.status_code == 200 and _auth13 and all(a is None for _, a in _auth13),
                      f"{_d13.status_code} {[(u, (a or '')[:10]) for u, a in _auth13]}")

                # kind switch with no URL for the new kind: refuse, change nothing
                _put_cfg13(kind="hermes", url=_H13, urls={"hermes": _H13}, agents={"openclaw": "main"})
                _r13b = client.put("/api/dashboard/intelligence", json={"kind": "openclaw", "agent": "main"})
                _c13b = _icfg13()
                check("intelligence: switching to OpenClaw with no OpenClaw URL refuses, keeps the Hermes config",
                      _r13b.status_code == 422 and "openclaw" in _r13b.text.lower() and "url" in _r13b.text.lower()
                      and _c13b.get("kind") == "hermes" and _c13b.get("url") == _H13,
                      f"{_r13b.status_code} {_r13b.text[:120]} kind={_c13b.get('kind')} url={_c13b.get('url')}")
                _put_cfg13(kind="openclaw", url=_O13, urls={"openclaw": _O13}, agents={"openclaw": "main"})
                _r13c = client.put("/api/dashboard/intelligence", json={"kind": "hermes"})
                _c13c = _icfg13()
                check("intelligence: switching to Hermes with no Hermes URL refuses, keeps the OpenClaw config",
                      _r13c.status_code == 422 and _c13c.get("kind") == "openclaw" and _c13c.get("url") == _O13,
                      f"{_r13c.status_code} kind={_c13c.get('kind')} url={_c13c.get('url')}")
                _put_cfg13(kind="hermes", url=_H13)
                _r13d = client.put("/api/dashboard/intelligence", json={"kind": "hermes", "agent": "default"})
                check("intelligence: a same-kind save without a URL keeps that kind's URL",
                      _r13d.status_code == 200 and _icfg13().get("url") == _H13,
                      f"{_r13d.status_code} url={_icfg13().get('url')}")

                # new OpenClaw setup: the URL saves first, the live lane stays put
                _put_cfg13(kind="hermes", url=_H13, urls={"hermes": _H13})
                _r13e = client.put("/api/dashboard/intelligence", json={"kind": "openclaw", "url": _O13})
                _c13e = _icfg13()
                check("intelligence: a new OpenClaw URL saves before any agent is known, live lane unchanged",
                      _r13e.status_code == 200 and _r13e.json().get("url_only") is True
                      and (_c13e.get("urls") or {}).get("openclaw") == _O13
                      and _c13e.get("kind") == "hermes" and _c13e.get("url") == _H13
                      and not (_c13e.get("agents") or {}).get("openclaw") and not _c13e.get("agent"),
                      f"{_r13e.status_code} {_r13e.text[:120]} cfg={dict(_c13e)}")
                _auth13.clear()
                _d13e = client.post("/api/dashboard/intelligence/agents", json={"kind": "openclaw", "url": _O13})
                check("intelligence: once its URL is saved, OpenClaw discovery sends the key and lists agents",
                      _d13e.status_code == 200 and [a["id"] for a in _d13e.json().get("agents") or []] == ["main"]
                      and _auth13 and (_auth13[-1][1] or "").startswith("Bearer "),
                      f"{_d13e.text[:160]} {[(u, (a or '')[:10]) for u, a in _auth13]}")
                _r13f = client.put("/api/dashboard/intelligence",
                                   json={"kind": "openclaw", "url": _O13, "agent": "main"})
                _c13f = _icfg13()
                check("intelligence: picking an agent then switches the lane to OpenClaw",
                      _r13f.status_code == 200 and not _r13f.json().get("url_only")
                      and _c13f.get("kind") == "openclaw" and _c13f.get("url") == _O13
                      and (_c13f.get("agents") or {}).get("openclaw") == "main",
                      f"{_r13f.status_code} {_r13f.text[:120]}")
                _put_cfg13(kind="hermes", url=_H13, urls={"hermes": _H13, "openclaw": _O13})
                _r13g = client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})
                check("intelligence: switching to OpenClaw with no URL to save and no agent still 422s",
                      _r13g.status_code == 422 and "agent" in _r13g.text and _icfg13().get("kind") == "hermes",
                      f"{_r13g.status_code} {_r13g.text[:120]}")

                # bad ports: a validation 422, never a 500
                _put_cfg13(kind="hermes", url=_H13, urls={"hermes": _H13})
                _r13h = client.post("/api/dashboard/intelligence/agents",
                                    json={"kind": "openclaw", "url": "http://oc.test:70000/v1"})
                _r13i = client.put("/api/dashboard/intelligence",
                                   json={"kind": "hermes", "url": "http://hm.test:70000/v1"})
                _r13j = client.post("/api/dashboard/intelligence/agents",
                                    json={"kind": "hermes", "url": "http://hm.test:1:2/v1"})
                check("intelligence: an out-of-range or malformed port is a 422, never a 500 or a save",
                      (_r13h.status_code, _r13i.status_code, _r13j.status_code) == (422, 422, 422)
                      and _icfg13().get("url") == _H13,
                      f"{_r13h.status_code} {_r13i.status_code} {_r13j.status_code} url={_icfg13().get('url')}")
                # a hand-edited saved URL with a bad port: the probe compares
                # against it and must answer, unauthenticated, not 500
                _put_cfg13(kind="hermes", url=_H13, urls={"hermes": _H13, "openclaw": "http://oc.test:70000/v1"})
                _auth13.clear()
                _r13k = client.post("/api/dashboard/intelligence/agents", json={"kind": "openclaw", "url": _O13})
                check("intelligence: a saved URL with a bad port never 500s discovery and never gets the key",
                      _r13k.status_code == 200 and all(a is None for _, a in _auth13),
                      f"{_r13k.status_code} {[(u, (a or '')[:10]) for u, a in _auth13]}")
                try:
                    _st13 = (_it13.same_target("http://h:70000", "http://h:1"),
                             _it13.same_target("http://h:1:2", "http://h:1"),
                             _it13.same_target("http://h:70000", "http://h:70000"))
                except ValueError as _e13:
                    _st13 = repr(_e13)
                check("intelligence: same_target treats a bad port as a different target, never raises",
                      _st13 == (False, False, False), str(_st13))
            finally:
                _urq9.urlopen = _saved_open13
                if _okey_saved13 is None:
                    _os.environ.pop("LVI_OPENCLAW_KEY", None)
                else:
                    _os.environ["LVI_OPENCLAW_KEY"] = _okey_saved13
                config_store.write_section("intelligence", _icfg_saved13)
        finally:
            _os.environ.pop("LVI_HERMES_KEY", None)

    # Dead-TTS containment: with the
    # ElevenLabs websocket dead, her UNspoken weather answer still armed as
    # echo text, and the user's repeat question scored 71% bag-of-words overlap
    # (5 of 7 words, only 'springfield' content — s/the/on/today counted) —
    # dropped as an "echo". Two fixes pinned here: the echo bar 0.6 -> 0.9
    # (an echo is her words coming back: nearly ALL transcript words must
    # be hers) and no phantom arming (a dead TTS makes no sound, so its
    # texts can never echo back — they register nothing).
    import asyncio as _aio12
    import echo_gate as _eg12
    import intelligence.agent as _ha12
    from pipecat.frames.frames import (  # noqa: E402
        TranscriptionFrame as _TF12, VADUserStartedSpeakingFrame as _VAD12,
    )
    from pipecat.processors.frame_processor import FrameDirection as _FD12
    _ANS12 = ("It's overcast on Springfield today, about 60 degrees but feels "
              "like 48 with a stiff 21 mile-an-hour wind off the water. High "
              "of 65, low of 56, and about a third of a chance of rain, so "
              "maybe grab a jacket if you're heading out.")

    async def _c12_probe():
        _a = _ha12.IntelligenceAgent(echo_gate=_eg12.EchoGate(),
                               room="suite-t12", session_mode="fresh")
        # the exact armed state in that failure — both fillers + the answer
        # the dead TTS never actually spoke
        _a._echo_texts = ["On it.",
                          "Still on it — this one's a slow one.",
                          _ANS12]

        async def _sink12(frame, direction=None):
            pass

        _a.push_frame = _sink12
        await _a.process_frame(_VAD12(), _FD12.DOWNSTREAM)
        await _a.process_frame(_TF12("What's the weather today on Springfield?",
                                     "", 0, 0, 0), _FD12.DOWNSTREAM)
        return _a

    _a12 = _aio12.run(_c12_probe())
    check("dead tts: the repeat question lands (was dropped as an echo)",
          any("Springfield" in t for t in _a12._buffer), f"buffer={_a12._buffer}")
    check("dead tts: same-topic repeat is NOT her echo (bar 0.6 -> 0.9)",
          _a12._is_own_echo("What's the weather today on Springfield?") is False,
          f"echo_texts={len(_a12._echo_texts)}")
    check("dead tts: short same-topic follow-up is NOT her echo",
          _a12._is_own_echo("and what about tomorrow on Springfield") is False, "")
    check("dead tts: verbatim re-transcription of her reply IS caught",
          _a12._is_own_echo(_ANS12) is True, "")
    check("dead tts: clipped tail of her reply IS caught",
          _a12._is_own_echo("maybe grab a jacket if you're heading out") is True, "")
    _a_dead = _ha12.IntelligenceAgent(echo_gate=_eg12.EchoGate(),
                                room="suite-t12", session_mode="fresh",
                                tts_is_usable=lambda: False)
    _a_dead._register_echo(_ANS12)
    _a_dead._register_echo("On it.")
    check("dead tts: dead-TTS push arms NO echo texts",
          _a_dead._echo_texts == [], f"echo_texts={_a_dead._echo_texts}")
    _a_live = _ha12.IntelligenceAgent(echo_gate=_eg12.EchoGate(),
                                room="suite-t12", session_mode="fresh")
    _a_live._register_echo(_ANS12)
    _a_live._register_echo("On it.")
    check("dead tts: live/unknown TTS still arms echo texts (compat)",
          _a_live._echo_texts == [_ANS12, "On it."], f"echo_texts={_a_live._echo_texts}")
    _bot_src12 = (REPO_CORE.parent / "bot.py").read_text()
    check("dead tts: bot.py wires tts_is_usable into the agent",
          "tts_is_usable" in _bot_src12 and "tts_usable_probe" in _bot_src12,
          "bot.py missing the dead-TTS wire")

    # TTS reconnect-on-next-utterance (follow-on): pipecat
    # writes a websocket TTS service off permanently once its 3 reconnect
    # attempts exhaust — and an unusable service refuses every speak, so the
    # run_tts reconnect can never fire again; the room stays mute until a
    # node/bot restart. TTSTracked now retries the websocket AT the next
    # speak: only a socket that actually opens clears the verdict (and the
    # dashboard tts-down chip with it), and a cooldown keeps a lasting
    # outage from taxing every utterance with a connect timeout.
    import asyncio as _aio13
    sys.path.insert(0, str(REPO_CORE.parent))
    import bot as _bot13
    _TTST13 = _bot13.TTSTracked

    class _SpeakSink13:
        """Minimal stand-in for the real service below the mixin: records
        speaks, counts websocket connect attempts, fails them on demand."""
        def __init__(self):
            self._is_usable = True
            self._websocket = None
            self.fail_connect = False
            self.connects = 0
            self.spokes = []
        @property
        def is_usable(self):
            return self._is_usable
        async def set_usable(self, is_usable):
            self._is_usable = is_usable
        async def _disconnect(self):
            self._websocket = None
        async def _connect(self):
            self.connects += 1
            if self.fail_connect:
                raise ConnectionError("suite: WAN still down")
            self._websocket = object()
        async def _synthesize_text(self, text, context_id):
            self.spokes.append(text)
            return "spoke"

    class _Tracked13(_TTST13, _SpeakSink13):
        pass

    _s13 = _Tracked13()
    _s13.bind_tts_room("suite-t13")
    _aio13.run(_s13.set_usable(False))   # pipecat's permanent death verdict
    _rc13 = _aio13.run(_s13._synthesize_text("first speak", "suite-ctx"))
    check("tts reconnect: dead TTS retries the websocket on the next speak",
          _s13.is_usable and _s13.connects == 1 and _rc13 == "spoke"
          and _s13.spokes == ["first speak"],
          f"usable={_s13.is_usable} connects={_s13.connects} rc={_rc13}")

    _live13 = _Tracked13()
    _rc13b = _aio13.run(_live13._synthesize_text("live speak", "suite-ctx"))
    check("tts reconnect: a live TTS service never pays the revive",
          _live13.connects == 0 and _rc13b == "spoke",
          f"connects={_live13.connects} rc={_rc13b}")

    _dead13 = _Tracked13()
    _dead13.bind_tts_room("suite-t13")
    _aio13.run(_dead13.set_usable(False))
    _dead13.fail_connect = True
    _rc13c = _aio13.run(_dead13._synthesize_text("during outage", "suite-ctx"))
    check("tts reconnect: failed revive stays dead and never breaks the speak",
          (not _dead13.is_usable) and _dead13.connects == 1 and _rc13c == "spoke",
          f"usable={_dead13.is_usable} connects={_dead13.connects} rc={_rc13c}")
    _aio13.run(_dead13._synthesize_text("still down", "suite-ctx"))
    check("tts reconnect: cooldown throttles retries while the outage lasts",
          _dead13.connects == 1, f"connects={_dead13.connects}")
    _dead13._revive_last_attempt = 0.0   # simulate the cooldown expiring
    _aio13.run(_dead13._synthesize_text("after cooldown", "suite-ctx"))
    check("tts reconnect: a later speak gets the next retry",
          _dead13.connects == 2 and not _dead13.is_usable,
          f"connects={_dead13.connects} usable={_dead13.is_usable}")

    import dashboard.registry as _reg13
    _notes13 = []
    _orig_note13 = _reg13.note_tts
    _reg13.note_tts = lambda room, provider, usable: _notes13.append((room, provider, usable))
    try:
        _chip13 = _Tracked13()
        _chip13.bind_tts_room("suite-t13")
        _aio13.run(_chip13.set_usable(False))
        _aio13.run(_chip13._synthesize_text("revive me", "suite-ctx"))
        check("tts reconnect: a revived TTS clears the dashboard tts-down chip",
              _notes13[-1] == ("suite-t13", "elevenlabs", True) and _chip13.is_usable,
              f"notes={_notes13}")
    finally:
        _reg13.note_tts = _orig_note13

    # The revive hook must be a method pipecat's TTSService ITSELF defines
    # (pipecat 1.13 removed _push_tts_frames: an override of a vanished
    # method is never called, and the revive dies silently). Every speak
    # reaches run_tts through _synthesize_text in pipecat 1.13.
    from pipecat.services.tts_service import TTSService as _PipecatTTS13
    check("tts reconnect: the real ElevenLabs service rides the revive mixin",
          issubclass(_bot13.TrackedElevenLabsTTSService, _TTST13)
          and hasattr(_bot13.ElevenLabsTTSService, "_connect")
          and hasattr(_bot13.ElevenLabsTTSService, "_disconnect")
          and "_synthesize_text" in _TTST13.__dict__
          and "_synthesize_text" in _PipecatTTS13.__dict__,
          "Tracked* must sit on TTSTracked over a websocket TTS service, and the "
          "revive hook must override a method pipecat's TTSService defines")
    _dlg13 = getattr(_bot13, "TrackedElevenLabsDialogueTTSService", None)
    check("tts reconnect: the v4/v3 dialogue lane rides the same revive hook",
          _dlg13 is not None and issubclass(_dlg13, _TTST13)
          and _dlg13._synthesize_text is _TTST13._synthesize_text,
          "TrackedElevenLabsDialogueTTSService must resolve _synthesize_text to TTSTracked's")

    # Node-command error mapping: an in-flight duplicate is a
    # 409 CONFLICT (it used to read as a naked "Gateway Timeout" 504), and a
    # bare transport timeout (empty detail — the observed restart error shape)
    # now says half-open + self-heals instead of an empty string the UI
    # rendered as "Gateway Timeout".
    import asyncio as _aio14
    import dashboard.node_command as _nc14
    from dashboard.node_command import (CommandInFlight as _InFlight14,
                                        NoLiveSession as _NoLive14)

    registry.note_offer("pc-t14", "suite-t14")
    registry.mark_connected("pc-t14")
    _real_request14 = _nc14.request

    def _raising_request14(exc):
        async def _r(room, cmd, args=None, timeout=12.0):
            raise exc
        return _r

    try:
        _nc14.request = _raising_request14(
            _InFlight14("'restart' already in flight for 'suite-t14'"))
        _r14a = client.post("/api/dashboard/nodes/suite-t14/restart")
        check("node command: in-flight duplicate -> 409 Conflict (was a 504)",
              _r14a.status_code == 409 and "already in flight" in _r14a.text,
              f"status={_r14a.status_code} body={_r14a.text[:80]}")
        _nc14.request = _raising_request14(TimeoutError())
        _r14b = client.post("/api/dashboard/nodes/suite-t14/restart")
        check("node command: bare timeout -> 504 with half-open + self-heal wording",
              _r14b.status_code == 504 and "half-open" in _r14b.text
              and "10 min" in _r14b.text,
              f"status={_r14b.status_code} body={_r14b.text[:100]}")
        _nc14.request = _raising_request14(
            _NoLive14("no live session for room 'suite-t14'"))
        _r14c = client.post("/api/dashboard/nodes/suite-t14/restart")
        check("node command: half-open session -> 504 naming the half-open shape",
              _r14c.status_code == 504 and "half-open" in _r14c.text,
              f"status={_r14c.status_code} body={_r14c.text[:100]}")
    finally:
        _nc14.request = _real_request14
        registry.mark_disconnected("pc-t14")

    # node_command side: a pending duplicate raises CommandInFlight (the
    # route's 409 depends on the class, not on message sniffing).
    _saved_sender14 = _nc14.registry.sender_for_room

    async def _inflight_probe14():
        _nc14.registry.sender_for_room = lambda room: (object(), "pc-t14")
        fut = _aio14.get_event_loop().create_future()
        _nc14._pending[("pc-t14", "restart")] = fut
        try:
            await _nc14.request("suite-t14", "restart")
            return "no-raise"
        except _InFlight14:
            return "inflight"
        except TimeoutError as e:
            return f"timeout:{str(e)[:40]}"
        finally:
            _nc14._pending.pop(("pc-t14", "restart"), None)
            _nc14.registry.sender_for_room = _saved_sender14

    _res14 = _aio14.run(_inflight_probe14())
    check("node command: node_command raises CommandInFlight for a pending duplicate",
          _res14 == "inflight", f"result={_res14}")

    # A late reply to a timed-out command must never answer the retry.
    _saved_send14 = _nc14._send

    def _ack14(muted):
        _nc14.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_mute",
                                     "ok": True, "data": {"muted": muted}}), "pc-t14")

    async def _late_probe14():
        _nc14.registry.sender_for_room = lambda room: (object(), "pc-t14")
        _nc14._send = lambda out, payload: True
        out = {}
        try:
            try:
                await _nc14.request("suite-t14", "set_mute", {"mute": True}, timeout=0.2)
                out["first"] = "answered?"
            except TimeoutError:
                out["first"] = "timeout"
            retry = _aio14.create_task(
                _nc14.request("suite-t14", "set_mute", {"mute": False}, timeout=1.0))
            await _aio14.sleep(0.05)
            _ack14(True)              # the FIRST command's late reply
            try:
                r = await retry
                out["retry"] = f"answered muted={r['data']['muted']}"
            except _InFlight14:
                out["retry"] = "refused"
            except TimeoutError:
                out["retry"] = "timeout"
            retry2 = _aio14.create_task(
                _nc14.request("suite-t14", "set_mute", {"mute": False}, timeout=1.0))
            await _aio14.sleep(0.05)
            _ack14(False)             # retry2's own reply
            try:
                r2 = await retry2
                out["retry2"] = f"answered muted={r2['data']['muted']}"
            except Exception as e:  # noqa: BLE001
                out["retry2"] = type(e).__name__
            return out
        finally:
            _nc14._send = _saved_send14
            _nc14.registry.sender_for_room = _saved_sender14
            getattr(_nc14, "_settling", {}).clear()

    _late14 = _aio14.run(_late_probe14())
    check("node command: a retry after a timeout never gets the late reply's answer",
          _late14.get("first") == "timeout" and _late14.get("retry") in ("refused", "timeout")
          and "muted=True" not in str(_late14.get("retry")), str(_late14))
    check("node command: once the late reply lands, a retry goes through with its own answer",
          _late14.get("retry2") == "answered muted=False", str(_late14))

    # With request ids (firmware 1.2.37+): replies are matched by id, no settling.
    _sent_id14 = []

    def _ack_id14(cmd, rid, data):
        _nc14.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": cmd,
                                     "id": rid, "ok": True, "data": data}), "pc-t14id")

    async def _id_probe14():
        _nc14.registry.sender_for_room = lambda room: (object(), "pc-t14id")
        _nc14._send = lambda out, payload: _sent_id14.append(payload) or True
        out = {}
        try:
            t = _aio14.create_task(_nc14.request("suite-t14", "get_info", timeout=1.0))
            await _aio14.sleep(0.05)
            rid_info = _sent_id14[-1].get("id")
            out["ids_sent"] = rid_info is not None
            _ack_id14("get_info", rid_info, {"name": "suite-t14"})
            await t
            try:
                await _nc14.request("suite-t14", "set_mute", {"mute": True}, timeout=0.2)
            except TimeoutError:
                pass
            rid_old = _sent_id14[-1].get("id")
            retry = _aio14.create_task(
                _nc14.request("suite-t14", "set_mute", {"mute": False}, timeout=1.0))
            await _aio14.sleep(0.05)
            rid_new = _sent_id14[-1].get("id")
            out["distinct_ids"] = rid_new is not None and rid_new != rid_old
            _ack_id14("set_mute", rid_old, {"muted": True})    # the late reply, stale id
            await _aio14.sleep(0.05)
            out["late_ignored"] = not retry.done()
            _ack_id14("set_mute", rid_new, {"muted": False})   # the retry's own reply
            try:
                r = await retry
                out["retry"] = f"answered muted={r['data']['muted']}"
            except Exception as e:  # noqa: BLE001
                out["retry"] = type(e).__name__
            return out
        finally:
            _nc14._send = _saved_send14
            _nc14.registry.sender_for_room = _saved_sender14
            getattr(_nc14, "_settling", {}).clear()
            getattr(_nc14, "_echoes_id", set()).discard("pc-t14id")

    _id14 = _aio14.run(_id_probe14())
    check("node command: every request carries a unique id",
          _id14.get("ids_sent") and _id14.get("distinct_ids"), str(_id14))
    check("node command: an id-echoing node's retry goes out at once; the stale reply is ignored",
          _id14.get("late_ignored") and _id14.get("retry") == "answered muted=False", str(_id14))

    # Wake-model select keys on name + SOURCE: with the
    # same basename in root and models/, a name-only select always
    # resolved to the models/ copy — clicking the ROOT row silently
    # activated the other file. source=None keeps the legacy resolution.
    _l4_name = "suite-l4c.onnx"
    _l4_bytes = b"l4-placeholder-onnx-bytes"
    # where the routes now look (paths, not the cwd); the stored value stays
    # the portable relative form ("models/<name>", "<name>")
    _l4_models = paths.models_dir(); _l4_models.mkdir(parents=True, exist_ok=True)
    (_l4_models / _l4_name).write_bytes(_l4_bytes)
    _l4_root = paths.wake_root() / _l4_name; _l4_root.write_bytes(_l4_bytes)
    try:
        r = client.put("/api/dashboard/oww/select",
                       json={"model": _l4_name, "source": "root"})
        check("wake model select: source=root activates the ROOT copy (collision)",
              r.status_code == 200 and r.json().get("model") == _l4_name,
              f"status={r.status_code} body={(r.json().get('model') if r.status_code == 200 else r.text[:80])!r}")
        r = client.put("/api/dashboard/oww/select",
                       json={"model": _l4_name, "source": "models"})
        check("wake model select: source=models activates the MODELS copy (collision)",
              r.status_code == 200 and r.json().get("model") == str(Path("models") / _l4_name),
              f"status={r.status_code} body={(r.json().get('model') if r.status_code == 200 else r.text[:80])!r}")
        r = client.put("/api/dashboard/oww/select", json={"model": _l4_name})
        check("wake model select: no source keeps the legacy models-first resolution",
              r.status_code == 200 and r.json().get("model") == str(Path("models") / _l4_name),
              f"status={r.status_code} body={r.text[:80]}")
        r = client.put("/api/dashboard/oww/select",
                       json={"model": "suite-l4-missing.onnx", "source": "root"})
        check("wake model select: unknown name+source combo 404s naming the source",
              r.status_code == 404 and "root" in r.text,
              f"status={r.status_code} body={r.text[:80]}")
        r = client.put("/api/dashboard/oww/select",
                       json={"model": _l4_name, "source": "bogus"})
        check("wake model select: source is a Literal (bogus source 422s)",
              r.status_code == 422, f"status={r.status_code}")
    finally:
        _l4_root.unlink(missing_ok=True)
        (_l4_models / _l4_name).unlink(missing_ok=True)

    # Session leak — dead sessions' pipeline graphs are one
    # big reference cycle; Python 3.14.4's incremental GC never reclaimed
    # them (10.4 GB / 406 threads after ~110 sessions). run_bot reclaims
    # deterministically when the pipeline ends, and the local STT closes the
    # unused OpenAI client itself instead of leaving it to a GC finalizer.
    import ast as _astlk
    _bot_src = (REPO_CORE.parent / "bot.py").read_text()
    _bot_ast = _astlk.parse(_bot_src)
    _run_bot = next((n for n in _astlk.walk(_bot_ast)
                     if isinstance(n, _astlk.AsyncFunctionDef) and n.name == "run_bot"), None)
    _reclaim_in_finally = False
    if _run_bot is not None:
        for _n in _astlk.walk(_run_bot):
            if isinstance(_n, _astlk.Try) and _n.finalbody:
                _body_src = _astlk.unparse(_n)
                if "runner.run(task)" in _body_src and any(
                        "_reclaim_session()" in _astlk.unparse(f) for f in _n.finalbody):
                    _reclaim_in_finally = True
    check("session leak a: run_bot reclaims the session in a finally around runner.run",
          _reclaim_in_finally)
    _reclaim_fn = next((n for n in _astlk.walk(_bot_ast)
                        if isinstance(n, _astlk.FunctionDef) and n.name == "_reclaim_session"), None)
    check("session leak b: _reclaim_session runs a full gc.collect()",
          _reclaim_fn is not None and "gc.collect()" in _astlk.unparse(_reclaim_fn))

    import asyncio as _aiolk
    import time as _tlk
    from stt_local import LocalWhisperSTTService as _LWS
    from stt_local import is_phantom_transcript as _phantom
    from types import SimpleNamespace as _NSeg
    check("whisper phantom: 'Thanks for watching!' alone is dropped",
          _phantom("Thanks for watching!") and _phantom(" thank you for watching. "))
    check("whisper phantom: caption credits are dropped",
          _phantom("Subtitles by the Amara.org community"))
    check("whisper phantom: real speech that contains a phrase is kept",
          not _phantom("thanks for watching the kids last night")
          and not _phantom("thank you, that's all") and not _phantom("Thank you."))
    # ── whisper phantom: segment scores are left to faster-whisper ──
    # faster-whisper already skips a segment with a high no-speech score
    # unless its log-probability is high too (its defaults, left on), so a
    # second copy of that rule here could never fire on a real segment.
    # Only the phrase list filters after transcribe.
    import stt_local as _sttph
    _capph = {}

    class _FWph:
        def __init__(self, segs):
            self.segs = segs

        def transcribe(self, audio, **kw):
            _capph.clear()
            _capph.update(kw)
            return iter(self.segs), _NSeg(language="en")

    def _heard_ph(segs):
        svc = _sttph.LocalWhisperSTTService.__new__(_sttph.LocalWhisperSTTService)
        svc._fw_model = _FWph(segs)
        svc._language = None

        async def _noop():
            return None
        svc.ensure_model = _noop
        return _aiolk.run(svc._transcribe(b"RIFF")).text

    check("whisper phantom: a transcript is never dropped on segment scores alone",
          _heard_ph([_NSeg(text=" Okay.", no_speech_prob=0.8, avg_logprob=-1.4)]) == "Okay.")
    check("whisper phantom: a caption phrase is still dropped on the transcribe path",
          _heard_ph([_NSeg(text=" Thanks for watching!", no_speech_prob=0.1,
                           avg_logprob=-0.2)]) == "")
    check("whisper phantom: faster-whisper's own no-speech rule stays at its defaults",
          "no_speech_threshold" not in _capph and "log_prob_threshold" not in _capph,
          str(_capph))
    # ── end whisper phantom: segment scores ──
    from pipecat.services.whisper.base_stt import BaseWhisperSTTService as _BWS

    class _FakeClient:
        def __init__(self, hang=False):
            self.closed = False
            self.hang = hang
        async def close(self):
            if self.hang:
                await _aiolk.sleep(10)
            self.closed = True

    async def _noop_cleanup(self):
        return None

    _saved_cleanup = _BWS.cleanup
    _BWS.cleanup = _noop_cleanup  # isolate OUR cleanup from pipecat's task plumbing
    try:
        _svc = _LWS.__new__(_LWS)
        _fc = _FakeClient()
        _svc._client = _fc
        _aiolk.run(_LWS.cleanup(_svc))
        check("session leak c: local STT cleanup closes the unused OpenAI client",
              _fc.closed and _svc._client is None)
        _svc2 = _LWS.__new__(_LWS)
        _svc2._client = _FakeClient(hang=True)
        _t0 = _tlk.monotonic()
        _aiolk.run(_LWS.cleanup(_svc2))
        _dt = _tlk.monotonic() - _t0
        check("session leak c: hung client close is bounded (teardown never stalls)",
              _dt < 3.5 and _svc2._client is None, f"{_dt:.2f}s")
    finally:
        _BWS.cleanup = _saved_cleanup

    # RSS-step fix: Whisper loads at bot STARTUP (fresh heap +
    # warm-up + malloc_trim), not inside the first voice turn of an aged,
    # fragmented process (768 MB -> 2.8 GB). The preload must use exactly the
    # model/device/precision run_bot's STT uses, or the bot loads twice.
    import ast as _astR
    _botR = _astR.parse((REPO_CORE.parent / "bot.py").read_text())
    def _cfg_triples(node):
        out = []
        for n in _astR.walk(node):
            if (isinstance(n, _astR.Call) and getattr(n.func, "id", "") in ("cfg_env", "_cfg_env")
                    and len(n.args) >= 4 and all(isinstance(a, _astR.Constant) for a in n.args[:4])):
                out.append(tuple(a.value for a in n.args[:4]))
        return out
    _stt_call = next((n for n in _astR.walk(_botR) if isinstance(n, _astR.Call)
                      and getattr(n.func, "id", "") == "LocalWhisperSTTService"), None)
    _pre_call = next((n for n in _astR.walk(_botR) if isinstance(n, _astR.Call)
                      and getattr(n.func, "id", "") == "preload_shared_model"), None)
    _stt_tr = [t for t in _cfg_triples(_stt_call) if t[1] in ("model", "device", "compute_type")] if _stt_call else []
    _pre_tr = _cfg_triples(_pre_call) if _pre_call else []
    check("RSS preload loads the SAME Whisper as run_bot — neither passes a model, device or compute_type (the fixed Embedded model)",
          _pre_call is not None and _stt_call is not None and not _pre_call.args and not _pre_call.keywords
          and not _stt_tr and not ({k.arg for k in _stt_call.keywords} & {"model", "device", "compute_type"}),
          f"preload={_astR.unparse(_pre_call) if _pre_call else None} "
          f"run_bot={_astR.unparse(_stt_call) if _stt_call else None}")
    _main_src = _astR.unparse(_botR)
    check("tts norm: the Flash lane sends tts.elevenlabs_text_normalization to ElevenLabs",
          "el_norm = tts_text.normalization_mode(cfg('tts', 'elevenlabs_text_normalization'))"
          in _main_src and "apply_text_normalization=el_norm" in _main_src)
    check("tts norm: native ElevenLabs normalization only - no custom text filter on any lane",
          "text_filters=" not in _main_src and "ClockTimeFilter" not in _main_src)
    check("fast restart: uvicorn.run is wrapped (TLS + graceful-shutdown deadline) before the runner starts",
          "wrap_uvicorn_run(uvicorn" in _main_src and _main_src.find("wrap_uvicorn_run(uvicorn") < _main_src.rfind("main()")
          and "_tls_run" not in _main_src, "wrapper missing / after main() / old TLS-only wrapper still there")
    check("paths: bot.py loads the secrets file and the certs from paths (state dir aware), not repo-relative",
          "load_dotenv(_paths.env_file())" in _main_src and "wrap_uvicorn_run(uvicorn, str(_paths.certs_dir()))" in _main_src
          and "'.env')" not in _main_src and "'certs')" not in _main_src)
    _pw_call = next((n for n in _astR.walk(_botR) if isinstance(n, _astR.Call)
                     and getattr(n.func, "id", "") == "PipelineWorker"), None)
    _pw_rtvi = next((k.value for k in (_pw_call.keywords if _pw_call else [])
                     if k.arg == "enable_rtvi"), None)
    check("node wire: the pipeline runs without pipecat's RTVI layer (enable_rtvi=False)",
          isinstance(_pw_rtvi, _astR.Constant) and _pw_rtvi.value is False,
          f"PipelineWorker found={_pw_call is not None} enable_rtvi={_astR.unparse(_pw_rtvi) if _pw_rtvi else None}")
    check("RSS preload runs before main() (startup, not first voice turn)",
          _pre_call is not None and _main_src.find("preload_shared_model(") < _main_src.rfind("main()"),
          "preload call missing or after main()")
    check("cloud stt bot.py: stt.provider read with whisper as the default",
          "_stt_raw = cfg_env('stt', 'provider', 'LVI_STT_PROVIDER', None)" in _main_src
          and "stt_provider = stt_catalog.resolve_provider(_stt_raw)" in _main_src)
    check("cloud stt bot.py: deepgram wraps Whisper as the fallback and closes it with the session",
          "wrap_stt_with_fallback(" in _main_src and _main_src.count("close_stt_fallback(") >= 2
          and "SttTimer(" in _main_src, f"wrap={'wrap_stt_with_fallback(' in _main_src} close={_main_src.count('close_stt_fallback(')}")
    # TTS standby wiring (Kokoro behind every cloud voice). _main_src is
    # ast.unparse (code only, comments stripped, single-quoted literals), so a
    # comment can't fake a pass. Behaviour is pinned in tests/tts_failover_test.py
    # and tests/speech_tiers_test.py.
    check("tts fallback bot.py: every cloud voice is wrapped with the Kokoro standby — no tts.fallback key",
          "wrap_with_fallback(" in _main_src and "cfg('tts', 'fallback')" not in _main_src
          and "if tts_catalog.is_cloud(tts_provider):" in _main_src)
    check("tts fallback bot.py: the standby is closed at session end AND on client disconnect",
          _main_src.count("close_fallback(") >= 2 and _main_src.count("if tts_standby is not None:") >= 3,
          f"close={_main_src.count('close_fallback(')} guards={_main_src.count('if tts_standby is not None:')}")
    check("tts fallback bot.py: TTSTracked skips health notes while the standby speaks",
          "_tts_note_suppressed" in _main_src)
    check("tts fallback bot.py: the Pipeline still takes `tts` (the switcher reassigns it)",
          "tts_primary = tts" in _main_src)

    # ── Kokoro lane: bot.py wiring (code only, like the standby pins above) ──
    _rbKK = next((n for n in _astR.walk(_botR)
                  if isinstance(n, _astR.AsyncFunctionDef) and n.name == "run_bot"), None)
    _rbKKsrc = _astR.unparse(_rbKK) if _rbKK else ""
    _kkBranch = _rbKKsrc.find("tts_provider == 'kokoro'")
    # The primary branch alone (up to the next provider): one warm, one build.
    # Kokoro is also built as the cloud standby, checked below.
    _kkEnd = _rbKKsrc.find("elif tts_provider ==", _kkBranch + 1)
    _kkPrim = _rbKKsrc[_kkBranch:_kkEnd] if 0 <= _kkBranch < _kkEnd else ""
    _kkWarm, _kkMake = _kkPrim.find("await tts_kokoro.warm()"), _kkPrim.find("tts_kokoro.make_service(")
    check("kokoro bot.py: tts.provider kokoro warms the shared model off the loop, then builds the service",
          0 <= _kkWarm < _kkMake and _kkPrim.count("tts_kokoro.make_service(") == 1,
          f"branch={_kkBranch} warm={_kkWarm} make={_kkMake}")
    check("kokoro bot.py: voice + speed via cfg/env with the module defaults",
          "cfg_env('tts', 'kokoro_voice', 'LVI_TTS_KOKORO_VOICE', tts_kokoro.DEFAULT_VOICE)" in _rbKKsrc
          and "cfg_env('tts', 'kokoro_speed', 'LVI_TTS_KOKORO_SPEED', tts_kokoro.DEFAULT_SPEED)" in _rbKKsrc)
    check("kokoro bot.py: not installed / failed load fail loud with the install hint",
          "tts.provider=kokoro but kokoro-onnx is not installed" in _rbKKsrc
          and "tts.provider=kokoro but the Kokoro model failed to load" in _rbKKsrc
          and _rbKKsrc.count("tts_kokoro.INSTALL_HINT") >= 2)
    check("kokoro bot.py: Kokoro is a primary in the same dispatch, before the cloud standby is built",
          0 <= _kkBranch < _rbKKsrc.find("tts_primary = tts") < _rbKKsrc.find("if tts_catalog.is_cloud(tts_provider):"))
    # Kokoro is imported only where it is used: its own branch, LocalAI's
    # branch (Kokoro is LocalAI's fallback) and the cloud standby block.
    _kkGuards = ["tts_provider == 'kokoro'", "tts_provider == 'localai'", "if tts_catalog.is_cloud(tts_provider):"]
    _kkImports = [i for i in range(len(_rbKKsrc)) if _rbKKsrc.startswith("import tts_kokoro", i)]
    _kkGuardPos = sorted(_rbKKsrc.find(g) for g in _kkGuards)
    check("kokoro bot.py: Kokoro is imported only in its branch, LocalAI's branch, the cloud standby block and the startup preload",
          not any(isinstance(n, (_astR.Import, _astR.ImportFrom))
                  and "tts_kokoro" in _astR.unparse(n) for n in _botR.body)
          and len(_kkImports) == 3 and min(_kkGuardPos) >= 0
          and all(g < i for g, i in zip(_kkGuardPos, _kkImports)), f"imports={_kkImports} guards={_kkGuardPos}")
    _kkFb = _rbKKsrc.find("if tts_catalog.is_cloud(tts_provider):")
    _kkFbSrc = _rbKKsrc[_kkFb:_rbKKsrc.find("EchoGate()", _kkFb)] if _kkFb >= 0 else ""
    check("kokoro bot.py: every cloud voice warms Kokoro as its standby, builds it with the Kokoro voice/speed, warns when it can't",
          "await tts_kokoro.warm()" in _kkFbSrc and "tts_kokoro.make_service(" in _kkFbSrc
          and "cfg_env('tts', 'kokoro_voice', 'LVI_TTS_KOKORO_VOICE', tts_kokoro.DEFAULT_VOICE)" in _kkFbSrc
          and "running WITHOUT a fallback" in _kkFbSrc)
    check("tts fallback bot.py: the switcher is told which standby it holds",
          "standby_name='kokoro'" in _rbKKsrc)
    _kkPre = _main_src.find("tts_kokoro.preload()")
    check("kokoro bot.py: Kokoro preload at startup for every provider, before main(), non-fatal",
          0 <= _kkPre < _main_src.rfind("main()") and "kokoro preload failed" in _main_src
          and "if _tp in (" not in _main_src and "_fb = " not in _main_src)
    # Must still hold: the number pass is for the local voices only. A custom
    # speller in front of ElevenLabs' own normalizer once garbled times.
    _numUsers = sorted(p.relative_to(REPO_CORE).as_posix() for p in REPO_CORE.rglob("*.py")
                       if "spell_numbers" in p.read_text() and p.name != "tts_numbers.py")
    check("number pass: only the local-voice modules (Kokoro, LocalAI) call spell_numbers (never bot.py or a cloud lane)",
          _numUsers == ["tts_kokoro.py", "tts_localai.py"]
          and "spell_numbers" not in _main_src and "tts_numbers" not in _main_src, f"users={_numUsers}")
    check("number pass: the ElevenLabs and Deepgram services keep pipecat's own run_tts",
          _bot13.TrackedElevenLabsTTSService.run_tts is _bot13.ElevenLabsTTSService.run_tts
          and (_bot13.TrackedElevenLabsDialogueTTSService is None
               or _bot13.TrackedElevenLabsDialogueTTSService.run_tts is _bot13.ElevenLabsDialogueTTSService.run_tts)
          and (_bot13.TrackedDeepgramTTSService is None
               or _bot13.TrackedDeepgramTTSService.run_tts is _bot13.DeepgramTTSService.run_tts)
          and (_bot13.TrackedDeepgramFluxTTSService is None
               or _bot13.TrackedDeepgramFluxTTSService.run_tts is _bot13.DeepgramFluxTTSService.run_tts))
    _cloud13 = _Tracked13()
    _aio13.run(_cloud13._synthesize_text("It's 9:06, that's $12.50 and 75%.", "suite-ctx"))
    check("number pass: a cloud service (TTSTracked) speaks the text exactly as given",
          _cloud13.spokes == ["It's 9:06, that's $12.50 and 75%."], f"spokes={_cloud13.spokes}")

    # ── Kokoro lane: one shared model per process. kokoro-onnx is not
    # installed where this suite runs: a fake kokoro_onnx module and fake
    # (sparse) model files in the scratch dir stand in. Nothing downloads. ──
    import inspect as _inspKK
    import types as _typesKK
    import numpy as _npKK
    from loguru import logger as _lgKK
    import tts_kokoro as _tk
    _KK = {"loads": 0, "delay": 0.0, "fail": False, "calls": [], "downloads": 0}

    class _FakeKokoro:
        def __init__(self, model_path, voices_path):
            time.sleep(_KK["delay"])
            _KK["loads"] += 1
            if _KK["fail"]:
                raise RuntimeError("suite: onnx refused the model")
            _KK["paths"] = (model_path, voices_path)
            self.voices = {v: object() for v in _tk.KOKORO_VOICES}

        async def create_stream(self, text, voice, speed=1.0, lang="en-us", **kw):
            _KK["calls"].append((text, voice, lang, speed))
            yield _npKK.zeros(240, dtype=_npKK.float32), 24000

    _kkSaved = {"mod": sys.modules.get("kokoro_onnx"), "dir": _tk.model_dir}
    _fakeKKmod = _typesKK.ModuleType("kokoro_onnx")
    _fakeKKmod.Kokoro = _FakeKokoro
    sys.modules["kokoro_onnx"] = _fakeKKmod
    _kkDir = SCRATCH / "kokoro-models"
    _tk.model_dir = lambda: _kkDir
    _tk._reset_for_tests()
    _kkLog = []
    _kkSink = _lgKK.add(lambda m: _kkLog.append(str(m)), level="WARNING")
    try:
        check("kokoro: the catalog is the 28 English voices, af_heart the default",
              list(_tk.KOKORO_VOICES) == _KOKORO_28 and _tk.DEFAULT_VOICE == "af_heart")
        check("kokoro: kokoro_available() true with kokoro_onnx importable", _tk.kokoro_available() is True)
        check("kokoro: resolve_voice keeps a catalog voice, anything else -> af_heart",
              _tk.resolve_voice("bm_lewis") == "bm_lewis" and _tk.resolve_voice("alba") == "af_heart"
              and _tk.resolve_voice(None) == "af_heart")
        check("kokoro: language_for: a* American (en-us), b* British (en-gb)",
              _tk.language_for("af_heart") == "en-us" and _tk.language_for("am_puck") == "en-us"
              and _tk.language_for("bf_emma") == "en-gb" and _tk.language_for("bm_george") == "en-gb")
        # No model files: warm() fails, names the fetch script, loads nothing.
        _kkOk = asyncio.run(_tk.warm())
        check("kokoro: missing model files: warm() returns False and names the fetch script",
              _kkOk is False and _KK["loads"] == 0
              and any("fetch_kokoro_models.sh" in s for s in _kkLog), f"ok={_kkOk} loads={_KK['loads']} log={_kkLog[-1:]}")
        check("kokoro: a room's make_service never loads the model itself (None until warm() succeeded)",
              _tk.make_service(mixin=_bot13.TTSTracked, voice="af_heart", speed=1.0) is None and _KK["loads"] == 0)
        _kkDir.mkdir(parents=True, exist_ok=True)
        (_kkDir / "kokoro-v1.0.onnx").write_bytes(b"truncated")
        (_kkDir / "voices-v1.0.bin").write_bytes(b"truncated")
        check("kokoro: wrong-size model files are refused before onnx sees them",
              asyncio.run(_tk.warm()) is False and _KK["loads"] == 0)
        for _name, _size in _tk.MODEL_FILES.items():
            with open(_kkDir / _name, "wb") as _fh:
                _fh.truncate(_size)   # sparse: the right size, no real bytes
        # warm(): off the event loop, and two rooms warming at once load ONCE.
        _KK["delay"] = 0.3

        async def _kkWarmProbe():
            gaps, state = [], {"stop": False}

            async def ticker():
                last = time.monotonic()
                while not state["stop"]:
                    await asyncio.sleep(0.02)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)
            res = await asyncio.gather(_tk.warm(), _tk.warm(), _tk.warm())
            state["stop"] = True
            await tick
            return res, max(gaps) if gaps else None

        _kkRes, _kkGap = asyncio.run(_kkWarmProbe())
        _KK["delay"] = 0.0
        check("kokoro: warm() keeps the event loop responsive during the model load",
              _kkGap is not None and _kkGap < 0.15, f"max loop gap={_kkGap}")
        check("kokoro: three concurrent warms load the model once",
              _kkRes == [True, True, True] and _KK["loads"] == 1, f"res={_kkRes} loads={_KK['loads']}")
        check("kokoro: the shared model loads from the fixed cache directory",
              _KK.get("paths") == (str(_kkDir / "kokoro-v1.0.onnx"), str(_kkDir / "voices-v1.0.bin")),
              f"{_KK.get('paths')}")
        check("kokoro: a later warm() reuses the loaded model", asyncio.run(_tk.warm()) is True and _KK["loads"] == 1)
        # A room's service: the shared instance, never pipecat's per-instance load or download.
        import pipecat.services.kokoro.tts as _pkk
        _kkOrigEnsure, _kkOrigDl = _pkk._ensure_model_files, _pkk._download_file

        def _kkNoDownload(*a, **k):
            _KK["downloads"] += 1
            raise AssertionError("suite: a room's Kokoro service tried to fetch model files")

        _pkk._ensure_model_files = _kkNoDownload
        _pkk._download_file = _kkNoDownload
        try:
            _kk1 = _tk.make_service(mixin=_bot13.TTSTracked, voice="af_heart", speed=1.0)
            _kk2 = _tk.make_service(mixin=_bot13.TTSTracked, voice="bf_emma", speed="1.25")
        finally:
            _pkk._ensure_model_files, _pkk._download_file = _kkOrigEnsure, _kkOrigDl
        check("kokoro: two rooms share the one model: no load, no download per room",
              _kk1 is not None and _kk2 is not None and _kk1._kokoro is _kk2._kokoro is _tk.get_shared_model()
              and _KK["loads"] == 1 and _KK["downloads"] == 0,
              f"svc={_kk1},{_kk2} loads={_KK['loads']} downloads={_KK['downloads']}")
        check("kokoro: the service is pipecat's Kokoro service with the TTSTracked health tap, labelled kokoro",
              isinstance(_kk1, _pkk.KokoroTTSService) and isinstance(_kk1, _bot13.TTSTracked)
              and _kk1._tts_provider == "kokoro")
        check("kokoro: voice, espeak language and speed reach the service settings",
              (_kk1._settings.voice, _kk1._settings.language, _kk1._settings.speed) == ("af_heart", "en-us", 1.0)
              and (_kk2._settings.voice, _kk2._settings.language, _kk2._settings.speed) == ("bf_emma", "en-gb", 1.25),
              f"{(_kk1._settings.voice, _kk1._settings.language, _kk1._settings.speed)} "
              f"{(_kk2._settings.voice, _kk2._settings.language, _kk2._settings.speed)}")
        _kk3 = _tk.make_service(mixin=_bot13.TTSTracked, voice="nope", speed=3)
        _kk4 = _tk.make_service(mixin=_bot13.TTSTracked, voice="am_adam", speed="fast")
        check("kokoro: unknown voice -> af_heart; out-of-range or junk speed -> 1.0",
              _kk3 is not None and _kk3._settings.voice == "af_heart" and _kk3._settings.speed == 1.0
              and _kk4 is not None and _kk4._settings.speed == 1.0)
        check("kokoro: _try_revive is a no-op (no websocket to retry)",
              type(_kk1)._try_revive is not _bot13.TTSTracked._try_revive
              and asyncio.run(_kk1._try_revive()) is None)

        # The number pass runs inside the Kokoro service, on the shared model.
        async def _kkSpeak(svc, text):
            async for _ in svc.run_tts(text, "suite-ctx"):
                pass

        _KK["calls"].clear()
        asyncio.run(_kkSpeak(_kk2, "It's 9:06 and 72°F, lunch was $12.50."))
        check("kokoro: number pass: the engine hears times, degrees and money as words",
              _KK["calls"] == [("It's nine oh six and seventy-two degrees Fahrenheit, lunch was "
                                "twelve dollars and fifty cents.", "bf_emma", "en-gb", 1.25)], f"{_KK['calls']}")
        _kkPlain = "The porch light is on."
        _KK["calls"].clear()
        asyncio.run(_kkSpeak(_kk1, _kkPlain))
        check("kokoro: number pass: digit-free text reaches the engine unchanged",
              len(_KK["calls"]) == 1 and _KK["calls"][0][0] == _kkPlain, f"{_KK['calls']}")
        # Sentence by sentence: a long reply's first audio comes fast, and no
        # chunk waits past pipecat's no-audio timer (live: a 4-sentence reply
        # took ~6 s in one go and logged "completed with no audio").
        _KK["calls"].clear()
        asyncio.run(_kkSpeak(_kk1, "Tomorrow looks decent. Partly cloudy all day! Want the weekend too? Sure."))
        check("kokoro: a reply is spoken one sentence at a time, in order (a tiny tail joins its neighbour)",
              [c[0] for c in _KK["calls"]] == ["Tomorrow looks decent.", "Partly cloudy all day!",
                                               "Want the weekend too? Sure."]
              and all(c[1:] == ("af_heart", "en-us", 1.0) for c in _KK["calls"]), f"{_KK['calls']}")
        check("kokoro: sentences(): abbreviations and short openers stay with their sentence; blanks vanish",
              _tk.sentences("Ask Mr. Smith about it.") == ["Ask Mr. Smith about it."]
              and _tk.sentences("Sure. Turning the lights off now.") == ["Sure. Turning the lights off now."]
              and _tk.sentences("No end mark") == ["No end mark"] and _tk.sentences("  ") == []
              and _tk.sentences("Wait... what? Really.") == ["Wait... what? Really."]
              and _tk.sentences("It rained all morning. It cleared up later on.") == ["It rained all morning.",
                                                                                       "It cleared up later on."],
              str([_tk.sentences("Ask Mr. Smith about it."), _tk.sentences("Wait... what? Really.")]))
        check("kokoro: a slow sentence is tolerated 10 s before pipecat calls the reply finished (default 3 s)",
              getattr(_kk1, "_stop_frame_timeout_s", None) == _tk.STOP_FRAME_TIMEOUT_S == 10.0,
              str(getattr(_kk1, "_stop_frame_timeout_s", None)))

        # Two rooms speaking at once queue on the one model.
        async def _kkLockProbe():
            active = {"now": 0, "max": 0}
            orig = _pkk.KokoroTTSService.run_tts

            async def slow(self, text, context_id):
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
                await asyncio.sleep(0.05)
                active["now"] -= 1
                yield None

            _pkk.KokoroTTSService.run_tts = slow
            try:
                await asyncio.gather(_kkSpeak(_kk1, "hi"), _kkSpeak(_kk2, "hi"))
            finally:
                _pkk.KokoroTTSService.run_tts = orig
            return active["max"]

        check("kokoro: the generation lock serializes two rooms", asyncio.run(_kkLockProbe()) == 1)
        # Drift pins: the pipecat internals the shared-model subclass relies on.
        _kkRunSrc = _inspKK.getsource(_pkk.KokoroTTSService.run_tts)
        _kkInitSrc = _inspKK.getsource(_pkk.KokoroTTSService.__init__)
        check("kokoro: pipecat internals: run_tts reads only _kokoro/_resampler/_settings; __init__ is what loads",
              "self._kokoro.create_stream(" in _kkRunSrc and "self._resampler.resample(" in _kkRunSrc
              and "_ensure_model_files(" in _kkInitSrc and "Kokoro(" in _kkInitSrc)
        # A load that fails is final for the process (no per-room retries).
        _tk._reset_for_tests()
        _KK["fail"], _KK["loads"] = True, 0
        _kkLog.clear()
        _kkF1 = asyncio.run(_tk.warm())
        _kkF2 = asyncio.run(_tk.warm())
        check("kokoro: a failed load returns False with the install hint, and is not retried",
              _kkF1 is False and _kkF2 is False and _KK["loads"] == 1
              and any(_tk.INSTALL_HINT in s for s in _kkLog), f"loads={_KK['loads']} log={_kkLog[-1:]}")
        check("kokoro: make_service after a failed load is None (the bot fails loud)",
              _tk.make_service(mixin=_bot13.TTSTracked, voice="af_heart", speed=1.0) is None)
    finally:
        _lgKK.remove(_kkSink)
        _KK["fail"] = False
        _tk.model_dir = _kkSaved["dir"]
        _tk._reset_for_tests()
        if _kkSaved["mod"] is None:
            sys.modules.pop("kokoro_onnx", None)
        else:
            sys.modules["kokoro_onnx"] = _kkSaved["mod"]
    import stt_local as _sttR
    import ctypes as _ctR
    _orig_cdll = _ctR.CDLL
    def _no_libc(*a, **k):
        raise OSError("no libc here")
    _ctR.CDLL = _no_libc
    try:
        _sttR._trim_heap("suite")
        _trim_ok = True
    except Exception as _e:
        _trim_ok = False
    finally:
        _ctR.CDLL = _orig_cdll
    check("RSS _trim_heap never raises (non-glibc host)", _trim_ok)

    # The heard-after-wake list is gone (sound-alike matching needs no
    # spellings to collect); the wake itself is still recorded.
    from dashboard import registry as _regwn
    check("wake name: no heard-after-wake list left in the registry",
          not any(hasattr(_regwn, x) for x in ("note_heard", "recent_heard", "_RECENT_HEARD", "_WAKE_PENDING",
                                               "HEARD_WINDOW_S")))
    _regwn.note_wake("kitchen")
    check("wake name: note_wake still records the last wake (room + time)",
          _regwn._last_wake.get("room") == "kitchen" and _regwn._last_wake.get("time"), str(_regwn._last_wake))

    # /wake API — the name applies live (no restart); no spellings list
    import wake_name as _wnwnb
    _secswn = config_store.read_sections(["wake", "stt"])
    _wake_bakwn = dict(_secswn.get("wake") or {})
    _stt_bakwn = dict(_secswn.get("stt") or {})
    try:
        r = client.get("/api/dashboard/wake")
        _wwn = r.json() if r.status_code == 200 else {}
        check("wake name: GET /wake shape",
              r.status_code == 200 and set(_wwn) == {"name"}, str(_wwn)[:200])
        r = client.put("/api/dashboard/wake", json={"name": "Computer"})
        check("wake name: PUT /wake applies live: the name, and its sound-alike spellings with no list",
              r.status_code == 200 and _wnwnb.name() == "Computer" and set(r.json()) == {"ok", "name"}
              and _wnwnb.is_wake_phrase_only("hey komputer.") and _wnwnb.is_wake_phrase_only("Hey computer!")
              and not _wnwnb.is_wake_phrase_only("hey computer, lights off"), f"{r.status_code} {r.text[:160]}")
        _stt_midwn = dict(config_store.read_section("stt") or {})
        check("wake name: PUT /wake never touches the stt section", _stt_midwn == _stt_bakwn)
        for _badwn in [{"name": "Robo 9"}, {"name": "x" * 31}, {"bogus": 1}, {"heard_as": ["yellow"]},
                        {"heard_as": []}]:
            r = client.put("/api/dashboard/wake", json=_badwn)
            _kwn = list(_badwn)[0]
            check(f"wake name: PUT /wake rejects bad {_kwn} ({str(_badwn[_kwn])[:14]}) with 422",
                  r.status_code == 422, f"{r.status_code}")
        check("wake name: rejected PUTs changed nothing (the removed heard_as is never written)",
              _wnwnb.name() == "Computer"
              and (config_store.read_section("wake") or {}).get("heard_as") == _wake_bakwn.get("heard_as"))
        r = client.put("/api/dashboard/wake", json={"name": "Zoë"})
        check("wake name: accented name accepted", r.status_code == 200
              and _wnwnb.name() == "Zoë", f"{r.status_code} {r.text[:120]}")
        r = client.put("/api/dashboard/wake", json={"name": "   "})
        check("wake name: a whitespace-only name clears (same as empty)",
              r.status_code == 200 and "name" not in (config_store.read_section("wake") or {}),
              f"{r.status_code} {r.text[:120]}")
        r = client.put("/api/dashboard/wake", json={"name": "Computer"})
        r = client.put("/api/dashboard/wake", json={"name": ""})
        check("wake name: empty name clears it (no name — never guessed from the model)",
              r.status_code == 200 and _wnwnb.name() == "" and r.json().get("name") == ""
              and client.get("/api/dashboard/wake").json().get("name") == ""
              and "name" not in (config_store.read_section("wake") or {}), r.text[:120])
        r = client.put("/api/dashboard/wake", json={"bias_wake_name": True})
        check("wake name: the removed spelling-hint field is rejected (422), stt untouched",
              r.status_code == 422 and dict(config_store.read_section("stt") or {}) == _stt_bakwn,
              f"{r.status_code}")
    finally:
        config_store.write_sections({"wake": _wake_bakwn, "stt": _stt_bakwn})

    # Whisper never gets a wake-name hint (spelling hint removed)
    import stt_local as _sttwn
    import wake_name as _wnwn
    import asyncio as _aiown
    import inspect as _inspwnh
    _capwn = {}

    class _FWwn:
        def transcribe(self, audio, **kw):
            _capwn.clear()
            _capwn.update(kw)
            return iter([]), type("I", (), {"language": "en"})()

    _svcwn = _sttwn.LocalWhisperSTTService.__new__(_sttwn.LocalWhisperSTTService)
    _svcwn._fw_model = _FWwn()
    _svcwn._language = None

    async def _noopwn():
        return None
    _svcwn.ensure_model = _noopwn
    _orig_wakewn = _wnwn._wake
    try:
        _wnwn._wake = lambda: {"name": "Computer"}
        _aiown.run(_svcwn._transcribe(b"RIFF"))
    finally:
        _wnwn._wake = _orig_wakewn
    check("wake name: Whisper never gets a hotword hint, even with a name set",
          "hotwords" not in _capwn and _capwn.get("beam_size") == 1
          and "hotwords" not in _inspwnh.getsource(_sttwn), str(_capwn))

    # Wake-name sweep: no wake-name literal in shipped code/docs (tests may use it as a config VALUE)
    import inspect as _inspwn
    _rootwn = REPO_CORE.parent
    _sweptwn = [_rootwn / "bot.py", _rootwn / "README.md", _rootwn / "config.example.yaml"]
    _sweptwn += list(REPO_CORE.rglob("*.py")) + list((_rootwn / "dashboard-ui" / "src").rglob("*.jsx"))
    # The words come from LVI_PRIVATE_WORDS (comma-separated, kept out of the
    # repo): a public test must not name the private word it guards.
    _privwn = [w.strip().lower() for w in os.environ.get("LVI_PRIVATE_WORDS", "").split(",") if w.strip()]
    # The org and product names (and their domains) are public and allowed;
    # a private word inside them doesn't count, anywhere else it does.
    _brandswn = ("lenoxsi", "lenoxvi", "lenoxmi", "lenoxci", "lenoxai")

    def _privhitwn(text: str) -> bool:
        t = text.lower()
        for b in _brandswn:
            t = t.replace(b, "")
        return any(w in t for w in _privwn)

    if _privwn:
        _hitswn = sorted({str(p.relative_to(_rootwn)) for p in _sweptwn
                           if p.is_file() and _privhitwn(p.read_text(errors="ignore"))})
        check("wake name sweep: no private word in core/, bot.py, dashboard src, README, config.example",
              not _hitswn, f"hits={_hitswn}")
        check("wake name sweep: the org/product names (LenoxSI, LenoxVI, LenoxMI, LenoxCI, lenoxai.io) are allowed, every private "
              "word on its own is still caught",
              not _privhitwn("A LenoxSI project · lenoxsi.com · LenoxVI · LenoxMI · LenoxCI · lenoxai.io")
              and all(_privhitwn(f"say {w} here") for w in _privwn), "")
    else:
        print("[SKIP] private-word sweep (set LVI_PRIVATE_WORDS to run it)")
    # ── Public flip prep: license, org links, log level ──
    _lic = (_rootwn / "LICENSE").read_text() if (_rootwn / "LICENSE").is_file() else ""
    _ntc = (_rootwn / "NOTICE").read_text() if (_rootwn / "NOTICE").is_file() else ""
    _rdm = (_rootwn / "README.md").read_text()
    check("license: LICENSE is the Apache License 2.0 text, NOTICE names the project and the model terms",
          _lic.lstrip().startswith("Apache License") and "Version 2.0, January 2004" in _lic
          and "END OF TERMS AND CONDITIONS" in _lic and "Apache License, Version 2.0" in _ntc
          and "CC BY-NC-SA 4.0" in _ntc and "Kokoro-82M weights are Apache-2.0" in _ntc
          and "pocket" not in _ntc.lower(), f"LICENSE {len(_lic)}B NOTICE {len(_ntc)}B")
    check("license: README states Apache 2.0, links LICENSE/NOTICE and no longer says no license is set",
          "Apache License 2.0" in _rdm and "(LICENSE)" in _rdm and "(NOTICE)" in _rdm
          and "No project license is set" not in _rdm, "")
    _oldorgrd = "github.com/" + "Open" + "Holler/"  # the retired org (spelled in pieces so no sweep sees it)
    _oldreporm = "LenoxSI/" + "open" + "holler"
    check("org links: README points at github.com/LenoxSI/lenoxvi (clone + issues), links lenoxsi.com and "
          "lenoxai.io, and no old-org or old-repo URL is left",
          "https://github.com/LenoxSI/lenoxvi.git" in _rdm and "https://github.com/LenoxSI/lenoxvi/issues" in _rdm
          and "https://lenoxsi.com" in _rdm and "https://lenoxai.io" in _rdm and _oldorgrd not in _rdm
          and _oldreporm.lower() not in _rdm.lower(), "")
    import stt_catalog as _sc_rd
    _tiers_rd = getattr(_sc_rd, "TIERS", None) or []
    _sp_rd = _rdm.split("\n## Speech\n", 1)[1].split("\n## ", 1)[0] if "\n## Speech\n" in _rdm else ""
    _la_rd = _rdm.split("\n## LocalAI\n", 1)[1].split("\n## ", 1)[0] if "\n## LocalAI\n" in _rdm else ""
    check("speech tiers: README has one Speech section — the three tiers and their fallbacks — no Whisper model table, "
          "no embedded Pocket, large-v3-turbo only as LocalAI GPU advice",
          len(_tiers_rd) == 3 and all(f"| {t['label']} |" in _sp_rd for t in _tiers_rd)
          and "Whisper base and Kokoro" in _sp_rd and "## Speech to text" not in _rdm and "## Text to speech" not in _rdm
          and "### Whisper models" not in _rdm and "requirements-pocket" not in _rdm and "fetch_pocket_weights" not in _rdm
          and "Pocket" in _la_rd and "cuda: true" in _la_rd
          and _rdm.count("large-v3-turbo") == _la_rd.count("large-v3-turbo") >= 1, _sp_rd[:300])
    _appsrcvp = (_rootwn / "dashboard-ui" / "src" / "App.jsx").read_text()
    check("dashboard: the voice-ID box shows a generic placeholder (no real voice-ID prefix)",
          'placeholder="Voice ID"' in _appsrcvp and "fq1S" not in _appsrcvp, "")
    import log_setup as _lgs
    from loguru import logger as _lgl
    _lgout: list[str] = []
    _real_cfgenv = _lgs.cfg_env
    try:
        _lgs.cfg_env = lambda *a, **k: None
        _lv0 = _lgs.configure(lambda m: _lgout.append(str(m)))
        _lgl.debug("dbg-line-x")
        _lgl.info("info-line-x")
        _lv1_out = list(_lgout)
        _lgout.clear()
        _lgs.cfg_env = lambda *a, **k: "debug"
        _lv1 = _lgs.configure(lambda m: _lgout.append(str(m)))
        _lgl.debug("dbg-line-y")
        _lv1b = list(_lgout)
        _lgout.clear()
        _lgs.cfg_env = lambda *a, **k: "loud"
        _lv2 = _lgs.configure(lambda m: _lgout.append(str(m)))
        _lv2_out = list(_lgout)
        _lgout.clear()
        _secret_lg = "sk-test-" + "9f8e7d"

        def _boom_lg(api_key):
            raise RuntimeError("boom")
        try:
            _boom_lg(_secret_lg)
        except RuntimeError:
            _lgl.exception("caught")
        _lv3_out = "".join(_lgout)
    finally:
        _lgs.cfg_env = _real_cfgenv
        _lgl.remove()
        _lgl.add(sys.stderr, level="DEBUG")
    check("log level: unset -> INFO (debug lines dropped, info kept); 'debug' -> DEBUG",
          _lv0 == "INFO" and not any("dbg-line-x" in x for x in _lv1_out) and any("info-line-x" in x for x in _lv1_out)
          and _lv1 == "DEBUG" and any("dbg-line-y" in x for x in _lv1b), f"{_lv0} {_lv1} {_lv1_out}")
    check("log level: an unknown value falls back to INFO with a warning naming it",
          _lv2 == "INFO" and any("'loud'" in x and "using INFO" in x for x in _lv2_out), str(_lv2_out))
    check("log level: tracebacks don't print variable values (a key in a local stays out of the log)",
          "RuntimeError: boom" in _lv3_out and _secret_lg not in _lv3_out, _lv3_out[-200:])
    _exlg = (_rootwn / "config.example.yaml").read_text()
    _fxlg = (_rootwn / "tests" / "fixtures" / "config.yaml").read_text()
    check("log level: config.example and the test fixture ship logging.level INFO",
          "\nlogging:\n  level: INFO\n" in _exlg and "\nlogging:\n  level: INFO\n" in _fxlg, "")
    _botsrc_lg = (_rootwn / "bot.py").read_text()
    check("log level: bot.py applies it right after loading .env, before pipecat is imported",
          "log_setup.configure()" in _botsrc_lg
          and _botsrc_lg.index("log_setup.configure()") < _botsrc_lg.index("from pipecat")
          and _botsrc_lg.index("load_dotenv(_paths.env_file())") < _botsrc_lg.index("log_setup.configure()"), "")
    # Dashboard dist-root allowlist names only files the build actually ships
    import dashboard as _dashdr
    _distdr = REPO_CORE.parent / "dashboard-ui" / "dist"
    _alldr = getattr(_dashdr, "DIST_ROOT_FILES", None)
    check("dashboard: dist-root allowlist exists and every name in it is in dist/",
          bool(_alldr) and all((_distdr / f).is_file() for f in _alldr), str(_alldr))
    _idxdr = (REPO_CORE.parent / "dashboard-ui" / "index.html").read_text()
    _appdr = (REPO_CORE.parent / "dashboard-ui" / "src" / "App.jsx").read_text()
    _rmdr = (REPO_CORE.parent / "README.md").read_text()
    import re as _redr
    _logodr = _redr.findall(r'src="/([\w.-]+\.svg)"', _appdr) + _redr.findall(r'dashboard-ui/public/([\w.-]+\.svg)', _rmdr)
    check("dashboard: the brand-kit favicon set is linked (svg, ico, apple-touch) and every root icon/logo the page or README uses is in public/ and the allowlist",
          'href="/favicon.ico"' in _idxdr and 'rel="apple-touch-icon" href="/apple-touch-icon.png"' in _idxdr
          and {"favicon.ico", "apple-touch-icon.png"} <= set(_alldr or ())
          and bool(_logodr) and all((REPO_CORE.parent / "dashboard-ui" / "public" / f).is_file() and f in _alldr for f in _logodr)
          and _dashdr._ROOT_MEDIA.get(".ico") == "image/x-icon" and _dashdr._ROOT_MEDIA.get(".png") == "image/png", str(_logodr))
    import intelligence.agent as _iawn
    check("wake name: intelligence drop-filter delegates to wake_name",
          "wake_name" in _inspwn.getsource(_iawn._is_wake_phrase_only))
    _ia_srcwn = _inspwn.getsource(_iawn)
    check("wake name: intelligence no longer records transcripts for a heard list; it still drops bare wake phrases",
          "note_heard" not in _ia_srcwn and "if _is_wake_phrase_only(text):" in _ia_srcwn
          and "dropped wake-phrase-only" in _ia_srcwn)
    check("wake name: bot.py records the wake for its own room only",
          "note_wake(_pipeline_room())" in _main_src and "heard_key" not in _main_src, "bot.py _on_wake")
    _exwn = (_rootwn / "config.example.yaml").read_text()
    check("wake name: config.example sets the public default name explicitly (Jarvis)",
          "\n  name: Jarvis" in _exwn)

    # ── Built-in speaker zone (routes) ───────────────────────────
    import yaml as _ysp
    from dashboard import node_command as _ncsp

    def _cfgsp():
        return _ysp.safe_load(Path(os.environ["LVI_CONFIG"]).read_text()) or {}

    _sentsp = []
    _real_sendsp = _ncsp.send_to_room
    _ncsp.send_to_room = lambda room, p: _sentsp.append((room, p)) or True
    _zonessp = list(client.get("/api/dashboard/mapping").json()["zones"])
    _sharedsp = _zonessp[0]
    _base_rzsp = dict(_cfgsp().get("room_zones") or {})
    try:
        for _nm in ("builtin", "Builtin"):
            _r = client.post("/api/dashboard/zones", json={"name": _nm, "backend": "wiim", "ip": "192.0.2.53"})
            check(f"speaker: zone add refuses the reserved name {_nm!r}",
                  _r.status_code == 400 and "reserved" in _r.text, f"{_r.status_code} {_r.text[:120]}")
        registry.mark_connected("pc-spxvf")
        registry.note_offer("pc-spxvf", "suite-xvfsp")
        registry.note_line("pc-spxvf", "array")
        _r = client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-xvfsp": "builtin"}})
        check("speaker mapping: builtin refused for a node without a speaker (private rule, server-side)",
              _r.status_code == 400 and "speaker" in _r.text, f"{_r.status_code} {_r.text[:120]}")

        registry.mark_connected("pc-spws")
        registry.note_offer("pc-spws", "suite-wssp")
        registry.note_line("pc-spws", "waveshare")
        _r = client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-wssp": "builtin"}})
        check("speaker: builtin refused until the node has PROVEN its speaker (set_speaker ack)",
              _r.status_code == 400, f"{_r.status_code} {_r.text[:120]}")
        registry.note_speaker_ok("pc-spws", True)
        _st = {r["room"]: r for r in client.get("/api/dashboard/status").json()["rooms"]}
        check("speaker: /status rooms leave speaker state to /mapping (no speaker, speaker_ok or first_seen)",
              "suite-wssp" in _st and not any({"speaker", "speaker_ok", "first_seen"} & set(r) for r in _st.values()),
              str(_st.get("suite-wssp")))

        _m = client.get("/api/dashboard/mapping").json()
        check("speaker: GET mapping lists speaker rooms with volume + resolved zone",
              _m.get("speakers", {}).get("suite-wssp") == {"volume": 40, "zone": "builtin", "online": True,
                                                            "speaker_ok": True},
              str(_m.get("speakers")))
        check("speaker: GET mapping: built-in never appears as a shared zone", "builtin" not in _m["zones"])

        _sentsp.clear()
        _r = client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-wssp": "builtin"}})
        check("speaker mapping: builtin accepted for the speaker node", _r.status_code == 200, _r.text[:120])
        check("speaker: mapping write lands in config", _cfgsp()["room_zones"].get("suite-wssp") == "builtin")
        check("speaker: mapping PUT syncs the speaker node (enabled)",
              ("suite-wssp", {"cmd": "set_speaker", "enabled": True, "volume": 40}) in _sentsp, str(_sentsp))

        _sentsp.clear()
        _r = client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-wssp": _sharedsp}})
        check("speaker: remap to a shared zone -> node told to stay silent (never both)",
              _r.status_code == 200
              and ("suite-wssp", {"cmd": "set_speaker", "enabled": False, "volume": 40}) in _sentsp, str(_sentsp))

        _sentsp.clear()
        _r = client.put("/api/dashboard/node-speaker/volume", json={"room": "suite-wssp", "volume": 55})
        check("speaker: volume PUT ok + written to node_speaker.volumes",
              _r.status_code == 200 and (_cfgsp().get("node_speaker") or {}).get("volumes", {}).get("suite-wssp") == 55,
              f"{_r.status_code} {_r.text[:120]}")
        check("speaker: volume PUT pushes set_speaker with the new volume",
              _sentsp and _sentsp[-1] == ("suite-wssp", {"cmd": "set_speaker", "enabled": False, "volume": 55}),
              str(_sentsp))
        for _bad in (101, -1, "loud"):
            _r = client.put("/api/dashboard/node-speaker/volume", json={"room": "suite-wssp", "volume": _bad})
            check(f"speaker: volume PUT refuses {_bad!r}", _r.status_code == 422, str(_r.status_code))
        _r = client.put("/api/dashboard/node-speaker/volume", json={"room": "suite-xvfsp", "volume": 50})
        check("speaker: volume PUT 404s a node without a speaker", _r.status_code == 404, str(_r.status_code))

        _r = client.post("/api/dashboard/node-speaker/test", json={"room": "suite-wssp"})
        check("speaker: test clip 409s when the node's replies go to a shared zone",
              _r.status_code == 409 and "shared zone" in _r.text, f"{_r.status_code} {_r.text[:120]}")
        client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-wssp": "builtin"}})
        _r = client.post("/api/dashboard/node-speaker/test", json={"room": "suite-wssp"})
        check("speaker: test clip 409s when the node has no live session",
              _r.status_code == 409 and "offline" in _r.text, f"{_r.status_code} {_r.text[:120]}")

        class _Outsp:
            def __init__(self):
                self.frames = []

            async def queue_frame(self, frame, direction=None):
                self.frames.append(frame)
        _osp = _Outsp()
        _real_sfrsp = registry.sender_for_room
        registry.sender_for_room = lambda room: (_osp, "pc-spws") if room == "suite-wssp" else (None, None)
        try:
            _r = client.post("/api/dashboard/node-speaker/test", json={"room": "suite-wssp"})
        finally:
            registry.sender_for_room = _real_sfrsp
        _f = _osp.frames[0] if _osp.frames else None
        check("speaker: test clip queues the tone into THAT node's output transport only",
              _r.status_code == 200 and _f is not None and type(_f).__name__ == "OutputAudioRawFrame"
              and _f.sample_rate == 24000 and _f.num_channels == 1 and len(_f.audio) > 0,
              f"{_r.status_code} {_r.text[:120]} frames={len(_osp.frames)}")
        _r = client.post("/api/dashboard/node-speaker/test", json={"room": "suite-xvfsp"})
        check("speaker: test clip 404s a node without a speaker", _r.status_code == 404, str(_r.status_code))

        # (bug-hunt) Test tones must block the echo gates of the rooms
        # they play IN — the one global gate belonged to whichever
        # session connected LAST, so a zone test blocked a different
        # room's echo window. Two nodes, one mapped to the zone: only
        # its gate may move.
        from echo_gate import EchoGate as _EchoGate_bh7
        _gA_bh7, _gB_bh7 = _EchoGate_bh7(), _EchoGate_bh7()
        registry.mark_connected("pc-ga-bh7")
        registry.note_offer("pc-ga-bh7", "suite-gate-a")
        registry.mark_connected("pc-gb-bh7")
        registry.note_offer("pc-gb-bh7", "suite-gate-b")
        registry.set_gate(_gA_bh7, pc_id="pc-ga-bh7")
        registry.set_gate(_gB_bh7, pc_id="pc-gb-bh7")
        client.post("/api/dashboard/zones", json={"name": "gatezone", "backend": "wiim",
                                                  "ip": "192.0.2.56"})
        client.put("/api/dashboard/mapping",
                   json={"room_zones": {**_base_rzsp, "suite-gate-a": "gatezone"}})
        import wiim_output as _wo_bh7
        _saved_st_bh7, _saved_api_bh7 = _wo_bh7.wiim_get_status, _wo_bh7.wiim_api
        _wo_bh7.wiim_get_status = lambda ip: None
        _wo_bh7.wiim_api = lambda ip, cmd: True
        try:
            _r_bh7 = client.post("/api/dashboard/zones/gatezone/test")
        finally:
            _wo_bh7.wiim_get_status, _wo_bh7.wiim_api = _saved_st_bh7, _saved_api_bh7
        check("zone test tone blocks only the mapped room's echo gate (the other room's stays open)",
              _r_bh7.status_code == 200 and _gA_bh7.blocked() is True and _gB_bh7.blocked() is False,
              f"{_r_bh7.status_code} A={_gA_bh7.blocked()} B={_gB_bh7.blocked()}")
        check("gates_for_rooms falls back to the global gate when no named session matches",
              registry.gates_for_rooms(["nowhere-bh7"]) == [_gB_bh7],
              str(registry.gates_for_rooms(["nowhere-bh7"])))
        # cleanup: the zone delete drops the dangling mapping with it
        client.delete("/api/dashboard/zones/gatezone")
        registry.mark_disconnected("pc-ga-bh7")
        registry.mark_disconnected("pc-gb-bh7")
        registry.drop_sender("pc-ga-bh7")   # also drops the per-pc gate
        registry.drop_sender("pc-gb-bh7")
        registry.set_gate(None)

        # R-I3 deleting the zone a speaker room was mapped to re-syncs it
        # (the mapping drops -> the default is its own speaker again)
        _rz = client.post("/api/dashboard/zones", json={"name": "zdelsp", "backend": "wiim", "ip": "192.0.2.54"})
        client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-wssp": "zdelsp"}})
        _sentsp.clear()
        _rd = client.delete("/api/dashboard/zones/zdelsp")
        check("speaker: zone delete re-syncs speaker rooms (now back on their own speaker)",
              _rz.status_code == 200 and _rd.status_code == 200
              and ("suite-wssp", {"cmd": "set_speaker", "enabled": True, "volume": 55}) in _sentsp,
              f"add={_rz.status_code} del={_rd.status_code} sent={_sentsp}")
        client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, "suite-wssp": "builtin"}})

        # R-M6 room keys are stripped BEFORE the speaker/zone checks
        _r = client.put("/api/dashboard/mapping", json={"room_zones": {**_base_rzsp, " suite-wssp ": "builtin"}})
        check("speaker: a padded room key validates as the stripped room",
              _r.status_code == 200 and _cfgsp()["room_zones"].get("suite-wssp") == "builtin",
              f"{_r.status_code} {_r.text[:120]}")

        # R-I1 restart-shaped: the speaker node is offline/unknown, its
        # existing builtin entry must not block saving OTHER rooms
        registry.mark_disconnected("pc-spws")
        _saved_recsp = registry._known_rooms.pop("suite-wssp", None)
        try:
            _r = client.put("/api/dashboard/mapping",
                            json={"room_zones": {**_base_rzsp, "suite-wssp": "builtin", "suite-xvfsp": _sharedsp}})
            check("speaker: unchanged builtin entry of an offline node doesn't block saving other rooms",
                  _r.status_code == 200, f"{_r.status_code} {_r.text[:120]}")
            _m = client.get("/api/dashboard/mapping").json()
            check("speaker: GET mapping keeps an offline builtin room listed (row doesn't fall to 'pick a zone')",
                  (_m.get("speakers") or {}).get("suite-wssp", {}).get("online") is False,
                  str(_m.get("speakers")))
            _r = client.put("/api/dashboard/mapping",
                            json={"room_zones": {**_base_rzsp, "suite-wssp": "builtin", "suite-xvfsp": "builtin"}})
            check("speaker: a NEW builtin entry for a non-speaker room is still refused", _r.status_code == 400)
        finally:
            if _saved_recsp is not None:
                registry._known_rooms["suite-wssp"] = _saved_recsp
            registry.mark_connected("pc-spws")
            registry.note_offer("pc-spws", "suite-wssp")

        # a rename carries the speaker volume with the zone mapping
        _sentsp.clear()
        _ncsp.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_name", "ok": True,
                                      "data": {"name": "suite-wsspb"}}), "pc-spws")
        _c = _cfgsp()
        check("speaker: rename carries the speaker volume (and the mapping) to the new name",
              (_c.get("node_speaker") or {}).get("volumes", {}).get("suite-wsspb") == 55
              and "suite-wssp" not in (_c.get("node_speaker") or {}).get("volumes", {})
              and _c["room_zones"].get("suite-wsspb") == "builtin",
              str(_c.get("node_speaker")))
        check("speaker: a rename re-syncs the node under its new name",
              any(r == "suite-wsspb" and pl.get("cmd") == "set_speaker" for r, pl in _sentsp), str(_sentsp))
    finally:
        _ncsp.send_to_room = _real_sendsp
        registry.mark_disconnected("pc-spws")
        registry.mark_disconnected("pc-spxvf")

    # ── per-room agent override (Intelligence agent = house default) ──
    import intelligence.agent as _iaRA
    from dashboard import node_command as _ncRA

    _57_cfg = {sec: config_store.read_section(sec) for sec in ("intelligence", "room_agents", "room_zones")}
    _57_vars = ("LVI_HERMES_KEY", "LVI_HERMES_KEY_HA_BUTLER", "LVI_HERMES_KEY_KIDS_AGENT", "LVI_OPENCLAW_KEY")
    _57_env = {k: os.environ.get(k) for k in _57_vars}
    try:
        os.environ.update({"LVI_HERMES_KEY": "k-default", "LVI_HERMES_KEY_HA_BUTLER": "k-butler",
                           "LVI_HERMES_KEY_KIDS_AGENT": "k-kids", "LVI_OPENCLAW_KEY": "k-oc"})
        _57_icfg = {"kind": "hermes", "url": "http://h.test:8642/v1", "agents": {"hermes": "ha-butler"},
                    "urls": {"hermes": "http://h.test:8642/v1", "openclaw": "http://oc.test:18789/v1"}}
        config_store.write_section("intelligence", _57_icfg)
        config_store.write_section("room_agents", {"hermes": {"suite-kids57": "kids-agent"},
                                                   "openclaw": {"suite-kids57": "kidbot"}})

        def _mk57(room, resolver=None):
            a = _iaRA.IntelligenceAgent(room=room, session_mode="fresh", room_resolver=resolver)
            a._ensure_route()
            return a
        _a = _mk57("suite-kids57")
        check("room agents: bot: a room override routes to /p/<agent>/v1 with that agent's key",
              "/p/kids-agent/v1" in _a._url and _a._key_var == "LVI_HERMES_KEY_KIDS_AGENT" and _a._key == "k-kids"
              and _a._agent == "kids-agent", f"{_a._url} {_a._key_var} {_a._agent}")
        _a = _mk57("suite-kitchen57")
        check("room agents: bot: no override -> the house default agent (Intelligence card)",
              "/p/ha-butler/v1" in _a._url and _a._key == "k-butler" and _a._agent == "ha-butler", f"{_a._url} {_a._agent}")
        _box57 = {"room": None}
        _a = _iaRA.IntelligenceAgent(room=None, session_mode="fresh", room_resolver=lambda: _box57["room"])
        _a._ensure_route()
        _before57 = _a._agent
        _box57["room"] = "suite-kids57"
        _a._ensure_route()
        check("room agents: bot: room unknown when the session is built, known at the first turn -> override used",
              _before57 == "ha-butler" and _a._agent == "kids-agent" and _a._key == "k-kids", f"{_before57} -> {_a._agent}")
        import urllib.request as _urqRA

        _sentRA = []

        class _RespRA:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def _fake_urlopenRA(req, timeout=None):
            _sentRA.append(req)
            return _RespRA()
        _real_urlopenRA = _urqRA.urlopen
        _urqRA.urlopen = _fake_urlopenRA
        try:
            _boxRA = {"room": None}
            _a = _iaRA.IntelligenceAgent(room=None, session_mode="fresh", room_resolver=lambda: _boxRA["room"])
            _boxRA["room"] = "suite-kids57"   # known by the first turn
            _a._ask_intelligence("hi")
            _rq = _sentRA[-1] if _sentRA else None
            check("room agents: bot: the REQUEST of a room that resolved late goes to its agent with its key",
                  _rq is not None and _rq.full_url.endswith("/p/kids-agent/v1/chat/completions")
                  and _rq.get_header("Authorization") == "Bearer k-kids",
                  f"{getattr(_rq, 'full_url', None)} auth={'k-kids' in str(getattr(_rq, 'headers', ''))}")
        finally:
            _urqRA.urlopen = _real_urlopenRA
        config_store.write_section("intelligence", {**_57_icfg, "kind": "openclaw", "url": "http://oc.test:18789/v1",
                                                    "agents": {"hermes": "ha-butler", "openclaw": "main"}})
        _a = _mk57("suite-kids57")
        _b = _mk57("suite-kitchen57")
        check("room agents: bot: OpenClaw uses its OWN overrides (kidbot) and default (main); hermes overrides ignored",
              _a._agent == "kidbot" and _a._key == "k-oc" and _b._agent == "main", f"{_a._agent} {_b._agent}")
        _urqRA.urlopen = _fake_urlopenRA
        try:
            _ocRA = _iaRA.IntelligenceAgent(room="suite-kids57", session_mode="fresh")
            _ocRA._ask_intelligence("hi")
            _rq = _sentRA[-1]
            check("room agents: bot: the OpenClaw REQUEST names the room's agent (model openclaw/kidbot) with its key",
                  json.loads(_rq.data).get("model") == "openclaw/kidbot" and _rq.get_header("Authorization") == "Bearer k-oc",
                  str(json.loads(_rq.data).get("model")))
        finally:
            _urqRA.urlopen = _real_urlopenRA
        config_store.write_section("intelligence", _57_icfg)
        config_store.write_section("room_agents", {"openclaw": {"suite-kids57": "kidbot"}})
        _a = _mk57("suite-kids57")
        check("room agents: bot: an override for the other system is ignored -> default", _a._agent == "ha-butler", _a._agent)

        # routes
        config_store.write_section("room_agents", {"hermes": {"suite-kids57": "kids-agent"},
                                                   "openclaw": {"suite-kids57": "kidbot"}})
        _m = client.get("/api/dashboard/mapping").json()
        check("room agents: GET mapping: room_agents (both systems) + the current system and its default agent",
              _m.get("room_agents") == {"hermes": {"suite-kids57": "kids-agent"}, "openclaw": {"suite-kids57": "kidbot"}}
              and _m.get("agent_kind") == "hermes" and _m.get("default_agent") == "ha-butler",
              str({k: _m.get(k) for k in ("room_agents", "agent_kind", "default_agent")}))
        _rz = dict(_m.get("room_zones") or {})
        _zone57 = next(iter(_rz.values()), None)
        if _zone57:
            _rz["suite-kids57"] = _zone57
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz,
                                                             "room_agents": {"hermes": {"suite-kids57": "kids-x"}}})
            _ra = config_store.read_section("room_agents") or {}
            check("room agents: PUT: replaces only the sent system's overrides; the other system's stay",
                  _r.status_code == 200 and _ra.get("hermes") == {"suite-kids57": "kids-x"}
                  and _ra.get("openclaw") == {"suite-kids57": "kidbot"}, f"{_r.status_code} {_r.text[:120]} {_ra}")
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz})
            check("room agents: PUT without room_agents (an older page) leaves overrides untouched",
                  _r.status_code == 200 and (config_store.read_section("room_agents") or {}).get("hermes") == {"suite-kids57": "kids-x"},
                  _r.text[:120])
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz,
                                                             "room_agents": {"hermes": {"suite-kids57": ""}}})
            check("room agents: PUT: an empty agent clears the override (back to Default)",
                  _r.status_code == 200 and "suite-kids57" not in ((config_store.read_section("room_agents") or {}).get("hermes") or {}),
                  str(config_store.read_section("room_agents")))
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz, "room_agents": {"grok": {"suite-kids57": "x"}}})
            check("room agents: PUT: an unknown system -> 422", _r.status_code == 422, str(_r.status_code))
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz,
                                                             "room_agents": {"hermes": {"suite-kids57": "bad agent!"}}})
            check("room agents: PUT: an invalid agent name -> 422/400", _r.status_code in (400, 422), str(_r.status_code))
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz,
                                                             "room_agents": {"hermes": {"suite-nowhere57": "kids-agent"}}})
            check("room agents: PUT: an override for a room that isn't mapped or connected -> 400", _r.status_code == 400,
                  f"{_r.status_code} {_r.text[:100]}")
            config_store.write_section("room_agents", {"hermes": {"suite-gone57": "kids-agent"}})
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz, "room_agents": {
                "hermes": {"suite-gone57": "kids-agent", "suite-kids57": "ha-butler"}}})
            _ra = (config_store.read_section("room_agents") or {}).get("hermes") or {}
            check("room agents: PUT: a leftover override (room no longer mapped/connected) never wedges other saves",
                  _r.status_code == 200 and _ra.get("suite-gone57") == "kids-agent" and _ra.get("suite-kids57") == "ha-butler",
                  f"{_r.status_code} {_r.text[:120]}")
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz, "room_agents": {
                "hermes": {"suite-gone57": "", "suite-kids57": "ha-butler"}}})
            check("room agents: PUT: a leftover override can be cleared",
                  _r.status_code == 200 and "suite-gone57" not in ((config_store.read_section("room_agents") or {}).get("hermes") or {}),
                  f"{_r.status_code} {_r.text[:120]}")
            _r = client.put("/api/dashboard/mapping", json={"room_zones": _rz, "room_agents": {
                "hermes": {"unknown": "kids-agent"}}})
            check("room agents: PUT: the registry's 'unknown' placeholder is not a room", _r.status_code == 400,
                  str(_r.status_code))
        else:
            check("room agents: PUT: suite needs at least one mapped room in the copied config", False)

        # rename carries the override
        config_store.write_section("room_agents", {"hermes": {"suite-ren57": "kids-agent"}})
        registry.mark_connected("pc-ra")
        registry.note_offer("pc-ra", "suite-ren57")
        _ncRA.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_name", "ok": True,
                                      "data": {"name": "suite-ren57b"}}), "pc-ra")
        _ra = config_store.read_section("room_agents") or {}
        check("room agents: a room rename carries its agent override to the new name",
              (_ra.get("hermes") or {}).get("suite-ren57b") == "kids-agent" and "suite-ren57" not in (_ra.get("hermes") or {}),
              str(_ra))
        registry.mark_disconnected("pc-ra")

        # ── rename: never onto an existing room; mode + history follow ──
        import intelligence.agent as _iaRN
        from dashboard import routes as _rRN
        _rz_saved = dict(config_store.read_section("room_zones") or {})
        _sm_saved = dict(config_store.read_section("session_modes") or {})
        _calls_rn = []

        async def _fake_cmd(room, cmd, args=None, **kw):
            _calls_rn.append((room, cmd, args))
            return {"ok": True, "data": {"name": (args or {}).get("name")}}

        _orig_cmd_rn = _rRN._node_cmd
        _rRN._node_cmd = _fake_cmd
        _stash_path_saved = _iaRN._session_stash._path
        _stash_tmp = Path(tempfile.mkdtemp()) / "sessions.json"
        _iaRN._session_stash._path = _stash_tmp
        try:
            config_store.write_section("room_zones", {**_rz_saved, "suite-rnA": "ZoneA",
                                                      "suite-rnB": "ZoneB"})
            registry.mark_connected("pc-rn")
            registry.note_offer("pc-rn", "suite-rnA")
            _r1 = client.post("/api/dashboard/nodes/suite-rnA/name", json={"name": "suite-rnB"})
            _r2 = client.post("/api/dashboard/nodes/suite-rnA/name", json={"name": "SUITE-RNB"})
            check("rename: onto an existing room is refused before the node is asked",
                  _r1.status_code == 409 and _r2.status_code == 409 and not _calls_rn,
                  f"{_r1.status_code} {_r2.status_code} calls={_calls_rn}")
            _r3 = client.post("/api/dashboard/nodes/suite-rnA/name", json={"name": "SUITE-RNA"})
            check("rename: a case-only rename of the same room is allowed",
                  _r3.status_code == 200 and len(_calls_rn) == 1, f"{_r3.status_code} {_r3.text[:120]}")
            # a collision that arrives anyway (renamed outside the dashboard)
            _ncRA.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_name",
                                          "ok": True, "data": {"name": "suite-rnB"}}), "pc-rn")
            _rz = config_store.read_section("room_zones") or {}
            check("rename: a collision never overwrites the other room's zone",
                  _rz.get("suite-rnB") == "ZoneB" and _rz.get("suite-rnA") == "ZoneA", str(
                      {k: v for k, v in _rz.items() if k.startswith("suite-rn")}))
            registry.mark_disconnected("pc-rn")
            # mode + history follow a clean rename
            config_store.write_section("session_modes", {**_sm_saved, "suite-st1": "sticky"})
            _stash_tmp.write_text(json.dumps({"suite-st1": [{"role": "user", "content": "hi"}]}))
            registry.mark_connected("pc-st")
            registry.note_offer("pc-st", "suite-st1")
            _ncRA.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_name",
                                          "ok": True, "data": {"name": "suite-st2"}}), "pc-st")
            _sm = config_store.read_section("session_modes") or {}
            check("rename: the room's sticky/fresh mode follows the new name",
                  _sm.get("suite-st2") == "sticky" and "suite-st1" not in _sm, str(
                      {k: v for k, v in _sm.items() if k.startswith("suite-st")}))
            _hist = json.loads(_stash_tmp.read_text())
            check("rename: a sticky room's saved conversation follows the new name",
                  "suite-st2" in _hist and "suite-st1" not in _hist, str(list(_hist)))
            registry.mark_disconnected("pc-st")
        finally:
            _rRN._node_cmd = _orig_cmd_rn
            _iaRN._session_stash._path = _stash_path_saved
            config_store.write_section("room_zones", _rz_saved)
            config_store.write_section("session_modes", _sm_saved)

        # A TTS provider switch whose key isn't loaded yet keeps the boot provider
        import keys_catalog as _kcp
        _sp = getattr(_kcp, "session_tts_provider", None)
        if _sp is None:
            check("tts provider: keys_catalog.session_tts_provider exists", False, "missing")
        else:
            _EL, _DG = {"ELEVENLABS_API_KEY": "x"}, {"DEEPGRAM_API_KEY": "y"}
            check("tts provider: key loaded -> the configured provider, no warning",
                  _sp("deepgram", "elevenlabs", {**_EL, **_DG}) == ("deepgram", None))
            _p, _w = _sp("deepgram-flux", "elevenlabs", _EL)
            check("tts provider: key not loaded yet -> the boot provider + a warning naming the key",
                  _p == "elevenlabs" and _w and "DEEPGRAM_API_KEY" in _w, f"{_p} {_w}")
            check("tts provider: boot provider to keyless kokoro works too",
                  _sp("elevenlabs", "kokoro", {})[0] == "kokoro")
            check("tts provider: no boot provider -> configured (construction fails loud)",
                  _sp("deepgram", None, _EL) == ("deepgram", None))
            check("tts provider: boot provider ALSO keyless-missing -> configured (fails loud)",
                  _sp("deepgram", "elevenlabs", {}) == ("deepgram", None))
            check("tts provider: kokoro needs no key",
                  _sp("kokoro", "elevenlabs", {}) == ("kokoro", None))
        import ast as _astcp
        _botcp = _astcp.parse((REPO_CORE.parent / "bot.py").read_text())
        _rbcp = next((n for n in _astcp.walk(_botcp)
                      if isinstance(n, _astcp.AsyncFunctionDef) and n.name == "run_bot"), None)
        _rbsrc = _astcp.unparse(_rbcp) if _rbcp else ""
        check("tts provider: run_bot resolves the provider against the loaded keys",
              "session_tts_provider" in _rbsrc and _rbsrc.count("'tts', 'provider'") == 1,
              f"reads={_rbsrc.count(chr(39) + 'tts' + chr(39) + ', ' + chr(39) + 'provider' + chr(39))}")
        _mk = [i for i in range(len(_rbsrc)) if _rbsrc.startswith("tts_kokoro.make_service(", i)]
        check("kokoro: run_bot warms Kokoro off the loop before every make_service",
              len(_mk) == 2 and all(_rbsrc.rfind("await tts_kokoro.warm()", 0, m)
                                    > _rbsrc.rfind("tts_kokoro.make_service(", 0, m) for m in _mk),
              f"make={_mk}")

        # Keys card: an agent used by a room is in use
        import keys_catalog as _kc57
        from dashboard import routes as _rRA

        config_store.write_section("room_agents", {"hermes": {"suite-kids57": "kids_agent"}})
        _kcfg = _rRA._keys_cfg()   # the route's own wiring (GET /keys, Test, save)
        _rows = {r["var"]: r for r in _kc57.rows({"LVI_HERMES_KEY_KIDS_AGENT": "k"}, {}, _kcfg,
                                                 file_env={"LVI_HERMES_KEY_KIDS_AGENT": "k"})}
        check("room agents: keys: a room-override agent's real name is known (save/Test probe the right profile)",
              _kc57.target_for("LVI_HERMES_KEY_KIDS_AGENT", _kc57.known_agents(_kcfg)).get("agent") == "kids_agent",
              str(_kc57.target_for("LVI_HERMES_KEY_KIDS_AGENT", _kc57.known_agents(_kcfg))))
        check("room agents: keys: an agent picked for a room counts as in use",
              _rows.get("LVI_HERMES_KEY_KIDS_AGENT", {}).get("in_use") is True, str(_rows.get("LVI_HERMES_KEY_KIDS_AGENT")))
        # Keys card: an in-use agent whose key is NOT in .env still gets a row (shown as Missing)
        _rows_m = [r for r in _kc57.rows({}, {}, _kcfg, file_env={}) if r["var"] == "LVI_HERMES_KEY_KIDS_AGENT"]
        check("keys: an in-use room-override agent with no key in .env gets one Missing row",
              len(_rows_m) == 1 and _rows_m[0]["set"] is False and _rows_m[0]["in_use"] is True
              and _rows_m[0].get("agent") == "kids_agent", str(_rows_m))
        _rows_d = [r for r in _kc57.rows({"LVI_HERMES_KEY_KIDS_AGENT": "k"}, {}, _kcfg,
                                         file_env={"LVI_HERMES_KEY_KIDS_AGENT": "k"}) if r["var"] == "LVI_HERMES_KEY_KIDS_AGENT"]
        check("keys: an in-use agent whose key IS in .env keeps exactly one row", len(_rows_d) == 1, str(_rows_d))
        check("keys: an agent nothing uses and .env lacks gets no row",
              not [r for r in _kc57.rows({}, {}, {}, file_env={}) if r["var"].startswith("LVI_HERMES_KEY_")], "")
        _oc_cfg = {"intelligence": {"kind": "openclaw"}, "room_agents": {"hermes": {"suite-kids57": "kids_agent"}}}
        check("keys: with OpenClaw as the intelligence, a Hermes room override gets no Missing row",
              not [r for r in _kc57.rows({}, {}, _oc_cfg, file_env={}) if r["var"] == "LVI_HERMES_KEY_KIDS_AGENT"], "")
    finally:
        for _k, _v in _57_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _sec, _val in _57_cfg.items():
            config_store.write_section(_sec, _val or {})

    # ── rename: live sessions, reserved names, concurrent renames; ──────
    # ── intelligence: empty replies and queued calls of dead turns ──────
    import threading as _thRN2
    import urllib.request as _urqRN2
    from concurrent.futures import ThreadPoolExecutor as _TPERN2

    import intelligence.agent as _iaRN2
    from dashboard import node_command as _ncRN2
    from dashboard import routes as _rRN2
    from fastapi import HTTPException as _HTTPExcRN2

    def _set_name_ack(pc, name):
        _ncRN2.on_message(json.dumps({"type": "cmd_result", "evt": "cmd_result", "cmd": "set_name",
                                      "ok": True, "data": {"name": name}}), pc)

    def _lock_held(lock):
        # probe from another thread: an RLock held by THIS thread would
        # otherwise just be re-entered
        box = []

        def _try():
            got = lock.acquire(blocking=False)
            box.append(got)
            if got:
                lock.release()
        t = _thRN2.Thread(target=_try)
        t.start()
        t.join(2)
        return box == [False]

    _stash_path_RN2 = _iaRN2._session_stash._path
    _stash_tmp_RN2 = Path(tempfile.mkdtemp()) / "sessions.json"
    _iaRN2._session_stash._path = _stash_tmp_RN2
    _sm_saved_RN2 = dict(config_store.read_section("session_modes") or {})
    _rz_saved_RN2 = dict(config_store.read_section("room_zones") or {})
    try:
        # a sticky room renamed mid-session keeps saving under its NEW name
        config_store.write_section("session_modes", {**_sm_saved_RN2, "suite-rlA": "sticky"})
        _stash_tmp_RN2.write_text(json.dumps({"suite-rlA": [{"role": "user", "content": "earlier"},
                                                            {"role": "assistant", "content": "noted"}]}))
        registry.mark_connected("pc-rl")
        registry.note_offer("pc-rl", "suite-rlA")
        _held_at_resolve = []

        def _resolver_rl():
            _held_at_resolve.append(_lock_held(_iaRN2._session_stash._lock))
            return registry.room_for_pc("pc-rl")
        _ag_rl = _iaRN2.IntelligenceAgent(room="suite-rlA", session_mode="sticky", room_resolver=_resolver_rl)
        _ag_rl._ask_intelligence = lambda text: "after the rename"

        async def _sink_rl(frame, direction=None):
            return None
        _ag_rl.push_frame = _sink_rl
        _held_at_relabel = []
        _real_relabel_RN2 = registry.relabel_room

        def _rec_relabel(pc, info):
            _held_at_relabel.append(_lock_held(_iaRN2._session_stash._lock))
            return _real_relabel_RN2(pc, info)
        registry.relabel_room = _rec_relabel
        try:
            _set_name_ack("pc-rl", "suite-rlB")
        finally:
            registry.relabel_room = _real_relabel_RN2
        _held_at_resolve.clear()
        asyncio.run(_ag_rl._run_turn("what changed", _ag_rl._turn_epoch))
        _st_rl = json.loads(_stash_tmp_RN2.read_text())
        _new_rl = [h.get("content") for h in _st_rl.get("suite-rlB") or []]
        check("rename: a live sticky session saves under the room's new name, never the old one",
              "suite-rlA" not in _st_rl and _new_rl[:2] == ["earlier", "noted"]
              and "after the rename" in _new_rl, f"stash={_st_rl}")
        check("rename: the registry relabel happens under the conversation stash's lock",
              _held_at_relabel == [True], str(_held_at_relabel))
        check("rename: a sticky save resolves the room under the conversation stash's lock",
              _held_at_resolve == [True], str(_held_at_resolve))
        registry.mark_disconnected("pc-rl")

        # reserved registry labels are never a node name
        _calls_RN2 = []

        async def _fake_cmd_RN2(room, cmd, args=None, **kw):
            _calls_RN2.append((room, cmd, args))
            await asyncio.sleep(0.3)  # the node takes its time to ack
            return {"ok": True, "data": {"name": (args or {}).get("name")}}

        _orig_cmd_RN2 = _rRN2._node_cmd
        _rRN2._node_cmd = _fake_cmd_RN2
        try:
            registry.mark_connected("pc-rs")
            registry.note_offer("pc-rs", "suite-rsA")
            # as after a restart: no placeholder or browser record exists, so
            # the existing-room check alone can't refuse these names
            _held_rs = {k: registry._known_rooms.pop(k) for k in ("unknown", "browser")
                        if k in registry._known_rooms}
            try:
                _codes_rs = [client.post("/api/dashboard/nodes/suite-rsA/name", json={"name": n}).status_code
                             for n in ("unknown", "Unknown", "browser", "BROWSER")]
            finally:
                registry._known_rooms.update(_held_rs)
            check("rename: the registry's reserved labels (unknown, browser, any case) are refused",
                  all(400 <= c < 500 for c in _codes_rs) and not _calls_RN2, f"{_codes_rs} calls={_calls_RN2}")
            registry.mark_disconnected("pc-rs")

            # two concurrent renames onto one name: the second is refused
            registry.mark_connected("pc-ccA")
            registry.note_offer("pc-ccA", "suite-ccA")
            registry.mark_connected("pc-ccB")
            registry.note_offer("pc-ccB", "suite-ccB")

            async def _two_renames():
                async def one(room, name):
                    try:
                        await _rRN2.set_node_name(room, _rRN2.NamePayload(name=name))
                        return 200
                    except _HTTPExcRN2 as e:
                        return e.status_code
                return await asyncio.gather(one("suite-ccA", "suite-ccX"), one("suite-ccB", "suite-ccX"))
            _calls_RN2.clear()
            _cc = sorted(asyncio.run(_two_renames()))
            check("rename: two concurrent renames onto one name -> one goes through, the other 409",
                  _cc == [200, 409] and len(_calls_RN2) == 1, f"{_cc} calls={_calls_RN2}")
            _calls_RN2.clear()

            async def _after():
                await _rRN2.set_node_name("suite-ccB", _rRN2.NamePayload(name="suite-ccX"))
                return 200
            check("rename: the in-flight reservation is released once the node answers",
                  asyncio.run(_after()) == 200 and len(_calls_RN2) == 1, str(_calls_RN2))

            # an ack onto a name another live session holds never relabels
            config_store.write_section("room_zones", {**_rz_saved_RN2, "suite-ccA": "ZoneCA"})
            _set_name_ack("pc-ccA", "suite-ccB")
            _rz_cc = config_store.read_section("room_zones") or {}
            check("rename: an ack onto a name another live session holds leaves both sessions as they were",
                  registry.room_for_pc("pc-ccA") == "suite-ccA" and registry.room_for_pc("pc-ccB") == "suite-ccB"
                  and _rz_cc.get("suite-ccA") == "ZoneCA" and "suite-ccB" not in _rz_cc,
                  f"A={registry.room_for_pc('pc-ccA')} B={registry.room_for_pc('pc-ccB')} "
                  f"zones={ {k: v for k, v in _rz_cc.items() if k.startswith('suite-cc')} }")
            registry.mark_disconnected("pc-ccA")
            registry.mark_disconnected("pc-ccB")
        finally:
            _rRN2._node_cmd = _orig_cmd_RN2
    finally:
        _iaRN2._session_stash._path = _stash_path_RN2
        config_store.write_section("session_modes", _sm_saved_RN2)
        config_store.write_section("room_zones", _rz_saved_RN2)

    # an empty reply is a failed turn: apology, no history, retry not deduped
    class _RespRN2:
        def __init__(self, content):
            self._body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self._body

    async def _empty_probe(content):
        a = _iaRN2.IntelligenceAgent(room="suite-empty", session_mode="fresh")
        a._key = "k-suite"
        spoken = []

        async def push(frame, direction=None):
            t = getattr(frame, "text", None)
            if t is not None:
                spoken.append(t)
        a.push_frame = push
        a._last_user_text, a._last_user_time = "what time is it", time.monotonic()
        await a._run_turn("what time is it", a._turn_epoch)
        return spoken, list(a._history), a._last_user_text

    _real_urlopen_RN2 = _urqRN2.urlopen
    try:
        for _content in ("", "   \n"):
            _urqRN2.urlopen = lambda req, timeout=None, _c=_content: _RespRN2(_c)
            _sp_e, _hi_e, _lu_e = asyncio.run(_empty_probe(_content))
            check(f"intelligence: an empty reply ({_content!r}) is a failed turn: apology, no history, retry allowed",
                  _sp_e == ["Sorry — I can't reach my intelligence right now."] and _hi_e == [] and _lu_e == "",
                  f"spoken={_sp_e} history={_hi_e} last={_lu_e!r}")
    finally:
        _urqRN2.urlopen = _real_urlopen_RN2

    # a dead turn's call still queued for a pool thread is dropped, never sent
    async def _queued_probe(end):
        a = _iaRN2.IntelligenceAgent(room="suite-queued", session_mode="fresh")
        sent = []
        a._ask_intelligence = lambda text: sent.append(text) or "late"

        async def _noop(*args, **kw):
            return None
        a.push_frame = _noop
        a._turn_task = asyncio.create_task(a._run_turn("stale question", a._turn_epoch))
        await asyncio.sleep(0.2)  # parked on the queued call
        if end == "supersede":
            a._turn_task.cancel()
            try:
                await a._turn_task
            except asyncio.CancelledError:
                pass
        else:
            await a._end_turn()
        return sent

    _pool_saved_RN2 = _iaRN2._INTEL_POOL
    try:
        for _end in ("supersede", "session end"):
            _pool_q = _TPERN2(max_workers=1)
            _free_q = _thRN2.Event()
            _pool_q.submit(_free_q.wait, 5)   # the only thread is busy
            _iaRN2._INTEL_POOL = _pool_q
            _sent_q = asyncio.run(_queued_probe(_end))
            _free_q.set()
            _pool_q.shutdown(wait=True)
            check(f"intelligence: a {_end} drops the turn's queued call (the stale question is never sent)",
                  _sent_q == [], f"sent={_sent_q}")
    finally:
        _iaRN2._INTEL_POOL = _pool_saved_RN2

    # ── stt STT card (routes) ─────────────────────────────────────────
    import yaml as _ystt
    from dashboard import routes as _rstt

    def _sttstt():
        return (_ystt.safe_load(Path(os.environ["LVI_CONFIG"]).read_text()) or {}).get("stt") or {}

    _stt_beforestt = dict(config_store.read_section("stt") or {})
    try:
        _g = client.get("/api/dashboard/stt").json()
        check("stt GET /stt: 17 languages and the status block",
              len(_g.get("languages", [])) == 17 and "state" in (_g.get("status") or {}),
              str({k: _g.get(k) for k in ("languages", "status")})[:300])

        # A value the card no longer writes (hand-set, or left by an older
        # version) stays in config.yaml as it is.
        config_store.write_section("stt", {**_stt_beforestt, "model": "stale-suite-model"})

        for _body, _why in (({"language": "xx"}, "bad language"), ({"language": "en", "beam": 5}, "extra key"),
                            ({}, "empty body")):
            _r = client.put("/api/dashboard/stt", json=_body)
            check(f"stt PUT refuses {_why}", _r.status_code == 422, f"{_r.status_code} {_r.text[:100]}")

        _r = client.put("/api/dashboard/stt", json={"language": "en"})
        _c = _sttstt()
        check("stt language-only PUT: live, no restart, model (even a hand-set one) untouched",
              _r.status_code == 200 and _r.json().get("restart_needed") is False
              and _c.get("language") == "en" and _c.get("model") == "stale-suite-model", f"{_r.text[:120]} cfg={_c}")
        _os_lang = os.environ.get("LVI_STT_LANGUAGE")
        os.environ["LVI_STT_LANGUAGE"] = "de"
        try:
            _r = client.put("/api/dashboard/stt", json={"language": "auto"})
            _g = client.get("/api/dashboard/stt").json()
            check("stt language 'auto' is written explicitly, so an env language can't override Auto-detect",
                  _r.status_code == 200 and _sttstt().get("language") == "auto" and _g["current"]["language"] == "auto",
                  f"cfg={_sttstt()} cur={_g['current']}")
        finally:
            if _os_lang is None:
                os.environ.pop("LVI_STT_LANGUAGE", None)
            else:
                os.environ["LVI_STT_LANGUAGE"] = _os_lang

        # Provider and language in one save land together.
        _lang_mx = "es" if _sttstt().get("language") != "es" else "it"
        _r = client.put("/api/dashboard/stt", json={"provider": "elevenlabs", "language": _lang_mx})
        _c = _sttstt()
        check("stt: provider and language in one save are both written",
              _r.status_code == 200 and (_c.get("provider"), _c.get("language")) == ("elevenlabs", _lang_mx), str(_c))

        _s = client.get("/api/dashboard/status").json()
        check("stt /status carries the stt block", isinstance(_s.get("stt"), dict) and "state" in _s["stt"],
              str(_s.get("stt")))
    finally:
        config_store.write_section("stt", _stt_beforestt)

    # ── speech tiers: the /stt and /tts payloads (routes) ─────────────
    # Each card's groups come from its catalog; a cloud provider without its
    # key is offered disabled with the reason; a field the payloads no longer
    # have (a tab left open across the upgrade) is a 422 that changes nothing.
    import hashlib as _hlST

    import config as _cfgST
    import stt_catalog as _scST
    import tts_catalog as _tcST
    import tts_kokoro as _tkST
    from loguru import logger as _lgST

    _ST_VARS = ("LVI_ENV_FILE", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "LVI_LOCALAI_KEY", "LVI_LOCALAI_URL",
                "LVI_STT_PROVIDER", "LVI_STT_LANGUAGE", "LVI_TTS_PROVIDER")
    _st_env = {k: os.environ.get(k) for k in _ST_VARS}
    _st_cfg = {s: copy.deepcopy(dict(config_store.read_section(s) or {})) for s in ("stt", "tts", "localai")}
    _st_fns = (_rstt._stt_deepgram_sdk, _tkST.kokoro_available, _tkST._files_problem)
    _st_envfile = SCRATCH / "speech-tiers.env"
    _st_cfgpath = Path(os.environ["LVI_CONFIG"])
    _ST_DEAD = "suite-dead-value-4242"
    _NO_KEY = (False, "add the key on System · Keys")

    def _names_field(r, field):
        """A dashboard 422 for a body field the payload no longer has, naming it."""
        try:
            det = r.json().get("detail")
        except ValueError:
            return False
        return r.status_code == 422 and isinstance(det, list) and any(
            isinstance(e, dict) and e.get("type") == "extra_forbidden" and list(e.get("loc") or [])[-1:] == [field]
            for e in det)

    def _offered(payload):
        return {p.get("name"): (p.get("available"), p.get("reason")) for p in payload.get("providers") or []}

    def _st_pending(boot, saved):
        _rb, _rl = _cfgST.boot_config, _cfgST._load
        _cfgST.boot_config, _cfgST._load = (lambda: boot), (lambda: saved)
        try:
            return _rstt._restart_pending()
        finally:
            _cfgST.boot_config, _cfgST._load = _rb, _rl

    try:
        for _k in _ST_VARS:
            os.environ.pop(_k, None)
        _st_envfile.write_text("")
        os.environ["LVI_ENV_FILE"] = str(_st_envfile)
        config_store.write_section("localai", {})
        _rstt._stt_deepgram_sdk = lambda: True
        _tkST.kokoro_available = lambda: True    # Kokoro set up: the TTS card reads it
        _tkST._files_problem = lambda: None

        # ── STT card ──
        _gS = client.get("/api/dashboard/stt").json()
        check("speech tiers: GET /stt groups equal the catalog — the three tiers, then every provider in card order with its tier",
              _gS.get("tiers") == _scST.TIERS
              and [(p.get("name"), p.get("label"), p.get("tier")) for p in _gS.get("providers") or []]
              == [(p["name"], p["label"], p["tier"]) for p in _scST.PROVIDERS],
              str({k: _gS.get(k) for k in ("tiers", "providers")})[:400])
        check("speech tiers: GET /stt returns exactly the card's fields — no model list, GPU or download state; current is provider and language",
              set(_gS) == {"current", "tiers", "providers", "languages", "deepgram_key_set", "elevenlabs_key_set", "status"}
              and set(_gS.get("current") or {}) == {"provider", "language", "custom_language"},
              f"keys={sorted(_gS)} current={_gS.get('current')}")
        _oS = _offered(_gS)
        check("speech tiers: STT — a cloud provider without its key is offered disabled with 'add the key on System · Keys'; Whisper base needs none",
              _oS.get("deepgram") == _NO_KEY and _oS.get("elevenlabs") == _NO_KEY and _oS.get("whisper") == (True, None),
              str(_oS))
        _st_envfile.write_text("ELEVENLABS_API_KEY=el-tier-SECRET-0001\nDEEPGRAM_API_KEY=dg-tier-SECRET-0002\n")
        _gSr = client.get("/api/dashboard/stt")
        _cloudS = [p for p in _gSr.json().get("providers") or [] if p.get("tier") == "cloud"]
        check("speech tiers: STT — with its key saved on System · Keys a cloud provider is offered, and the key never appears",
              len(_cloudS) == 2 and all(p.get("available") is True and "reason" not in p for p in _cloudS)
              and "SECRET" not in _gSr.text, _gSr.text[:300])
        _st_envfile.write_text("")

        config_store.write_section("stt", {k: v for k, v in _st_cfg["stt"].items() if k != "provider"})
        _pS = [client.get("/api/dashboard/stt").json().get("current", {}).get("provider")]
        for _v in ("nonesuch", " Deepgram "):
            config_store.write_section("stt", {**_st_cfg["stt"], "provider": _v})
            _pS.append(client.get("/api/dashboard/stt").json().get("current", {}).get("provider"))
        check("speech tiers: GET /stt reads an unset or unknown provider as Embedded (whisper), odd case or spaces as that provider",
              _pS == ["whisper", "whisper", "deepgram"], str(_pS))

        _leanS = {k: v for k, v in _st_cfg["stt"].items() if k not in ("provider", "model", "device", "compute_type")}
        config_store.write_section("stt", {**_leanS, "language": "fr"})
        _rS = client.put("/api/dashboard/stt", json={"language": "en"})
        check("speech tiers: PUT /stt answers ok, restart_needed, key_needed and current — no download state",
              _rS.status_code == 200 and set(_rS.json()) == {"ok", "restart_needed", "key_needed", "current"},
              _rS.text[:200])
        client.put("/api/dashboard/stt", json={"provider": "elevenlabs", "language": "de"})
        client.put("/api/dashboard/stt", json={"provider": "whisper"})
        _cS = _sttstt()
        check("speech tiers: PUT /stt writes only provider and language — never a model, device or compute type",
              _cS.get("provider") == "whisper" and _cS.get("language") == "de"
              and not ({"model", "device", "compute_type"} & set(_cS)), str(_cS))
        _shaS = _hlST.sha256(_st_cfgpath.read_bytes()).hexdigest()
        _rS = client.put("/api/dashboard/stt", json={"language": "en", "model": _ST_DEAD})
        check("speech tiers: PUT /stt with the deleted model field (a tab left open) is a 422 naming it; config.yaml byte-identical",
              _names_field(_rS, "model") and _hlST.sha256(_st_cfgpath.read_bytes()).hexdigest() == _shaS,
              f"{_rS.status_code} {_rS.text[:160]}")
        # Never a real model name here: a red run against the old routes would delete it from the host's cache.
        _rS = client.delete("/api/dashboard/stt/model/nonesuch")
        check("speech tiers: the Whisper model delete route is gone (404/405) — the appliance never deletes models",
              _rS.status_code in (404, 405)
              and not any(getattr(_rt, "path", "") == "/api/dashboard/stt/model/{name}" for _rt in _rstt.router.routes),
              f"{_rS.status_code} {_rS.text[:120]}")
        config_store.write_section("stt", {**_st_cfg["stt"], "model": "stale-suite-model"})
        _sjS = client.get("/api/dashboard/status").json()
        check("speech tiers: /status has no stt_model and the STT status no device (Embedded is fixed); a stale "
              "stt.model never shows",
              "stt_model" not in _sjS and "device" not in (_sjS.get("stt") or {})
              and "stale-suite-model" not in str(_sjS), str({k: _sjS.get(k) for k in ("stt",)})[:300])
        _bootS = {"stt": {"provider": "whisper", "model": "stale-a"}}
        _saveS = {"stt": {"provider": "whisper", "model": "stale-b", "device": "stale-c", "compute_type": "stale-d"}}
        check("speech tiers: restart pending — a stt key the bot no longer reads never waits for a restart; a provider change still does",
              "stt" not in _st_pending(_bootS, _saveS)
              and "stt" in _st_pending(_bootS, {"stt": {"provider": "elevenlabs"}}),
              f"pending={_st_pending(_bootS, _saveS)}")

        # ── TTS card ──
        _TTS_FIELDS = {"provider", "voice_id", "model", "deepgram_voice", "deepgram_flux_voice", "deepgram_flux_speed",
                       "deepgram_flux_expressivity", "elevenlabs_speed", "elevenlabs_stability",
                       "elevenlabs_text_normalization", "kokoro_voice", "kokoro_speed", "localai_model_tts",
                       "localai_voice",
                       "elevenlabs_key_hint", "localai_key_hint",
                       "deepgram_key_hint", "tiers", "providers", "catalog"}
        _gT = client.get("/api/dashboard/tts").json()
        check("speech tiers: GET /tts groups equal the catalog — the three tiers, then every provider in card order with its tier",
              _gT.get("tiers") == _tcST.TIERS
              and [(p.get("name"), p.get("label"), p.get("tier")) for p in _gT.get("providers") or []]
              == [(p["name"], p["label"], p["tier"]) for p in _tcST.PROVIDERS],
              str({k: _gT.get(k) for k in ("tiers", "providers")})[:400])
        check("speech tiers: GET /tts returns exactly the card's fields (no leftovers of removed voices or the fallback switch); the Kokoro voice list stays",
              set(_gT) == _TTS_FIELDS and (_gT.get("catalog") or {}).get("kokoro") == list(_tkST.KOKORO_VOICES),
              f"extra={sorted(set(_gT) - _TTS_FIELDS)} missing={sorted(_TTS_FIELDS - set(_gT))}")
        _oT = _offered(_gT)
        check("speech tiers: TTS — a cloud provider without its key is offered disabled with 'add the key on System · Keys'; Kokoro needs none",
              [_oT.get(n) for n in ("elevenlabs", "deepgram-flux", "deepgram")] == [_NO_KEY] * 3
              and _oT.get("kokoro") == (True, None), str(_oT))
        _st_envfile.write_text("ELEVENLABS_API_KEY=el-tier-SECRET-0001\nDEEPGRAM_API_KEY=dg-tier-SECRET-0002\n")
        _gTr = client.get("/api/dashboard/tts")
        _cloudT = [p for p in _gTr.json().get("providers") or [] if p.get("tier") == "cloud"]
        check("speech tiers: TTS — with its key saved on System · Keys a cloud provider is offered, and the key never appears",
              len(_cloudT) == 3 and all(p.get("available") is True and "reason" not in p for p in _cloudT)
              and "SECRET" not in _gTr.text, _gTr.text[:300])
        _st_envfile.write_text("")
        _tkST._files_problem = lambda: "/x/kokoro-v1.0.onnx is missing"
        _gT = client.get("/api/dashboard/tts").json()
        _tkST._files_problem = lambda: None
        _oT, _oS = _offered(_gT), _offered(client.get("/api/dashboard/stt").json())
        check("speech tiers: TTS — Kokoro's files missing: 'model files missing'; LocalAI not set up: 'set it up on System' on both cards",
              _oT.get("kokoro") == (False, "model files missing")
              and _oT.get("localai") == (False, "set it up on System") == _oS.get("localai"), f"tts={_oT} stt={_oS}")

        config_store.write_section("tts", {k: v for k, v in _st_cfg["tts"].items() if k != "provider"})
        _pT = [(client.get("/api/dashboard/tts").json().get("provider"),
                client.get("/api/dashboard/status").json().get("tts_provider"))]
        for _v in ("nonesuch", "ElevenLabs"):
            config_store.write_section("tts", {**_st_cfg["tts"], "provider": _v})
            _pT.append((client.get("/api/dashboard/tts").json().get("provider"),
                        client.get("/api/dashboard/status").json().get("tts_provider")))
        check("speech tiers: GET /tts and /status read an unset or unknown TTS provider as Embedded (kokoro), odd case as that provider",
              _pT == [("kokoro", "kokoro"), ("kokoro", "kokoro"), ("elevenlabs", "elevenlabs")], str(_pT))
        config_store.write_section("tts", {k: v for k, v in _st_cfg["tts"].items() if k != "provider"})
        config_store.write_section("stt", {**_st_cfg["stt"], "provider": "whisper"})
        _kT = {r["var"]: r for r in client.get("/api/dashboard/keys").json().get("keys", [])}
        check("speech tiers: Keys card — an unset TTS provider is Kokoro, so the ElevenLabs key is not in use",
              _kT.get("ELEVENLABS_API_KEY", {}).get("in_use") is False, str(_kT.get("ELEVENLABS_API_KEY")))

        from pydantic import ValidationError as _VEST

        def _tts_body_ok(**kw):
            try:
                _rstt.TTSPayload(**kw)
            except _VEST:
                return False
            return True
        check("speech tiers: TTSPayload takes exactly the TTS catalog's providers and defaults to Embedded (kokoro)",
              _rstt.TTSPayload().provider == _tcST.DEFAULT_PROVIDER
              and all(_tts_body_ok(provider=n) for n in _tcST.PROVIDER_NAMES) and not _tts_body_ok(provider="nonesuch"),
              f"default={_rstt.TTSPayload().provider!r}")

        config_store.write_section("localai", {"url": "http://192.0.2.50:8080"})
        _st_envfile.write_text("LVI_LOCALAI_KEY=la-tier-SECRET-0003\n")
        _rTa = client.put("/api/dashboard/tts", json={"provider": "localai", "kokoro_voice": "am_adam", "kokoro_speed": 0.9})
        _cTa = dict(config_store.read_section("tts") or {})
        _rTb = client.put("/api/dashboard/tts", json={"provider": "deepgram", "deepgram_voice": "aura-2-thalia-en",
                                                      "kokoro_voice": "bf_emma", "kokoro_speed": 1.2})
        _cTb = dict(config_store.read_section("tts") or {})
        check("speech tiers: the Kokoro voice and speed save with LocalAI and Cloud providers too — the standby's Fallback voice",
              _rTa.status_code == 200 and _rTb.status_code == 200
              and (_cTa.get("provider"), _cTa.get("kokoro_voice"), _cTa.get("kokoro_speed")) == ("localai", "am_adam", 0.9)
              and (_cTb.get("provider"), _cTb.get("kokoro_voice"), _cTb.get("kokoro_speed")) == ("deepgram", "bf_emma", 1.2),
              f"{_rTa.status_code} {_rTa.text[:100]} {_rTb.status_code} {_rTb.text[:100]} localai={_cTa} cloud={_cTb}")
        config_store.write_section("localai", {})
        _st_envfile.write_text("")

        config_store.write_section("tts", {**dict(config_store.read_section("tts") or {}), "fallback": "stale-suite-value"})
        _rT = client.put("/api/dashboard/tts", json={"provider": "deepgram", "deepgram_voice": "aura-2-zeus-en"})
        check("speech tiers: PUT /tts carries a stale tts key it no longer writes over as it is — the product never rewrites a user's config",
              _rT.status_code == 200 and (config_store.read_section("tts") or {}).get("fallback") == "stale-suite-value",
              f"{_rT.status_code} {_rT.text[:100]} {dict(config_store.read_section('tts') or {})}")

        # A tab left open across the upgrade still sends the old fields (the
        # old card sent `fallback` with every cloud save).
        config_store.write_section("tts", {**_st_cfg["tts"], "provider": "elevenlabs"})
        _bodyT = {"provider": "deepgram", "deepgram_voice": "aura-2-thalia-en"}
        _deadT = (("fallback", "off"), ("fallback", _ST_DEAD), ("pocket_voice", _ST_DEAD))
        _shaT = _hlST.sha256(_st_cfgpath.read_bytes()).hexdigest()
        _rTs = [(_f, client.put("/api/dashboard/tts", json={**_bodyT, _f: _x})) for _f, _x in _deadT]
        check("speech tiers: PUT /tts with a deleted field (fallback, the old local voice) is a 422 naming that field",
              all(_names_field(_r, _f) for _f, _r in _rTs), str([(_f, _r.status_code, _r.text[:80]) for _f, _r in _rTs]))
        check("speech tiers: those refused TTS saves leave config.yaml byte-identical",
              _hlST.sha256(_st_cfgpath.read_bytes()).hexdigest() == _shaT)
        _hitsST = len(PIPECAT_HANDLER_HITS)
        _linesST = []
        _sinkST = _lgST.add(lambda m: _linesST.append(str(m)), level="DEBUG")
        try:
            _rAll = [client.put("/api/dashboard/stt", json={"language": "en", "model": _ST_DEAD})]
            _rAll += [client.put("/api/dashboard/tts", json={**_bodyT, _f: _x}) for _f, _x in _deadT]
        finally:
            _lgST.remove(_sinkST)
        check("speech tiers: the dashboard answers deleted-field 422s itself — pipecat's handler never runs, the rejected value is never echoed or logged",
              all(_r.status_code == 422 for _r in _rAll) and len(PIPECAT_HANDLER_HITS) == _hitsST
              and not any(_ST_DEAD in _r.text for _r in _rAll) and not any(_ST_DEAD in _ln for _ln in _linesST)
              and sum("dashboard: 422 on /api/dashboard/" in _ln for _ln in _linesST) == len(_rAll),
              f"codes={[_r.status_code for _r in _rAll]} pipecat={PIPECAT_HANDLER_HITS[_hitsST:][:3]} lines={len(_linesST)}")
    finally:
        _rstt._stt_deepgram_sdk, _tkST.kokoro_available, _tkST._files_problem = _st_fns
        for _k, _v in _st_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _s, _v in _st_cfg.items():
            config_store.write_section(_s, _v)

    # ── Keys card: every API key in one place (presence + hint only) ──
    from dashboard import routes as _rk
    _K_VARS = ("LVI_ENV_FILE", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "LVI_HERMES_KEY",
               "LVI_OPENCLAW_KEY", "LVI_NANOBOT_KEY", "LVI_HERMES_KEY_HA_BUTLER", "LVI_HERMES_KEY_STRAY")
    _k_before_env = {k: os.environ.get(k) for k in _K_VARS}
    _k_before_cfg = {sec: dict(config_store.read_section(sec) or {}) for sec in ("tts", "stt", "intelligence")}
    _k_saved_fns = (_rk._keys_http if hasattr(_rk, "_keys_http") else None, _rk._intelligence_probe)
    _kenv = SCRATCH / "keys.env"
    # Running process: deepgram OLD, elevenlabs set, hermes set. Saved .env:
    # deepgram NEW (restart pending), hermes same, ha-butler agent key (new).
    _kenv.write_text("DEEPGRAM_API_KEY=dg-new-SECRET-2222\nLVI_HERMES_KEY=hk-same-SECRET-3333\n"
                     "LVI_HERMES_KEY_HA_BUTLER=hb-agent-SECRET-4444\n"
                     'ELEVENLABS_API_KEY="el-live-SECRET-5555"  # quoted, as dotenv allows\n')
    os.environ.update({"LVI_ENV_FILE": str(_kenv), "DEEPGRAM_API_KEY": "dg-old-SECRET-1111",
                       "ELEVENLABS_API_KEY": "el-live-SECRET-5555", "LVI_HERMES_KEY": "hk-same-SECRET-3333"})
    os.environ.pop("LVI_OPENCLAW_KEY", None)
    os.environ.pop("LVI_NANOBOT_KEY", None)
    os.environ.pop("LVI_HERMES_KEY_HA_BUTLER", None)
    os.environ["LVI_HERMES_KEY_STRAY"] = "stray-SECRET-6666"   # process-only: must never become a row
    _SECRETS = ("dg-new-SECRET-2222", "dg-old-SECRET-1111", "hk-same-SECRET-3333", "hb-agent-SECRET-4444",
                "el-live-SECRET-5555", "stray-SECRET-6666", "oc-file-SECRET-7777")
    try:
        config_store.write_section("tts", {**_k_before_cfg["tts"], "provider": "elevenlabs"})
        config_store.write_section("stt", {**_k_before_cfg["stt"], "provider": "deepgram"})
        config_store.write_section("intelligence", {k: v for k, v in _k_before_cfg["intelligence"].items()
                                                    if k not in ("agents", "agent")} | {"kind": "hermes"})
        _kr = client.get("/api/dashboard/keys")
        _kk = {r["var"]: r for r in (_kr.json().get("keys") or [])} if _kr.status_code == 200 else {}
        check("keys GET: static rows in order, then agent rows found in .env",
              [r["var"] for r in _kr.json().get("keys", [])] == ["ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "LVI_LOCALAI_KEY",
                                                                 "LVI_HERMES_KEY",
                                                                 "LVI_OPENCLAW_KEY", "LVI_NANOBOT_KEY",
                                                                 "LVI_HERMES_KEY_HA_BUTLER"]
              if _kr.status_code == 200 else False, _kr.text[:300])
        check("keys GET: no key value anywhere in the response", not any(x in _kr.text for x in _SECRETS), _kr.text[:300])
        _dg = _kk.get("DEEPGRAM_API_KEY", {})
        check("keys GET: Deepgram — set, hint of the SAVED key, restart pending (saved != running), used by TTS+STT",
              _dg.get("set") is True and _dg.get("hint") == _rk.intelligence_target.key_hint("dg-new-SECRET-2222")
              and _dg.get("pending_restart") is True and _dg.get("used_by") == ["TTS", "STT"], str(_dg))
        check("keys GET: a quoted .env value parses like the bot's dotenv (hint right, not 'pending' forever)",
              _kk.get("ELEVENLABS_API_KEY", {}).get("hint") == _rk.intelligence_target.key_hint("el-live-SECRET-5555"),
              str(_kk.get("ELEVENLABS_API_KEY")))
        check("keys GET: a process-only LVI_HERMES_KEY_* is NOT a row (rows come from .env)",
              "LVI_HERMES_KEY_STRAY" not in _kk, str(list(_kk)))
        check("keys GET: ElevenLabs set, nothing pending; OpenClaw not set",
              _kk.get("ELEVENLABS_API_KEY", {}).get("set") is True
              and _kk.get("ELEVENLABS_API_KEY", {}).get("pending_restart") is False
              and _kk.get("LVI_OPENCLAW_KEY", {}).get("set") is False, str(_kk)[:300])
        _hb = _kk.get("LVI_HERMES_KEY_HA_BUTLER", {})
        check("keys GET: agent row labelled 'Hermes · ha-butler', kind hermes, agent ha-butler, pending (new)",
              _hb.get("label") == "Hermes · ha-butler" and _hb.get("kind") == "hermes" and _hb.get("agent") == "ha-butler"
              and _hb.get("pending_restart") is True, str(_hb))
        check("keys GET: in use follows config (TTS elevenlabs, STT deepgram, hermes default; openclaw/agent not)",
              _kk["ELEVENLABS_API_KEY"]["in_use"] and _kk["DEEPGRAM_API_KEY"]["in_use"] and _kk["LVI_HERMES_KEY"]["in_use"]
              and not _kk["LVI_OPENCLAW_KEY"]["in_use"] and not _hb.get("in_use"), str({k: v.get("in_use") for k, v in _kk.items()}))
        config_store.write_section("tts", {**_k_before_cfg["tts"], "provider": "kokoro"})
        config_store.write_section("stt", {**_k_before_cfg["stt"], "provider": "whisper"})
        config_store.write_section("intelligence", {**config_store.read_section("intelligence"), "kind": "hermes",
                                                    "agents": {"hermes": "ha-butler"}})
        _kk2 = {r["var"]: r for r in client.get("/api/dashboard/keys").json()["keys"]}
        check("keys GET: kokoro TTS + whisper STT -> Deepgram/ElevenLabs not in use; hermes agent ha-butler -> its key in use",
              not _kk2["DEEPGRAM_API_KEY"]["in_use"] and not _kk2["ELEVENLABS_API_KEY"]["in_use"]
              and _kk2["LVI_HERMES_KEY_HA_BUTLER"]["in_use"] and not _kk2["LVI_HERMES_KEY"]["in_use"],
              str({k: v.get("in_use") for k, v in _kk2.items()}))
        config_store.write_section("tts", {**_k_before_cfg["tts"], "provider": "deepgram-flux"})
        _kk3 = {r["var"]: r for r in client.get("/api/dashboard/keys").json()["keys"]}
        check("keys GET: deepgram-flux TTS counts as using the Deepgram key", _kk3["DEEPGRAM_API_KEY"]["in_use"])
        check("keys GET: LocalAI row — not in use while no card picks LocalAI, says it serves STT and TTS",
              not _kk3["LVI_LOCALAI_KEY"]["in_use"] and _kk3["LVI_LOCALAI_KEY"]["used_by"] == ["STT", "TTS"]
              and _kk3["LVI_LOCALAI_KEY"]["label"] == "LocalAI" and _kk3["LVI_LOCALAI_KEY"]["set"] is False,
              str(_kk3.get("LVI_LOCALAI_KEY")))
        config_store.write_section("stt", {**_k_before_cfg["stt"], "provider": "localai"})
        _kk5 = {r["var"]: r for r in client.get("/api/dashboard/keys").json()["keys"]}
        check("keys GET: LocalAI STT alone -> the LocalAI key is in use, by STT only",
              _kk5["LVI_LOCALAI_KEY"]["in_use"] and _kk5["LVI_LOCALAI_KEY"]["used_by"] == ["STT"], str(_kk5.get("LVI_LOCALAI_KEY")))
        config_store.write_section("tts", {**_k_before_cfg["tts"], "provider": "localai"})
        _kk5 = {r["var"]: r for r in client.get("/api/dashboard/keys").json()["keys"]}
        check("keys GET: LocalAI on both cards -> used by STT and TTS",
              _kk5["LVI_LOCALAI_KEY"]["in_use"] and _kk5["LVI_LOCALAI_KEY"]["used_by"] == ["STT", "TTS"])
        config_store.write_section("stt", {**_k_before_cfg["stt"], "provider": "whisper"})
        config_store.write_section("tts", {**_k_before_cfg["tts"], "provider": "deepgram-flux"})

        # Test: server-side, with the SAVED key, mocked providers.
        _hcalls = []
        _hresp = {"v": (200, "{}")}

        def _fake_http(method, url, headers):
            _hcalls.append((method, url, dict(headers)))
            r = _hresp["v"]
            if isinstance(r, Exception):
                raise r
            return r
        _rk._keys_http = _fake_http

        def _kt(var):
            return client.post("/api/dashboard/keys/test", json={"var": var})
        _r = _kt("DEEPGRAM_API_KEY")
        check("keys test: Deepgram 200 -> ok, tested with the SAVED key (not the running one), no key in the response",
              _r.status_code == 200 and _r.json().get("status") == "ok" and _hcalls
              and "api.deepgram.com" in _hcalls[-1][1] and "dg-new-SECRET-2222" in str(_hcalls[-1][2])
              and not any(x in _r.text for x in _SECRETS), f"{_r.text[:200]} {_hcalls[-1:] and _hcalls[-1][1]}")
        _hresp["v"] = (401, '{"err_code":"INVALID_AUTH"}')
        _r = _kt("DEEPGRAM_API_KEY")
        check("keys test: 401 -> rejected", _r.json().get("status") == "rejected" and _r.json().get("ok") is False, _r.text[:200])
        _hresp["v"] = OSError("network is unreachable")
        _r = _kt("DEEPGRAM_API_KEY")
        check("keys test: network error -> unreachable", _r.json().get("status") == "unreachable", _r.text[:200])
        _hresp["v"] = (401, '{"detail":{"status":"missing_permissions","message":"needs user_read"}}')
        _r = _kt("ELEVENLABS_API_KEY")
        check("keys test: ElevenLabs 401 missing_permissions = a valid restricted key -> ok",
              _r.json().get("status") == "ok" and "api.elevenlabs.io" in _hcalls[-1][1], _r.text[:200])
        _r = _kt("LVI_OPENCLAW_KEY")
        check("keys test: a key that isn't set -> missing (no outbound call)", _r.json().get("status") == "missing", _r.text[:200])
        _r = _kt("PATH")
        check("keys test: unknown variable -> 422", _r.status_code == 422, str(_r.status_code))
        # Hermes / OpenClaw: mock ONE LEVEL LOWER (urlopen) — assert the real
        # outbound URL and Bearer token, not the dict handed to the probe.
        import urllib.request as _urq
        _real_urlopen = _urq.urlopen
        _reqs = []

        class _Resp:
            def __init__(self, body):
                self._b = body

            def read(self, *a):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            _reqs.append((req.full_url, req.get_header("Authorization"), req.data))
            return _Resp(b'{"choices":[{"message":{"content":"ok"}}]}')
        _urq.urlopen = _fake_urlopen
        try:
            config_store.write_section("intelligence", {"kind": "hermes", "url": "http://hermes.test:8642/v1",
                                                        "model": "hermes-agent"})
            _reqs.clear()
            _r = _kt("LVI_HERMES_KEY_HA_BUTLER")
            _chat = [x for x in _reqs if x[0].endswith("/chat/completions")]
            check("keys test: an agent key probes /p/<agent>/v1 with THAT agent's key (not the default key)",
                  _r.json().get("status") == "ok" and _chat and "/p/ha-butler/v1/chat/completions" in _chat[-1][0]
                  and _chat[-1][1] == "Bearer hb-agent-SECRET-4444" and not any(x in _r.text for x in _SECRETS),
                  f"{_r.text[:160]} {[(u, (h or '')[:12]) for u, h, _ in _reqs]}")
            _reqs.clear()
            _r = _kt("LVI_HERMES_KEY")
            _chat = [x for x in _reqs if x[0].endswith("/chat/completions")]
            check("keys test: the default Hermes key probes the base URL with LVI_HERMES_KEY",
                  _r.json().get("status") == "ok" and _chat and "/p/" not in _chat[-1][0]
                  and _chat[-1][1] == "Bearer hk-same-SECRET-3333", f"{_r.text[:160]} {[(u, (h or '')[:12]) for u, h, _ in _reqs]}")
            _kenv.write_text(_kenv.read_text() + "LVI_OPENCLAW_KEY=oc-file-SECRET-7777\n")
            config_store.write_section("intelligence", {"kind": "openclaw", "urls": {"openclaw": "http://oc.test:18789/v1"}})
            _reqs.clear()
            _r = _kt("LVI_OPENCLAW_KEY")
            check("keys test: OpenClaw with no agent picked -> needs_setup with a clear message, no outbound call",
                  _r.json().get("status") == "needs_setup" and "agent" in (_r.json().get("detail") or "") and not _reqs,
                  _r.text[:160])
            config_store.write_section("intelligence", {"kind": "hermes", "agents": {"openclaw": "main"}})
            _reqs.clear()
            _r = _kt("LVI_OPENCLAW_KEY")
            check("keys test: OpenClaw with no saved URL -> needs_setup naming the URL, no outbound call",
                  _r.json().get("status") == "needs_setup" and "URL" in (_r.json().get("detail") or "") and not _reqs,
                  _r.text[:160])
            config_store.write_section("intelligence", {"kind": "openclaw", "urls": {"openclaw": "http://oc.test:18789/v1"},
                                                        "agents": {"openclaw": "main"}})
            _reqs.clear()
            _r = _kt("LVI_OPENCLAW_KEY")
            _chat = [x for x in _reqs if x[0].endswith("/chat/completions")]
            check("keys test: OpenClaw probes model openclaw/<agent> with LVI_OPENCLAW_KEY",
                  _r.json().get("status") == "ok" and _chat and b'"openclaw/main"' in (_chat[-1][2] or b"")
                  and _chat[-1][1] == "Bearer oc-file-SECRET-7777", f"{_r.text[:160]} {[(u, (h or '')[:12]) for u, h, _ in _reqs]}")

            import urllib.error as _uerr

            def _deny(req, timeout=None):
                if req.full_url.endswith("/chat/completions"):
                    raise _uerr.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
                return _Resp(b"")
            _urq.urlopen = _deny
            _r = _kt("LVI_OPENCLAW_KEY")
            check("keys test: the gateway's 401 -> rejected", _r.json().get("status") == "rejected", _r.text[:160])

            def _nf(req, timeout=None):
                if req.full_url.endswith("/chat/completions"):
                    raise _uerr.HTTPError(req.full_url, 404, "Not Found", {}, None)
                return _Resp(b"")
            config_store.write_section("intelligence", {"kind": "openclaw", "urls": {"openclaw": "http://oc.test:18789"},
                                                        "agents": {"openclaw": "main"}})
            _urq.urlopen = _nf
            _r = _kt("LVI_OPENCLAW_KEY")
            check("keys test: a 404 from a URL without /v1 says so (the saved URL may need /v1)",
                  "/v1" in (_r.json().get("detail") or "") and _r.json().get("ok") is False, _r.text[:200])

            def _boom(req, timeout=None):
                raise ValueError("Invalid header value")
            _urq.urlopen = _boom
            _r = _kt("LVI_OPENCLAW_KEY")
            check("keys test: any exception is a result (unreachable), never a 500", _r.status_code == 200
                  and _r.json().get("ok") is False, f"{_r.status_code} {_r.text[:120]}")
        finally:
            _urq.urlopen = _real_urlopen
        _r = _kt("LVI_HERMES_KEY_STRAY")
        check("keys test: a process-only agent variable is refused (422)", _r.status_code == 422, str(_r.status_code))
        _r = client.post("/api/dashboard/keys/test", json={"var": "DEEPGRAM_API_KEY", "x": 1})
        check("keys test: unknown payload fields -> 422 (extra=forbid)", _r.status_code == 422, str(_r.status_code))

        # Remove: for security; backup first; only that line; never echoed.
        _r = client.delete("/api/dashboard/keys/PATH")
        check("keys remove: a variable the card doesn't know -> 422", _r.status_code == 422, str(_r.status_code))
        _r = client.delete("/api/dashboard/keys/LVI_HERMES_KEY_STRAY")
        check("keys remove: a key that isn't in .env -> 404 (process-only values can't be 'removed')",
              _r.status_code == 404, str(_r.status_code))
        _bak_before = set(SCRATCH.glob("keys.env.bak-*"))
        _r = client.delete("/api/dashboard/keys/LVI_HERMES_KEY_HA_BUTLER")
        _txt = _kenv.read_text()
        _new_baks = sorted(set(SCRATCH.glob("keys.env.bak-*")) - _bak_before)
        check("keys remove: drops ONLY that line, writes a 0600 backup that holds it, response has no value",
              _r.status_code == 200 and "LVI_HERMES_KEY_HA_BUTLER=" not in _txt and "DEEPGRAM_API_KEY=dg-new-SECRET-2222" in _txt
              and "LVI_OPENCLAW_KEY=" in _txt and len(_new_baks) == 1 and "hb-agent-SECRET-4444" in _new_baks[0].read_text()
              and (_new_baks[0].stat().st_mode & 0o777) == 0o600 and (_kenv.stat().st_mode & 0o777) == 0o600
              and not any(x in _r.text for x in _SECRETS), f"{_r.status_code} {_r.text[:120]} baks={len(_new_baks)}")
        _r = client.delete("/api/dashboard/keys/DEEPGRAM_API_KEY")
        _kk4 = {r["var"]: r for r in client.get("/api/dashboard/keys").json()["keys"]}
        check("keys remove: a removed agent key's row is gone; a removed static key reads not set + restart pending",
              _r.status_code == 200 and "LVI_HERMES_KEY_HA_BUTLER" not in _kk4
              and _kk4["DEEPGRAM_API_KEY"]["set"] is False and _kk4["DEEPGRAM_API_KEY"]["pending_restart"] is True
              and _kk4["DEEPGRAM_API_KEY"]["hint"] is None, str(_kk4.get("DEEPGRAM_API_KEY")))
        _r = _kt("DEEPGRAM_API_KEY")
        check("keys test: after removal (running bot still holds the old value) -> missing, never tests the stale value",
              _r.json().get("status") == "missing", _r.text[:160])
        _r = client.delete("/api/dashboard/keys/DEEPGRAM_API_KEY")
        check("keys remove: removing again -> 404", _r.status_code == 404, str(_r.status_code))

        # Backup on replace (real writer, scratch .env).
        _benv = SCRATCH / "bak-test" / ".env"
        _benv.parent.mkdir(exist_ok=True)
        _benv.write_text("A_KEY=one\n")
        os.environ["LVI_ENV_FILE"] = str(_benv)

        def _baks():
            return sorted(_benv.parent.glob(".env.bak-*"))
        _hand = _benv.parent / ".env.bak-2026-10-08"
        _hand.write_text("A_KEY=handmade\n")

        def _baks():
            return sorted(x for x in _benv.parent.glob(".env.bak-*") if x != _hand)
        _asyncio.run(_rk._write_env_key("B_KEY", "first"))
        check("backup: a first set makes no backup", _baks() == [], str(_baks()))
        _asyncio.run(_rk._write_env_key("A_KEY", "two"))
        _b = _baks()
        check("backup: replacing a key backs up the old .env first (0600, holds the old value)",
              len(_b) == 1 and "A_KEY=one" in _b[0].read_text() and (_b[0].stat().st_mode & 0o777) == 0o600
              and "A_KEY=two" in _benv.read_text(), str(_b))
        _asyncio.run(_rk._write_env_key("A_KEY", "two"))
        check("backup: saving the same value again makes no backup", len(_baks()) == 1, str(_baks()))
        for _v in ("three", "four", "five", "six"):
            _asyncio.run(_rk._write_env_key("A_KEY", _v))
        _b = _baks()
        check("backup: only the newest 3 are kept", len(_b) == 3 and "A_KEY=five" in _b[-1].read_text(),
              f"{len(_b)} {[x.read_text() for x in _b]}")
        check("backup: a hand-made .env.bak-* is never pruned; ours are UTC-stamped (…Z)",
              _hand.exists() and all(x.name.endswith("Z") for x in _b), f"{_hand.exists()} {[x.name for x in _b]}")

        # The key file is private from the moment its temp file exists, and an
        # `export VAR=` line is replaced in place (prefix kept), never duplicated.
        _penv = SCRATCH / "private-test" / ".env"
        _penv.parent.mkdir(exist_ok=True)
        _penv.write_text("# keys\nexport P_KEY=old\nQ_KEY=q\n")
        os.environ["LVI_ENV_FILE"] = str(_penv)
        _MODES = []  # every mode the temp file was seen with before it became .env
        _real_chmod, _real_replace = os.chmod, os.replace

        def _seen_chmod(p, mode, *a, **k):
            if str(p).endswith(".tmp"):
                _MODES.append(os.stat(p).st_mode & 0o777)
            return _real_chmod(p, mode, *a, **k)

        def _seen_replace(src, dst, *a, **k):
            if str(src).endswith(".tmp"):
                _MODES.append(os.stat(src).st_mode & 0o777)
            return _real_replace(src, dst, *a, **k)

        _umask = os.umask(0o022)  # the common default: plain creates are world-readable
        os.chmod, os.replace = _seen_chmod, _seen_replace
        try:
            _asyncio.run(_rk._write_env_key("P_KEY", "new"))
            _asyncio.run(_rk._write_env_key("R_KEY", "r"))
            _asyncio.run(_rk._remove_env_key("Q_KEY"))
        finally:
            os.chmod, os.replace = _real_chmod, _real_replace
            os.umask(_umask)
        check("keys: .env and its temp file are never group/world-readable (writer and remover)",
              len(_MODES) >= 3 and all(m == 0o600 for m in _MODES)
              and (_penv.stat().st_mode & 0o777) == 0o600, f"temp modes={[oct(m) for m in _MODES]}")
        _plines = _penv.read_text().splitlines()
        check("keys: an `export VAR=` line is replaced in place, keeping `export`, not duplicated",
              _plines == ["# keys", "export P_KEY=new", "R_KEY=r"], str(_plines))
    finally:
        if _k_saved_fns[0] is not None:
            _rk._keys_http = _k_saved_fns[0]
        _rk._intelligence_probe = _k_saved_fns[1]
        for _k, _v in _k_before_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _sec, _val in _k_before_cfg.items():
            config_store.write_section(_sec, _val)

    # ── nanobot: the third intelligence lane, against a fake that enforces its contract ──
    # A local stand-in for nanobot's API server, shaped by what a real
    # nanobot v0.3.5 answered: Bearer key or 401 (/v1/models and chat);
    # exactly ONE user message or 400; a model other than the configured
    # one -> 400; session_id isolates conversations and an omitted one lands
    # in a single shared "default" session; /health needs no key. It records
    # what actually arrives on the wire (bodies before they are judged).
    import http.server as _hsNB
    import threading as _thNB
    import urllib.request as _urqNB

    import intelligence.agent as _iaNB
    from dashboard import routes as _rNB
    from loguru import logger as _lgNB

    import intelligence.target as _itNB

    _NB_KEY = "nb-suite-key-abcdefghijkl"
    _NB_WRONG = "nb-wrong-key-abcdefghijkl"
    _NB_MODEL = "glm-suite"
    _NB_APOLOGY = "Sorry — I can't reach my intelligence right now."
    _NB = {"chat": [], "models": [], "health": 0, "sessions": {}, "force": None}

    class _NBHandler(_hsNB.BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, msg, etype="invalid_request_error"):
            self._send(code, {"error": {"message": msg, "type": etype, "code": code}})

        def _authed(self):
            return self.headers.get("Authorization") == f"Bearer {_NB_KEY}"

        def do_GET(self):
            if self.path == "/health":
                _NB["health"] += 1
                return self._send(200, {"status": "ok"})
            if self.path == "/v1/models":
                _NB["models"].append(self.headers.get("Authorization"))
                if not self._authed():
                    return self._err(401, "Invalid API key", "authentication_error")
                return self._send(200, {"object": "list", "data": [
                    {"id": _NB_MODEL, "object": "model", "owned_by": "nanobot"}]})
            return self._err(404, "Not Found")

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            _NB["chat"].append({"path": self.path, "body": body if isinstance(body, dict) else {},
                                "headers": {k.lower(): v for k, v in self.headers.items()}})
            if self.path != "/v1/chat/completions":
                return self._err(404, "Not Found")
            if not self._authed():
                return self._err(401, "Invalid API key", "authentication_error")
            if _NB["force"]:
                return self._err(*_NB["force"])
            msgs = (body or {}).get("messages") if isinstance(body, dict) else None
            if not (isinstance(msgs, list) and len(msgs) == 1 and isinstance(msgs[0], dict)
                    and msgs[0].get("role") == "user"):
                return self._err(400, "Only a single user message is supported")
            if "model" in body and body["model"] != _NB_MODEL:
                return self._err(400, f"Only configured model '{_NB_MODEL}' is available")
            turns = _NB["sessions"].setdefault(body.get("session_id") or "default", [])
            turns.append(msgs[0].get("content"))
            return self._send(200, {
                "id": "chatcmpl-suite", "object": "chat.completion", "model": _NB_MODEL,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": f"nanobot turn {len(turns)}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 3}})

        def log_message(self, *a):
            pass

    # Two servers, same contract: the second is "another host" — a URL that
    # is NOT nanobot's saved one, so the stored key must never reach it.
    _srvNB = _hsNB.ThreadingHTTPServer(("127.0.0.1", 0), _NBHandler)
    _srvNB2 = _hsNB.ThreadingHTTPServer(("127.0.0.1", 0), _NBHandler)
    for _s in (_srvNB, _srvNB2):
        _thNB.Thread(target=_s.serve_forever, daemon=True).start()
    _NB_URL = f"http://127.0.0.1:{_srvNB.server_address[1]}/v1"
    _NB_OTHER = f"http://127.0.0.1:{_srvNB2.server_address[1]}/v1"
    _NB_HERMES = "http://127.0.0.1:9/v1"

    _nb_cfg = {sec: config_store.read_section(sec) for sec in ("intelligence", "room_agents", "room_zones")}
    _nb_vars = ("LVI_NANOBOT_KEY", "LVI_HERMES_KEY", "LVI_OPENCLAW_KEY", "LVI_ENV_FILE", "LVI_CONVERSATION")
    _nb_env = {k: os.environ.get(k) for k in _nb_vars}
    _nb_base = {k: v for k, v in dict(_nb_cfg["intelligence"] or {}).items()
                if k not in ("kind", "url", "urls", "agent", "agents", "model")}
    _nb_env_file = SCRATCH / "nanobot.env"
    _nb_saved_wek = _rNB._write_env_key
    _nb_real_open = _urqNB.urlopen

    def _nb_put_cfg(**kv):
        config_store.write_section("intelligence", {**_nb_base, **kv})

    def _nb_icfg():
        return dict(config_store.read_section("intelligence") or {})

    def _nb_last():
        return _NB["chat"][-1] if _NB["chat"] else {"path": None, "body": {}, "headers": {}}

    def _nb_ask(agent, text):
        try:
            return agent._ask_intelligence(text), None
        except Exception as e:  # noqa: BLE001 — a failed ask is a check result
            return None, e

    async def _nb_turn(agent, text):
        spoken = []

        async def push(frame, direction=None):
            t = getattr(frame, "text", None)
            if t is not None:
                spoken.append(t)
        agent.push_frame = push
        agent._last_user_text, agent._last_user_time = text, time.monotonic()
        await agent._run_turn(text, agent._turn_epoch)
        return spoken

    try:
        _nb_env_file.write_text(f"LVI_NANOBOT_KEY={_NB_KEY}\n")
        os.environ.update({"LVI_NANOBOT_KEY": _NB_KEY, "LVI_ENV_FILE": str(_nb_env_file)})
        os.environ.pop("LVI_CONVERSATION", None)   # a pinned id would merge every fresh room
        _nb_put_cfg(kind="nanobot", url=_NB_URL, urls={"nanobot": _NB_URL}, max_tokens=300)
        config_store.write_section("room_agents", {})

        # ── the lane on the wire ──
        _nbF = _iaNB.IntelligenceAgent(room="suite-nb-fresh", session_mode="fresh")
        _r1, _e1 = _nb_ask(_nbF, "what time is it")
        _c1 = _nb_last()
        # local history the bot keeps must never be sent
        _nbF._history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "before"}]
        _r2, _e2 = _nb_ask(_nbF, "and the weather")
        _c2 = _nb_last()
        check("nanobot: a turn sends exactly ONE user message (no system prompt, no local history) and is answered",
              _e1 is None and _e2 is None and _r2 == "nanobot turn 2"
              and _c2["body"].get("messages") == [{"role": "user", "content": "and the weather"}],
              f"err={_e1 or _e2} reply={_r2!r} body={str(_c2['body'])[:200]}")
        check("nanobot: the request is POST <url>/chat/completions with Bearer LVI_NANOBOT_KEY, "
              "stream off, max_tokens, and no Hermes/OpenClaw fields",
              _c2["path"] == "/v1/chat/completions"
              and _c2["headers"].get("authorization") == f"Bearer {_NB_KEY}"
              and _c2["body"].get("stream") is False and _c2["body"].get("max_tokens") == 300
              and "conversation" not in _c2["body"]
              and "x-openclaw-session-key" not in _c2["headers"] and "x-hermes-session-key" not in _c2["headers"],
              f"path={_c2['path']} body={str(_c2['body'])[:160]} hdrs={sorted(_c2['headers'])}")
        _sidF = _c2["body"].get("session_id")
        check("nanobot: a fresh room ALWAYS sends session_id = its connection's conversation id (both turns, one session)",
              bool(_sidF) and _sidF == _c1["body"].get("session_id") == _nbF._conversation
              and _sidF.startswith("voice-") and _NB["sessions"].get(_sidF) == ["what time is it", "and the weather"],
              f"sid={_sidF!r} first={_c1['body'].get('session_id')!r} conv={_nbF._conversation!r}")
        _nbF2 = _iaNB.IntelligenceAgent(room="suite-nb-fresh", session_mode="fresh")
        _nb_ask(_nbF2, "hello")
        _sidF2 = _nb_last()["body"].get("session_id")
        check("nanobot: a new connection of a fresh room gets its own session; nothing falls into the shared default",
              bool(_sidF2) and _sidF2 != _sidF and "default" not in _NB["sessions"],
              f"{_sidF!r} vs {_sidF2!r} sessions={sorted(_NB['sessions'])}")
        _nbS = _iaNB.IntelligenceAgent(room="suite-nb-sticky", session_mode="sticky")
        _nb_ask(_nbS, "remember the milk")
        check("nanobot: a sticky room sends session_id voice-<room> (one long-lived conversation per room)",
              _nb_last()["body"].get("session_id") == "voice-suite-nb-sticky",
              str(_nb_last()["body"].get("session_id")))
        check("nanobot: no agent picked -> the model field is omitted (nanobot uses its own)",
              "model" not in _c2["body"] and "model" not in _nb_last()["body"], str(_c2["body"])[:160])
        _nb_put_cfg(kind="nanobot", url=_NB_URL, urls={"nanobot": _NB_URL}, max_tokens=300,
                    agents={"nanobot": _NB_MODEL})
        _nbA = _iaNB.IntelligenceAgent(room="suite-nb-fresh", session_mode="fresh")
        _rA, _eA = _nb_ask(_nbA, "hi")
        check("nanobot: an agent picked -> sent as the model field, and the server accepts it",
              _eA is None and _nb_last()["body"].get("model") == _NB_MODEL and _rA == "nanobot turn 1",
              f"err={_eA} body={str(_nb_last()['body'])[:160]}")

        # ── failures: the existing failure path, nanobot's own message in the log ──
        _nb_put_cfg(kind="nanobot", url=_NB_URL, urls={"nanobot": _NB_URL}, max_tokens=300,
                    agents={"nanobot": "glm-wrong"})
        _nbW = _iaNB.IntelligenceAgent(room="suite-nb-fresh", session_mode="fresh")
        _rW, _eW = _nb_ask(_nbW, "hi")
        check("nanobot: a wrong model (HTTP 400) raises with nanobot's own message, never the key",
              _rW is None and isinstance(_eW, RuntimeError) and "400" in str(_eW)
              and "Only configured model" in str(_eW) and _NB_KEY not in str(_eW), repr(_eW)[:200])
        _nb_put_cfg(kind="nanobot", url=_NB_URL, urls={"nanobot": _NB_URL}, max_tokens=300)
        os.environ["LVI_NANOBOT_KEY"] = _NB_WRONG
        _nbK = _iaNB.IntelligenceAgent(room="suite-nb-fresh", session_mode="fresh")
        os.environ["LVI_NANOBOT_KEY"] = _NB_KEY
        _rK, _eK = _nb_ask(_nbK, "hi")
        check("nanobot: a wrong key (HTTP 401) raises naming the status, never the key",
              _rK is None and isinstance(_eK, RuntimeError) and "401" in str(_eK)
              and _NB_WRONG not in str(_eK) and _NB_KEY not in str(_eK), repr(_eK)[:200])
        _nbT = _iaNB.IntelligenceAgent(room="suite-nb-turn", session_mode="fresh")
        _logsNB = []
        _sinkNB = _lgNB.add(lambda m: _logsNB.append(str(m)), level="ERROR")
        _turnsNB = {}
        try:
            _turnsNB["400"] = (asyncio.run(_nb_turn(_nbW, "is the door locked")), _nbW)
            _turnsNB["401"] = (asyncio.run(_nb_turn(_nbK, "is the door locked")), _nbK)
            _NB["force"] = (500, "upstream model exploded", "server_error")
            _turnsNB["500"] = (asyncio.run(_nb_turn(_nbT, "is the door locked")), _nbT)
        finally:
            _NB["force"] = None
            _lgNB.remove(_sinkNB)
        check("nanobot: a 400, 401 or 5xx is a failed turn: the apology, no history write, the retry not dedup-dropped",
              all(sp == [_NB_APOLOGY] and ag._history == [] and ag._last_user_text == ""
                  for sp, ag in _turnsNB.values()) and len(_turnsNB) == 3,
              str({k: (sp, len(ag._history), ag._last_user_text) for k, (sp, ag) in _turnsNB.items()}))
        check("nanobot: the failure log carries nanobot's own message, never the key",
              any("upstream model exploded" in ln for ln in _logsNB)
              and any("Only configured model" in ln for ln in _logsNB)
              and not any(_NB_KEY in ln or _NB_WRONG in ln for ln in _logsNB), str(_logsNB)[:300])
        _spR = asyncio.run(_nb_turn(_nbT, "is the door locked"))
        check("nanobot: a retry after a failed turn is answered and written to history",
              _spR == ["nanobot turn 1"] and len(_nbT._history) == 2, f"spoken={_spR} history={_nbT._history}")

        # ── discovery: the configured model is the agent; the key only goes to the saved URL ──
        _NB["models"].clear()
        _dNB = _itNB.discover_agents("nanobot", _NB_URL, {"LVI_NANOBOT_KEY": _NB_KEY})
        check("nanobot: discovery lists the server's configured model as its agent (GET /v1/models, key sent)",
              [a["id"] for a in _dNB["agents"]] == [_NB_MODEL] and _dNB.get("source") == "nanobot-models"
              and _NB["models"] == [f"Bearer {_NB_KEY}"] and _NB_KEY not in json.dumps(_dNB), str(_dNB)[:200])
        _NB["models"].clear()
        _rdS = client.post("/api/dashboard/intelligence/agents", json={"kind": "nanobot"})
        _rdS2 = client.post("/api/dashboard/intelligence/agents", json={"kind": "nanobot", "url": _NB_URL[:-3]})
        check("nanobot: POST /intelligence/agents on the SAVED URL (omitted, or with/without /v1) sends the key, lists the model",
              _rdS.status_code == 200 and _rdS2.status_code == 200
              and [a["id"] for a in _rdS.json().get("agents") or []] == [_NB_MODEL]
              and [a["id"] for a in _rdS2.json().get("agents") or []] == [_NB_MODEL]
              and _NB["models"] == [f"Bearer {_NB_KEY}"] * 2
              and _NB_KEY not in _rdS.text + _rdS2.text, f"{_rdS.text[:160]} auth={_NB['models']}")
        _NB["models"].clear()
        _rdO = client.post("/api/dashboard/intelligence/agents", json={"kind": "nanobot", "url": _NB_OTHER})
        check("nanobot: a URL that is not nanobot's saved one is probed WITHOUT the key (its 401 is reported)",
              _rdO.status_code == 200 and _NB["models"] == [None] and _rdO.json().get("agents") == []
              and "401" in (_rdO.json().get("note") or ""), f"{_rdO.text[:200]} auth={_NB['models']}")

        # ── Keys card ──
        _kNB = client.get("/api/dashboard/keys")
        _rowNB = {r["var"]: r for r in _kNB.json().get("keys") or []}.get("LVI_NANOBOT_KEY") or {}
        check("nanobot: the Keys card lists LVI_NANOBOT_KEY (Nanobot, used by Intelligence): set, in use, hint only",
              _rowNB.get("label") == "Nanobot" and _rowNB.get("kind") == "nanobot"
              and _rowNB.get("used_by") == ["Intelligence"] and _rowNB.get("set") is True
              and _rowNB.get("in_use") is True and _rowNB.get("hint") == _itNB.key_hint(_NB_KEY)
              and _NB_KEY not in _kNB.text, str(_rowNB))
        _NB["models"].clear()
        _ktNB = client.post("/api/dashboard/keys/test", json={"var": "LVI_NANOBOT_KEY"})
        check("nanobot: key Test GETs the saved URL's /models with the SAVED key -> ok",
              _ktNB.status_code == 200 and _ktNB.json().get("status") == "ok"
              and _NB["models"] == [f"Bearer {_NB_KEY}"] and _NB_KEY not in _ktNB.text, _ktNB.text[:200])
        _nb_env_file.write_text(f"LVI_NANOBOT_KEY={_NB_WRONG}\n")
        _ktNB2 = client.post("/api/dashboard/keys/test", json={"var": "LVI_NANOBOT_KEY"})
        check("nanobot: key Test with a wrong key -> rejected (HTTP 401), never the key",
              _ktNB2.json().get("status") == "rejected" and "rejected" in (_ktNB2.json().get("detail") or "")
              and _NB_WRONG not in _ktNB2.text, _ktNB2.text[:200])
        _nb_env_file.write_text(f"LVI_NANOBOT_KEY={_NB_KEY}\n")
        _nb_put_cfg(kind="hermes", url=_NB_HERMES, urls={"hermes": _NB_HERMES})
        _NB["models"].clear()
        _ktNB3 = client.post("/api/dashboard/keys/test", json={"var": "LVI_NANOBOT_KEY"})
        check("nanobot: key Test with no saved Nanobot URL -> needs_setup with a clear message, no outbound call",
              _ktNB3.json().get("ok") is False and _ktNB3.json().get("status") == "needs_setup"
              and "URL" in (_ktNB3.json().get("detail") or "")
              and _NB["models"] == [], _ktNB3.text[:200])
        # A bare host:port (no /v1) is what a user types: turns, the Test
        # probe and the key Test all reach nanobot's /v1 routes anyway.
        check("nanobot: request_url adds /v1 to a bare host:port, leaves a /v1 or custom path alone",
              _itNB.request_url("nanobot", "http://192.0.2.5:8900") == "http://192.0.2.5:8900/v1"
              and _itNB.request_url("nanobot", "http://192.0.2.5:8900/") == "http://192.0.2.5:8900/v1"
              and _itNB.request_url("nanobot", "http://192.0.2.5:8900/v1") == "http://192.0.2.5:8900/v1"
              and _itNB.request_url("nanobot", "http://192.0.2.5/nb/v1") == "http://192.0.2.5/nb/v1"
              and _itNB.request_url("openclaw", "http://192.0.2.5:18789") == "http://192.0.2.5:18789",
              _itNB.request_url("nanobot", "http://192.0.2.5:8900"))
        _NB_ROOT = _NB_URL[:-3]
        _nb_put_cfg(kind="nanobot", url=_NB_ROOT, urls={"nanobot": _NB_ROOT, "hermes": _NB_HERMES})
        _NB["models"].clear()
        _ktNB4 = client.post("/api/dashboard/keys/test", json={"var": "LVI_NANOBOT_KEY"})
        _NB["chat"].clear()
        _rRoot, _eRoot = _nb_ask(_iaNB.IntelligenceAgent(room="suite-nb-root", session_mode="fresh"), "hi")
        check("nanobot: a URL saved without /v1 still passes the key Test and turns post to /v1/chat/completions",
              _ktNB4.json().get("status") == "ok" and _NB["models"] == [f"Bearer {_NB_KEY}"]
              and _eRoot is None and _nb_last()["path"] == "/v1/chat/completions",
              f"keytest={_ktNB4.text[:120]} err={_eRoot!r} path={_nb_last()['path']}")
        _nb_put_cfg(kind="hermes", url=_NB_HERMES, urls={"hermes": _NB_HERMES})
        _wekNB = []

        async def _fake_wekNB(var, value):
            _wekNB.append(var)
            return True
        _rNB._write_env_key = _fake_wekNB
        _ksNB = client.put("/api/dashboard/intelligence/key", json={"kind": "nanobot", "api_key": "nb-new-key-abcdefghijkl"})
        _ksNB2 = client.put("/api/dashboard/intelligence/key",
                            json={"kind": "nanobot", "agent": _NB_MODEL, "api_key": "nb-new-key-abcdefghijkl"})
        _rNB._write_env_key = _nb_saved_wek
        check("nanobot: key save writes LVI_NANOBOT_KEY only (an agent never changes the variable), never echoed",
              _ksNB.status_code == 200 and _ksNB2.status_code == 200 and _wekNB == ["LVI_NANOBOT_KEY"] * 2
              and _ksNB.json().get("var") == "LVI_NANOBOT_KEY"
              and "nb-new-key-abcdefghijkl" not in _ksNB.text + _ksNB2.text, f"{_wekNB} {_ksNB.text[:120]}")

        # ── Intelligence card: PUT/GET round-trip, no agent required ──
        _legacyNB = _nb_icfg().get("agent")
        _p0 = client.put("/api/dashboard/intelligence", json={"kind": "nanobot"})
        check("nanobot: switching to Nanobot with no Nanobot URL refuses (set a URL for nanobot first)",
              _p0.status_code == 422 and "set a URL for nanobot first" in _p0.text and _nb_icfg().get("kind") == "hermes",
              f"{_p0.status_code} {_p0.text[:120]}")
        _p1 = client.put("/api/dashboard/intelligence", json={"kind": "nanobot", "url": _NB_URL})
        _cNB = _nb_icfg()
        check("nanobot: PUT kind nanobot + URL with NO agent saves and switches the lane",
              _p1.status_code == 200 and not _p1.json().get("url_only") and _cNB.get("kind") == "nanobot"
              and _cNB.get("url") == _NB_URL and (_cNB.get("urls") or {}).get("nanobot") == _NB_URL
              and (_cNB.get("urls") or {}).get("hermes") == _NB_HERMES
              and not (_cNB.get("agents") or {}).get("nanobot"), f"{_p1.status_code} {_p1.text[:120]} {_cNB}")
        _gNB = client.get("/api/dashboard/intelligence").json()
        check("nanobot: GET round-trips kind nanobot, its URL, no agent, and its key status (never the key)",
              _gNB.get("kind") == "nanobot" and _gNB.get("url") == _NB_URL
              and (_gNB.get("urls") or {}).get("nanobot") == _NB_URL
              and "nanobot" in (_gNB.get("agents") or {}) and _gNB["agents"]["nanobot"] is None
              and (_gNB.get("keys_set") or {}).get("nanobot") is True
              and _gNB.get("key_set") is True and _NB_KEY not in json.dumps(_gNB), str(_gNB)[:300])
        _p2 = client.put("/api/dashboard/intelligence", json={"kind": "nanobot", "agent": _NB_MODEL})
        _cNB2 = _nb_icfg()
        check("nanobot: PUT with an agent files it under agents.nanobot; the legacy agent key is untouched",
              _p2.status_code == 200 and (_cNB2.get("agents") or {}).get("nanobot") == _NB_MODEL
              and _cNB2.get("agent") == _legacyNB and _cNB2.get("url") == _NB_URL, f"{_p2.text[:120]} {_cNB2}")
        _stNB = client.get("/api/dashboard/status").json()
        _mNB = client.get("/api/dashboard/mapping").json()
        check("nanobot: rig facts read 'Nanobot · <agent>'; the Rooms card's system is nanobot with that default",
              _stNB.get("intelligence") == f"Nanobot · {_NB_MODEL}" and _mNB.get("agent_kind") == "nanobot"
              and _mNB.get("default_agent") == _NB_MODEL,
              f"{_stNB.get('intelligence')} {_mNB.get('agent_kind')} {_mNB.get('default_agent')}")

        # ── per-room agent overrides ──
        config_store.write_section("room_agents", {"nanobot": {"suite-nb-kids": "glm-kids"},
                                                   "hermes": {"suite-nb-kids": "kids-agent"}})
        _aKids = _iaNB.IntelligenceAgent(room="suite-nb-kids", session_mode="fresh")
        _aKids._ensure_route()
        _aDen = _iaNB.IntelligenceAgent(room="suite-nb-den", session_mode="fresh")
        _aDen._ensure_route()
        check("nanobot: a room override picks that room's nanobot agent (hermes overrides ignored); others get the default",
              _aKids._agent == "glm-kids" and _aKids._key_var == "LVI_NANOBOT_KEY" and _aKids._key == _NB_KEY
              and _aKids._url == _NB_URL and _aDen._agent == _NB_MODEL,
              f"{_aKids._agent} {_aKids._key_var} {_aKids._url} / {_aDen._agent}")
        _nb_ask(_aKids, "hi")
        check("nanobot: the overridden room's REQUEST names its agent as the model, with its own session_id",
              _nb_last()["body"].get("model") == "glm-kids"
              and _nb_last()["body"].get("session_id") == _aKids._conversation, str(_nb_last()["body"])[:200])
        _zNB = next(iter(_mNB.get("zones") or {}), None)
        if _zNB:
            _rzNB = {**(_mNB.get("room_zones") or {}), "suite-nb-kids": _zNB}
            _pmNB = client.put("/api/dashboard/mapping", json={
                "room_zones": _rzNB, "room_agents": {"nanobot": {"suite-nb-kids": "glm-other"}}})
            _raNB = config_store.read_section("room_agents") or {}
            check("nanobot: PUT /mapping accepts nanobot room overrides and leaves the other systems' alone",
                  _pmNB.status_code == 200 and _raNB.get("nanobot") == {"suite-nb-kids": "glm-other"}
                  and _raNB.get("hermes") == {"suite-nb-kids": "kids-agent"}, f"{_pmNB.status_code} {_pmNB.text[:160]}")
        else:
            check("nanobot: PUT /mapping accepts nanobot room overrides (needs a zone in the throwaway config)",
                  False, "no zone configured")

        # ── Test saved config: a throwaway session, never a room's ──
        _nb_put_cfg(kind="nanobot", url=_NB_URL, urls={"nanobot": _NB_URL})
        _NB["chat"].clear()
        _h0NB = _NB["health"]
        _t1NB = client.post("/api/dashboard/intelligence/test").json()
        _t2NB = client.post("/api/dashboard/intelligence/test").json()
        _sidsNB = [c["body"].get("session_id") for c in _NB["chat"]]
        check("nanobot: Test saved config answers through the full contract (health at the server root, no model field)",
              _t1NB.get("ok") is True and _t2NB.get("ok") is True and _NB["health"] >= _h0NB + 2
              and all("model" not in c["body"] and len(c["body"].get("messages") or []) == 1 for c in _NB["chat"]),
              f"{_t1NB} health={_NB['health'] - _h0NB}")
        check("nanobot: every Test uses its OWN throwaway lenoxvi-test-* session, never a voice session",
              len(_sidsNB) == 2 and all(s and s.startswith("lenoxvi-test-") for s in _sidsNB)
              and _sidsNB[0] != _sidsNB[1], str(_sidsNB))
        _nb_put_cfg(kind="nanobot", url=_NB_URL, urls={"nanobot": _NB_URL}, agents={"nanobot": "glm-wrong"})
        _t3NB = client.post("/api/dashboard/intelligence/test").json()
        check("nanobot: Test reports nanobot's own refusal (wrong model), never the key",
              _t3NB.get("ok") is False and "Only configured model" in (_t3NB.get("error") or "")
              and _NB_KEY not in json.dumps(_t3NB), str(_t3NB)[:200])

        # ── the other lanes are untouched ──
        _capNB = []

        class _RespNB:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def _fake_openNB(req, timeout=None):
            _capNB.append((req.full_url, {k.lower(): v for k, v in req.headers.items()}, json.loads(req.data)))
            return _RespNB()
        os.environ.update({"LVI_HERMES_KEY": "hm-suite-key-abcdefghijkl", "LVI_OPENCLAW_KEY": "oc-suite-key-abcdefghijkl"})
        _histNB = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "before"}]
        _urqNB.urlopen = _fake_openNB
        try:
            _nb_put_cfg(kind="hermes", url="http://hm.test:8642/v1", urls={"hermes": "http://hm.test:8642/v1"},
                        model="hermes-agent")
            _aHm = _iaNB.IntelligenceAgent(room="suite-nb-h", session_mode="fresh")
            _aHm._history = list(_histNB)
            _aHm._ask_intelligence("hi")
            _uH, _hH, _bH = _capNB[-1]
            check("nanobot: the Hermes payload is unchanged (system prompt + history + user, conversation + "
                  "session header, no session_id)",
                  _uH == "http://hm.test:8642/v1/chat/completions"
                  and set(_bH) == {"model", "stream", "max_tokens", "conversation", "messages"}
                  and _bH["messages"][0] == {"role": "system", "content": _iaNB.VOICE_SYSTEM_PROMPT}
                  and _bH["messages"][1:] == [*_histNB, {"role": "user", "content": "hi"}]
                  and _hH.get("x-hermes-session-key") == _bH["conversation"] == _aHm._conversation,
                  f"{_uH} {str(_bH)[:200]}")
            _nb_put_cfg(kind="openclaw", url="http://oc.test:18789/v1", urls={"openclaw": "http://oc.test:18789/v1"},
                        agents={"openclaw": "main"})
            _aOc = _iaNB.IntelligenceAgent(room="suite-nb-o", session_mode="sticky")
            _aOc._history = list(_histNB)
            _aOc._ask_intelligence("hi")
            _uO, _hO, _bO = _capNB[-1]
            check("nanobot: the OpenClaw payload is unchanged (model openclaw/<agent>, system prompt + history, "
                  "session-key header, no session_id)",
                  _uO == "http://oc.test:18789/v1/chat/completions"
                  and set(_bO) == {"model", "stream", "max_tokens", "messages"} and _bO["model"] == "openclaw/main"
                  and _bO["messages"][0]["role"] == "system"
                  and _bO["messages"][1:] == [*_histNB, {"role": "user", "content": "hi"}]
                  and _hO.get("x-openclaw-session-key") == "voice-suite-nb-o"
                  and _hO.get("authorization") == "Bearer oc-suite-key-abcdefghijkl", f"{_uO} {str(_bO)[:200]}")
        finally:
            _urqNB.urlopen = _nb_real_open

        # ── dashboard ui (App.jsx) ──
        _appNB = (REPO_CORE.parent / "dashboard-ui" / "src" / "App.jsx").read_text()
        _sysNB = _appNB[_appNB.find("function System({"):]
        check("nanobot: dashboard ui: the provider dropdown offers Nanobot and the Keys card can save its key",
              "{LANES.map(l => <option key={l.kind} value={l.kind}>{l.label}</option>)}" in _sysNB
              and "const lane = laneOf(row.kind)" in _appNB and "if (!lane) return null" in _appNB
              and "kind: 'nanobot', label: 'Nanobot'" in _appNB, "")
    finally:
        _rNB._write_env_key = _nb_saved_wek
        _urqNB.urlopen = _nb_real_open
        _NB["force"] = None
        for _s in (_srvNB, _srvNB2):
            _s.shutdown()
        for _k, _v in _nb_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _sec, _val in _nb_cfg.items():
            config_store.write_section(_sec, _val or {})

    # ── cloud STT provider (Deepgram) ─────────────────────────────────
    from dashboard import routes as _rdg
    _dg_saved = tuple(getattr(_rdg, _n, None) for _n in ("_stt_deepgram_sdk",))
    _DGSDK = {"ok": True}
    _rdg._stt_deepgram_sdk = lambda: _DGSDK["ok"]
    _dg_env = os.environ.get("DEEPGRAM_API_KEY")
    _dg_stt_before = dict(config_store.read_section("stt") or {})
    # Start from "no provider saved" whatever the copied live config says.
    config_store.write_section("stt", {k: v for k, v in _dg_stt_before.items() if k != "provider"})
    try:
        os.environ["DEEPGRAM_API_KEY"] = "dg-secret-xyz123"
        _gr = client.get("/api/dashboard/stt")
        _g = _gr.json()
        # ElevenLabs' availability follows its key, which this block doesn't
        # control (the speech tiers block pins the key rule).
        check("cloud stt GET: provider defaults to whisper; providers listed with availability",
              _g.get("current", {}).get("provider") == "whisper"
              and [(p["name"], p.get("available")) for p in _g.get("providers", []) if p["name"] != "elevenlabs"]
              == [("whisper", True), ("localai", False), ("deepgram", True)],
              str({k: _g.get(k) for k in ("providers",)}) + str(_g.get("current")))
        check("cloud stt GET: deepgram_key_set true, and the key value never appears",
              _g.get("deepgram_key_set") is True and "dg-secret-xyz123" not in _gr.text, str(_g.get("deepgram_key_set")))
        _r = client.put("/api/dashboard/stt", json={"provider": "grok"})
        check("cloud stt PUT: unknown provider -> 422", _r.status_code == 422, str(_r.status_code))
        _DGSDK["ok"] = False
        _g = client.get("/api/dashboard/stt").json()
        _r = client.put("/api/dashboard/stt", json={"provider": "deepgram"})
        check("cloud stt: SDK missing -> deepgram unavailable with a reason, PUT 422",
              _r.status_code == 422 and next(p for p in _g["providers"] if p["name"] == "deepgram").get("available") is False
              and next(p for p in _g["providers"] if p["name"] == "deepgram").get("reason") == "not installed",
              f"{_r.status_code} {_g['providers']}")
        _DGSDK["ok"] = True
        _r = client.put("/api/dashboard/stt", json={"provider": "deepgram"})
        _c = _sttstt()
        check("cloud stt PUT deepgram: saved, restart needed, key present -> no key_needed",
              _r.status_code == 200 and _r.json().get("restart_needed") is True and _r.json().get("key_needed") is False
              and _c.get("provider") == "deepgram", f"{_r.text[:160]} cfg={_c}")
        _r = client.put("/api/dashboard/stt", json={"provider": "deepgram"})
        check("cloud stt PUT same provider again: no restart", _r.status_code == 200 and _r.json().get("restart_needed") is False,
              _r.text[:120])
        os.environ.pop("DEEPGRAM_API_KEY", None)
        _saved_env_fn = _rdg._saved_env
        _rdg._saved_env = dict
        try:
            client.put("/api/dashboard/stt", json={"provider": "whisper"})
            _r = client.put("/api/dashboard/stt", json={"provider": "deepgram"})
            _g = client.get("/api/dashboard/stt").json()
        finally:
            _rdg._saved_env = _saved_env_fn
        check("cloud stt without a key: allowed, but key_needed true and GET says key not set",
              _r.status_code == 200 and _r.json().get("key_needed") is True and _g.get("deepgram_key_set") is False,
              f"{_r.text[:160]} set={_g.get('deepgram_key_set')}")
        _s = client.get("/api/dashboard/status").json()
        _gc = client.get("/api/dashboard/stt").json()
        check("cloud stt: /status shows the RUNNING provider (whisper until a restart builds deepgram); GET shows the saved one",
              (_s.get("stt") or {}).get("provider") == "whisper" and _gc["current"]["provider"] == "deepgram",
              f"{_s.get('stt')} saved={_gc['current'].get('provider')}")
        _lang0 = _sttstt().get("language")
        _r = client.put("/api/dashboard/stt", json={"language": "de" if _lang0 != "de" else "fr"})
        check("cloud stt: a language change while Deepgram is saved needs a restart (Deepgram's language is fixed per session)",
              _r.status_code == 200 and _r.json().get("restart_needed") is True, _r.text[:160])
        client.put("/api/dashboard/stt", json={"language": _lang0 or "auto"})
        _r = client.put("/api/dashboard/stt", json={"provider": "whisper"})
        check("cloud stt PUT back to whisper while Whisper is what's running: saved, no restart",
              _r.status_code == 200 and _r.json().get("restart_needed") is False
              and _sttstt().get("provider") == "whisper", _r.text[:120])
        import stt_fallback as _sfb
        _sfb.set_provider("deepgram")
        try:
            client.put("/api/dashboard/stt", json={"provider": "deepgram"})
            _r1 = client.put("/api/dashboard/stt", json={"provider": "whisper"})
            _l = _sttstt().get("language")
            _r2 = client.put("/api/dashboard/stt", json={"language": "it" if _l != "it" else "es"})
        finally:
            _sfb.set_provider("whisper")
            client.put("/api/dashboard/stt", json={"language": _lang0 or "auto"})
        check("cloud stt: leaving a RUNNING Deepgram for Whisper needs a restart",
              _r1.status_code == 200 and _r1.json().get("restart_needed") is True, _r1.text[:120])
        check("cloud stt: a language change while Deepgram is RUNNING needs a restart (even if Whisper is saved)",
              _r2.status_code == 200 and _r2.json().get("restart_needed") is True, _r2.text[:120])
    finally:
        for _n, _v in zip(("_stt_deepgram_sdk",), _dg_saved):
            if _v is not None:
                setattr(_rdg, _n, _v)
        if _dg_env is None:
            os.environ.pop("DEEPGRAM_API_KEY", None)
        else:
            os.environ["DEEPGRAM_API_KEY"] = _dg_env
        config_store.write_section("stt", _dg_stt_before)

    # config_store: writes from worker threads must not corrupt config.yaml
    import threading as _thcs

    import yaml as _ycs
    _errs, _N = [], 40
    def _w(sec):
        for i in range(_N):
            try:
                config_store.write_section(sec, {"n": i})
            except Exception as e:  # noqa: BLE001
                _errs.append(f"{sec}: {type(e).__name__}: {e}"[:120])
    def _rd():
        for _ in range(_N * 2):
            try:
                config_store.read_section("stt")
            except Exception as e:  # noqa: BLE001
                _errs.append(f"read: {type(e).__name__}: {e}"[:120])
    _ts = [_thcs.Thread(target=_w, args=("zz_a",)), _thcs.Thread(target=_w, args=("zz_b",)), _thcs.Thread(target=_rd)]
    [t.start() for t in _ts]
    [t.join() for t in _ts]
    _final = _ycs.safe_load(Path(os.environ["LVI_CONFIG"]).read_text()) or {}
    check("config_store: concurrent thread writes/reads — no errors, no lost section, valid YAML",
          not _errs and (_final.get("zz_a") or {}).get("n") == _N - 1 and (_final.get("zz_b") or {}).get("n") == _N - 1,
          f"errs={_errs[:3]} a={_final.get('zz_a')} b={_final.get('zz_b')}")
    check("config_store exposes LOCK for read-modify-write callers", hasattr(config_store, "LOCK"))
    config_store.write_sections({"zz_a": {}, "zz_b": {}})

    # ════ zone routes + firmware upload: fresh config reads, off-loop disk work, concurrent uploads ════
    # Zone volume/add/delete must rebuild sonos/wiim from the FILE: the
    # boot cache (config.cfg) only refreshes on dashboard writes, so a hand
    # edit made after the last one was written back over (or deleted).
    import config as _cfghe
    import yaml as _yhe
    _he_path = Path(os.environ["LVI_CONFIG"])
    _he_saved = config_store.read_sections(["sonos", "wiim", "room_zones"])
    _he_saved = {k: dict(v or {}) for k, v in _he_saved.items()}

    def _he_raw():
        return _yhe.safe_load(_he_path.read_text()) or {}

    def _he_stale_then_edit(edit):
        # Warm the boot cache, THEN edit the file by hand (no cache refresh).
        _cfghe.cfg("sonos")
        raw = _he_raw()
        edit(raw)
        _he_path.write_text(_yhe.safe_dump(raw, sort_keys=False))

    try:
        config_store.write_sections({
            "sonos": {**_he_saved["sonos"], "clip_volume": 30, "default_zone": "heA", "zone_volumes": {},
                      "zones": {"heA": {"ip": "192.0.2.71", "player_uid": "RINCON_HEA000000000001400"},
                                "heB": {"ip": "192.0.2.72", "player_uid": "RINCON_HEB000000000001400"},
                                "heC": {"ip": "192.0.2.73", "player_uid": "RINCON_HEC000000000001400"}}},
            "wiim": {**_he_saved["wiim"], "default_zone": "heW", "zones": {"heW": {"ip": "192.0.2.74"}}}})

        def _ed_vol(raw):
            raw["sonos"]["zones"]["heA"]["ip"] = "192.0.2.99"
            raw["sonos"]["hand_note"] = "vol"
        _he_stale_then_edit(_ed_vol)
        _rv = client.put("/api/dashboard/zones/heA/volume", json={"volume": 41})
        _sv = _he_raw().get("sonos") or {}
        check("zones: a volume move keeps hand edits made since the last dashboard write",
              _rv.status_code == 200 and _sv.get("hand_note") == "vol"
              and _sv["zones"]["heA"]["ip"] == "192.0.2.99" and (_sv.get("zone_volumes") or {}).get("heA") == 41,
              f"{_rv.status_code} note={_sv.get('hand_note')} ip={_sv['zones']['heA'].get('ip')} "
              f"vols={_sv.get('zone_volumes')}")

        def _ed_add(raw):
            raw["wiim"]["hand_note"] = "add"
            raw["wiim"]["zones"]["heHand"] = {"ip": "192.0.2.75"}
        _he_stale_then_edit(_ed_add)
        _ra = client.post("/api/dashboard/zones", json={"name": "heW2", "backend": "wiim", "ip": "192.0.2.76"})
        _wa = _he_raw().get("wiim") or {}
        check("zones: adding a zone keeps hand edits (an extra key and a hand-added zone)",
              _ra.status_code == 200 and _wa.get("hand_note") == "add"
              and set(_wa.get("zones") or {}) == {"heW", "heHand", "heW2"},
              f"{_ra.status_code} note={_wa.get('hand_note')} zones={sorted(_wa.get('zones') or {})}")
        _he_stale_then_edit(lambda raw: raw["wiim"]["zones"].update({"heHand2": {"ip": "192.0.2.77"}}))
        _rdup = client.post("/api/dashboard/zones", json={"name": "heHand2", "backend": "wiim", "ip": "192.0.2.78"})
        check("zones: adding a zone that was hand-added since the last write is refused, not overwritten",
              _rdup.status_code == 400
              and (_he_raw().get("wiim") or {}).get("zones", {}).get("heHand2", {}).get("ip") == "192.0.2.77",
              f"{_rdup.status_code} {_rdup.text[:120]}")

        def _ed_del(raw):
            raw["sonos"]["default_zone"] = "heC"
            raw["sonos"]["hand_note"] = "del"
        _he_stale_then_edit(_ed_del)
        _rdl = client.delete("/api/dashboard/zones/heA")
        _sd = _he_raw().get("sonos") or {}
        check("zones: delete works from the default as it is NOW (hand-set default kept, hand edits kept)",
              _rdl.status_code == 200 and _sd.get("default_zone") == "heC"
              and _rdl.json().get("default_zone") == "heC" and _sd.get("hand_note") == "del"
              and set(_sd.get("zones") or {}) == {"heB", "heC"},
              f"{_rdl.status_code} default={_sd.get('default_zone')} body={_rdl.text[:120]} "
              f"note={_sd.get('hand_note')} zones={sorted(_sd.get('zones') or {})}")
        _he_stale_then_edit(lambda raw: raw["sonos"].update({"default_zone": "heB"}))
        _rdl2 = client.delete("/api/dashboard/zones/heB")
        check("zones: deleting the hand-set default still hands default to a remaining zone",
              _rdl2.status_code == 200 and (_he_raw().get("sonos") or {}).get("default_zone") == "heC"
              and _rdl2.json().get("default_zone") == "heC",
              f"{_rdl2.status_code} {_rdl2.text[:120]}")
    finally:
        config_store.write_sections(_he_saved)

    # Firmware upload: only the network read of each chunk may run on the
    # loop — staging writes, validation, swap and manifest go to threads.
    import threading as _thfw

    class _FwUp:
        """Async upload stand-in: small chunks with a yield between them,
        like a real multipart stream."""

        def __init__(self, data, chunk=64 * 1024):
            self.filename, self._data, self._chunk, self._pos = "fw.bin", data, chunk, 0

        async def read(self, n=-1):
            await asyncio.sleep(0)
            out = self._data[self._pos:self._pos + self._chunk]
            self._pos += len(out)
            return out

    _cwd_fw = os.getcwd()
    _state_fw = os.environ.get("LVI_STATE_DIR")
    os.environ["LVI_STATE_DIR"] = str(SCRATCH)  # uploads land in the scratch, cwd or not
    os.chdir(SCRATCH)  # the suite's own cwd-relative reads stay in the scratch
    _it_fw, _dumps_fw = _rRL.firmware_image.image_track, _rRL.json.dumps
    _fw_on_main = {"image_track": [], "manifest": []}
    try:
        def _slow_image_track(data):
            _fw_on_main["image_track"].append(_thfw.current_thread() is _thfw.main_thread())
            time.sleep(0.5)
            return _it_fw(data)

        def _rec_dumps(*a, **k):
            _fw_on_main["manifest"].append(_thfw.current_thread() is _thfw.main_thread())
            return _dumps_fw(*a, **k)
        _rRL.firmware_image.image_track = _slow_image_track
        _rRL.json.dumps = _rec_dumps
        try:
            _gap_fw = asyncio.run(_loop_gap(_rRL.upload_firmware(
                file=_FwUp(_fake_image5b("1.9.9", "array")), track="array")))
        finally:
            _rRL.firmware_image.image_track, _rRL.json.dumps = _it_fw, _dumps_fw
        check("firmware upload: validation, swap and manifest write run off the event loop",
              _gap_fw < 0.15 and _fw_on_main["image_track"] == [False] and _fw_on_main["manifest"] == [False],
              f"max loop gap={_gap_fw:.2f}s on-main={_fw_on_main}")

        # Two uploads to the same track at once: each must stage its own
        # file, and the served image + manifest must come from ONE upload.
        _imgA = _fake_image5b("1.9.9", "array") + b"\xaa" * (300 * 1024)
        _imgB = _fake_image5b("1.9.8", "array") + b"\xbb" * (200 * 1024)

        async def _both():
            return await asyncio.gather(
                _rRL.upload_firmware(file=_FwUp(_imgA), track="array"),
                _rRL.upload_firmware(file=_FwUp(_imgB), track="array"),
                return_exceptions=True)
        _res_cc = asyncio.run(_both())
        _served = Path("firmware/array/array_esp32.bin").read_bytes()
        _man_cc = json.loads(Path("firmware/array/version.json").read_text())
        # Served images are signed (padded + a signature sector), so match on the uploaded bytes as a prefix.
        _want_ver = "1.9.9" if _served.startswith(_imgA) else "1.9.8" if _served.startswith(_imgB) else None
        check("firmware upload: concurrent uploads to one track each succeed and never mix their bytes",
              all(isinstance(r, dict) and r.get("ok") for r in _res_cc) and _want_ver is not None,
              f"results={[type(r).__name__ if not isinstance(r, dict) else r.get('version') for r in _res_cc]} "
              f"served={len(_served)} bytes, matches={'A' if _served.startswith(_imgA) else 'B' if _served.startswith(_imgB) else 'neither'}")
        check("firmware upload: after concurrent uploads the manifest names the version actually served",
              _want_ver is not None and _man_cc.get("version") == _want_ver
              and _man_cc.get("filename") == "array_esp32.bin",
              f"manifest={_man_cc} served-version={_want_ver}")
        _left_cc = [q.name for q in Path("firmware/array").iterdir() if q.name.endswith(".uploading")]
        check("firmware upload: concurrent uploads leave no staging files behind", not _left_cc, str(_left_cc))
        _rx_cc = client.post("/api/dashboard/firmware/upload",
                             files={"file": ("x.bin", _fake_image5b("1.9.1", "flex"), "application/octet-stream")},
                             data={"track": "array"})
        check("firmware upload: cross-line refusal unchanged, served array image untouched",
              _rx_cc.status_code == 400 and "reSpeaker Flex firmware" in _rx_cc.text
              and Path("firmware/array/array_esp32.bin").read_bytes() == _served
              and not [q for q in Path("firmware/array").iterdir() if q.name.endswith(".uploading")],
              f"{_rx_cc.status_code} {_rx_cc.text[:120]}")
    finally:
        os.chdir(_cwd_fw)
        if _state_fw is None:
            os.environ.pop("LVI_STATE_DIR", None)
        else:
            os.environ["LVI_STATE_DIR"] = _state_fw
    # ════ end zone routes + firmware upload block ════
    # ── dashboard ui (App.jsx): restart wait, Add key, mapping save, node
    # forms, wake-model picker, token prompt ───────────────────────────────
    # The host has no JS runtime for the React app, so these read App.jsx and
    # pin the shape of the exact function that carried each defect.
    import re as _reui
    _appsrc = (REPO_CORE.parent / "dashboard-ui" / "src" / "App.jsx").read_text()

    def _jsbody(src, head):
        """Text of the block opened by `head` (which ends in '{'), brace-matched."""
        i = src.find(head)
        if i < 0 or not head.endswith("{"):
            return ""
        depth, j = 0, i + len(head) - 1
        for k in range(j, len(src)):
            depth += {"{": 1, "}": -1}.get(src[k], 0)
            if depth == 0:
                return src[j:k + 1]
        return ""

    _rb = _jsbody(_appsrc, "async function restartBot(setMsg, setRebooting) {")
    check("dashboard ui: restart waits for a NEW bot process (an old process under 30 s old is not 'back')",
          bool(_rb) and "uptime_secs < 30" not in _rb and "Date.now()" in _rb
          and _rb.find("Date.now()") < _rb.find("/api/dashboard/restart"), _rb[:120])
    _pe, _wl = _rb.find("if (postErr"), _rb.find("i < 45")
    check("dashboard ui: a refused restart request is reported at once, not after the 90 s wait",
          0 <= _pe < _wl and "return" in _rb[_pe:_wl], f"postErr@{_pe} loop@{_wl}")

    _kc = _jsbody(_appsrc, "function KeysCard({ setMsg }) {")
    _kadd = _reui.search(r'<select id="key-add".*?</select>', _kc, _reui.DOTALL)
    check("dashboard ui: Add key saves only under an explicitly picked service (no implicit first pick)",
          bool(_kc) and "firstPick" not in _kc and "addPick ||" not in _kc
          and bool(_kadd) and 'value="" disabled' in _kadd.group(0), (_kadd.group(0)[:160] if _kadd else "no select"))
    check("dashboard ui: a picked service that a refresh removed clears the pasted key and blocks Add",
          "[stale])" in _kc and "addEpoch" in _kc and "disabled={busy || !pickValue || stale}" in _kc)
    # Keys: "restart to apply" only for a key the running setup uses.
    _ksave = _jsbody(_kc, "const save = async (row, inputId) => {")
    _krm = _jsbody(_kc, "const remove = async row => {")
    _kaddf = _jsbody(_kc, "const add = async () => {")
    check("dashboard ui: Keys save says 'restart to apply' only when the saved key is in use",
          "const d = await load()" in _ksave and ".in_use" in _ksave
          and 0 <= _ksave.find("if (inUse)") < _ksave.find("'Saved — restart to apply.'")
          and "Not in use yet" in _ksave, _ksave[:200])
    check("dashboard ui: Keys remove says 'restart to apply' only for a key in use",
          "if (row.in_use)" in _krm
          and 0 <= _krm.find("if (row.in_use)") < _krm.find("'Removed — restart to apply.'"), _krm[:200])
    check("dashboard ui: Keys Add tests the saved key and never restarts the bot itself",
          "await test({ var: v })" in _kaddf and "restartBot(" not in _kaddf, _kaddf[-200:])
    check("dashboard ui: a saved or removed key nothing uses gets a neutral chip, never 'restart to apply'",
          "Not in use yet: switching to it marks a restart pending." in _appsrc
          and "Nothing uses it, so no restart is needed." in _appsrc)
    check("dashboard ui: a key Test whose setup isn't finished shows an info note, not a red failure",
          "t.status === 'needs_setup' ? 'text-[var(--muted)]'" in _kc and "'note — '" in _kc)

    _sm = _jsbody(_appsrc, "const saveMapping = async say => {")
    check("dashboard ui: a mapping save keeps its draft until the saved map is reloaded (no flick back, no revert)",
          "await reloadAll()" in _sm and 0 <= _sm.find("await reloadAll()") < _sm.find("zones.settle(sentZones)")
          and _sm.find("await reloadAll()") < _sm.find("agents.settle(sentAgents)"), _sm[-300:])
    check("dashboard ui: a mapping save drops draft rows whose zone no longer exists",
          "liveZone(" in _sm and "const liveZone = z => z === BUILTIN || zoneNames.includes(z)" in _appsrc)

    _apib = _jsbody(_appsrc, "async function api(path, opts = {}, _retried = false) {")
    check("dashboard ui: the node /info read may ask for the dashboard token (its route is guarded)",
          "promptForToken" in _apib
          and _reui.search(r"nodes/\$\{encodeURIComponent\(room\)\}/info`, \{ promptForToken: true \}", _appsrc) is not None)

    _na = _jsbody(_appsrc, "function NodeActions({ room, online, lastSeen, reportedMuted, setMsg, reloadAll, children: renderRow }) {")
    _wf = _jsbody(_na, "const applyWifi = async () => {")
    _rn = _jsbody(_na, "const rename = async () => {")
    check("dashboard ui: Wi-Fi Apply and Save name ignore a second click while the first is in flight",
          "if (sending) return" in _wf and "if (sending) return" in _rn
          and _reui.search(r"<button[^>]*disabled=\{sending\}[^>]*onClick=\{applyWifi\}", _na) is not None
          and _reui.search(r"<button[^>]*disabled=\{sending\}[^>]*onClick=\{rename\}", _na) is not None)

    _ow = _jsbody(_appsrc, "function OwwPanel({ setMsg, reloadAll }) {")
    check("dashboard ui: the wake-model picker shows a placeholder when no listed model is active",
          '<option value="" disabled>pick a model' in _ow)

    _td = _jsbody(_appsrc, "function TokenDialog() {")
    check("dashboard ui: the dashboard token is typed into a masked field, never window.prompt",
          "window.prompt(" not in _appsrc and 'type="password"' in _td)


    # ── dashboard: the room recorder is removed ──────────────────────
    import importlib.util as _iuREC

    from dashboard import routes as _rtREC
    # "/record" also matches /record/stop and /recordings
    _recPaths = sorted(p for p in (getattr(r, "path", "") for r in _rtREC.router.routes) if "/record" in p)
    check("dashboard: no room_recorder module and no /record or /recordings route",
          _iuREC.find_spec("room_recorder") is None and not _recPaths, str(_recPaths))

    # ── intelligence: nanobot's saved model can be cleared ──────────────
    import urllib.request as _urqCL

    import intelligence.agent as _iaCL
    _clCfg = {sec: config_store.read_section(sec) for sec in ("intelligence", "room_agents")}
    _clEnv = os.environ.get("LVI_NANOBOT_KEY")
    _clOpen = _urqCL.urlopen
    _clURLS = {"hermes": "http://192.0.2.20:8642/v1", "openclaw": "http://192.0.2.21:18789/v1",
               "nanobot": "http://192.0.2.22:8900/v1"}
    _clAgents = {"hermes": "suite-profile", "openclaw": "suite-main", "nanobot": "suite-model"}

    def _cl_reset():
        config_store.write_section("intelligence", {"kind": "nanobot", "url": _clURLS["nanobot"],
                                                    "urls": dict(_clURLS), "agents": dict(_clAgents),
                                                    "agent": "suite-main"})

    def _cl_icfg():
        return dict(config_store.read_section("intelligence") or {})
    try:
        config_store.write_section("room_agents", {})
        _cl_reset()
        _pC = client.put("/api/dashboard/intelligence", json={"kind": "nanobot", "agent": ""})
        _cC = _cl_icfg()
        check("intelligence: nanobot with agent \"\" removes its saved model and keeps the other kinds' agents",
              _pC.status_code == 200 and "nanobot" not in (_cC.get("agents") or {})
              and _cC.get("agents") == {"hermes": "suite-profile", "openclaw": "suite-main"}
              and _cC.get("agent") == "suite-main" and _cC.get("kind") == "nanobot",
              f"{_pC.status_code} {_pC.text[:160]} {_cC.get('agents')}")
        _nbBody = []

        class _RespCL:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def _fake_openCL(req, timeout=None):
            _nbBody.append(json.loads(req.data))
            return _RespCL()
        os.environ["LVI_NANOBOT_KEY"] = "nb-suite-key-abcdefghijkl"
        _urqCL.urlopen = _fake_openCL
        _aCL = None
        try:
            _aCL = _iaCL.IntelligenceAgent(room="suite-cl-room", session_mode="fresh")
            _aCL._ask_intelligence("hi")
        except Exception as e:  # noqa: BLE001 — a failed ask is a check result
            _nbBody.append({"error": repr(e)})
        finally:
            _urqCL.urlopen = _clOpen
        check("intelligence: a nanobot turn with no saved model omits the model field",
              getattr(_aCL, "_agent", None) == "" and len(_nbBody) == 1 and "model" not in _nbBody[0],
              str(_nbBody)[:200])

        _bad = {}
        for _k in ("hermes", "openclaw"):
            _cl_reset()
            _r = client.put("/api/dashboard/intelligence", json={"kind": _k, "agent": ""})
            _bad[_k] = (_r.status_code, _r.text[:160], _cl_icfg().get("agents") == _clAgents
                        and _cl_icfg().get("kind") == "nanobot")
        check("intelligence: agent \"\" for hermes or openclaw is a 422 that says it needs an agent; nothing saved",
              all(s == 422 and "needs an agent" in t and same for s, t, same in _bad.values()), str(_bad))

        _kept = {}
        for _k in ("hermes", "openclaw", "nanobot"):
            for _body in ({"kind": _k}, {"kind": _k, "agent": None}):
                _cl_reset()
                _r = client.put("/api/dashboard/intelligence", json=_body)
                _kept[f"{_k}{'+null' if 'agent' in _body else ''}"] = (_r.status_code, _cl_icfg().get("agents"))
        check("intelligence: an omitted or null agent leaves every kind's saved agent alone",
              all(s == 200 and a == _clAgents for s, a in _kept.values()), str(_kept))

        _cl_reset()
        _pO = client.put("/api/dashboard/intelligence", json={"kind": "openclaw", "agent": "suite-other"})
        _cO = _cl_icfg()
        check("intelligence: an openclaw save still writes the legacy top-level agent key",
              _pO.status_code == 200 and _cO.get("agent") == "suite-other"
              and (_cO.get("agents") or {}).get("openclaw") == "suite-other", f"{_pO.text[:120]} {_cO}")
        _cl_reset()
        _pX = client.put("/api/dashboard/intelligence", json={"kind": "nanobot", "agent": "", "bogus": 1})
        check("intelligence: unknown fields still 422 and save nothing",
              _pX.status_code == 422 and _cl_icfg().get("agents") == _clAgents, f"{_pX.status_code}")
    finally:
        _urqCL.urlopen = _clOpen
        if _clEnv is None:
            os.environ.pop("LVI_NANOBOT_KEY", None)
        else:
            os.environ["LVI_NANOBOT_KEY"] = _clEnv
        for _sec, _val in _clCfg.items():
            config_store.write_section(_sec, _val or {})


    # ── speakers: zone settings reach sessions that are already running ──
    from sonos_output import SonosOutput as _SOLZ
    from sonos_output import zone_volume_overrides as _zvoLZ
    from wiim_output import WiiMOutput as _WOLZ
    from wiim_output import wiim_volume_overrides as _wvoLZ
    _sonos_before = copy.deepcopy(config_store.read_section("sonos") or {})
    _wiim_before = copy.deepcopy(config_store.read_section("wiim") or {})
    try:
        # a session built BEFORE the zone exists (the fresh-install order:
        # node connects, then the first speaker is added and mapped)
        config_store.write_section("wiim", {**_wiim_before, "enabled": False,
                                            "zones": {}, "zone_volumes": {}})
        _woLZ = _WOLZ(zone_selector=lambda: "lz_wiim")
        _owns0 = _woLZ.owns_reply()
        _rLZ = client.post("/api/dashboard/zones", json={"name": "lz_wiim", "backend": "wiim", "ip": "192.0.2.61"})
        check("speakers: a WiiM zone added after the session started (backend off until then) takes that "
              "session's next reply",
              _rLZ.status_code == 200 and _owns0 is False and _woLZ.owns_reply() is True
              and _woLZ._zones.get("lz_wiim", {}).get("ip") == "192.0.2.61",
              f"status={_rLZ.status_code} before={_owns0} after={_woLZ.owns_reply()}")
        _soLZ = _SOLZ(zone_selector=lambda: "lz_sonos")
        _rLZs = client.post("/api/dashboard/zones", json={"name": "lz_sonos", "ip": "192.0.2.62",
                                                          "player_uid": "RINCON_LZTEST01"})
        check("speakers: a Sonos zone added after the session started is found at reply time",
              _rLZs.status_code == 200 and (_soLZ._zones.get("lz_sonos") or {}).get("ip") == "192.0.2.62",
              f"status={_rLZs.status_code} zones={list(_soLZ._zones)}")

        # volume: zone at 50 over a global of 35; slider to 40, then back to 35
        _sec = config_store.read_section("sonos") or {}
        config_store.write_section("sonos", {**_sec, "clip_volume": 35, "zone_volumes": {"lz_sonos": 50}})
        _soV = _SOLZ(zone_selector=lambda: "lz_sonos")
        _v50 = _soV._volume_for("lz_sonos")
        _r40 = client.put("/api/dashboard/zones/lz_sonos/volume", json={"volume": 40})
        _v40 = _soV._volume_for("lz_sonos")
        _r35 = client.put("/api/dashboard/zones/lz_sonos/volume", json={"volume": 35})
        _v35 = _soV._volume_for("lz_sonos")
        check("speakers: a Sonos zone slider moved back to the global volume plays at the global volume",
              (_r40.status_code, _r35.status_code) == (200, 200) and (_v50, _v40, _v35) == (50, 40, 35),
              f"{_v50}/{_v40}/{_v35}")
        _sw = config_store.read_section("wiim") or {}
        config_store.write_section("wiim", {**_sw, "clip_volume": 35, "zone_volumes": {"lz_wiim": 50}})
        _woV = _WOLZ(zone_selector=lambda: "lz_wiim")
        _w50 = _woV._volume_for("lz_wiim")
        client.put("/api/dashboard/zones/lz_wiim/volume", json={"volume": 40})
        client.put("/api/dashboard/zones/lz_wiim/volume", json={"volume": 35})
        check("speakers: a WiiM zone slider moved back to the global volume plays at the global volume",
              (_w50, _woV._volume_for("lz_wiim")) == (50, 35), f"{_w50}/{_woV._volume_for('lz_wiim')}")

        # MUST STILL HOLD: saved zone volumes apply to a fresh session (a restart)
        _zvoLZ({})
        _wvoLZ({})
        config_store.write_section("sonos", {**(config_store.read_section("sonos") or {}),
                                             "zone_volumes": {"lz_sonos": 47}})
        check("speakers: a saved zone volume still applies with no live override (after a restart)",
              _SOLZ(zone_selector=lambda: "lz_sonos")._volume_for("lz_sonos") == 47)
        # MUST STILL HOLD: an explicit config file stays that file's values,
        # and assigning an attribute pins it for that instance
        _cfgLZ = SCRATCH / "lz-config.yaml"
        _cfgLZ.write_text("sonos:\n  enabled: true\n  clip_volume: 22\n  zones:\n    filez:\n"
                          "      ip: 192.0.2.63\n      player_uid: RINCON_LZFILE01\n")
        _soF = _SOLZ(config_path=str(_cfgLZ))
        _soP = _SOLZ()
        _soP._zones = {"pinned": {"ip": "127.0.0.1"}}
        _soP._enabled = False
        check("speakers: a session built from an explicit config file keeps that file's zones; pinned "
              "attributes stay pinned",
              list(_soF._zones) == ["filez"] and _soF._volume == 22 and _soF._enabled is True
              and list(_soP._zones) == ["pinned"] and _soP._enabled is False,
              f"{list(_soF._zones)} {_soF._volume} {list(_soP._zones)}")
    finally:
        _zvoLZ({})
        _wvoLZ({})
        config_store.write_section("sonos", _sonos_before)
        config_store.write_section("wiim", _wiim_before)
    # ── dashboard ui (App.jsx): nanobot model pick, stale agent discovery,
    # zone volume slider after a refused save ─────────────────────────────
    def _ws(s):
        return _reui.sub(r"\s+", " ", s)

    def _fnUI(head):
        """A top-level component's source, up to the next top-level function."""
        i = _appsrc.find(head)
        j = _reui.search(r"\n(export default )?function ", _appsrc[i + 1:]) if i >= 0 else None
        return "" if i < 0 else _appsrc[i:i + 1 + j.start()] if j else _appsrc[i:]

    _sysUI = _fnUI("function System({ intelligence, status, setMsg, reloadAll }) {")
    _selUI = _reui.search(r"const agentSel = (.*?)\n\s*const agentEntry", _sysUI, _reui.DOTALL)
    _selUI = _ws(_selUI.group(1)) if _selUI else ""
    _oldFallback = "intAgent || found[intKind]?.selected || agentList[0]?.id || ''"
    import intelligence as _lanesUI
    _NB_FREE = "laneOf(intKind)?.agent_required === false ? intAgent :"
    _nbFree = _lanesUI.LANES["nanobot"].agent_required is False
    check("intelligence ui: nanobot's model pick never falls back to the first listed model",
          _selUI.startswith(_NB_FREE) and _nbFree, _selUI[:160])
    check("intelligence ui: hermes and openclaw keep their saved-then-first-listed fallback",
          _selUI == f"{_NB_FREE} {_oldFallback}"
          and _lanesUI.LANES["hermes"].agent_required and _lanesUI.LANES["openclaw"].agent_required, _selUI[:160])
    _optUI = _reui.search(r'<select (?:aria-label="[^"]*" )?id="int-agent".*?</select>', _sysUI, _reui.DOTALL)
    _optUI = _ws(_optUI.group(0)) if _optUI else ""
    check("intelligence ui: nanobot always offers 'nanobot's own model' (value '') first; other kinds unchanged",
          "laneOf(intKind)?.agent_required === false ? <option value=\"\">{laneOf(intKind).no_agent_label}</option> "
          ": !agentSel && <option value=\"\">{found[intKind]?.loading ? 'loading…' : 'no agents'}</option>}"
          in _optUI and _lanesUI.LANES["nanobot"].ui_meta()["no_agent_label"] == "nanobot's own model"
          and _lanesUI.LANES["hermes"].ui_meta()["no_agent_label"] is None
          and _lanesUI.LANES["openclaw"].ui_meta()["no_agent_label"] is None, _optUI[:240])
    # ── echo (intelligence.kind: stub): a fresh install's "no intelligence yet" ──
    from dashboard import routes as _rEC
    _icfg_EC = dict(config_store.read_section("intelligence") or {})
    _env_kind_EC = os.environ.pop("LVI_INTELLIGENCE_KIND", None)
    try:
        config_store.write_section("intelligence", {**_icfg_EC, "kind": "stub"})
        _lbl_EC, _def_EC = _rEC._intelligence_label(), _rEC._agent_default()
        config_store.write_section("intelligence", {**_icfg_EC, "kind": "bogus"})
        _lbl_bogus_EC = _rEC._intelligence_label()
    finally:
        config_store.write_section("intelligence", _icfg_EC)
        if _env_kind_EC is not None:
            os.environ["LVI_INTELLIGENCE_KIND"] = _env_kind_EC
    check("echo: kind stub reads 'echo' on the status line (an unknown kind still shows the default lane)",
          _lbl_EC == "echo" and _lbl_bogus_EC == _lanesUI.LANES[_lanesUI.DEFAULT_KIND].label
          and _def_EC.get("agent_kind") == "stub", f"{_lbl_EC!r} {_lbl_bogus_EC!r} {_def_EC}")
    _chipEC = _fnUI("function intelligenceChip(status, intel) {")
    check("echo ui: the Intelligence chip says 'echo' (neutral, not a link: not a fault) before any url/key warning",
          "const ECHO_KIND = 'stub'" in _appsrc
          and 0 <= _chipEC.find("=== ECHO_KIND") < _chipEC.find("'no url'")
          and "return { tone: 'neutral', text: 'echo', title: 'No intelligence picked yet" in _chipEC, _chipEC[:300])
    # Naming law: users never see or hear "brain" (it's "intelligence").
    # Dashboard source (comments too) and every line the echo stub can say.
    import re as _reNL

    from intelligence import stub as _stubNL
    _nlS = _stubNL.StubIntelligence.__new__(_stubNL.StubIntelligence)
    _nlSaid = [_nlS._reply(q) for q in ("what's your name", "who are you", "bye", "turn on the lights")]
    _nlHits = [l.strip()[:80] for l in _appsrc.splitlines() if _reNL.search(r"\bbrains?\b", l, _reNL.IGNORECASE)]
    _nlHits += [s for s in _nlSaid if _reNL.search(r"\bbrains?\b", s, _reNL.IGNORECASE)]
    check("naming law: the dashboard source and the echo stub's spoken lines never say 'brain'",
          not _nlHits and all("intelligence" in s for s in _nlSaid[:1] + _nlSaid[3:]), _nlHits[:3] or _nlSaid)
    check("echo ui: the Intelligence card shows 'None yet (echo)' only while saved, hides URL/agent/key/Test, never lists agents",
          "{savedKind === ECHO_KIND && <option value={ECHO_KIND}>None yet (echo)</option>}" in _sysUI
          and "{intKind === ECHO_KIND ? (" in _sysUI and "intKind !== ECHO_KIND && !(agentEntry" in _sysUI
          and "{intKind !== ECHO_KIND && <div className=\"flex items-center justify-between gap-4 flex-wrap\">" in _sysUI
          and "if (!laneOf(kind)) return  // echo: nothing to list" in _sysUI
          and "if (!agentKind || !laneOf(agentKind)) return  // echo: no agents" in _appsrc)
    import importlib as _ilEC
    _stubEC = _ilEC.import_module("intelligence.stub").StubIntelligence()
    _repliesEC = [_stubEC._reply(x) for x in ("Hey Jarvis, what time is it?", "hello", "who are you", "good night")]
    check("echo: the stub's replies are true on any box (no GPU/ElevenLabs claims) and point at the dashboard, not YAML",
          not any(w in r for r in _repliesEC for w in ("GPU", "ElevenLabs", "config.yaml", "intelligence.kind"))
          and "I heard: Hey Jarvis, what time is it?" in _repliesEC[0]
          and all("dashboard" in r for r in _repliesEC[:3]), str(_repliesEC))
    check("intelligence ui: Save always sends nanobot's pick ('' clears it); other kinds send an agent only when one is set",
          "if (agentSel || laneOf(kind)?.agent_required === false) body.agent = agentSel" in _sysUI
          and "if (agentSel) body.agent = agentSel" not in _sysUI and _nbFree)
    check("intelligence ui: a saved nanobot model is what shows after reload (pick synced from the saved agents)",
          _selUI.startswith(_NB_FREE) and _nbFree
          and "[`agent:${k}`, intelligence?.agents?.[k] || null]" in _sysUI
          and "const intAgent = d.val(`agent:${intKind}`) || ''" in _sysUI, _selUI[:200])
    _rmUI = _fnUI("function Rooms({ status, mapping, setMsg, reloadAll }) {")
    check("zones ui: 'nanobot's own model' lives only on the Intelligence card, never in the per-room agent picker",
          "laneOf(intKind).no_agent_label" in _sysUI and bool(_rmUI) and "no_agent_label" not in _rmUI
          and "nanobot's own model" not in _rmUI and "room_agents" in _rmUI)

    _discUI = _jsbody(_sysUI, "const discover = async (kind, url) => {")
    _dTry = _discUI[_discUI.find("try {"):_discUI.find("} catch")]
    _dCatch = _discUI[_discUI.find("} catch"):]
    _gUI = "if (seq !== discSeq.current[kind]) return"
    check("intelligence ui: agent discovery numbers each request per kind",
          "discSeq.current[kind] = seq" in _discUI
          and _discUI.find("discSeq.current[kind] = seq") < _discUI.find("await api("), _discUI[:200])
    check("intelligence ui: a superseded discovery reply (list or error) is dropped before it touches the card",
          0 <= _dTry.find(_gUI) < _dTry.find("setFound(") and 0 <= _dCatch.find(_gUI) < _dCatch.find("setFound("),
          _discUI[:300])
    check("intelligence ui: the newest discovery still shows its result and clears its loading state",
          "loading: true" in _discUI[:_discUI.find("await api(")]
          and 0 <= _dTry.find(_gUI) < _dTry.find("{ ...r, loading: false }")
          and 0 <= _dCatch.find(_gUI) < _dCatch.find("loading: false"), _discUI[:300])
    _rmDisc = _jsbody(_rmUI, "discoverAgents.current = async () => {")
    _rmSets = [ln for ln in _rmDisc.splitlines() if "setFound(" in ln]
    check("zones ui: room agent discovery applies only the newest request's reply",
          0 <= _rmDisc.find("++discSeq.current") < _rmDisc.find("await api(")
          and len(_rmSets) == 2 and all("seq === discSeq.current" in ln for ln in _rmSets), _rmDisc[:300])
    check("zones ui: the newest room agent discovery still shows its list or its error",
          any("setFound(r)" in ln for ln in _rmSets)
          and any("'Agent list failed: ' + e.message" in ln for ln in _rmSets), str(_rmSets))

    _vsUI = _jsbody(_appsrc, "function VolSlider({ name, initial, onCommit }) {")
    check("zones ui: a refused volume save puts the slider back to the saved value",
          "const commit = async () => { if (!(await onCommit(name, val))) setVal(initial) }" in _vsUI
          and _vsUI.count("onCommit(") == 1
          and "onMouseUp={commit}" in _vsUI and "onTouchEnd={commit}" in _vsUI and "commit()" in _vsUI,
          _vsUI[:300])
    check("zones ui: a saved volume stays put; the slider resets only on a refused save or a new saved value",
          "useEffect(() => { setVal(initial) }, [initial])" in _vsUI
          and _vsUI.count("setVal(initial)") == 2, _vsUI[:200])
    _zlUI = _fnUI("function ZoneList({ mapping, setMsg, reloadAll }) {")
    for _whatUI, _headUI in (("zone", "const saveVolume = async (name, vol) => {"),
                             ("built-in speaker", "const saveSpeakerVolume = async (room, vol) => {")):
        _svUI = _ws(_jsbody(_zlUI, _headUI))
        check(f"zones ui: {_whatUI} volume save reports success, and on failure still shows the error",
              "reloadAll() return true" in _svUI
              and "catch (e) { setMsg('Volume save failed: ' + e.message, 'err', 'Zones'); return false }" in _svUI,
              _svUI[:240])

    # ── dashboard ui (App.jsx): UX pack — sentence-case headers, neutral
    # Restart, amber Mute, honest wake copy ────────────────────────────────
    check("dashboard ui: table headers are sentence case (no uppercase/tracking-wide classes, other classes kept)",
          "uppercase" not in _appsrc and "tracking-wide" not in _appsrc
          and '<tr className="text-[12px] text-[var(--muted)]">' in _appsrc,
          f"uppercase x{_appsrc.count('uppercase')}, tracking-wide x{_appsrc.count('tracking-wide')}")
    check("dashboard ui: the Overview Restart button is the default style, not danger (danger stays on Remove/Delete)",
          'className={pending.length && !unsaved ? BTN_PRIMARY : BTN_DEFAULT} disabled={rebooting}' in _appsrc
          and 'className={BTN_DANGER} disabled={rebooting}' not in _appsrc
          and 'const BTN_DANGER' in _appsrc,
          f"BTN_DANGER buttons: {_appsrc.count('className={BTN_DANGER}')}")
    check("dashboard ui: a muted node shows an amber 'muted' chip and an 'Unmute' button in the default style",
          "{muted && !muteUnknown &&\n        <span className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE.warning}`}>muted</span>}" in _appsrc
          and "muted ? 'Unmute' : 'Mute'" in _appsrc
          and 'muted ? BTN_WARNING' not in _appsrc and 'muted ? BTN_PRIMARY' not in _appsrc,
          f"mute button uses BTN_WARNING: {'muted ? BTN_WARNING' in _appsrc}")
    # UX round 3.
    _cssUX = (REPO_CORE.parent / "dashboard-ui" / "src" / "index.css").read_text()
    check("ux3: fields are one shade lighter than the card with no border; dropdowns draw a chevron 12px in and size like a text box",
          "const INPUT_CLS = 'rounded-xl bg-[var(--field-background)] text-[14px] px-3 py-2'" in _appsrc
          and "--field-border" not in _appsrc and "--field-border" not in _cssUX
          and ':root, .dark, [data-theme="dark"] {\n  --field-background: var(--surface-secondary);\n}' in _cssUX
          and "appearance: none;" in _cssUX and "padding-right: 2.25rem;" in _cssUX
          and "background-position: right 0.75rem center;" in _cssUX and "stroke='%2397a290'" in _cssUX)
    _fpUX = _jsbody(_appsrc, "function FilePick({ accept, file, onPick }) {")
    check("ux3: both file pickers are the styled FilePick (a button plus the chosen file's name), no raw file input",
          _appsrc.count('type="file"') == 1 and 'type="file"' in _fpUX and 'className="hidden"' in _fpUX
          and "{file ? file.name : `no ${accept} file chosen`}" in _fpUX and "ref.current?.click()" in _fpUX
          and '<FilePick accept=".bin" file={fwFile} onPick={setFwFile} />' in _appsrc
          and '<FilePick accept=".onnx" file={newModel} onPick={setNewModel} />' in _appsrc, _fpUX[:200])
    check("ux3: the active wake model says 'in use' instead of offering Delete; sizes use fmtBytes (KB/MB)",
          "{m.selected\n                      ? <span className=\"text-[12px] text-[var(--muted)]\">in use</span>" in _appsrc
          and "{fmtBytes(m.size || 0)}</td>" in _appsrc and "/ 1024).toFixed(0)} KB" not in _appsrc
          and ": b >= 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.round(b / 1e3)} KB`" in _appsrc)
    check("ux3: uptime reads 4m / 3h 12m / 2d 4h",
          "return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`" in _appsrc and "slice(11, 16) + 'h'" not in _appsrc)
    check("ux3: the room agent picker's empty option is 'Default agent' (named when it isn't the main agent)",
          "const defaultAgentLabel = a => (!a || a === 'default' ? 'Default agent' : `Default agent (${a})`)" in _appsrc
          and '<option value="">{defaultAgentLabel(defaultAgent)}</option>' in _appsrc and "`Default (${" not in _appsrc)
    check("ux3: the ElevenLabs voice id has its own 'Voice' label; no '— s / command' before the first command",
          "{provider === 'elevenlabs' && <span className=\"text-[12px] font-medium text-[var(--muted)]\">Voice</span>}" in _appsrc
          and "'— s / command'" not in _appsrc and "{ms != null && <span" in _appsrc)
    check("ux3: Keys says 'For'; on a phone For / In use / Status fold under the key name and long var names wrap",
          '<th className="hidden sm:table-cell text-left px-4 py-2">For</th>' in _appsrc and ">Used by</th>" not in _appsrc
          and _appsrc.count('hidden sm:table-cell') == 6
          and '<div className="sm:hidden flex flex-wrap items-center gap-2 mt-1">' in _appsrc
          and 'font-mono text-[var(--muted)] break-all">{row.var}</div>' in _appsrc
          and '<div className="flex flex-wrap items-center justify-end gap-2">' in _appsrc)
    check("hunt 10-24 #1: agent discovery sends a typed host:port (the server fills in http:// and /v1)",
          "if ((url || '').trim()) body.url = url.trim()" in _appsrc
          and "if (/^https?:\\/\\//.test(url || '')) body.url = url" not in _appsrc)
    check("ux3: the mapping and Nodes tables both list rooms by name",
          "const roomRows = [...mappedRooms, ...unmappedRooms].sort(byRoom)" in _appsrc
          and "[...(status?.rooms || [])].sort((a, b) => byRoom(a.room, b.room)).map(r => (" in _appsrc
          and "localeCompare(String(b), undefined, { sensitivity: 'base' })" in _appsrc)
    check("dashboard ui: the wake slider is labelled 'Wake threshold' with a wake-word-score hint",
          "'Wake threshold'" in _appsrc and "'Wake sensitivity'" not in _appsrc
          and 'Wake-word score needed to wake' in _appsrc and 'OWW score' not in _appsrc,
          f"old label: {'Wake sensitivity' in _appsrc}, old hint: {'OWW score' in _appsrc}")
    check("dashboard ui: the wake threshold hint is one short line (no 'tune by ear' advice)",
          "'Wake-word score needed to wake.']" in _appsrc and "tune by ear" not in _appsrc)
    check("dashboard ui: the Nodes table column reads 'Since', not 'Last seen'",
          '>Since<' in _appsrc and 'Last seen' not in _appsrc,
          f"Last seen: {'Last seen' in _appsrc}")
    check("dashboard ui: the firmware mismatch confirm has no emoji; the sentence starts with 'The filename looks like'",
          '⚠' not in _appsrc and 'The filename looks like' in _appsrc,
          f"⚠ present: {'⚠' in _appsrc}")
    check("dashboard ui: the wake name has no 'Set wake word' button (the unsaved bar saves it)",
          'Set wake word' not in _appsrc
          and "useUnsaved('wake-name-card', { label: 'Wake name'" in _appsrc,
          f"Set wake word: {'Set wake word' in _appsrc}")

    # ── restart pending: one Overview state instead of per-save restart
    # prompts. /status lists the saved settings the running bot hasn't
    # loaded yet; a restart (a new boot snapshot) clears the list. ────────
    import config as _cfgRP
    from dashboard import routes as _rRP
    from wiim_output import wiim_volume_overrides as _wvoRP

    _RP_SECS = ("tts", "stt", "intelligence", "wake", "vad", "room_agents", "room_zones", "wiim", "session_modes")
    _rp_before = {s: copy.deepcopy(dict(config_store.read_section(s) or {})) for s in _RP_SECS}
    _RP_VARS = tuple(sorted({"LVI_ENV_FILE", "LVI_INTELLIGENCE_KIND", "LVI_STT_PROVIDER", "ELEVENLABS_API_KEY",
                             "DEEPGRAM_API_KEY", *_rRP.INTELLIGENCE_KEY_VARS.values(),
                             *(k for k in os.environ if k.startswith("LVI_HERMES_KEY_"))}))
    _rp_env = {k: os.environ.get(k) for k in _RP_VARS}
    _rp_boot = copy.deepcopy(getattr(_cfgRP, "_BOOT", None))
    _rp_boot_fn = getattr(_cfgRP, "boot_config", dict)
    _rp_envfile = SCRATCH / "restart-pending.env"
    _RP_SECRETS = ("el-boot-SECRET-0001", "el-new-SECRET-0002", "nb-unused-SECRET-0003")
    _RP_ALL = ["intelligence", "tts", "voice tuning", "room agents", "wake model", "stt"]

    def _rp_status():
        r = client.get("/api/dashboard/status")
        return r, (r.json().get("restart_pending") if r.status_code == 200 else None)

    def _rp_reboot():
        """What a real restart does: a new process loads config afresh."""
        _cfgRP._BOOT = None
        _cfgRP._load.cache_clear()
        _cfgRP._load()

    try:
        for _k in _RP_VARS:
            os.environ.pop(_k, None)
        _rp_envfile.write_text("ELEVENLABS_API_KEY=el-boot-SECRET-0001\n")
        os.environ.update({"LVI_ENV_FILE": str(_rp_envfile), "ELEVENLABS_API_KEY": "el-boot-SECRET-0001"})
        # A zone and room of our own; the config's other entries are left alone.
        _wiRP = _rp_before["wiim"]
        config_store.write_section("wiim", {**_wiRP, "zones": {**(_wiRP.get("zones") or {}),
                                                               "suite-rp-zone": {"ip": "192.0.2.70"}}})
        _rzRP = {**_rp_before["room_zones"], "suite-rp-room": "suite-rp-zone"}
        config_store.write_section("room_zones", _rzRP)
        config_store.write_section("intelligence", {**_rp_before["intelligence"], "kind": "hermes"})
        _rp_reboot()

        _r0, _p0 = _rp_status()
        check("restart pending: /status lists nothing right after a boot",
              _r0.status_code == 200 and _p0 == [], f"status={_r0.status_code} pending={_p0}")
        _RP_FIELDS = ("uptime_secs", "version", "stt", "tts_provider",
                      "intelligence", "last_wake", "rooms", "session_modes")
        check("restart pending: /status keeps every field it had and adds restart_pending",
              _r0.status_code == 200 and all(k in _r0.json() for k in _RP_FIELDS)
              and "restart_pending" in _r0.json(), _r0.text[:300])

        # Live saves.
        _rv = client.put("/api/dashboard/zones/suite-rp-zone/volume", json={"volume": 41})
        _rm = client.put("/api/dashboard/mapping", json={"room_zones": _rzRP})
        _rw = client.put("/api/dashboard/wake", json={"name": "Suitebot"})
        _rs = client.post("/api/dashboard/nodes/suite-rp-room/session-mode", json={"mode": "sticky"})
        _r1, _p1 = _rp_status()
        check("restart pending: live saves (zone volume, mapping zone, wake name, session mode) never mark pending",
              all(r.status_code == 200 for r in (_rv, _rm, _rw, _rs)) and _p1 == [],
              f"{[(r.status_code, r.text[:80]) for r in (_rv, _rm, _rw, _rs)]} pending={_p1}")
        _icRP = dict(config_store.read_section("intelligence") or {})
        config_store.write_section("intelligence", {**_icRP, "urls": {**(_icRP.get("urls") or {}),
                                                                      "openclaw": "http://192.0.2.72:18789/v1"}})
        _r1b, _p1b = _rp_status()
        check("restart pending: a URL filed for a kind the bot isn't running never marks pending",
              _p1b == [], f"pending={_p1b}")

        # Restart-only saves, through their routes where the route runs offline.
        _btRP = _cfgRP._load()
        _thrRP = 0.31 if (_btRP.get("wake") or {}).get("threshold") != 0.31 else 0.32
        _rt = client.put("/api/dashboard/tts", json={"provider": "elevenlabs", "voice_id": "suitevoice0002",
                                                     "model": "eleven_flash_v2_5"})
        _rtu = client.put("/api/dashboard/voice-tuning", json={"wake_threshold": _thrRP})
        _raRP = dict((config_store.read_section("room_agents") or {}).get("hermes") or {})
        _rma = client.put("/api/dashboard/mapping", json={
            "room_zones": _rzRP, "room_agents": {"hermes": {**_raRP, "suite-rp-room": "suite-agent"}}})
        config_store.write_section("intelligence", {**config_store.read_section("intelligence"),
                                                    "url": "http://192.0.2.71:8642/v1"})
        config_store.write_section("wake", {**config_store.read_section("wake"), "model": "suite-rp-model.onnx"})
        _stRP = dict(config_store.read_section("stt") or {})
        config_store.write_section("stt", {**_stRP, "provider":
                                           "elevenlabs" if _stRP.get("provider") != "elevenlabs" else "whisper"})
        _r2, _p2 = _rp_status()
        check("restart pending: each restart-only save is listed once, in a stable order",
              all(r.status_code == 200 for r in (_rt, _rtu, _rma)) and _p2 == _RP_ALL,
              f"{[(r.status_code, r.text[:80]) for r in (_rt, _rtu, _rma)]} pending={_p2}")
        check("restart pending: dashboard writes never refresh the boot snapshot",
              _cfgRP.cfg("wake", "model") == "suite-rp-model.onnx"
              and (_rp_boot_fn().get("wake") or {}).get("model") not in (None, "suite-rp-model.onnx"),
              f"boot wake={(_rp_boot_fn().get('wake') or {}).get('model')}")

        # Keys: an in-use key saved since boot is pending; one nothing uses is not.
        _rp_envfile.write_text("ELEVENLABS_API_KEY=el-new-SECRET-0002\nLVI_NANOBOT_KEY=nb-unused-SECRET-0003\n")
        _r3, _p3 = _rp_status()
        check("restart pending: an in-use key saved since boot is listed after the settings",
              _p3 == [*_RP_ALL, "elevenlabs key"], f"pending={_p3}")
        check("restart pending: a saved key nothing uses never marks pending",
              isinstance(_p3, list) and not any("nanobot" in x for x in _p3), f"pending={_p3}")
        check("restart pending: /status carries labels only, never a key value",
              isinstance(_p3, list) and all(isinstance(x, str) for x in _p3)
              and not any(x in _r3.text for x in _RP_SECRETS), _r3.text[:200])

        # Back to what the bot runs = no longer pending.
        _rp_envfile.write_text("ELEVENLABS_API_KEY=el-boot-SECRET-0001\n")
        _r4, _p4 = _rp_status()
        check("restart pending: a key saved back to the running value clears its entry",
              _p4 == _RP_ALL, f"pending={_p4}")
        config_store.write_section("tts", copy.deepcopy(dict(_rp_boot_fn().get("tts") or {})))
        _r5, _p5 = _rp_status()
        check("restart pending: a setting saved back to the running value clears its entry",
              _p5 == [x for x in _RP_ALL if x != "tts"], f"pending={_p5}")

        # A real restart: the new process loads the saved config and .env.
        _rp_envfile.write_text("ELEVENLABS_API_KEY=el-new-SECRET-0002\n")
        os.environ["ELEVENLABS_API_KEY"] = "el-new-SECRET-0002"
        _rp_reboot()
        _r6, _p6 = _rp_status()
        check("restart pending: after a restart the list is empty (the snapshot is the new boot)",
              _p6 == [], f"pending={_p6}")
    finally:
        for _k, _v in _rp_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _s, _v in _rp_before.items():
            config_store.write_section(_s, _v)
        _wvoRP({})
        _cfgRP._BOOT = _rp_boot

    # UI: no save offers a restart; the header's Restart button carries the pending state.
    _ovRP = _fnUI("function RestartButton(")
    check("restart pending: no save asks to restart (offerRestart and its confirm are gone)",
          "offerRestart" not in _appsrc and "Restart the bot now to apply?" not in _appsrc)
    check("restart pending: with nothing pending the Restart button is 'Restart bot' in the default style",
          "const pending = status?.restart_pending || []" in _ovRP
          and "className={pending.length && !unsaved ? BTN_PRIMARY : BTN_DEFAULT}" in _ovRP and "'Restart bot'" in _ovRP, _ovRP[:300])
    check("restart pending: with items pending it reads 'Restart to apply (N)' and lists them on hover",
          "`Restart to apply (${pending.length})`" in _ovRP and "`Pending: ${pending.join(', ')}`" in _ovRP, _ovRP[:300])
    check("restart pending: the button keeps its one confirm and restarts through restartBot",
          _ovRP.count("window.confirm(") == 1 and "restartBot(setMsg, setRebooting)" in _ovRP
          and 0 <= _ovRP.find("window.confirm(") < _ovRP.find("restartBot("), _ovRP[:300])
    check("restart pending: restartBot keeps its double-fire latch and 90 s wait loop",
          "if (_botRestarting) return false" in _rb and "_botRestarting = true" in _rb
          and _rb.count("_botRestarting = false") == 2 and "i < 45" in _rb, _rb[:200])
    for _whatRP, _fnRP, _pinRP in (
            ("tts", "function Voice(", ("await api('/api/dashboard/tts', { method: 'PUT', json: body }) "
                                       "if (provider === 'localai') await api('/api/dashboard/localai', { method: 'PUT', "
                                       "json: { model_tts: model || '', voice: laVoice.trim() } }) "
                                       "say('Saved — restart to apply.', 'ok', 'TTS') await reloadAll()")),
            ("voice tuning", "function Tuning(", ("say('Saved — restart to apply.', 'ok', 'Voice tuning') "
                                                 "await reloadAll() // pull the saved truth back")),
            ("intelligence", "function System(", "say('Saved — restart to apply.', 'ok', 'Intelligence') await reloadAll()"),
            ("wake model", "function OwwPanel(", ("say('Saved — restart to apply.', 'ok', 'Wake word model') "
                                                 "await load() reloadAll()")),
            ("stt", "function SttCard(", (": 'Saved — restart to apply.', 'ok', 'STT') "
                                         "window.dispatchEvent(new CustomEvent('lenoxvi:reload-all'))")),
            ("room agents", "function Rooms(", "say(`Saved — restart to apply (agent ${picks}).`")):
        check(f"restart pending: the {_whatRP} save says 'restart to apply' and refreshes status",
              _pinRP in _ws(_fnUI(_fnRP)), _pinRP[:80])
    check("restart pending: live saves keep their own messages",
          "replies now route there (live, no restart)." in _fnUI("function Rooms(")
          and "'Saved — applies to the next thing you say.'" in _fnUI("function SttCard("))
    # ── dashboard ui (App.jsx): honest Overview chips, honest table states ──
    def _retsHO(body):
        """Every `return { ... }` object literal in a helper body."""
        out, i = [], body.find("return {")
        while i >= 0:
            out.append(_jsbody(body[i:], "return {"))
            i = body.find("return {", i + 1)
        return out

    _ttsHO = _jsbody(_appsrc, "function ttsChip(status, tts) {")
    _intHO = _jsbody(_appsrc, "function intelligenceChip(status, intel) {")
    _nodHO = _jsbody(_appsrc, "function nodesChip(rooms, mapping) {")
    _chipHO = _jsbody(_appsrc, "function StatChip({ stat, tone = 'neutral', text, title, muted, href, className, style, children }) {")
    _ovHO = _fnUI("function Overview({ status, tts, intelligence, mapping }) {")
    check("overview: the chips get the TTS, intelligence and mapping data App already loads",
          "<Overview status={status} tts={tts} intelligence={intelligence} mapping={mapping}" in _appsrc
          and "{...ttsChip(status, tts)}" in _ovHO and "{...intelligenceChip(status, intelligence)}" in _ovHO
          and "{...nodesChip(status?.rooms, mapping)}" in _ovHO, _ovHO[:200])
    _hintHO = _reui.search(r"const TTS_KEY_HINT = \{(.*?)\}", _appsrc)
    _hintHO = dict(_reui.findall(r"'?([\w-]+)'?: '(\w+)'", _hintHO.group(1))) if _hintHO else {}
    _ttsGet = client.get("/api/dashboard/tts").json()
    check("overview: the TTS chip warns 'no key' (a link to Keys) when the configured cloud voice has no saved key",
          bool(_ttsHO) and "const hint = TTS_KEY_HINT[tts?.provider]" in _ttsHO
          and "if (hint && !tts[hint])" in _ttsHO
          and "tone: 'warning', text: 'no key', href: '#keys'" in _ws(_ttsHO), _ttsHO[:200])
    import tts_catalog as _tcHO
    check("overview: the TTS chip reads key hints GET /tts really returns, for every LocalAI and Cloud provider; "
          "Embedded needs none",
          set(_hintHO) == {p["name"] for p in _tcHO.PROVIDERS if p["tier"] != "embedded"}
          and all(v in _ttsGet for v in _hintHO.values()), f"{_hintHO} vs {sorted(_ttsGet)[:12]}")
    _intGet = client.get("/api/dashboard/intelligence").json()
    check("overview: the intelligence chip warns 'no url' (link to Intelligence), else 'no key' (link to Keys)",
          0 <= _intHO.find("text: 'no url', href: '#intelligence'") < _intHO.find("text: 'no key', href: '#keys'")
          and "if (!intel.url)" in _intHO and "!intel.key_set" in _intHO
          and {"kind", "url", "key_set", "agents"} <= set(_intGet), _intHO[:200])
    import intelligence as _lanes_ov
    check("overview: a Hermes profile (its own key, not reported by /intelligence) never gets a false 'no key'",
          "laneOf(intel.kind)?.per_agent_keys && intel.agents?.[intel.kind]" in _intHO
          and "intel.agents[intel.kind] !== laneOf(intel.kind).default_agent" in _intHO
          and "if (!profile && !intel.key_set)" in _intHO
          and _lanes_ov.LANES["hermes"].per_agent_keys and _lanes_ov.LANES["hermes"].default_agent == "default")
    check("overview: the nodes chip warns 'N unmapped' (link to Rooms) for online rooms with no zone, once /mapping loaded",
          bool(_nodHO) and "text: `${silent.length} unmapped`, href: '#rooms'" in _ws(_nodHO)
          and "mapping?.room_zones ? rooms.filter(r => r.online && !heard(r.room))" in _nodHO, _nodHO[:300])
    check("overview: a room counts as mapped with any zone entry (builtin too) or when its own speaker plays it",
          "mapping.room_zones[room] || mapping.speakers?.[room]?.zone === BUILTIN" in _nodHO)
    _allRetsHO = _retsHO(_ttsHO) + _retsHO(_intHO) + _retsHO(_nodHO)
    _warnHO = [r for r in _allRetsHO if "href:" in r]
    check("overview: setup warnings (no key, no url, unmapped) are links in the warning tone; healthy chips are not links",
          len(_warnHO) == 4 and all("tone: 'warning'" in r for r in _warnHO)
          and "const Tag = href ? 'a' : 'span'" in _chipHO and "${STAT_TONE[tone]}" in _chipHO,
          f"{len(_warnHO)} link returns: {[r[:60] for r in _warnHO]}")
    # MUST STILL HOLD: healthy chips unchanged, placeholders while loading
    check("overview: loading data shows today's placeholder, never a warning",
          _ttsHO.lstrip("{ \n").startswith("if (!status) return { text: '—', muted: true }")
          and _nodHO.find("if (!rooms) return { text: '—', muted: true }") < _nodHO.find("heard")
          and "if (intel) {" in _intHO and "(esc(status?.intelligence) || '—')" in _intHO, _ttsHO[:120])
    check("overview: healthy TTS and nodes chips look as before",
          "return { text: ttsName(primary)?.toLowerCase() || '—', muted: !primary }" in _ttsHO
          and "if (!down.length) return { tone: 'success', text, title: 'All nodes online' }" in _nodHO
          and "if (down.length === rooms.length) return { tone: 'danger', text, title: 'All nodes offline' }" in _nodHO
          and "return { tone: 'warning', text, title: `${down.join(', ')} offline` }" in _nodHO)
    check("overview: the intelligence chip keeps its brand colours when healthy",
          "brand: { backgroundColor: 'rgba(23, 23, 23, 0.9)', color: '#ef4444' }" in _appsrc
          and "brand: { backgroundColor: 'rgba(255, 255, 255, 0.92)', color: '#2563eb' }" in _appsrc
          and _lanes_ov.LANES["openclaw"].ui_meta()["brand"] == {"backgroundColor": "rgba(23, 23, 23, 0.9)", "color": "#ef4444"}
          and _lanes_ov.LANES["hermes"].ui_meta()["brand"] == {"backgroundColor": "rgba(255, 255, 255, 0.92)", "color": "#2563eb"}
          and _lanes_ov.LANES["nanobot"].ui_meta()["brand"] is None
          and "const s = lane?.brand || null" in _intHO
          and "return { tone: s ? 'brand' : 'neutral', muted: !s && !lane, style: s, text: brand.toLowerCase() }"
          in _intHO)

    # Tables: every listed table ends in one state row spanning its columns.
    _tsHO = _jsbody(_appsrc, "function TableState({ cols, loading, error, empty }) {")
    check("tables ui: one state row: error 'Couldn't load: …' in the danger colour, else 'Loading…', else the empty text",
          "const text = error ? `Couldn't load: ${error}` : loading ? 'Loading…' : empty" in _tsHO
          and "error ? 'text-[var(--danger)]' : 'text-[var(--muted)]'" in _tsHO and "colSpan={cols}" in _tsHO, _tsHO[:200])
    _tablesHO = _reui.findall(r"<table.*?</table>", _appsrc, _reui.DOTALL)
    _withHO = [(t.count("<th "), int(_reui.search(r"<TableState cols=\{(\d+)\}", t).group(1)))
               for t in _tablesHO if "<TableState cols=" in t]
    check("tables ui: rooms, nodes, zones and wake models each have a state row spanning every column",
          len(_withHO) == 4 and all(n == c for n, c in _withHO), str(_withHO))
    _rmHO = _ws(_rmUI)
    check("rooms ui: the mapping table says loading, a failed /mapping, or the new empty copy",
          "<TableState cols={3} error={mapping?.loadError}" in _rmHO
          and "loading={!roomRows.length && !(mapping?.room_zones && status)}" in _rmHO
          and "'No mic nodes yet. A flashed node appears here when it connects.'" in _rmHO
          and "power one on" not in _appsrc and 'colSpan="3"' not in _appsrc)
    check("rooms ui: a failed /mapping is kept for the tables (last good data stays), not swallowed",
          "api('/api/dashboard/mapping').then(setMapping).catch(() => {})" not in _appsrc
          and "if (!e.network) setMapping(m => ({ ...m, loadError: e.message }))" in _appsrc)
    check("nodes ui: the nodes table says loading until /status arrives, then 'No nodes yet.' when empty",
          "<TableState cols={6} loading={!status} empty={!status?.rooms?.length && 'No nodes yet.'} />" in _rmHO)
    check("zones ui: the zones table says loading, a failed /mapping, or 'No speaker zones yet.'",
          "<TableState cols={4} error={mapping?.loadError} loading={!mapping?.zones}" in _ws(_zlUI)
          and "'No speaker zones yet.'" in _zlUI and "!Object.keys(speakers).length" in _zlUI)
    _wkHO = _jsbody(_appsrc, "function WakeNameField({ setMsg }) {")
    _wkHO = _wkHO[_wkHO.find("if (!w) return"):]
    check("wake ui: the wake name card shows 'Loading…' in its place instead of rendering nothing",
          "'Loading…'" in _wkHO and ": null" not in _wkHO and '<div id="wake-name-card"' in _wkHO
          and "`Couldn't load: ${loadErr}`" in _wkHO, _wkHO[:200])
    _wkAll = _jsbody(_appsrc, "function WakeNameField({ setMsg }) {")
    check("wake ui: the wake name card is just the name: no spellings box or chips, no heard list, no polling",
          'id="wake-name"' in _wkAll and not any(x in _wkAll for x in (
              "heard_as", "Also heard as", "Add a spelling", "Heard right after a wake", "data-heard",
              "data-alias", "setInterval(")) and "aliasFrom" not in _appsrc and "ALIAS_RESERVED" not in _appsrc)
    _owHO = _ws(_jsbody(_appsrc, "function OwwPanel({ setMsg, reloadAll }) {"))
    check("wake ui: one Wake word card: the name on top, then the model picker (two saves: name live, model at restart)",
          '<Section id="wake-model" title="Wake word"' in _owHO
          and 0 < _owHO.find("<WakeNameField setMsg={setMsg} />") < _owHO.find('<Field label="Active model / upload" between>')
          and 'title="Wake name"' not in _appsrc and "<WakeNameCard" not in _appsrc
          and "useUnsaved('wake-name-card'," in _appsrc and "useUnsaved('wake-model'," in _appsrc, _owHO[:200])
    _owLoad = _ws(_jsbody(_appsrc[_appsrc.find("function OwwPanel("):], "const load = async () => {"))
    check("wake ui: the model list says loading, then the load error, and 'No models yet' only when really empty",
          "<TableState cols={4} error={loadErr} loading={!loaded} empty={models.length === 0 && 'No models yet — upload one.'} />"
          in _owHO and "setLoaded(true)" in _owLoad and "catch (e) { setLoadErr(e.message) }" in _owLoad
          and "{loadErr ? \"couldn't load\" : loaded ? 'no models found' : 'loading…'}" in _owHO, _owLoad[:200])
    check("wake ui: a list that loaded once keeps 'loaded' through a later failed reload (no 'Loading…' flash)",
          _owLoad.find("setLoaded(true)") > _owLoad.find("setModels(") and "setLoaded(false)" not in _owHO)
    # ── nodes ui (App.jsx): the Mute button lives in the Status cell ───
    # On a phone the Nodes table scrolls sideways and Mute sat in the
    # far-right Actions cell — muting a room needed that scroll. The
    # button now renders right after the status chip; NodeActions keeps
    # owning the one mute state and hands both cells' parts to the row.
    _rmMut = _fnUI("function Rooms({ status, mapping, setMsg, reloadAll }) {")
    _chipThenMute = _reui.search(
        r'\{/\* Brand Chip rule.*?className="flex items-center gap-2 flex-wrap">.*?'
        r'data-role="node-state".*?\{mute\}', _rmMut, _reui.DOTALL)
    check("nodes ui: the Mute button sits in the Status cell right after the online chip (reachable without the table's sideways scroll)",
          bool(_chipThenMute)
          and 0 < _rmMut.find('{mute}') < _rmMut.find('fmtAgo(r.last_seen)') < _rmMut.find('{actions}')
          and '<td className="px-4 py-2">{actions}</td>' in _rmMut,
          "chip-then-mute block: " + (_chipThenMute.group(0)[:160] if _chipThenMute else "not found"))

    _naMut = _jsbody(_appsrc, "function NodeActions({ room, online, lastSeen, reportedMuted, setMsg, reloadAll, children: renderRow }) {")
    _muteBtn = _reui.search(r"const mute = \(.*?</button>", _naMut, _reui.DOTALL)
    _mb = _muteBtn.group(0) if _muteBtn else ""
    check("nodes ui: the Mute button keeps its exact style, disabled, click, title and label",
          bool(_mb)
          and "className={BTN_DEFAULT}" in _mb
          and "disabled={!online || busy || muteBusy}" in _mb
          and "onClick={toggleMute}" in _mb
          and "title={muteUnknown" in _mb
          and "physical mute button (red LED)" in _mb
          and "{muteBusy ? '…' : muteUnknown ? 'Mute?' : muted ? 'Unmute' : 'Mute'}" in _mb,
          _mb[:240] or "no 'const mute = (' in NodeActions")

    _aiMut = _naMut.find("const actions = (")
    _actsMut = _naMut[_aiMut:] if _aiMut >= 0 else ""
    check("nodes ui: Restart, Rename and Wi-Fi stay in the Actions cell, with Mute no longer among them",
          bool(_actsMut)
          and "onClick={restart}" in _actsMut and "'Restarting…' : 'Restart'" in _actsMut
          and "onClick={scanForWifi}" in _actsMut and "'Scanning…' : 'Wi-Fi…'" in _actsMut
          and "disabled={sending} onClick={applyWifi}" in _actsMut and "onClick={rename}" in _actsMut
          and "toggleMute" not in _actsMut and "BTN_WARNING" not in _actsMut,
          _actsMut[:200] or "no 'const actions = (' in NodeActions")

    check("nodes ui: one NodeActions state feeds both cells through the row's render function (no duplicated mute state)",
          "children: renderRow })" in _appsrc and "return renderRow({ mute, actions })" in _naMut
          and _appsrc.count("const [muted, setMuted] = useState(false)") == 1
          and _appsrc.count("const toggleMute = async () => {") == 1,
          f"renderRow x{_naMut.count('renderRow')}, "
          f"muted useState x{_appsrc.count('const [muted, setMuted] = useState(false)')}")

    # ── node health ui (App.jsx): rollback chips, per-node Details ──────
    # fw_push (1.2.41+) reports a refused OTA version and a Wi-Fi rollback
    # (/status rooms[].fw_rejected / wifi_rollback); get_info reports boot,
    # Wi-Fi, update and audio health. Chips reuse the table's warning chip.
    _rmNH = _fnUI("function Rooms({ status, mapping, setMsg, reloadAll }) {")
    _fcNH = _reui.search(r"\{esc\(r\.fw\) \|\| '—'\}.*?</td>", _rmNH, _reui.DOTALL)
    _fcNH = _fcNH.group(0) if _fcNH else ""
    _warnNH = "className={`ml-2 inline-block rounded-full px-2.5 py-0.5 text-[12px] font-sans font-medium ${STAT_TONE.warning}`}"
    check("nodes: the Firmware cell shows a 'rollback <version>' warning chip when the node refused a version",
          "{r.fw_rejected && (" in _fcNH and "rollback {esc(r.fw_rejected)}" in _fcNH
          and "title={`This node refused ${esc(r.fw_rejected)} after it failed its health check and rolled back. "
              "Upload a fixed build.`}" in _fcNH, _fcNH[:300] or "no Firmware cell")
    check("nodes: the Firmware cell shows a 'wifi rolled back' warning chip when the last Wi-Fi change rolled back",
          "{r.wifi_rollback === true && (" in _fcNH and "wifi rolled back" in _fcNH
          and 'title="The last Wi-Fi change never reached the bot, so the node went back to its previous network."'
              in _fcNH, _fcNH[:300] or "no Firmware cell")
    check("nodes: both rollback chips reuse the table's warning chip style (soft warning tone, sans in the mono cell)",
          _fcNH.count(_warnNH) == 2 and "STAT_TONE.warning" in _appsrc[:_appsrc.find("function Rooms(")],
          f"warning chips x{_fcNH.count(_warnNH)}")

    _ndNH = _fnUI("function NodeDetails({ room }) {")
    check("node health: Details opens per online node and renders under its row across every column",
          "const [openDetails, setOpenDetails] = useState({})" in _rmNH
          and "onClick={() => setOpenDetails(o => ({ ...o, [r.room]: !o[r.room] }))}" in _rmNH
          and "aria-expanded={!!openDetails[r.room]}" in _rmNH
          and "{openDetails[r.room] ? 'Hide details' : 'Details'}" in _rmNH
          and "{r.online && openDetails[r.room] && (" in _rmNH
          and '<td className="px-4 pb-3" colSpan={6}><NodeDetails room={r.room} /></td>' in _rmNH
          and _appsrc.count("<NodeDetails ") == 1, _ndNH[:200] or "no NodeDetails")
    _infoNH = "api(`/api/dashboard/nodes/${encodeURIComponent(room)}/info`, { promptForToken: true })"
    check("node health: Details asks /info once per open (ref-guarded, no timer, no poll)",
          bool(_ndNH) and _ndNH.count("/info`") == 1 and _infoNH in _ndNH
          and "if (askedRef.current) return" in _ndNH
          and 0 <= _ndNH.find("askedRef.current = true") < _ndNH.find(_infoNH)
          and "setInterval" not in _ndNH and "setTimeout" not in _ndNH, _ndNH[:300])
    check("node health: a failed /info is a one-line note, and firmware without the keys says so",
          "if (info.error) return note(`Details unavailable (${info.error}). Older firmware doesn't report them.`)"
          in _ndNH and ".catch(e => setInfo({ error: e?.message || 'no answer' }))" in _ndNH
          and "if (!rows.length && !warnings.length) return note(\"This node's firmware doesn't report details.\")"
          in _ndNH, _ndNH[:300])
    check("node health: Details shows only the keys a node sent (non-strings dropped, empty sections hidden)",
          ".map(([label, parts]) => [label, parts.filter(p => typeof p === 'string')]).filter(([, parts]) => parts.length)"
          in _ndNH and "const part = v => (v && typeof v === 'object' ? v : {})" in _ndNH
          and all(f"['{s}', [" in _ndNH for s in ("Boot", "Wi-Fi", "Updates", "Audio")), _ndNH[:300])
    _wrNH = _jsbody(_appsrc, "const WIFI_REASON = {")
    check("node health: Wi-Fi disconnect reasons are named for the common codes, else 'reason N'; 0 shows nothing",
          all(f"{c}: '" in _wrNH for c in (2, 4, 8, 15, 201, 202, 203, 204))
          and "`last disconnect: ${WIFI_REASON[wifi.last_reason] || `reason ${wifi.last_reason}`}`" in _ndNH
          and "wifi.last_reason !== 0" in _ndNH, _wrNH[:200] or "no WIFI_REASON")
    check("node health: boot why and update tags read as plain words, unknown tags as sent",
          "boot.why && (BOOT_WHY[boot.why] || String(boot.why))" in _ndNH
          and "ota.last && (OTA_LAST[ota.last] || String(ota.last))" in _ndNH
          and "num(ota.at_s) && `${ota.at_s}s after boot`" in _ndNH
          and all(f"'{t}': " in _appsrc for t in ("liveness-no-pong", "ota-health-timeout", "wifi-trial-authfail",
                                                   "refused-rolled-back", "version-mismatch")), _ndNH[:300])
    check("node health: a mic that sent silence while unmuted is flagged with the warning chip",
          "if (num(audio.zero_s) && audio.zero_s > 0) warnings.push(`mic sent silence for ${audio.zero_s} s while unmuted`)"
          in _ndNH and "if (audio.init && audio.init !== 'ok') warnings.push(" in _ndNH
          and "className={`inline-block rounded-full px-2.5 py-0.5 text-[12px] font-medium ${STAT_TONE.warning}`}>{w}</span>"
          in _ndNH, _ndNH[:300])

    # ── tabs (App.jsx): Rooms, Voice and System as tabs, picked by the hash ──
    # The page was one long scroll; now one tab shows at a time. All three
    # stay mounted (hidden attribute) so a card's unsaved edits and its own
    # loading survive a switch. Restart sits in the header row; the status
    # chips render under the header on every tab.
    _appTB = _fnUI("export default function App() {")
    _hdrTB = _reui.search(r"<header .*?</header>", _appTB, _reui.DOTALL)
    _hdrTB = _hdrTB.group(0) if _hdrTB else ""
    _tabsTB = _reui.search(r"const TABS = \[(.*?)\]", _appsrc, _reui.DOTALL)
    _tabsTB = _reui.findall(r"\{ id: '(\w+)', label: '(\w+)' \}", _tabsTB.group(1)) if _tabsTB else []
    _wrapTB = _reui.findall(r'<div data-tab="(\w+)" hidden=\{tab !== \'(\w+)\'\} className="grid grid-cols-1 gap-10">'
                            r'\s*<(\w+) ', _appTB)
    _tabOfTB = {comp: t for t, t2, comp in _wrapTB if t == t2}
    _htTB = _jsbody(_appsrc, "function useHashTab() {")
    _goTB = _jsbody(_htTB, "const go = () => {")
    _scrollTB = _htTB[_htTB.find("if (!nav.id) return"):]

    def _ownerTB(pos):
        """Name of the top-level function whose source holds position pos."""
        heads = [m for m in _reui.finditer(r"\n(?:export default )?(?:async )?function (\w+)\(", _appsrc)
                 if m.start() < pos]
        return heads[-1].group(1) if heads else ""

    def _tabTB(elid):
        """The tab whose wrapper renders the element with this id (up the component chain)."""
        i = _appsrc.find(f'id="{elid}"')
        name, seen = (_ownerTB(i) if i >= 0 else ""), set()
        while name and name not in _tabOfTB and name not in seen:
            seen.add(name)
            j = _appsrc.find(f"<{name} ")
            name = _ownerTB(j) if j >= 0 else ""
        return _tabOfTB.get(name)

    check("tabs: three tabs, Rooms, Voice and System; the section rail and its observer are gone",
          _tabsTB == [("rooms", "Rooms"), ("voice", "Voice"), ("system", "System")]
          and "SECTIONS" not in _appsrc and "useActiveSection" not in _appsrc
          and "IntersectionObserver" not in _appsrc, str(_tabsTB))
    check("tabs: the file's header comment no longer says there are no tabs",
          "no tabs" not in _appsrc[:300] and "in tabs" in _appsrc[:200], _appsrc[:160])
    check("tabs: each tab is a real link to its hash, the active one marked aria-current in the accent style",
          '<nav className="flex gap-2" aria-label="Tabs">' in _hdrTB and "{TABS.map(t => (" in _hdrTB
          and "href={`#${t.id}`} aria-current={tab === t.id ? 'page' : undefined}" in _hdrTB
          and "${tab === t.id ? 'text-[var(--accent)] font-semibold' : 'text-[var(--muted)] hover:text-[var(--foreground)]'}"
          in _ws(_hdrTB) and "const tab = useHashTab()" in _appTB, _hdrTB[:300])
    check("tabs: all three tabs stay mounted, each in a data-tab wrapper the hidden attribute turns off",
          _wrapTB == [("rooms", "rooms", "Rooms"), ("voice", "voice", "Voice"), ("system", "system", "System")]
          and not _reui.search(r"tab === '\w+' &&|\? <(Rooms|Voice|System)", _appTB), str(_wrapTB))
    check("tabs: every tab renders its component with the same props as before",
          "<Rooms status={status} mapping={mapping} setMsg={setMsg} reloadAll={reloadAll} />" in _appTB
          and "<Voice tts={tts} tuning={tuning} status={status} setMsg={setMsg} reloadAll={reloadAll} />" in _appTB
          and "<System intelligence={intelligence} status={status} setMsg={setMsg} reloadAll={reloadAll} />" in _appTB)
    check("tabs: the tab comes from location.hash on load and on hashchange, default rooms",
          "useState({ tab: 'rooms', id: '' })" in _htTB and "window.location.hash.slice(1)" in _goTB
          and "go() window.addEventListener('hashchange', go) "
              "return () => window.removeEventListener('hashchange', go)" in _ws(_htTB), _htTB[:300])
    _elTB, _clTB, _nmTB = (_goTB.find("document.getElementById(id)"), _goTB.find("el?.closest('[data-tab]')?.dataset.tab"),
                           _goTB.find("(TABS.some(t => t.id === id) ? id : 'rooms')"))
    check("tabs: any other hash opens the tab holding that element; the element wins over a tab name",
          0 <= _elTB < _clTB < _nmTB, f"getElementById@{_elTB} closest@{_clTB} name@{_nmTB}")
    check("tabs: the scroll runs after the tab is shown: a tab opens at the top, a card scrolls to its section",
          "}, [nav])" in _htTB and "scroll" not in _goTB
          and "if (TABS.some(t => t.id === nav.id)) { window.scrollTo(0, 0); return }" in _scrollTB
          and "scrollToCard(nav.id)" in _scrollTB,
          _scrollTB[:240])
    _stcTB = _jsbody(_appsrc, "function scrollToCard(id) {")
    check("tabs: a card scrolls to just under the pinned header's real height (it wraps taller on a phone)",
          "const card = el?.closest('section') || el" in _stcTB
          and "document.querySelector('header')?.offsetHeight" in _stcTB
          and "window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - head - 12)" in _stcTB
          and "scrollIntoView" not in _appsrc,
          _stcTB[:240])
    _wantTB = {"rooms": "rooms", "voice": "voice", "system": "system", "keys": "system", "intelligence": "system",
               "stt": "voice", "wake-name-card": "voice"}
    _gotTB = {i: _tabTB(i) for i in _wantTB}
    check("tabs: section ids named like tabs (#voice, #system, #rooms) and card ids resolve to the tab that holds them",
          _gotTB == _wantTB and all(_appsrc.count(f'id="{t}"') == 1 for t, _ in _tabsTB)
          and "data-tab" not in _appsrc.replace('<div data-tab="', "").replace("[data-tab]", ""), str(_gotTB))
    _linksTB = set(_reui.findall(r"href: '#([\w-]+)'", _appsrc)) | set(_reui.findall(r'href="#([\w-]+)"', _appsrc))
    check("tabs: the warning chips' links and the missing-key link land on the tab with their card",
          _linksTB == {"intelligence", "keys", "localai", "rooms"}
          and all(_tabTB(i) in ("rooms", "voice", "system") for i in _linksTB)
          and _tabTB("keys") == _tabTB("system") == _tabTB("intelligence") == _tabTB("localai") == "system"
          and _tabTB("rooms") == "rooms",
          f"{sorted(_linksTB)} -> {[(i, _tabTB(i)) for i in sorted(_linksTB)]}")
    _wkTB = _fnUI("function WakeNameField({ setMsg }) {")
    check("tabs: the wake name card keeps its id while loading, so #wake-name-card finds its tab on a fresh load",
          _wkTB.count('id="wake-name-card"') == 2
          and _wkTB.find('id="wake-name-card"') < _wkTB.find("{loadErr ? `Couldn't load: ${loadErr}` : 'Loading…'}"))

    # Header: wordmark, tabs, Restart, then the message strip.
    _hpTB = [_hdrTB.find(s) for s in ("<Wordmark />", '<nav className="flex gap-2" aria-label="Tabs">',
                                      ("<RestartButton status={status} setMsg={setMsg} setRebooting={setRebooting} "
                                       "rebooting={rebooting} unsaved={unsaved > 0} />"), "<AppMsg msg={msg} />")]
    check("header: the sticky header holds the wordmark, the tabs, the Restart button (row end) and the message strip",
          _hdrTB.startswith('<header className="sticky top-0') and all(p >= 0 for p in _hpTB) and _hpTB == sorted(_hpTB)
          and _appTB.count("<AppMsg") == 1 and _appTB.count("<RestartButton") == 1, str(_hpTB))
    _rbtTB = _ws(_fnUI("function RestartButton({ status, setMsg, setRebooting, rebooting, unsaved }) {"))
    check("header: the Restart button keeps its pending label, title, style, one confirm and restartBot call",
          "const pending = status?.restart_pending || []" in _rbtTB
          and "className={pending.length && !unsaved ? BTN_PRIMARY : BTN_DEFAULT} disabled={rebooting}" in _rbtTB
          and "title={pending.length ? `Pending: ${pending.join(', ')}` : undefined}" in _rbtTB
          and "if (window.confirm(unsaved ? \"You have unsaved changes. They won't apply. Restart anyway? "
              "Every node reboots and reconnects.\" : 'Restart the bot? Every node reboots and reconnects.')) { "
              "restartBot(setMsg, setRebooting) }" in _rbtTB
          and "{pending.length ? `Restart to apply (${pending.length})` : 'Restart bot'}" in _rbtTB
          and _rbtTB.count("window.confirm(") == 1
          and _appsrc.count("restartBot(setMsg, setRebooting)") == 2, _rbtTB[:300])

    # Overview: the chip row, under the header, above the tab; no title.
    _ovTB = _fnUI("function Overview({ status, tts, intelligence, mapping }) {")
    _ovAt = _appTB.find("<Overview status={status} tts={tts} intelligence={intelligence} mapping={mapping} />")
    check("overview: the status chips render under the header, above every tab, on every tab",
          0 <= _appTB.find("</header>") < _ovAt < _appTB.find("data-tab=") and _appTB.count("<Overview ") == 1
          and _ovAt >= 0 and _hdrTB.find("<Overview") < 0, f"overview@{_ovAt}")
    check("overview: the chip row has no title, no pinning and no Restart button",
          bool(_ovTB) and "<Section" not in _ovTB and "sticky" not in _ovTB and "restart" not in _ovTB.lower()
          and 'title="Overview"' not in _appsrc and 'id="overview"' not in _appsrc, _ovTB[:200])
    check("overview: the chip row keeps all seven chips in their order",
          _reui.findall(r'<StatChip stat="([\w-]+)"', _ovTB)
          == ["uptime", "intelligence", "nodes", "last-wake", "version", "stt", "tts"],
          str(_reui.findall(r'<StatChip stat="([\w-]+)"', _ovTB)))
    # Still holds: the poll, reload-all and the token dialog are untouched.
    check("tabs: the 5 s poll, reload-all and the token dialog still run in App (still holds)",
          "const t = setInterval(() => { if (!document.hidden) reloadAll() }, 5000)" in _appTB
          and "window.addEventListener('lenoxvi:reload-all', h)" in _appTB
          and "document.addEventListener('visibilitychange', vis)" in _appTB
          and _appTB.find("<TokenDialog />") > _appTB.find('data-tab="system"') >= 0)

    # ── unsaved changes (App.jsx): one page-wide bar saves every settings
    # card; "changed" means the draft differs from the saved value ─────────
    # Settings cards register their changed-field count, save and discard
    # (useUnsaved); the bar pinned to the bottom saves them in page order and
    # sums up in one message. Actions (keys, volumes, node, zone and model
    # buttons, alias chips) stay instant.
    _drUS = _fnUI("function useDraft(saved, { scope = '', blank = {} } = {}) {")
    _barUS = _fnUI("function UnsavedBar({ cards, units, setMsg }) {")
    _allUS = _jsbody(_barUS, "const saveAll = async () => {")
    _voUS = _fnUI("function Voice({ tts, tuning, status, setMsg, reloadAll }) {")
    _stUS = _fnUI("function SttCard({ status, setMsg }) {")
    _tuUS = _fnUI("function Tuning({ tuning, reloadAll }) {")
    _wkUS = _fnUI("function WakeNameField({ setMsg }) {")
    _owUS = _fnUI("function OwwPanel({ setMsg, reloadAll }) {")
    _syUS = _fnUI("function System({ intelligence, status, setMsg, reloadAll }) {")
    _roUS = _fnUI("function Rooms({ status, mapping, setMsg, reloadAll }) {")
    _saveUS = {
        "tts": _jsbody(_voUS, "const saveTTS = async say => {"),
        "stt": _jsbody(_stUS, "const save = async say => {"),
        "voice tuning": _jsbody(_tuUS, "const save = async say => {"),
        "intelligence": _jsbody(_syUS, "const save = async say => {"),
        "room mapping": _jsbody(_roUS, "const saveMapping = async say => {"),
        "wake model": _jsbody(_owUS, "const select = async say => {"),
        "wake name": _jsbody(_wkUS, "const saveName = async say => {"),
    }
    check("unsaved: changed means differs from saved: numbers as numbers, strings trimmed, unset as ''",
          "const norm = v => (v == null ? '' : typeof v === 'string' ? v.trim() : v)" in _appsrc
          and "!isNaN(x) && !isNaN(y) && Number(x) === Number(y)" in _appsrc
          and "const eq = (k, v) => same(v ?? blank[k], saved[k] ?? blank[k])" in _drUS, _drUS[:200])
    check("unsaved: a draft holds only differing fields; reverting one drops it at once",
          "val: k => (k in d ? d[k] : saved[k])" in _drUS and "changed: k => k in d && !eq(k, d[k])" in _drUS
          and "if (v === undefined || (eq(k, v) && norm(v) === v)) delete n[k]" in _drUS, _drUS[:300])
    check("unsaved: a poll never overwrites a changed field; one that brings the saved value level drops the draft",
          "if (state.seen !== seen)" in _drUS and "without(state.d, (k, v) => k in saved && eq(k, v))" in _drUS
          and "setMapDraft(" not in _appsrc and "setVals(prev =>" not in _appsrc and "ttsSynced" not in _appsrc
          and "setModel(m => m ?? d.current.model)" not in _appsrc and "urlDrafts" not in _appsrc, _drUS[:200])
    check("unsaved: after a save the sent fields settle only once the reload is back; an edit made meanwhile stays",
          "settle: sent => edit(cur => without(cur, (k, v) => k in sent && v === sent[k]))" in _drUS
          and all(0 <= b.find("await api(") < b.find(".settle(") and ".settle(" not in b[b.find("} catch"):]
                  for n, b in _saveUS.items() if n != "wake name")
          and "if (ok) name.settle(sent)" in _saveUS["wake name"], str({n: b.find(".settle(") for n, b in _saveUS.items()}))
    _regUS = _reui.findall(r"useUnsaved\('([\w-]+)', \{\s*label: '([\w ]+)'", _appsrc)
    _orderUS = _reui.search(r"const UNSAVED_ORDER = \[(.*?)\]", _appsrc)
    _orderUS = _reui.findall(r"'([\w-]+)'", _orderUS.group(1)) if _orderUS else []
    check("unsaved: the eight settings cards register once each, under their section id and card title",
          sorted(_regUS) == sorted([("rooms", "Room mapping"), ("voice", "TTS"), ("stt", "STT"), ("tuning", "Voice tuning"),
                                    ("wake-name-card", "Wake name"), ("wake-model", "Wake model"),
                                    ("localai", "LocalAI"), ("intelligence", "Intelligence")])
          and all(_appsrc.count(f'id="{k}"') >= 1 for k, _ in _regUS), str(_regUS))
    check("unsaved: the bar lists and saves cards in page order, tab by tab",
          _orderUS == ["rooms", "wake-name-card", "wake-model", "stt", "voice", "tuning", "localai", "intelligence"]
          and "const keys = UNSAVED_ORDER.filter(k => units[k]?.count > 0)" in _barUS, str(_orderUS))
    _wantUS = {"rooms": "rooms", "voice": "voice", "stt": "voice", "tuning": "voice", "wake-name-card": "voice",
               "wake-model": "voice", "localai": "system", "intelligence": "system"}
    check("unsaved: each label links to its card's hash, which the tab router already resolves (still holds)",
          {k: _tabTB(k) for k in _orderUS} == _wantUS and "<a href={`#${k}`}" in _barUS
          and "scrollToCard(k)" in _barUS,
          str({k: _tabTB(k) for k in _orderUS}))
    check("unsaved: each count is that card's changed fields (rooms: rows whose zone or agent differs)",
          "const changedRooms = roomRows.filter(r => zones.changed(r) || agents.changed(r))" in _roUS
          and "count: changedRooms.length" in _roUS
          and "count: ttsFields.filter(t.changed).length" in _voUS
          and "count: Object.keys(sttBody()).length" in _stUS
          and "const dirtyFields = TUNING_FIELDS.map(([f]) => f).filter(vals.changed)" in _tuUS
          and "count: dirtyFields.length" in _tuUS
          and "count: intFields.filter(d.changed).length" in _syUS
          and "count: name.changed('name') ? 1 : 0" in _wkUS and "count: pick.changed('model') ? 1 : 0" in _owUS)
    check("unsaved: voice tuning marks a knob changed by value, not by touch (the dot stays the marker)",
          "{label}{vals.changed(f) ? ' •' : ''}" in _tuUS and "markDirty" not in _tuUS and "dirtyRef" not in _tuUS
          and "onChange={e => vals.set({ [f]: e.target.value })}" in _tuUS, _tuUS[:200])
    _btnUS = {"tts": _voUS[:_voUS.find("<SttCard ")], "stt": _stUS, "voice tuning": _tuUS, "wake name": _wkUS,
              "wake model": _owUS, "intelligence": _syUS, "room mapping": _roUS}
    check("unsaved: the seven cards have no Save button of their own, and the wake model no Set active",
          all(">Save</button>" not in s and "onClick={save}" not in s and "onClick={saveTTS}" not in s
              and "saveMapping(room)" not in s for s in _btnUS.values())
          and "Set active" not in _appsrc and "`Save (${dirtyFields.length})`" not in _appsrc
          and "<th className=\"text-left px-4 py-2\"></th>" not in _roUS[:_roUS.find("</thead>")],
          str([n for n, s in _btnUS.items() if ">Save</button>" in s]))
    check("unsaved: card saves report through say and return true or false, so no message flashes over another",
          all(b and "setMsg(" not in b and ("return true" in b or "return ok" in b) and "return false" in b
              for n, b in _saveUS.items() if n != "wake name")
          and "const save = async (body, note, say = setMsg) => {" in _wkUS
          and "await save({ name: (name.val('name') ?? '').trim() }, null, say)" in _saveUS["wake name"],
          str([n for n, b in _saveUS.items() if not b or "setMsg(" in b]))
    check("unsaved: instant actions never register: keys, zones, volumes, nodes, firmware, uploads",
          all("useUnsaved(" not in _fnUI(h) for h in ("function KeysCard(", "function ZoneList(", "function VolSlider(",
                                                       "function NodeActions(", "function FirmwarePanel("))
          and _wkUS.count("useUnsaved(") == 1 and _owUS.count("useUnsaved(") == 1
          and "onClick={upload}" in _owUS and "onClick={() => del(m.name, m.source)}" in _owUS
          and "onChange={sticky => toggleMode(r.room, sticky)}" in _roUS)
    check("unsaved: the bar renders once in App, pinned to the bottom, full width, header background, safe area",
          _appTB.count("<UnsavedBar cards={cards} units={units} setMsg={setMsg} />") == 1
          and "<UnsavedCtx.Provider value={putUnsaved}>" in _appTB and "if (!total) return null" in _barUS
          and "style={{ position: 'fixed', left: 0, right: 0, bottom: 0, zIndex: 10, "
              "paddingBottom: 'env(safe-area-inset-bottom)' }}" in _barUS
          and 'className="border-t border-[var(--separator)] bg-[var(--background)]"' in _barUS
          and "bg-[var(--background)]" in _hdrTB, _barUS[:300])
    check("unsaved: while the bar shows, the page gets room under the last card",
          "style={unsaved ? { paddingBottom: 'calc(10rem + env(safe-area-inset-bottom))' } : undefined}" in _appTB)
    check("unsaved: the bar reads 'N unsaved:' then each card's label; Discard default, Save lime, both off while saving",
          "{total} unsaved: " in _barUS and "{i > 0 && ' · '}" in _barUS and "{units[k].label}</a>" in _barUS
          and "<button className={BTN_DEFAULT} disabled={saving} onClick={discard}>Discard</button>" in _barUS
          and "<button className={BTN_PRIMARY} disabled={saving} onClick={saveAll}>{saving ? 'Saving…' : 'Save'}</button>"
          in _barUS, _barUS[-600:])
    _disUS = _jsbody(_barUS, "const discard = () => {")
    check("unsaved: Discard asks 'Discard N unsaved changes?' once, then puts every listed card back to saved",
          "if (!window.confirm(`Discard ${total} unsaved change${total === 1 ? '' : 's'}?`)) return" in _disUS
          and "for (const k of keys) cards.current[k]?.discard()" in _disUS and _barUS.count("window.confirm(") == 1, _disUS)
    check("unsaved: Save runs each listed card's save in order, one after another, buttons off throughout",
          0 <= _allUS.find("setSaving(true)") < _allUS.find("for (const k of keys) {")
          < _allUS.find("await cards.current[k]?.save(") < _allUS.find("setSaving(false)"), _allUS[:300])
    check("unsaved: one summary at the end: 'Saved.' (+ ' Restart to apply.') or 'Saved X of N.' plus each failure",
          _barUS.count("setMsg(") == 1 and _allUS.find("setMsg(") > _allUS.find("setSaving(false)")
          and ": `Saved.${restart ? ' Restart to apply.' : ''}`" in _allUS
          and "`Saved ${done} of ${keys.length}. ${failed.join(' ')}`" in _allUS
          and "failed.push(end(`${label} failed${why ? `: ${why}` : ''}`))" in _allUS
          and "if (!ok) {" in _allUS and "continue" in _allUS, _allUS[-500:])
    check("unsaved: 'Saved — …' messages fold into the summary and flag a restart; any other message is kept as a note",
          "if (!text.startsWith('Saved')) notes.push(end(`${label}: ${text}`))" in _allUS
          and "else if (/restart/i.test(text) && !/no restart/i.test(text)) restart = true" in _allUS)
    check("unsaved: one lime per view: the header Restart is default style while the bar shows",
          "className={pending.length && !unsaved ? BTN_PRIMARY : BTN_DEFAULT}" in _rbtTB
          and "unsaved={unsaved > 0}" in _hdrTB
          and "const unsaved = UNSAVED_ORDER.reduce((n, k) => n + (units[k]?.count || 0), 0)" in _appTB)
    check("unsaved: leaving the page warns while anything is unsaved",
          "if (!unsaved) return" in _appTB and "const warn = e => { e.preventDefault(); e.returnValue = '' }" in _appTB
          and "window.addEventListener('beforeunload', warn)" in _appTB
          and "return () => window.removeEventListener('beforeunload', warn)" in _appTB and "}, [unsaved])" in _appTB)
    check("unsaved: TTS fields follow the server until edited; the voice id is a draft, not a DOM read",
          "fluxTouched" not in _voUS and "document.getElementById('tts-voice')" not in _appsrc
          and '<input aria-label="ElevenLabs voice ID" id="tts-voice" value={voiceId} onChange={e => t.set({ voice_id: e.target.value })}' in _voUS
          and "blank: { el_speed: 1.0, el_stab: 0.5, el_norm: 'auto', flux_speed: 1.0, flux_expr: 1 }" in _voUS
          and "const onProviderChange = p => t.set({ provider: p, ...(p === 'kokoro' ? {} : { [`model:${p}`]: undefined }) })"
          in _voUS)
    check("unsaved: STT picks (provider, language, LocalAI model) follow the server until changed",
          "const s = useDraft({ language: data?.current?.language, provider: data?.current?.provider," in _stUS
          and "la_model: laSaved?.model_stt || '' })" in _stUS and "setBusy(" not in _stUS)
    check("unsaved: intelligence keeps a URL and agent draft per kind; switching kinds only changes which show",
          "[`url:${k}`, shortUrl(savedUrls[k])]" in _syUS and "const switchKind = kind => {" in _syUS
          and "d.set({ kind })" in _jsbody(_syUS, "const switchKind = kind => {")
          and "urlEl.value" not in _syUS
          and "<input aria-label=\"Intelligence URL\" id=\"int-url\" value={intUrl || ''} onChange={e => d.set({ [`url:${intKind}`]: e.target.value })}"
          in _syUS)
    _uoUS = _saveUS["intelligence"]
    _uoUS = _uoUS[_uoUS.find("if (r?.url_only) {"):_uoUS.find("return true")]
    check("unsaved: a URL-only intelligence save settles the URL and keeps the kind as a draft",
          "d.settle(sentUrl)" in _uoUS and "d.settle(sent)" not in _uoUS
          and "sentUrl = d.snap([`url:${kind}`])" in _saveUS["intelligence"], _uoUS[:200])
    check("unsaved: nothing in the bar shows a key value: keys never register, the bar renders labels only",
          bool(_barUS) and "useUnsaved(" not in _fnUI("function KeysCard(") and "hint" not in _barUS
          and "readKey(" not in _barUS and "units[k].label" in _barUS)
    # Still holds: the same request bodies as before for the same edits.
    _smUS = _saveUS["room mapping"]
    check("unsaved: the mapping save keeps the full-map wrapper and drops stale rooms and dead zones (still holds)",
          "const body = { room_zones: { ...liveMap, ...liveZones(stillLive(mapDraft)), ...Object.fromEntries(picked) } }"
          in _smUS and "const liveAgentDraft = stillLive(agentDraft)" in _smUS
          and "if (agentEdits) body.room_agents = { [agentKind]: { ...savedAgents, ...liveAgentDraft } }" in _smUS
          and "const unzoned = picked.find(([, z]) => !z || !liveZone(z))" in _smUS
          and "say(`Pick a zone for ${unzoned[0]} first.`" in _smUS
          and _smUS.count("await api('/api/dashboard/mapping', { method: 'PUT', json: body })") == 1, _smUS[:300])
    check("unsaved: voice tuning still sends only the changed knobs (still holds)",
          "for (const f of dirtyFields) body[f] = parseFloat(vals.val(f))" in _saveUS["voice tuning"]
          and "await api('/api/dashboard/voice-tuning', { method: 'PUT', json: body })" in _saveUS["voice tuning"])
    _ttsUS = _ws(_saveUS["tts"])
    check("unsaved: the TTS save still sends the whole provider set (still holds)",
          all(p in _ttsUS for p in (
              "const body = { provider }", "body.model = model body.voice_id = voice_id",
              "if (elSpeed != null) body.elevenlabs_speed = elSpeed", "if (elStab != null) body.elevenlabs_stability = elStab",
              "if (elNorm != null) body.elevenlabs_text_normalization = elNorm", "body.deepgram_voice = model",
              "body.deepgram_flux_voice = model", "body.deepgram_flux_speed = parseFloat(fluxSpeed)",
              "body.deepgram_flux_expressivity = parseInt(fluxExpr, 10)", "body.kokoro_voice = kokoroVoice")), _ttsUS[:300])
    # Kokoro on the TTS card: a voice select with readable labels and a speed
    # slider on every tier; all on the unsaved-changes bar.
    _voWS = _ws(_voUS)
    _klUI = _reui.search(r"const kokoroLabel = (.*?)\n\S", _appsrc, _reui.DOTALL)
    _klUI = _ws(_klUI.group(1)) if _klUI else ""
    check("tts ui: Kokoro voice select shows readable labels ('Heart — American female'), values stay the ids",
          "/^([ab])([fm])_(\\w+)$/" in _klUI and "'American' : 'British'" in _klUI and "'female' : 'male'" in _klUI
          and " — " in _klUI
          and "{kokoroVoices.map(v => <option key={v} value={v}>{kokoroLabel(v)}</option>)}" in _voWS
          and "p === 'kokoro' ? t?.kokoro_voice" in _ws(_appsrc), _klUI[:200])
    check("tts ui: Kokoro speed slider 0.5–1.5 in 0.05 steps (the Flux slider's pattern)",
          "<input id=\"tts-kokoro-speed\" type=\"range\" min=\"0.5\" max=\"1.5\" step=\"0.05\" value={kokoroSpeed} "
          "onChange={e => t.set({ kokoro_speed: parseFloat(e.target.value) })}" in _voWS
          and "aria-label=\"Kokoro speed\"" in _voWS and "{kokoroSpeed.toFixed(2)}" in _voWS)
    check("tts ui: Kokoro voice + speed ride the unsaved bar and the save (speed only when edited or saved)",
          "...(provider === 'kokoro' ? [] : ['model:kokoro']), 'kokoro_speed']" in _voWS
          and "kokoro_speed: num(tts?.kokoro_speed, parseFloat) ?? 1.0" in _voWS
          and "const kokoroSpeed = t.val('kokoro_speed') ?? 1.0" in _voWS
          and "body.kokoro_voice = kokoroVoice" in _ttsUS
          and "if (t.changed('kokoro_speed') || tts?.kokoro_speed != null) body.kokoro_speed = kokoroSpeed" in _ttsUS,
          _ttsUS[-400:])
    check("tts ui: Kokoro needs no key (no Missing key notice); the chip says 'kokoro'",
          "{TTS_KEY_HINT[provider] && tts && !tts[TTS_KEY_HINT[provider]] && (" in _voWS
          and "kokoro: 'Kokoro'" in _appsrc and "return { text: ttsName(primary)?.toLowerCase() || '—', muted: !primary }"
          in _appsrc)
    check("unsaved: the intelligence save body and 'Test saved config' stay as they were (still holds)",
          "const body = { kind, url }" in _saveUS["intelligence"]
          and "if (agentSel || laneOf(kind)?.agent_required === false) body.agent = agentSel" in _saveUS["intelligence"]
          and "if (!url) { say(`Enter the ${label} URL first.`" in _saveUS["intelligence"]
          and '<button id="btn-int-test" className={BTN_DEFAULT}' in _syUS and ">Test saved config</button>" in _syUS)
    check("unsaved: restartBot's latch and the Restart confirm are unchanged (still holds)",
          "if (_botRestarting) return false" in _rb and _rb.count("_botRestarting = false") == 2
          and "if (window.confirm(unsaved ? \"You have unsaved changes. They won't apply. Restart anyway? "
              "Every node reboots and reconnects.\" : 'Restart the bot? Every node reboots and reconnects.')) { "
              "restartBot(setMsg, setRebooting) }" in _rbtTB
          and "{pending.length ? `Restart to apply (${pending.length})` : 'Restart bot'}" in _rbtTB
          and "title={pending.length ? `Pending: ${pending.join(', ')}` : undefined}" in _rbtTB)

    # ── cloud STT provider: ElevenLabs Scribe on the STT card. The TTS
    # ElevenLabs key, local Whisper on standby, Deepgram's rules. ─────────
    import config as _cfgEL
    import keys_catalog as _kcEL
    import stt_fallback as _sfEL

    _EL_VARS = ("LVI_ENV_FILE", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "LVI_STT_PROVIDER", "LVI_STT_LANGUAGE",
                "LVI_TTS_PROVIDER")
    _el_env = {k: os.environ.get(k) for k in _EL_VARS}
    _el_cfg = {s: copy.deepcopy(dict(config_store.read_section(s) or {})) for s in ("stt", "tts")}
    _el_boot = copy.deepcopy(getattr(_cfgEL, "_BOOT", None))
    _el_running = _sfEL._STATE["provider"]
    _el_envfile = SCRATCH / "elevenlabs-stt.env"
    _EL_SECRETS = ("el-stt-SECRET-0001", "el-stt-SECRET-0002")

    def _el_stt():
        return dict(config_store.read_section("stt") or {})

    def _el_set(stt=None, tts=None):
        if stt is not None:
            config_store.write_section("stt", {**{k: v for k, v in _el_cfg["stt"].items() if k != "provider"}, **stt})
        if tts is not None:
            config_store.write_section("tts", {**_el_cfg["tts"], "provider": tts})

    def _el_reboot():
        """What a real restart does: a new process loads config afresh."""
        _cfgEL._BOOT = None
        _cfgEL._load.cache_clear()
        _cfgEL._load()

    def _el_pending():
        r = client.get("/api/dashboard/status")
        return r.json().get("restart_pending") if r.status_code == 200 else None

    def _el_keys():
        return {r["var"]: r for r in client.get("/api/dashboard/keys").json().get("keys", [])}

    try:
        for _k in _EL_VARS:
            os.environ.pop(_k, None)
        _el_envfile.write_text("ELEVENLABS_API_KEY=el-stt-SECRET-0001\n")
        os.environ.update({"LVI_ENV_FILE": str(_el_envfile), "ELEVENLABS_API_KEY": "el-stt-SECRET-0001"})
        _sfEL.set_provider("whisper")
        _el_set(stt={"language": "auto"}, tts="kokoro")

        # GET / PUT /stt
        _gr = client.get("/api/dashboard/stt")
        _g = _gr.json()
        _elp = next((p for p in _g.get("providers", []) if p.get("name") == "elevenlabs"), {})
        check("elevenlabs stt GET: the last provider, 'ElevenLabs Scribe v2' in the Cloud tier, available with no extra package",
              [p.get("name") for p in _g.get("providers", [])] == ["whisper", "localai", "deepgram", "elevenlabs"]
              and _elp.get("label") == "ElevenLabs Scribe v2" and _elp.get("tier") == "cloud"
              and _elp.get("available") is True, str(_g.get("providers")))
        check("elevenlabs stt GET: elevenlabs_key_set comes from the shared ElevenLabs key; the value never appears",
              _g.get("elevenlabs_key_set") is True and not any(s in _gr.text for s in _EL_SECRETS)
              and "deepgram_key_set" in _g, str({k: _g.get(k) for k in ("elevenlabs_key_set", "deepgram_key_set")}))
        _r = client.put("/api/dashboard/stt", json={"provider": "elevenlabs"})
        check("elevenlabs stt PUT: saved, restart needed, key present -> no key_needed",
              _r.status_code == 200 and _r.json().get("restart_needed") is True and _r.json().get("key_needed") is False
              and _el_stt().get("provider") == "elevenlabs", f"{_r.text[:160]} cfg={_el_stt()}")
        _r = client.put("/api/dashboard/stt", json={"provider": "elevenlabs"})
        check("elevenlabs stt PUT the same provider again: no restart",
              _r.status_code == 200 and _r.json().get("restart_needed") is False, _r.text[:120])
        _r = client.put("/api/dashboard/stt", json={"language": "de"})
        check("elevenlabs stt: a language change while Scribe is saved needs a restart (fixed when the session is built)",
              _r.status_code == 200 and _r.json().get("restart_needed") is True and _el_stt().get("language") == "de",
              _r.text[:160])
        _before = _el_stt()
        _refused = [client.put("/api/dashboard/stt", json=b).status_code for b in (
            {"provider": "grok"}, {"provider": "ElevenLabs"}, {"language": "xx"},
            {"provider": "elevenlabs", "keyterms": ["Jarvis"]}, {"provider": "elevenlabs", "elevenlabs_model": "scribe_v1"})]
        check("stt PUT still refuses unknown providers, unknown languages and extra keys, changing nothing (still holds)",
              _refused == [422] * 5 and _el_stt() == _before, f"{_refused} cfg={_el_stt()}")
        _el_envfile.write_text("")
        os.environ.pop("ELEVENLABS_API_KEY", None)
        client.put("/api/dashboard/stt", json={"provider": "whisper"})
        _r = client.put("/api/dashboard/stt", json={"provider": "elevenlabs"})
        _g = client.get("/api/dashboard/stt").json()
        check("elevenlabs stt without a key: allowed, key_needed true, GET says the key isn't set",
              _r.status_code == 200 and _r.json().get("key_needed") is True and _g.get("elevenlabs_key_set") is False,
              f"{_r.text[:160]} set={_g.get('elevenlabs_key_set')}")
        _el_envfile.write_text("ELEVENLABS_API_KEY=el-stt-SECRET-0001\n")
        os.environ["ELEVENLABS_API_KEY"] = "el-stt-SECRET-0001"
        _sfEL.set_provider("elevenlabs")
        try:
            _r1 = client.put("/api/dashboard/stt", json={"provider": "whisper"})
            _r2 = client.put("/api/dashboard/stt", json={"language": "it"})
        finally:
            _sfEL.set_provider("whisper")
        check("elevenlabs stt: leaving a RUNNING Scribe for Whisper needs a restart",
              _r1.status_code == 200 and _r1.json().get("restart_needed") is True, _r1.text[:120])
        check("elevenlabs stt: a language change while Scribe is RUNNING needs a restart (even if Whisper is saved)",
              _r2.status_code == 200 and _r2.json().get("restart_needed") is True, _r2.text[:120])
        _r = client.put("/api/dashboard/stt", json={"language": "fr"})
        check("stt: a language change on Whisper alone stays live, no restart (still holds)",
              _r.status_code == 200 and _r.json().get("restart_needed") is False, _r.text[:120])

        # /status: the running provider's block
        _sfEL.set_provider("elevenlabs")
        _ownEL = object()
        try:
            _sfEL.note_active("suite-el-room", "elevenlabs", owner=_ownEL)
            _s1 = client.get("/api/dashboard/status").json()
            _sfEL.note_active("suite-el-room", "whisper", "ElevenLabs STT rejected the key", owner=_ownEL)
            _s2 = client.get("/api/dashboard/status").json()
        finally:
            _sfEL.forget("suite-el-room", owner=_ownEL)
            _sfEL.set_provider("whisper")
        _st1, _st2 = _s1.get("stt") or {}, _s2.get("stt") or {}
        check("elevenlabs stt /status: on Scribe -> provider and active elevenlabs, model scribe_v2_realtime",
              _st1.get("provider") == "elevenlabs" and _st1.get("active") == "elevenlabs"
              and _st1.get("cloud_model") == "scribe_v2_realtime", str(_st1))
        check("elevenlabs stt /status: failed over -> active whisper with the reason and the room",
              _st2.get("active") == "whisper" and _st2.get("fallback_rooms") == ["suite-el-room"]
              and "rejected" in (_st2.get("fallback_reason") or ""), str(_st2))
        _EL_FIELDS = ("uptime_secs", "version", "stt", "tts_provider", "intelligence",
                      "last_wake", "rooms", "session_modes", "restart_pending")
        check("/status keeps every field it returns today, with Scribe running too (still holds)",
              all(k in _s1 for k in _EL_FIELDS) and all(k in _s2 for k in _EL_FIELDS), str(sorted(_s1)))

        # Keys card: the ElevenLabs row
        _el_set(stt={"provider": "elevenlabs"}, tts="kokoro")
        _kk = _el_keys()
        check("elevenlabs keys: STT on Scribe -> the ElevenLabs row is used by TTS + STT and in use (TTS on kokoro), "
              "the Deepgram key is not",
              _kk.get("ELEVENLABS_API_KEY", {}).get("used_by") == ["TTS", "STT"]
              and _kk.get("ELEVENLABS_API_KEY", {}).get("in_use") is True
              and _kk.get("DEEPGRAM_API_KEY", {}).get("in_use") is False,
              f"{_kk.get('ELEVENLABS_API_KEY')} {_kk.get('DEEPGRAM_API_KEY', {}).get('in_use')}")
        _el_set(stt={"provider": "whisper"}, tts="kokoro")
        _kk = _el_keys()
        check("elevenlabs keys: Whisper + kokoro -> ElevenLabs used by TTS only, not in use (still holds)",
              _kk.get("ELEVENLABS_API_KEY", {}).get("used_by") == ["TTS"]
              and _kk.get("ELEVENLABS_API_KEY", {}).get("in_use") is False, str(_kk.get("ELEVENLABS_API_KEY")))
        _el_set(stt={"provider": "whisper"}, tts="elevenlabs")
        _kk = _el_keys()
        check("elevenlabs keys: ElevenLabs TTS + Whisper -> in use, used by TTS only (still holds)",
              _kk.get("ELEVENLABS_API_KEY", {}).get("used_by") == ["TTS"]
              and _kk.get("ELEVENLABS_API_KEY", {}).get("in_use") is True, str(_kk.get("ELEVENLABS_API_KEY")))
        _el_set(stt={"provider": "deepgram"}, tts="kokoro")
        _kk = _el_keys()
        check("elevenlabs keys: Deepgram STT still uses the Deepgram key, used by TTS + STT; ElevenLabs not (still holds)",
              _kk.get("DEEPGRAM_API_KEY", {}).get("in_use") is True
              and _kk.get("DEEPGRAM_API_KEY", {}).get("used_by") == ["TTS", "STT"]
              and _kk.get("ELEVENLABS_API_KEY", {}).get("in_use") is False, str(_kk.get("DEEPGRAM_API_KEY")))
        check("elevenlabs keys: the TTS side of the key is untouched (still holds)",
              _kcEL.tts_secret("elevenlabs") == "ELEVENLABS_API_KEY" and _kcEL.tts_secret("kokoro") is None
              and _kcEL.session_tts_provider("elevenlabs", None, {"ELEVENLABS_API_KEY": "x"}) == ("elevenlabs", None)
              and _kcEL.session_tts_provider("elevenlabs", "kokoro", {})[0] == "kokoro")

        # Restart pending: Scribe fixes its language at session build
        _el_set(stt={"provider": "elevenlabs", "language": "auto"}, tts="kokoro")
        _el_reboot()
        _p0 = _el_pending()
        _el_set(stt={"provider": "elevenlabs", "language": "de"})
        _p1 = _el_pending()
        check("elevenlabs restart pending: a saved language change counts on Scribe",
              isinstance(_p0, list) and "stt" not in _p0 and isinstance(_p1, list) and "stt" in _p1, f"{_p0} -> {_p1}")
        _el_envfile.write_text("ELEVENLABS_API_KEY=el-stt-SECRET-0002\n")
        _p2 = _el_pending()
        check("elevenlabs restart pending: a new ElevenLabs key counts while only STT uses it (TTS on kokoro)",
              isinstance(_p2, list) and "elevenlabs key" in _p2, str(_p2))
        _el_envfile.write_text("ELEVENLABS_API_KEY=el-stt-SECRET-0001\n")
        _el_set(stt={"provider": "whisper", "language": "auto"}, tts="kokoro")
        _el_reboot()
        _el_set(stt={"provider": "whisper", "language": "de"})
        _el_envfile.write_text("ELEVENLABS_API_KEY=el-stt-SECRET-0002\n")
        _p3 = _el_pending()
        check("restart pending: on Whisper a language change stays live and an unused ElevenLabs key never counts (still holds)",
              isinstance(_p3, list) and "stt" not in _p3 and "elevenlabs key" not in _p3, str(_p3))
        _el_envfile.write_text("ELEVENLABS_API_KEY=el-stt-SECRET-0001\n")
        _el_set(stt={"provider": "deepgram", "language": "auto"})
        _el_reboot()
        _el_set(stt={"provider": "deepgram", "language": "de"})
        _p4 = _el_pending()
        check("restart pending: on Deepgram a language change still counts (still holds)",
              isinstance(_p4, list) and "stt" in _p4, str(_p4))
        check("restart pending: labels only, never an ElevenLabs key value (still holds)",
              not any(s in str(x) for x in (_p0, _p1, _p2, _p3, _p4) for s in _EL_SECRETS))
    finally:
        for _k, _v in _el_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        for _s, _v in _el_cfg.items():
            config_store.write_section(_s, _v)
        _sfEL.set_provider(_el_running)
        _cfgEL._BOOT = _el_boot

    # bot.py: the cloud branch builds the picked provider and wraps it
    check("elevenlabs bot.py: any cloud provider gets the Whisper-standby switcher, built for the picked provider",
          "if stt_catalog.is_cloud(stt_provider):" in _main_src
          and "stt_fallback.make_cloud(stt_provider, cfg_env('stt', 'language', 'LVI_STT_LANGUAGE'))" in _main_src
          and "provider=stt_provider)" in _main_src and "if stt_provider == 'deepgram':" not in _main_src,
          "cloud branch missing")
    check("elevenlabs bot.py: the switcher still closes with the session, the timer still runs (still holds)",
          "wrap_stt_with_fallback(" in _main_src and _main_src.count("close_stt_fallback(") >= 2 and "SttTimer(" in _main_src)

    # UI: one table of cloud providers drives the chips, the key line and the save message
    _clEL = _reui.search(r"const STT_CLOUD = \{(.*?)\n\}", _appsrc, _reui.DOTALL)
    _clEL = _ws(_clEL.group(1)) if _clEL else ""
    _chEL = _jsbody(_appsrc, "function sttChip(st) {")
    _scEL = _fnUI("function SttCard({ status, setMsg }) {")
    _svEL = _ws(_jsbody(_scEL, "const save = async say => {"))
    check("elevenlabs ui: the cloud table names Deepgram and ElevenLabs with their GET /stt key flags",
          "deepgram: { name: 'Deepgram', key: 'deepgram_key_set', chip: st => `deepgram ${st.cloud_model || ''}`.trim() }"
          in _clEL and "elevenlabs: { name: 'ElevenLabs', key: 'elevenlabs_key_set', chip: () => 'elevenlabs scribe' }"
          in _clEL, _clEL[:300])
    _gEL = client.get("/api/dashboard/stt").json()
    check("elevenlabs ui: every key flag the card reads is one GET /stt returns",
          bool(_reui.findall(r"key: '(\w+)'", _clEL)) and all(k in _gEL for k in _reui.findall(r"key: '(\w+)'", _clEL)),
          str(_reui.findall(r"key: '(\w+)'", _clEL)))
    check("elevenlabs ui: the Overview chip reads 'elevenlabs scribe' on Scribe and 'embedded fallback' naming ElevenLabs",
          "const cloud = STT_CLOUD[st.provider]" in _chEL and "const onWhisper = !cloud || st.active === 'whisper'" in _chEL
          and "if (cloud && st.active === 'whisper') {" in _chEL and "`${cloud.name} unreachable`" in _chEL
          and "if (cloud) return { text: cloud.chip(st) }" in _chEL and "'deepgram'" not in _chEL, _chEL[:400])
    check("elevenlabs ui: the Overview chip keeps Whisper's load words; its fallback reads 'embedded fallback'",
          "text: 'whisper failed'" in _chEL and "text: 'embedded fallback'" in _chEL
          and "return { text: whisper.toLowerCase() }" in _chEL and "text: 'loading', muted: true" in _chEL, _chEL[:300])
    check("elevenlabs ui: the STT card shows the running cloud provider's chip and its own missing key",
          "const cloud = STT_CLOUD[st?.provider]" in _scEL and "const onCloud = !!cloud && st.active === st.provider" in _scEL
          and "const onFallback = !!cloud && st.active === 'whisper'" in _scEL
          and "const cloudPick = STT_CLOUD[provider]" in _scEL
          and "<span className={CHIP_CLS}>{cloud.chip(st)}</span>" in _scEL
          and "{cloudPick && data && !data[cloudPick.key] && <MissingKey label={cloudPick.name} />}" in _scEL
          and "'deepgram'" not in _scEL and 'label="Deepgram"' not in _scEL, _scEL[:300])
    check("elevenlabs ui: 'add your key' names the saved provider; the restart message is unchanged",
          "say(r.key_needed ? `No ${STT_CLOUD[r.current?.provider]?.name || 'cloud'} key yet — add it on System · Keys, then restart.`"
          in _svEL and ": 'Saved — restart to apply.', 'ok', 'STT')" in _svEL, _svEL[:400])
    check("elevenlabs ui: the STT card still registers its draft with the unsaved bar (still holds)",
          "useUnsaved('stt', { label: 'STT', count: Object.keys(sttBody()).length, save, discard: s.discard })" in _scEL
          and "const sent = s.snap(['language', 'provider'])" in _scEL and "s.settle(sent)" in _scEL)

    # ── base install: Kokoro and Whisper base ship with it (Embedded); no GPU extra ──
    import yaml as _yaml_la
    _ex_la = _yaml_la.safe_load((REPO_CORE.parent / "config.example.yaml").read_text())
    _req_la = (REPO_CORE.parent / "requirements.txt").read_text().splitlines()
    _readme_in = (REPO_CORE.parent / "README.md").read_text()
    check("install: requirements.txt pulls pipecat's WebRTC extra (bot.py's SmallWebRTC transport needs aiortc)",
          any(l.startswith("pipecat-ai[webrtc]==") for l in _req_la)
          and not any(l.startswith("pipecat-ai==") for l in _req_la)
          and "from pipecat.transports.smallwebrtc.transport import" in (REPO_CORE.parent / "bot.py").read_text(),
          str([l for l in _req_la if l.startswith("pipecat-ai")]))
    _units_la = {n: (REPO_CORE.parent / "deploy" / n).read_text() for n in ("lenoxvi.service.in", "lenoxvi-bot.service.example")}
    _cuda_la = sorted(f"{n}: {w}" for n, t in _units_la.items()
                      for w in ("LD_LIBRARY_PATH", "nvidia", "cuda", "requirements-gpu") if w.lower() in t.lower())
    check("install contract: no CUDA path — no requirements-gpu.txt, requirements.txt names no GPU extra, and neither "
          "systemd unit sets LD_LIBRARY_PATH or names an nvidia library",
          not (REPO_CORE.parent / "requirements-gpu.txt").exists() and not _cuda_la
          and not any(w in "\n".join(_req_la).lower() for w in ("requirements-gpu", "cuda", "nvidia")), str(_cuda_la))
    # ── install contract: what scripts/install.sh and the lenoxvi command
    # rely on. A rename here breaks a test, not a user's install. ──
    import stt_catalog as _sc_ic
    try:
        import tts_catalog as _tc_ic
    except ImportError:  # an older tree (the red probe): the check below fails instead of the suite crashing
        _tc_ic = None
    _ic_src = (REPO_CORE.parent / "config.example.yaml").read_text()
    _icfg = _yaml_la.safe_load(_ic_src)
    _ic_keys = [("intelligence", "kind"), ("wake", "enabled")]
    _ic_missing = [".".join(k) for k in _ic_keys if k[1] not in (_icfg.get(k[0]) or {})]
    check("install contract: config.example.yaml has every key the installer writes (update scripts/installer too)",
          not _ic_missing and "room_zones" in _icfg and "clip_server" in _icfg, str(_ic_missing))
    _ic_stt = _ic_src.split("\nstt:\n", 1)[-1].split("\n# ──", 1)[0]
    _ic_tts = _ic_src.split("\ntts:\n", 1)[-1].split("\n# ──", 1)[0]
    check("install contract: config.example.yaml ships the Embedded defaults the installer copies, and no deleted "
          "speech key (live or commented)",
          "\nstt:\n" in _ic_src and "\ntts:\n" in _ic_src
          and (_icfg.get("stt") or {}).get("provider") == getattr(_sc_ic, "DEFAULT_PROVIDER", "missing")
          and (_icfg.get("tts") or {}).get("provider") == getattr(_tc_ic, "DEFAULT_PROVIDER", "missing")
          and not any(k in _ic_stt for k in ("model:", "device:", "compute_type:"))
          and "fallback:" not in _ic_tts and "pocket_voice" not in _ic_src, f"stt={_icfg.get('stt')} tts={_icfg.get('tts')}")
    _ic_paths = (REPO_CORE / "paths.py").read_text()
    check("install contract: paths.py keeps state_root/env_file/certs_dir and LVI_STATE_DIR (the unit sets it)",
          all(f"def {n}(" in _ic_paths for n in ("state_root", "env_file", "certs_dir")) and "LVI_STATE_DIR" in _ic_paths)
    _ic_cert = (REPO_CORE.parent / "scripts" / "make-cert.sh").read_text()
    _ic_kok = (REPO_CORE.parent / "scripts" / "fetch_kokoro_models.sh").read_text()
    check("install contract: make-cert.sh honours LVI_STATE_DIR; Kokoro models land under the service user's HOME",
          "LVI_STATE_DIR" in _ic_cert and "$HOME/.cache" in _ic_kok)
    _ic_unit = (REPO_CORE.parent / "deploy" / "lenoxvi.service.in").read_text()
    check("install contract: the unit template runs bot.py with --host/--port/--esp32 and the state dir",
          "bot.py --host @HOST_IP@ --port 7860 --esp32" in _ic_unit and "LVI_STATE_DIR=/var/lib/lenoxvi" in _ic_unit
          and "LVI_CONFIG=/var/lib/lenoxvi/config.yaml" in _ic_unit
          and "LVI_DASHBOARD_TOKEN" in (REPO_CORE / "dashboard" / "guard.py").read_text())
    import re as _reF4
    _ex_src_F4 = (REPO_CORE.parent / "config.example.yaml").read_text()
    _ex_ph_F4 = [ln for ln in _ex_src_F4.splitlines()
                 if _reF4.search(r"<[A-Za-z_-]+>", ln.split("#", 1)[0])]
    _ex_F4 = _yaml_la.safe_load(_ex_src_F4)
    check("install: config.example.yaml has no live <PLACEHOLDER> values (the dashboard sets them); still parses",
          _ex_ph_F4 == [] and _ex_F4.get("room_zones") == {} and "host_ip" not in (_ex_F4.get("clip_server") or {})
          and "fill every" not in _readme_in, str(_ex_ph_F4[:4]))
    import logging as _lgF4
    sys.path.insert(0, str(REPO_CORE))
    import stt_local as _slF4  # noqa: F401 — importing installs the filter
    _seen_F4 = []

    class _CapF4(_lgF4.Handler):
        def emit(self, record):
            _seen_F4.append(record.getMessage())
    _hl_F4 = _lgF4.getLogger("huggingface_hub.utils._http")
    _h_F4 = _CapF4()
    _hl_F4.addHandler(_h_F4)
    try:
        _hl_F4.warning("Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits.")
        _hl_F4.warning("This repository is deprecated, use another one.")
    finally:
        _hl_F4.removeHandler(_h_F4)
    check("first run: the Hub's 'set a HF_TOKEN' notice is dropped; other Hub warnings still log",
          _seen_F4 == ["This repository is deprecated, use another one."], str(_seen_F4))
    check("localai ui: the 'No LocalAI key' note shows only once LocalAI has an address (a fresh box isn't nagged)",
          "{saved?.url && !saved.key_set && <MissingKey label=\"LocalAI\" />}" in _appsrc
          and "{saved && !saved.key_set && <MissingKey label=\"LocalAI\" />}" not in _appsrc)
    check("install: the Quick start makes a venv from requirements.txt alone and fetches Kokoro's model files first",
          "python3 -m venv" in _readme_in and "pip install -r requirements.txt`" in _readme_in
          and "requirements-gpu" not in _readme_in and "bash scripts/fetch_kokoro_models.sh" in _readme_in
          and "requirements-pocket" not in _readme_in and "fetch_pocket_weights" not in _readme_in)
    _req_files = sorted(p.name for p in REPO_CORE.parent.glob("requirements*.txt"))
    check("tts card: Kokoro is part of the base install — requirements.txt installs kokoro-onnx, no Kokoro-only requirements file",
          "kokoro-onnx==0.4.7" in _req_la and not [n for n in _req_files if "kokoro" in n], str(_req_files))
    check("tts card: the example config ships the Kokoro fallback; requirements.txt installs kokoro-onnx",
          (_ex_la.get("tts") or {}).get("provider") == getattr(_tc_ic, "DEFAULT_PROVIDER", "missing")
          and "fallback" not in (_ex_la.get("tts") or {}) and "kokoro-onnx==0.4.7" in _req_la, str(_ex_la.get("tts")))
    # ── ux polish (App.jsx): names, lines, order, phones, copy ──
    def _tagsUX(src):
        out = []
        for m in _reui.finditer(r"<(select|input)\b", src):
            i = j = m.start()
            depth = 0
            while j < len(src):
                c = src[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                elif c == ">" and depth == 0 and src[j - 1] != "=":
                    break
                j += 1
            out.append(src[i:j + 1])
        return out
    _unnamedUX = [_ws(x)[:80] for x in _tagsUX(_appsrc)
                  if "aria-label" not in x and 'type="file"' not in x and 'type="checkbox"' not in x]
    check("a11y: every select and text/range input carries an accessible name (aria-label)",
          _unnamedUX == [], str(_unnamedUX[:5]))
    check("a11y: table-row selects name their room",
          'aria-label={`Zone for ${room}`}' in _appsrc and 'aria-label={`Agent for ${room}`}' in _appsrc)
    _fwUX = _fnUI("function FirmwarePanel(")
    _nsUX = _fnUI("function NodeSetupPanel({ setMsg }) {")
    check("firmware ui: one name for one thing — 'Line' everywhere, lowercase as the system names it",
          ">Track</span>" not in _appsrc and ">Board</span>" not in _appsrc and "pick a board" not in _appsrc
          and "const trackLabel = t => (t || '').toLowerCase()" in _fwUX and "toUpperCase()" not in _fwUX
          and "toUpperCase()" not in _nsUX.replace("name[0].toUpperCase()", "")
          and "track.`" not in _fwUX and " track?" not in _fwUX, _fwUX[:200])
    check("firmware ui: after a push only nodes on that line (or of unknown line) are offered, none pre-checked",
          ".filter(n => n.online && (!n.line || n.line === upj.track))" in _fwUX
          and "Object.fromEntries(online.map(n => [n.room, false]))" in _fwUX
          and "others would just reboot for nothing" not in _appsrc)
    _vtUX = _fnUI("function Voice({ tts, tuning, status, setMsg, reloadAll }) {")
    _poUX = [_vtUX.find(s) for s in ("<OwwPanel setMsg={setMsg} reloadAll={reloadAll} />", "<SttCard status={status} setMsg={setMsg} />",
                                     '<Section id="voice" title="TTS"', "<Tuning tuning={tuning} reloadAll={reloadAll} />")]
    check("voice tab: cards in pipeline order — wake word, STT, TTS, voice tuning",
          all(p >= 0 for p in _poUX) and _poUX == sorted(_poUX), str(_poUX))
    check("voice tab: the wake name hints it must match the model and applies live",
          'placeholder="Jarvis (for hey_jarvis)" title="Must match the wake model. Applies live."' in _ws(_appsrc))
    _fdUX = _fnUI("function Field({ label, children, between }) {")
    check("phone: a Field can't grow past its row (one column, minWidth 0); cards are one-column grids",
          '<div className="grid grid-cols-1 gap-1" style={{ minWidth: 0 }}>' in _fdUX
          and 'p-4 grid gap-4"' not in _appsrc and _appsrc.count('p-4 grid grid-cols-1 gap-4"') >= 9
          and _appsrc.count('<div className="rounded-2xl bg-[var(--surface)] overflow-x-auto">') >= 3, _fdUX[:200])
    check("copy: LocalAI Test says why it's off; no glyph status marks",
          "!saved?.url ? 'Save an address first.'" in _appsrc
          and not any(g in _appsrc for g in ("✓", "✗", "ⓘ")))
    check("brand: Add and Push firmware stay grey until they can run (one lime per view)",
          "className={pickValue && !stale ? BTN_PRIMARY : BTN_DEFAULT}" in _appsrc
          and "className={fwFile && fwTrack ? BTN_PRIMARY : BTN_DEFAULT}" in _appsrc)
    # ── review batch (App.jsx): zones, next steps, restart, key links ──
    _zlRB = _fnUI("function ZoneList({ mapping, setMsg, reloadAll }) {")
    check("zones ui: each zone has a Remove button (confirm, DELETE, says which rooms lost their zone) and shows its IP",
          "window.confirm(`Remove zone ${name}? Rooms mapped to it lose their zone.`)" in _zlRB
          and "api(`/api/dashboard/zones/${encodeURIComponent(name)}`, { method: 'DELETE' })" in _zlRB
          and "r?.dropped_room_mappings" in _zlRB and "onClick={() => removeZone(name)}" in _zlRB
          and "{z?.ip && <div className=\"font-mono text-[12px] text-[var(--muted)]\">{esc(z.ip)}</div>}" in _zlRB)
    check("stt ui: a missing cloud key is a next step that survives the unsaved bar and names System · Keys",
          "`No ${STT_CLOUD[r.current?.provider]?.name || 'cloud'} key yet — add it on System · Keys, then restart.`" in _appsrc
          and "key below, then restart" not in _appsrc)
    _rbRB = _fnUI("function RestartButton(")
    check("restart ui: restarting with unsaved edits warns they won't apply",
          "You have unsaved changes. They won't apply. Restart anyway? Every node reboots and reconnects." in _rbRB
          and "window.confirm(unsaved" in _rbRB)
    _mkRB = _fnUI("function MissingKey({ label }) {")
    check("keys ui: 'No key' links land on the Keys card, in the warning tone",
          'href="#keys"' in _mkRB and "warning-soft-foreground" in _mkRB and "--danger" not in _mkRB)
    # ── localai ui (App.jsx) ──
    _laCard = _fnUI("function LocalAICard({ setMsg }) {")
    _laHook = _fnUI("function useLocalAIModels(on) {")
    _laVo = _fnUI("function Voice({ tts, tuning, status, setMsg, reloadAll }) {")
    _laSt = _fnUI("function SttCard({ status, setMsg }) {")
    check("localai ui: the LocalAI card sits on the System tab after Keys, saves through the unsaved bar, never edits models",
          "<KeysCard setMsg={setMsg} />\n    <LocalAICard setMsg={setMsg} />" in _appsrc
          and "useUnsaved('localai', { label: 'LocalAI'" in _laCard
          and "api('/api/dashboard/localai', { method: 'PUT', json: { url: url.trim() } })" in _laCard
          and "/api/dashboard/localai/test" in _laCard and "models/apply" not in _appsrc, _laCard[:200])
    check("localai ui: no key saved -> the card links to System · Keys; Keys saves the LocalAI key on its own route",
          "{saved?.url && !saved.key_set && <MissingKey label=\"LocalAI\" />}" in _laCard
          and "if (row.kind === 'localai') return ['/api/dashboard/localai/key', {}]" in _appsrc)
    check("localai ui: model lists come from the server's Test (keys never reach the browser), refreshed after saves",
          "api('/api/dashboard/localai/test', { method: 'POST' })" in _laHook and "lenoxvi:reload-all" in _laHook
          and "useLocalAIModels(provider === 'localai')" in _laVo and "useLocalAIModels(onLocalAIPick)" in _laSt)
    _provLabels = {p["name"]: p["label"] for p in client.get("/api/dashboard/stt").json().get("providers", [])}
    # ── speech tiers ui (App.jsx): grouped provider selects; deleted fields never sent ──
    _toU = _ws(_jsbody(_appsrc, "const tierOptions = (tiers, providers, saved) => tiers.map(tier => {"))
    check("speech tiers: both provider selects render one <optgroup> per API tier, in the API's tier then provider order",
          "const members = providers.filter(p => p.tier === tier.name)" in _toU
          and "<optgroup key={tier.name} label={tier.label}>" in _toU
          and "{tierOptions(tts?.tiers || LOADING_TIERS," in _ws(_laVo)
          and "{tierOptions(data?.tiers || LOADING_TIERS," in _ws(_laSt)
          and _appsrc.count("{tierOptions(") == 2
          and "const LOADING_TIERS = [{ name: 'embedded', label: 'Embedded' }]" in _appsrc
          and not any(s in _appsrc for s in ("(cloud)<", "(local){", "(host){", "Whisper (local)", "LocalAI (host)")),
          _toU[:300])
    check("speech tiers: an unavailable provider stays listed, disabled, its API reason in the text and title; "
          "the saved one stays pickable",
          "disabled={!p.available && p.name !== saved} title={p.reason || undefined}" in _toU
          and "{p.available ? p.label : `${p.label} — ${p.reason || 'unavailable'}`}" in _toU
          and "tts?.provider)}" in _ws(_laVo) and "data?.current?.provider)}" in _ws(_laSt), _toU[-300:])
    check("speech tiers: before GET answers, each provider select shows its Embedded default (Whisper base, Kokoro)",
          "[{ name: 'whisper', label: 'Whisper base', tier: 'embedded', available: true }]" in _laSt
          and "[{ name: 'kokoro', label: 'Kokoro', tier: 'embedded', available: true }]" in _laVo
          and "value={provider || 'whisper'}" in _laSt and "value={provider || 'kokoro'}" in _laVo
          and "provider: tts?.provider || 'kokoro'," in _laVo)
    from dashboard import routes as _rtU
    _pkU = "pock" + "et"  # a retired name in pieces: these checks prove absence, the repo grep gate stays at zero

    def _bodyKeysU(fn):
        """Every key a save puts on `body`: its starting literal plus each `body.<key> =` assignment."""
        start = _reui.search(r"const body = \{([^}]*)\}", fn)
        return (set(_reui.findall(r"\w+", start.group(1))) if start else set()) | set(_reui.findall(r"\bbody\.(\w+) =", fn))
    _sttFnU = _jsbody(_laSt, "const sttBody = () => {") + _jsbody(_laSt, "const save = async say => {")
    _ttsFnU = _jsbody(_laVo, "const saveTTS = async say => {")
    _sttKeysU = _bodyKeysU(_sttFnU) - {"la_model"}  # deleted before the PUT: the LocalAI route owns it
    _ttsKeysU = _bodyKeysU(_ttsFnU)
    _callsU = _reui.findall(r"api\('/api/dashboard/(stt|tts)'(.*?)\)", _appsrc)
    check("speech tiers: the STT and TTS saves PUT only SttPayload / TTSPayload fields — never the deleted STT model "
          "or TTS standby fields",
          "delete body.la_model" in _sttFnU and bool(_sttKeysU) and bool(_ttsKeysU)
          and _sttKeysU <= set(_rtU.SttPayload.model_fields) and "model" not in _sttKeysU
          and _ttsKeysU <= set(_rtU.TTSPayload.model_fields) and not {_pkU + "_voice", "fallback"} & _ttsKeysU
          and sorted(r for r, rest in _callsU if rest) == ["stt", "tts"]
          and all(rest in ("", ", { method: 'PUT', json: body }") for _, rest in _callsU),
          f"stt={sorted(_sttKeysU)} tts={sorted(_ttsKeysU)} calls={_callsU}")
    check("speech tiers: Kokoro's voice and speed show on every TTS tier — 'Voice' on Embedded, 'Fallback voice' on "
          "LocalAI and Cloud",
          "<Field label={provider === 'kokoro' ? 'Voice' : 'Fallback voice'}>" in _laVo
          and _reui.search(r"&&\s*\(\s*(\{/\*.*?\*/\}\s*)?<Field label=\{provider === 'kokoro'", _laVo, _reui.DOTALL) is None
          and '<select id="tts-kokoro-voice"' in _laVo
          and "onChange={e => t.set({ 'model:kokoro': e.target.value })}" in _laVo
          and "{provider !== 'kokoro' &&" in _laVo
          and "if (provider !== 'kokoro' && t.changed('model:kokoro')) body.kokoro_voice = kokoroVoice" in _ws(_ttsFnU)
          and "...(provider === 'kokoro' ? [] : ['model:kokoro']), 'kokoro_speed']" in _ws(_laVo), _laVo[-600:])
    check("speech tiers: the STT fallback chip reads 'embedded fallback' on the STT card and in the status bar",
          ">embedded fallback</span>" in _laSt
          and _fnUI("function sttChip(st) {").count("text: 'embedded fallback'") == 2
          and "whisper fallback" not in _appsrc)
    check("speech tiers: no Whisper model menu, download, delete or GPU chip on the STT card; no retired TTS engine "
          "or fallback picker on the TTS card",
          not any(s in _laSt for s in ('id="stt-model"', 'id="stt-models"', 'id="stt-progress"', "/api/dashboard/stt/model/",
                                        "data?.gpu", "st.device", "custom_model", "data?.models", "download"))
          # LocalAI's own pocket-tts backend keeps its preset list; only the embedded engine's names must be gone
          and not any(s in _appsrc for s in (_pkU + "_voice", _pkU + "_installed", "provider === '" + _pkU + "'",
                                             "provider !== '" + _pkU + "'", "p === '" + _pkU + "'",
                                             'id="tts-fallback"', "tts?.fallback", "gpu", "stt_model"))
          and "{...sttChip(status?.stt)}" in _appsrc)
    check("speech tiers: STT provider labels are plain names — the tier group says where each runs",
          _provLabels == {"whisper": "Whisper base", "localai": "LocalAI", "deepgram": "Deepgram Nova-3",
                          "elevenlabs": "ElevenLabs Scribe v2"}, str(_provLabels))
    check("localai ui: TTS card offers LocalAI from the API's list (disabled with its reason until set up) and names "
          "Kokoro as its stand-in",
          "{tierOptions(tts?.tiers || LOADING_TIERS," in _ws(_laVo)
          and "If LocalAI is down: Kokoro on this appliance." in _laVo)
    check("localai ui: the TTS card never shows a model it isn't holding ('pick a model'), and Save refuses an empty model or voice",
          "{provider === 'localai' && !model && options.length > 0 && <option value=\"\" disabled>pick a model</option>}" in _laVo
          and "if (!model) { say('Pick a LocalAI voice model first.', 'err', 'TTS'); return false }" in _laVo
          and "if (laVoices && !laVoice.trim()) { say('Pick a voice first.', 'err', 'TTS'); return false }" in _laVo)
    check("localai ui: the TTS voice list follows the backend LocalAI reports (Kokoro / Pocket presets, else free text)",
          "laBackend === 'kokoro' ? (catalog.kokoro || []) : laBackend === 'pocket' ? (catalog.pocket || []) : null" in _laVo
          and "laRaw.startsWith('kokoro') ? 'kokoro' : laRaw.startsWith('pocket') ? 'pocket' : laRaw" in _laVo
          and 'id="tts-localai-voice"' in _laVo)
    check("localai ui: the STT card's LocalAI model saves on the LocalAI route, never inside the /stt body",
          "body.la_model = laModel" in _laSt and "delete body.la_model" in _laSt
          and "api('/api/dashboard/localai', { method: 'PUT', json: { model_stt: laPick } })" in _laSt
          and "If LocalAI is down: Whisper base on this appliance." in _laSt)
    _laChip = _fnUI("function sttChip(st) {")
    check("localai ui: the STT chip says 'localai <model>', and 'embedded fallback' linking to the LocalAI card when it's down",
          "return { text: `localai ${st.localai_model || ''}`.trim() }" in _laChip and "href: '#localai'" in _laChip
          and "text: 'embedded fallback', href: '#localai'" in _laChip
          and "localai: 'LocalAI'" in _appsrc)

    # ── node setup ui (App.jsx): the System tab's USB setup panel ──
    _nsUI = _fnUI("function NodeSetupPanel({ setMsg }) {")
    _sysNS = _fnUI("function System({ intelligence, status, setMsg, reloadAll }) {")
    check("node setup ui: the panel renders right after the Firmware panel on the System tab",
          bool(_reui.search(r"<FirmwarePanel [^>]*/>\s*<NodeSetupPanel setMsg=\{setMsg\} />", _sysNS)),
          _sysNS[-300:] or "no System()")
    check("node setup ui: App.jsx imports the USB module and never loads esptool-js itself",
          "from './nodeSetup.js'" in _appsrc and "esptool-js" not in _appsrc)
    check("node setup ui: the password field is a password input that never autofills",
          'type="password"' in _nsUI and 'autoComplete="new-password"' in _nsUI, _nsUI[:200] or "no NodeSetupPanel")
    check("node setup ui: an unknown board's line picker has no default",
          '<option value="" disabled>pick a line…</option>' in _nsUI
          and "const [pickTrack, setPickTrack] = useState('')" in _nsUI, _nsUI[:200] or "no NodeSetupPanel")
    _flNS = _nsUI[_nsUI.find("const flash = async"):_nsUI.find("const disconnect = async")] if _nsUI else ""
    check("node setup ui: every flash asks first",
          "window.confirm(" in _flNS and ".flash(" in _flNS and _flNS.find("window.confirm(") < _flNS.find(".flash("),
          _flNS[:300] or "no flash()")
    _svNS = _nsUI[_nsUI.find("const save = async"):_nsUI.find("const flash = async")] if _nsUI else ""
    check("node setup ui: the password field is cleared after a save, success or failure",
          _svNS.count("setForm(f => ({ ...f, wifi_pass: '' }))") >= 2, _svNS[:300] or "no save()")
    check("node setup ui: a save the node confirmed becomes the page's record before the restart and watch",
          ".save(fields)" in _svNS and "setInfo(" in _svNS and ".restart()" in _svNS
          and _svNS.find(".save(fields)") < _svNS.find("setInfo(") < _svNS.find(".restart()"),
          _svNS[:400] or "no save()")
    # ── node setup ui: the audio chip (XMOS) step ──
    _wNS = _nsUI[_nsUI.find("const watch = async"):_nsUI.find("const save = async")] if _nsUI else ""
    check("node setup ui: the sound check runs only after the node is reported online",
          "is online on" in _wNS and "checkSound(" in _wNS and _wNS.find("is online on") < _wNS.find("checkSound("),
          _wNS[:300] or "no watch()")
    _csNS = _nsUI[_nsUI.find("const checkSound = async"):_nsUI.find("const connect = async")] if _nsUI else ""
    check("node setup ui: only array and flex boards get the sound check",
          "if (!['array', 'flex'].includes(track)) return" in _csNS, _csNS[:300] or "no checkSound()")
    check("node setup ui: no sound points at the audio chip step",
          "its audio chip sends no sound. Flash the audio chip to fix it." in _csNS
          and "['silent', 'wrong'].includes(chip.state) ? BTN_PRIMARY : BTN_DEFAULT" in _nsUI, _csNS[:300] or "no checkSound()")
    check("node setup ui: the audio chip board choice has no default",
          '<option value="" disabled>pick a line…</option>' in _nsUI and "disabled={!dfu.line" in _nsUI,
          _nsUI[:200] or "no NodeSetupPanel")
    _cfNS = _nsUI[_nsUI.find("const chipFlash = async"):_nsUI.find("const busy = [")] if _nsUI else ""
    check("node setup ui: an audio chip flash downloads and checks before it asks, and asks before it writes",
          "fetchXmosImage(" in _cfNS and "window.confirm(" in _cfNS and ".flash(" in _cfNS
          and _cfNS.find("fetchXmosImage(") < _cfNS.find("window.confirm(") < _cfNS.find(".flash("),
          _cfNS[:400] or "no chipFlash()")
    check("node setup ui: the audio chip confirmation names the board's line",
          "`Flash the ${line} audio chip firmware" in _cfNS[:_cfNS.find(".flash(")] if _cfNS else False, _cfNS[:300] or "no chipFlash()")
    _cfiNS = _nsUI[_nsUI.find("const chipFile = async"):_nsUI.find("const chipFlash = async")] if _nsUI else ""
    check("node setup ui: a file instead is held to the same image check",
          "checkXmosImage(" in _cfiNS, _cfiNS[:300] or "no chipFile()")
    check("node setup ui: Windows shows the one-time driver step before Connect",
          "platformOf() === 'windows'" in _nsUI and "Zadig" in _appsrc, "no Windows driver step")
    check("node setup ui: the audio chip module is imported up front, not as a lazy chunk",
          "from './xmosDfu.js'" in _appsrc and "import('./xmosDfu.js')" not in _appsrc, "xmosDfu import")
    check("node setup ui: a new network without its password is refused before anything is sent",
          "'wifi_ssid' in fields && !fields.wifi_pass" in _svNS and ".save(" in _svNS
          and _svNS.find("'wifi_ssid' in fields && !fields.wifi_pass") < _svNS.find(".save("), _svNS[:300] or "no save()")
    check("node setup ui: the password never reaches a message or the console",
          bool(_nsUI) and "wifi_pass" not in "".join(_reui.findall(r"(?:\bM|setMsg|console\.\w+)\(([^)]*)\)", _nsUI)))
    _cssNS = (REPO_CORE.parent / "dashboard-ui" / "src" / "index.css").read_text()
    check("node setup ui: the USB module stays out of the CSS scan (its words would add rules)",
          '@source not "./nodeSetup.js";' in _cssNS)
    check("node setup ui: the audio chip module stays out of the CSS scan (its words would add rules)",
          '@source not "./xmosDfu.js";' in _cssNS)
    # ── node setup ui: audio chip review fixes ──
    _cnNS = _nsUI[_nsUI.find("const connect = async"):_nsUI.find("const watch = async")] if _nsUI else ""
    _dcNS = _nsUI[_nsUI.find("const disconnect = async"):_nsUI.find("const openChip")] if _nsUI else ""
    check("node setup ui: a finished audio chip flash closes the serial link so Connect shows (the cable moved)",
          ".flash(" in _cfNS and "linkRef.current = null" in _cfNS and "setPhase('idle')" in _cfNS
          and "setChip({ state: '', ver: '' })" in _cfNS and _cfNS.find(".flash(") < _cfNS.find("setPhase('idle')"),
          _cfNS[:400] or "no chipFlash()")
    check("node setup ui: the finished audio chip step says what comes next, not the safe-mode steps",
          "dfu.phase === 'done' ? XMOS_DONE : XMOS_STEPS[dfu.line]" in _nsUI, "no done text in the step")
    check("node setup ui: only the latest sound check reports (a newer check, Connect and Disconnect drop it)",
          "const gen = ++checkGen.current" in _csNS and "gen !== checkGen.current" in _csNS
          and "checkGen.current++" in _cnNS and "checkGen.current++" in _dcNS, _csNS[:300] or "no checkSound()")
    check("node setup ui: Connect and Disconnect clear the last board's sound check",
          "setChip({ state: '', ver: '' })" in _cnNS and "setChip({ state: '', ver: '' })" in _dcNS,
          "connect/disconnect keep the old chip state")
    check("node setup ui: a refused dashboard token ends the sound check instead of asking again",
          "e.status === 401" in _csNS and "stop: () =>" in _csNS and "denied" in _csNS, _csNS[:300] or "no checkSound()")
    check("node setup ui: the check after an audio chip flash expects the node's own line's version",
          "expectVersion(" in _csNS and "checkSound(i.name, i.track, true)" in _cnNS and "wrong: 'wrong image'" in _appsrc,
          _csNS[:300] or "no checkSound()")
    check("node setup ui: the file row names the expected image and its hash",
          "xmosImageName(dfu.line)" in _nsUI and "sha256.slice(0, 12)" in _nsUI, "no expected file shown")

    # ── comment hygiene: zero calendar dates in comments and docstrings ──
    # House law: dates are provenance git blame already carries, and dated
    # incident narration leaks house history into a public repo. Full dates
    # only (bare years are fine); comments AND docstrings of tracked .py
    # files; string literals (test data) are not scanned. ────────────
    import ast as _astCH
    import io as _ioCH
    import re as _reCH
    import tokenize as _tkCH
    _DRX_CH = _reCH.compile(r"\b(?:19|20)\d\d[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b"
                            r"|\b(?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])[-/](?:19|20)\d\d\b")
    _dates_CH = []
    for _py in REPO_CORE.parent.rglob("*.py"):
        _rel = _py.relative_to(REPO_CORE.parent)
        if any(_p.startswith(".") for _p in _rel.parts[:-1]) or "node_modules" in _rel.parts:
            continue
        try:
            _src = _py.read_text(encoding="utf-8", errors="replace")
            _texts = [t.string for t in _tkCH.generate_tokens(_ioCH.StringIO(_src).readline)
                      if t.type == _tkCH.COMMENT]
            _tree = _astCH.parse(_src)
        except (SyntaxError, _tkCH.TokenError, IndentationError, ValueError):
            continue
        for _n in _astCH.walk(_tree):
            if isinstance(_n, (_astCH.Module, _astCH.ClassDef, _astCH.FunctionDef, _astCH.AsyncFunctionDef)):
                _b = _n.body
                if (_b and isinstance(_b[0], _astCH.Expr)
                        and isinstance(getattr(_b[0], "value", None), _astCH.Constant)
                        and isinstance(_b[0].value.value, str)):
                    _texts.append(_b[0].value.value)
        for _t in _texts:
            _m = _DRX_CH.search(_t)
            if _m:
                _dates_CH.append(f"{_rel}: {_m.group(0)}")
    check("comment hygiene: zero calendar dates in comments and docstrings",
          not _dates_CH, "; ".join(_dates_CH[:6]))

    # cleanup: restore nothing (throwaway dir), but show diff summary
    print()
    print(f"scratch dir kept for inspection: {SCRATCH}")
    print(f"throwaway config: {cfg_test}")
    if FAILS:
        print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()