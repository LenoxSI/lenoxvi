"""Offline test rig for the lenoxvi dashboard package.

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

    # ── throwaway config: copy the repo's real config, then point the
    # package at it via LVI_CONFIG (the documented env override).
    cfg_src = REPO_CORE.parent / "config.yaml"
    cfg_test = SCRATCH / "config.yaml"
    shutil.copy2(cfg_src, cfg_test)
    # The LIVE bot owns :8765 — this rig runs out-of-process, so serve
    # test clips from a free port on the same LAN IP (Sonos fetches the
    # URL over HTTP either way). In production the dashboard shares the
    # bot's own clip server, so this collision cannot happen there.
    cfg_test.write_text(cfg_test.read_text().replace("port: 8765", "port: 8766"))
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
    #     (session end — measured live 2026-09-29).
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
    # Mirror the LIVE app (2026-10-13): pipecat's runner installs this
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

    # 2026-10-16: page.html fallback DELETED — dist/ missing
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
          rooms.get("bedroom", {}).get("online") is True
          or registry.active_room() == "bedroom",
          f"rooms={list(rooms)} active={registry.active_room()}")

    # connect-BEFORE-offer race: mark connected first, then tee fires
    pc2 = "fake-pc-race"
    registry.mark_connected(pc2)  # provisional 'unknown'
    check("provisional room assigned pre-offer",
          registry.active_room() == "unknown")
    registry.note_offer(pc2, "kitchen")
    rooms = {r["room"]: r for r in registry.known_rooms()}
    check("race reconciled to kitchen",
          rooms.get("kitchen", {}).get("online") is True and "unknown" not in rooms,
          f"rooms={list(rooms)}")

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
          ("uptime_secs", "active_room", "last_wake", "rooms")), f"s={s}")
    # Contract: bot.py passes THIS pipeline's room (room_for_pc) — the
    # bare-call form is gone; a wake with no room records room=None.
    registry.note_wake("bedroom")
    s = client.get("/api/dashboard/status").json()
    check("note_wake records room", s["last_wake"] and s["last_wake"]["room"] == "bedroom",
          f"last_wake={s['last_wake']}")

    # 5. config write round-trip on throwaway file
    before_text = cfg_test.read_text()
    r = client.put("/api/dashboard/mapping", json={"room_zones": {"bedroom": "Dining Room"}})
    check("mapping PUT ok", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
    after_text = cfg_test.read_text()
    check("config write changed the file", after_text != before_text)
    check("comment preservation (header survives)",
          "# lenoxvi config" in after_text or "THE tuning surface" in after_text,
          )
    m = client.get("/api/dashboard/mapping").json()
    check("mapping read-back after write", m["room_zones"] == {"bedroom": "Dining Room"},
          f"m={m['room_zones']}")
    check("backup file created", (SCRATCH / "config.backup.yaml").is_file())

    # 6. zone add round-trip
    r = client.post("/api/dashboard/zones", json={
        "name": "test_zone", "ip": "192.0.2.222", "player_uid": "RINCON_TEST"})
    check("zone add ok", r.status_code == 200, f"status={r.status_code}")
    m = client.get("/api/dashboard/mapping").json()
    check("new zone visible in mapping GET", "test_zone" in m["zones"], f"zones={list(m['zones'])}")

    # 7. validation: unknown zone in mapping rejected
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "nonexistent_zone"}})
    check("mapping PUT rejects unknown zone", r.status_code == 400, f"status={r.status_code}")

    # 7b. mapping-wipe guards (2026-10-07 bug: the React UI sent a BARE
    # merged dict — no room_zones wrapper — and Pydantic's default_factory
    # silently turned it into {} and the route wiped all mappings; three
    # real wipes hit production config before these guards existed).
    r = client.put("/api/dashboard/mapping", json={"bedroom": "Dining Room"})
    check("unwrapped mapping PUT 422s (extra=forbid)",
          r.status_code == 422, f"status={r.status_code} body={r.text[:200]}")
    r = client.put("/api/dashboard/mapping", json={"room_zones": {}})
    check("empty mapping PUT refused (wipe guard)",
          r.status_code == 400, f"status={r.status_code} body={r.text[:200]}")
    m = client.get("/api/dashboard/mapping").json()
    check("mapping survived the two rejected PUTs", m["room_zones"] == {"bedroom": "Dining Room"},
          f"room_zones={m['room_zones']}")

    # 8. REAL Sonos test clip on the REAL zone
    real_zones = m["zones"]
    # use the REAL configured zone (from the live config), not test_zone
    real = [z for z in ("Dining Room", "Living Room") if z in real_zones]
    if real:
        r = client.post(f"/api/dashboard/zones/{real[0]}/test")
        ok = r.status_code == 200
        check(f"REAL test clip on '{real[0]}'", ok,
              "" if ok else f"status={r.status_code} body={r.text[:200]}")
    else:
        print("[SKIP] no real zone in config — skipping live Sonos test")

    # 8m. Small-fix pins (fixed 2026-10-08)
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
              "lenoxvi" in _cp and os.path.isabs(_cp)
              and _cp.endswith("config.yaml"), f"CONFIG_PATH={_cp}")
        check("config loads away from repo root",
              bool(_cfg_mod.cfg("tts")), f"tts={_cfg_mod.cfg('tts')}")
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

    # /mapping only runs soco name lookups on ?names=1, and only
    # for SONOS zones (the 5s UI poll used to hit every zone, WiiM
    # included, on every tick).
    from dashboard import routes as _routes_m5
    _m5_calls = []
    _real_names_m5 = _routes_m5._sonos_names
    _routes_m5._sonos_names = lambda zones: (_m5_calls.append(sorted(zones)) or {k: k for k in zones})
    try:
        _rm = client.get("/api/dashboard/mapping")
        check("plain /mapping poll runs NO soco lookup and omits sonos_names",
              _rm.status_code == 200 and not _m5_calls and "sonos_names" not in _rm.json(),
              f"calls={_m5_calls} keys={list(_rm.json())}")
        _rn = client.get("/api/dashboard/mapping?names=1")
        _wiim_keys_m5 = set(((config_store.read_section("wiim") or {}).get("zones") or {}))
        _sonos_keys_m5 = set(((config_store.read_section("sonos") or {}).get("zones") or {}))
        check("?names=1 looks up Sonos zones only (never WiiM)",
              _rn.status_code == 200 and len(_m5_calls) == 1
              and set(_m5_calls[0]) == _sonos_keys_m5
              and not (set(_m5_calls[0]) & _wiim_keys_m5)
              and "sonos_names" in _rn.json(),
              f"calls={_m5_calls} sonos={sorted(_sonos_keys_m5)} wiim={sorted(_wiim_keys_m5)}")
    finally:
        _routes_m5._sonos_names = _real_names_m5

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
        _wiim_glob = (config_store.read_section("wiim") or {}).get("clip_volume")
        _wiim_expect = int(_wiim_glob) if _wiim_glob is not None else getattr(_routes_l3, "DEFAULT_CLIP_VOLUME", None)
        check("Sonos global moves Sonos zones only; WiiM resolves in its own section",
              bool(_ev2) and all(_ev2[z] == 61 for z in _sonos_keys_l3)
              and all(_ev2[z] == _wiim_expect for z in _wiim_keys_l3 if z not in _wo_l3._LIVE_WIIM_VOLUMES),
              f"ev2={_ev2} wiim_expect={_wiim_expect}")
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
    ok = r.status_code == 200 and "provider" in r.json() and "deepgram_key_set" in r.json()
    check("TTS GET returns provider config + key status", ok, f"status={r.status_code}")
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
        check("TTS GET catalog sizes (elevenlabs 2, aura 40, flux 36, pocket 21)",
              len(_cat.get("elevenlabs", [])) == 2 and len(_cat.get("deepgram", [])) == 40
              and len(_cat.get("deepgram-flux", [])) == 36
              and len(_cat.get("pocket", [])) == 21,
              str({k: len(v) for k, v in _cat.items()}))
        from dashboard import routes as _routes_m1
        import tts_pocket as _tp_m1
        _m1_avail = _tp_m1.pocket_available
        _tp_m1.pocket_available = lambda: True  # validate the list, not this host's install
        _m1_bad = []
        for _prov, _vals in _cat.items():
            for _v in _vals:
                try:
                    if _prov == "elevenlabs":
                        _p = _routes_m1.TTSPayload(provider=_prov, model=_v, voice_id="AbC123xyZw9876543210")
                    elif _prov == "deepgram":
                        _p = _routes_m1.TTSPayload(provider=_prov, deepgram_voice=_v)
                    elif _prov == "pocket":
                        _p = _routes_m1.TTSPayload(provider=_prov, pocket_voice=_v)
                    else:
                        _p = _routes_m1.TTSPayload(provider=_prov, deepgram_flux_voice=_v)
                    _routes_m1._validate_tts_payload(_p)
                except Exception as _e:
                    _m1_bad.append((_prov, _v, str(_e)[:40]))
        _tp_m1.pocket_available = _m1_avail
        check("every catalog entry passes the server's own TTS validation",
              not _m1_bad and sum(len(v) for v in _cat.values()) == 99, f"bad={_m1_bad[:3]}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs",
        "voice_id": "AbC123xyZw9876543210", "model": "eleven_flash_v2_5"})
    check("TTS PUT (elevenlabs) ok", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")
    t = client.get("/api/dashboard/tts").json()
    check("TTS round-trip: elevenlabs voice_id + model persisted",
          t.get("voice_id") == "AbC123xyZw9876543210" and t.get("model") == "eleven_flash_v2_5",
          f"t={t}")
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
    # 050 Pocket lane + fallback (routes). pocket_available is monkeypatched
    # so the pins are deterministic whether or not torch is on this host.
    import tts_pocket as _tp050
    _orig_avail050 = _tp050.pocket_available
    _tp050.pocket_available = lambda: True
    try:
        t = client.get("/api/dashboard/tts").json()
        check("050 GET /tts catalog.pocket = 21 presets, alba first",
              (t.get("catalog", {}).get("pocket") or [None])[0] == "alba"
              and len(t["catalog"]["pocket"]) == 21, f"{t.get('catalog', {}).get('pocket')}")
        check("050 GET /tts reports pocket_installed + pocket_voice + fallback",
              t.get("pocket_installed") is True and t.get("pocket_voice") == "alba"
              and "fallback" in t,
              str({k: t.get(k) for k in ("pocket_installed", "pocket_voice", "fallback")}))
        r = client.put("/api/dashboard/tts", json={"provider": "pocket", "pocket_voice": "vera"})
        check("050 PUT pocket provider ok", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        t = client.get("/api/dashboard/tts").json()
        check("050 round-trip: provider pocket + voice vera",
              t.get("provider") == "pocket" and t.get("pocket_voice") == "vera", str(t)[:160])
        r = client.put("/api/dashboard/tts", json={"provider": "pocket", "pocket_voice": "nope"})
        check("050 PUT rejects unknown pocket voice (422)", r.status_code == 422, f"{r.status_code}")
        r = client.put("/api/dashboard/tts", json={"provider": "pocket", "pocket_voice": "/x/clone.wav"})
        check("050 PUT rejects a wav path from the dashboard (config-file only)",
              r.status_code == 422, f"{r.status_code}")
        r = client.put("/api/dashboard/tts", json={"provider": "pocket", "pocket_voice": "alba",
                                                   "fallback": "pocket"})
        check("050 PUT rejects pocket primary + pocket fallback (422)", r.status_code == 422, f"{r.status_code}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "pocket"})
        t = client.get("/api/dashboard/tts").json()
        check("050 PUT cloud primary + pocket fallback persists",
              r.status_code == 200 and t.get("fallback") == "pocket", f"{r.status_code} {t.get('fallback')}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en"})
        t = client.get("/api/dashboard/tts").json()
        check("050 PUT without fallback leaves it as-is (partial update)",
              r.status_code == 200 and t.get("fallback") == "pocket", f"{t.get('fallback')}")
        r = client.put("/api/dashboard/tts", json={"provider": "pocket", "pocket_voice": "alba"})
        t = client.get("/api/dashboard/tts").json()
        check("050 switching to pocket primary drops a saved fallback",
              r.status_code == 200 and t.get("fallback") is None, f"{t.get('fallback')}")
        client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                               "deepgram_voice": "aura-2-thalia-en", "fallback": "pocket"})
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "off"})
        t = client.get("/api/dashboard/tts").json()
        check("050 PUT fallback off clears it", r.status_code == 200 and t.get("fallback") is None,
              f"{r.status_code} {t.get('fallback')}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "flux"})
        check("050 PUT rejects unknown fallback (422)", r.status_code == 422, f"{r.status_code}")
        # Fallback voice: pickable with a cloud primary (it rides with
        # fallback: pocket), validated, and ignored when the fallback is off.
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "pocket", "pocket_voice": "vera"})
        t = client.get("/api/dashboard/tts").json()
        check("050 fallback voice saves with a cloud primary",
              r.status_code == 200 and t.get("pocket_voice") == "vera"
              and t.get("provider") == "deepgram" and t.get("fallback") == "pocket",
              f"{r.status_code} {t.get('provider')}/{t.get('fallback')}/{t.get('pocket_voice')}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "pocket", "pocket_voice": "nope"})
        check("050 fallback voice must be a Pocket preset (422)", r.status_code == 422, f"{r.status_code}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "off", "pocket_voice": "charles"})
        t = client.get("/api/dashboard/tts").json()
        check("050 fallback voice ignored when the fallback is off",
              r.status_code == 200 and t.get("pocket_voice") == "vera", f"{t.get('pocket_voice')}")
        # R2: a saved fallback must never lock the panel when Pocket goes
        # missing later — re-sending the SAME fallback isn't a new request.
        client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                               "deepgram_voice": "aura-2-thalia-en",
                                               "fallback": "pocket"})
        _tp050.pocket_available = lambda: False
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-zeus-en",
                                                   "fallback": "pocket"})
        check("050 R2 saved fallback + Pocket missing: other edits still save",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "off"})
        check("050 R2 saved fallback + Pocket missing: fallback can be turned off",
              r.status_code == 200, f"{r.status_code}")
        t = client.get("/api/dashboard/tts").json()
        check("050 GET /tts reports pocket_installed false when missing",
              t.get("pocket_installed") is False, f"{t.get('pocket_installed')}")
        r = client.put("/api/dashboard/tts", json={"provider": "pocket", "pocket_voice": "alba"})
        check("050 PUT pocket primary refused when not installed (422)", r.status_code == 422, f"{r.status_code}")
        r = client.put("/api/dashboard/tts", json={"provider": "deepgram",
                                                   "deepgram_voice": "aura-2-thalia-en",
                                                   "fallback": "pocket"})
        check("050 PUT pocket fallback refused when not installed (422)", r.status_code == 422, f"{r.status_code}")
    finally:
        _tp050.pocket_available = _orig_avail050
    # restore the real provider (the suite config is a copy, but leave it
    # in a sane state anyway)
    client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_flash_v2_5"})

    # 9b. Bot restart route (suite MUST NEVER restart the real bot — it
    # runs on the same host as prod). LVI_RESTART_COMMAND used to override
    # the command, but that env hook was a prod code-exec lane
    # (found 2026-10-04) and is now deleted. The suite monkeypatches
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

    # 9b1m. Mute error honesty (2026-10-09, the lying-error incident):
    # a node answering {ok: false, data: "unknown cmd"} to set_mute must
    # 501 with the per-line TRUTH (array = physical button, flex needs
    # 1.1.0+) — never the old lie "(firmware 1.1.0+ required)" that
    # told a 1.1.3 array node to upgrade toward a feature it can't get.
    from dashboard import routes as _routes_mut
    from dashboard import registry as _reg_mut
    _real_request = _routes_mut.node_command.request
    # seed an online room so _room_online passes (the suite's
    # connect/tee pattern — see the 9b5 fw_push pins)
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

    # 2026-10-16: the legacy manifest-only POST route is
    # REMOVED — the upload route is the only manifest writer (version and
    # track come from the image itself, board-identity-guarded). A bare
    # manifest write could point the array track at anything.
    r_leg = client.request("POST", "/api/dashboard/firmware/version",
                           json={"version": "1.0.0"})
    check("legacy POST /firmware/version removed (404, never a bare manifest write)",
          r_leg.status_code == 404, f"status={r_leg.status_code} body={r_leg.text[:100]}")

    # 2026-10-13 key-leak pin: a key PUT that fails validation must not
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

    # 9b2. Key-route hardening: the character-class
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

    # 9b3. Clip server singleton: SonosOutput and
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

    # 9b4. Reply-time backend dispatch regression (the
    # 2026-10-04 bedroom outage): bot.py must wire BOTH output
    # backends into the pipeline UNCONDITIONALLY. The old build-time
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
    # 9b11 rider (the echo-stub incident): the intelligence construction ternary
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
    _routes_live_kinds = False
    if _brain_exp is not None:
        for _sub in _ast.walk(_brain_exp.test):
            if isinstance(_sub, _ast.Constant) and _sub.value in ("hermes", "openclaw"):
                _routes_live_kinds = True
    check("bot.py: intelligence dispatch routes hermes AND openclaw to the real agent",
          _routes_live_kinds, "construction ternary must accept both live kinds")
    # Naming rule (2026-10-17): the intelligence step is named for what it
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
          and (REPO_CORE / "intelligence_agent.py").exists(), "")
    _ia_src = (REPO_CORE / "intelligence_agent.py").read_text() \
        if (REPO_CORE / "intelligence_agent.py").exists() else ""
    check("intelligence_agent logs under its own module name",
          "intelligence_agent:" in _ia_src and "hermes_agent:" not in _ia_src
          and "def _ask_intelligence(" in _ia_src, "")
    # The skip mechanism, both directions, against the throwaway config
    # (sonos zones: Dining Room/Living Room; wiim zone: Master Bedroom).
    from loguru import logger as _lg
    _cap = []
    _sink = _lg.add(lambda m: _cap.append(str(m)), level="DEBUG")
    try:
        _so_skip = SonosOutput(echo_gate=None,
                               zone_selector=lambda: "Master Bedroom", led=None)
        _wo_skip = WiiMOutput(echo_gate=None,
                              zone_selector=lambda: "Dining Room", led=None)
        _asyncio.run(_so_skip._speak_to_sonos(b"\x00" * 48000))
        _asyncio.run(_wo_skip._speak_to_wiim(b"\x00" * 48000))
        _joined = "\n".join(_cap)
        check("sonos_output skips a WiiM-owned zone at reply time (no play)",
              "zone 'Master Bedroom' not in config zones" in _joined
              and "sonos_output: playing clip" not in _joined,
              f"skip-warning-seen={'not in config zones' in _joined}")
        check("wiim_output skips a Sonos-owned zone at reply time (no play)",
              "not in wiim zones" in _joined
              and "wiim_output: playing clip" not in _joined,
              f"skip-warning-seen={'not in wiim zones' in _joined}")
    finally:
        _lg.remove(_sink)

    # 9b5. fw_push (firmware 1.1.2 connect-push): a node announcing its
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

    # 9b5c. The OTHER race order (fw=None bug, 2026-10-09): fw_push lands
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

    # Mute state (2026-10-16, firmware 1.2.11+): the node reports its mic-mute
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

    # 9b5b. Track-aware firmware upload: the image + manifest
    # must land in the DECLARED track's directory, and an unknown track
    # 400s. The old route staged everything into firmware/node/ — a
    # Flex .bin there would be OTA'd by an array node (cross-flash).
    # Board identity: real images carry exactly one OTA-track
    # URL format; the route refuses an image whose own track differs.
    # 2026-10-16: one track vocabulary (array | flex); 1.2.9+ images
    # also carry a per-line project name. "legacy" = array <=1.2.8's
    # track-less URL format (still a valid array witness on node_esp32).
    _MARK5B = {"flex": b"%.*s/firmware/flex/%s", "array": b"%.*s/firmware/array/%s",
               "legacy": b"%.*s/firmware/%s"}
    _PROJ5B = {"flex": b"flex_esp32", "array": b"array_esp32", "legacy": b"node_esp32",
               None: b"node_esp32"}

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

    _saved_updir5b = os.getcwd()
    os.chdir(SCRATCH)  # uploads are relative to cwd; keep them in scratch
    try:
        # (a) flex-declared upload → firmware/flex/
        _r = client.post("/api/dashboard/firmware/upload",
                         files={"file": ("flex.bin", _fake_image5b("1.9.9", "flex"),
                                         "application/octet-stream")},
                         data={"track": "flex"})
        _j = _r.json()
        check("9b5b flex-track upload lands in firmware/flex/",
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
        check("9b5b unknown track 400s (never a silent wrong dir)",
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
        check("9b5b upload without track 422s (no silent array default; not pipecat's Stream-consumed 500)",
              _rc.status_code == 422 and "track" in _rc.text, f"{_rc.status_code} {_rc.text[:120]}")
        check("9b5b track-less upload left both manifests untouched",
              all((Path(f"firmware/{t}/version.json").read_text()
                   if Path(f"firmware/{t}/version.json").exists() else None) == _snap5c[t]
                  for t in ("array", "flex")), str(_snap5c)[:120])
        # explicit array track still works
        _rd = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("arr.bin", _fake_image5b("1.9.7"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("9b5b explicit array track upload lands in firmware/array/",
              _rd.status_code == 200 and _rd.json().get("track") == "array"
              and json.loads(Path("firmware/array/version.json").read_text())["version"] == "1.9.7",
              _rd.text[:120])

        # Board identity — the image's own OTA-track marker must match
        # the declared track; every refusal leaves BOTH tracks' served
        # image + manifest byte-identical.
        def _snap045():
            out = {}
            for t in ("array", "flex"):
                for f in ("version.json", f"{t}_esp32.bin"):
                    q = Path(f"firmware/{t}/{f}")
                    out[f"{t}/{f}"] = q.read_bytes() if q.exists() else None
            return out
        _before045 = _snap045()
        _re = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", "flex"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("9b5b Flex image declared for the ARRAY track -> 400, names the board",
              _re.status_code == 400 and "reSpeaker Flex firmware" in _re.text
              and "track 'flex'" in _re.text, f"{_re.status_code} {_re.text[:160]}")
        _rf = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", "array"),
                                          "application/octet-stream")},
                          data={"track": "flex"})
        check("9b5b array image declared for the FLEX track -> 400, names the board",
              _rf.status_code == 400 and "array (XVF3800" in _rf.text
              and "track 'array'" in _rf.text, f"{_rf.status_code} {_rf.text[:160]}")
        _rg = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", None),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("9b5b image with NO track marker -> 400 (never guessed onto a track)",
              _rg.status_code == 400 and "can't tell which board" in _rg.text,
              f"{_rg.status_code} {_rg.text[:160]}")
        _rh = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("node_esp32.bin", _fake_image5b("1.9.6", both=True),
                                          "application/octet-stream")},
                          data={"track": "flex"})
        check("9b5b image with BOTH markers -> 400 (can't be trusted either way)",
              _rh.status_code == 400 and "can't tell which board" in _rh.text,
              f"{_rh.status_code} {_rh.text[:160]}")
        _after045 = _snap045()
        check("9b5b every refusal left both tracks' served image + manifest byte-identical",
              _after045 == _before045,
              str([k for k in _before045 if _before045[k] != _after045.get(k)]))
        _leftover045 = [str(q) for t in ("array", "flex")
                        for q in Path(f"firmware/{t}").glob("*.uploading")]
        check("9b5b every refusal cleaned up its .uploading temp", not _leftover045,
              str(_leftover045))

        # deploy_firmware.sh's backstop uses the same helper via the CLI
        import subprocess as _sp045
        _abv = str(REPO_CORE.parent / "scripts" / "app_bin_version.py")
        _cli = {}
        for _lbl, _img in (("flex", _fake_image5b("1.9.5", "flex")), ("array", _fake_image5b("1.9.5", "array")),
                           ("none", _fake_image5b("1.9.5", None)), ("both", _fake_image5b("1.9.5", both=True))):
            _fp = Path(SCRATCH) / f"cli045-{_lbl}.bin"
            _fp.write_bytes(_img)
            _cli[_lbl] = _sp045.run([sys.executable, _abv, "--track", str(_fp)],
                                    capture_output=True, text=True).stdout.strip()
            _fp.unlink()
        check("9b5b app_bin_version.py --track: flex/array/none/both -> flex/array/''/''",
              _cli == {"flex": "flex", "array": "array", "none": "", "both": ""}, str(_cli))

        # 2026-10-16: firmware names — one vocabulary, per-line files.
        _rn = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("x.bin", _fake_image5b("1.9.4", "array"),
                                          "application/octet-stream")},
                          data={"track": "node"})
        check("9b5b retired track name 'node' -> 400 naming array | flex",
              _rn.status_code == 400 and "array | flex" in _rn.text, f"{_rn.status_code} {_rn.text[:120]}")
        # In-field images (<=1.2.8, project node_esp32) are still judged by
        # their URL marker: array legacy URL -> array, flex URL -> flex.
        _rl = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("old.bin", _fake_image5b("1.9.3", "legacy"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        check("9b5b legacy array image (node_esp32 + track-less URL) still uploads to array",
              _rl.status_code == 200 and _rl.json().get("track") == "array", f"{_rl.status_code} {_rl.text[:120]}")
        _rlf = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("oldf.bin", _fake_image5b("1.9.3", "flex", project=b"node_esp32"),
                                           "application/octet-stream")},
                           data={"track": "flex"})
        check("9b5b legacy flex image (node_esp32 + flex URL) still uploads to flex",
              _rlf.status_code == 200 and _rlf.json().get("track") == "flex", f"{_rlf.status_code} {_rlf.text[:120]}")
        # A named image must agree with its own URL marker.
        _before046 = _snap045()
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
        check("9b5b project name vs URL disagreement / unknown project -> 400, nothing written",
              all(r.status_code == 400 and "can't tell which board" in r.text for r in (_rm, _rm2, _rm3))
              and _snap045() == _before046,
              f"{_rm.status_code} {_rm2.status_code} {_rm3.status_code}")
        # Served file is named for its track; a pre-rename .bin is swept.
        Path("firmware/array/node_esp32.bin").write_bytes(b"stale")
        _rs = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("whatever-name.bin", _fake_image5b("1.9.9", "array"),
                                          "application/octet-stream")},
                          data={"track": "array"})
        _man046 = json.loads(Path("firmware/array/version.json").read_text())
        check("9b5b upload serves firmware/array/array_esp32.bin, manifest names it, stale node_esp32.bin removed",
              _rs.status_code == 200 and _rs.json().get("filename") == "array_esp32.bin"
              and _man046 == {"version": "1.9.9", "filename": "array_esp32.bin"}
              and sorted(q.name for q in Path("firmware/array").glob("*.bin")) == ["array_esp32.bin"],
              f"{_rs.text[:120]} {_man046} {[q.name for q in Path('firmware/array').glob('*.bin')]}")
        _rsf = client.post("/api/dashboard/firmware/upload",
                           files={"file": ("f.bin", _fake_image5b("1.9.9", "flex"),
                                           "application/octet-stream")},
                           data={"track": "flex"})
        check("9b5b flex upload serves firmware/flex/flex_esp32.bin",
              _rsf.status_code == 200 and _rsf.json().get("filename") == "flex_esp32.bin"
              and sorted(q.name for q in Path("firmware/flex").glob("*.bin")) == ["flex_esp32.bin"],
              _rsf.text[:120])

        # The OTA server (core/firmware_server.py) — first suite
        # coverage of the node-facing routes. Tracks point at this scratch.
        import firmware_server as _fws
        from fastapi import FastAPI as _FA046
        _saved_tracks046 = dict(_fws.FIRMWARE_TRACKS)
        _fws.FIRMWARE_TRACKS.update({"array": Path("firmware/array").resolve(),
                                     "flex": Path("firmware/flex").resolve()})
        try:
            _app046 = _FA046()
            _fws.register_firmware_routes(_app046)
            _c046 = TestClient(_app046)
            _legacy = _c046.get("/firmware/version")
            _arr = _c046.get("/firmware/array/version")
            _flx = _c046.get("/firmware/flex/version")
            check("9b5b OTA: legacy /firmware/version == /firmware/array/version (array <=1.2.8 keeps updating)",
                  _legacy.status_code == 200 and _legacy.json() == _arr.json()
                  == {"version": "1.9.9", "filename": "array_esp32.bin"},
                  f"{_legacy.status_code} {_legacy.text[:80]} | {_arr.text[:80]}")
            check("9b5b OTA: /firmware/flex/version serves the flex manifest",
                  _flx.status_code == 200 and _flx.json() == {"version": "1.9.9", "filename": "flex_esp32.bin"},
                  _flx.text[:80])
            _lb = _c046.get("/firmware/array_esp32.bin")
            _tb = _c046.get("/firmware/array/array_esp32.bin")
            check("9b5b OTA: legacy /firmware/<bin> and /firmware/array/<bin> serve the same array image",
                  _lb.status_code == 200 and _lb.content == _tb.content
                  == Path("firmware/array/array_esp32.bin").read_bytes(), f"{_lb.status_code} {_tb.status_code}")
            check("9b5b OTA: retired /firmware/node/version 404s (unknown track = no update, safe)",
                  _c046.get("/firmware/node/version").status_code == 404, "")
            check("9b5b OTA: path traversal out of a track dir 404s",
                  _c046.get("/firmware/array/..%2F..%2Fconfig.yaml").status_code == 404, "")
        finally:
            _fws.FIRMWARE_TRACKS.clear()
            _fws.FIRMWARE_TRACKS.update(_saved_tracks046)
    finally:
        os.chdir(_saved_updir5b)

    # 9b6. OWW model panel (Voice tab): upload validates, select merges,
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
        # (b) upload accepts the REAL production model bytes
        prod = Path("Hey_Willow_1-0.onnx")
        if prod.is_file():
            _r_up = client.post("/api/dashboard/oww/models",
                            files={"file": (prod.name, prod.read_bytes(), "application/octet-stream")})
            check("oww: real model upload accepted",
                  _r_up.status_code == 200 and _r_up.json().get("name") == prod.name,
                  f"status={_r_up.status_code}")
        # (d) select merges into wake section (threshold survives) — run
        # FIRST so the uploaded models/ copy becomes the ACTIVE file
        prod = Path("Hey_Willow_1-0.onnx")
        if prod.is_file():
            _r_sel = client.put("/api/dashboard/oww/select",
                            json={"model": prod.name})
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
        # spare from the prod bytes, then delete it (200). Never touches
        # the real root copy: a suite must not delete production files
        # (and the dev host's root copy is the ONLY local model copy).
        if prod.is_file():
            _r_sp = client.post("/api/dashboard/oww/models",
                            files={"file": ("zz_suite_probe.onnx", prod.read_bytes(), "application/octet-stream")})
            _r_spdel = client.delete("/api/dashboard/oww/models/zz_suite_probe.onnx?source=models")
            check("oww: non-active spare uploads and deletes cleanly",
                  _r_sp.status_code == 200 and _r_spdel.status_code == 200,
                  f"upload={_r_sp.status_code} delete={_r_spdel.status_code}")
    finally:
        os.environ.pop("LVI_MODELS_DIR", None)
        shutil.rmtree(models_dir, ignore_errors=True)

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
    check("tuning write preserves intelligence.fillers (merge, not replace)",
          isinstance(brain_after.get("fillers"), list) and len(brain_after["fillers"]) == 2,
          f"fillers={str(brain_after.get('fillers'))[:80]}")
    wake_after = config_store.read_section("wake") or {}
    check("tuning write preserves wake.enabled (merge, not replace)",
          wake_after.get("enabled") is True, f"wake={wake_after}")

    # 9b7. Load-anchored speech timers (2026-10-05): the v4
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
                              if m in ("bot-stopped-speaking", "purple", "oww-sleeping")]
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

            _asyncio.run(_run())
        finally:
            _wo_mod.wiim_api = _real_wiim_api
            if _saved_sonos_ws is None:
                sys.modules.pop("sonos_websocket", None)
            else:
                sys.modules["sonos_websocket"] = _saved_sonos_ws

    # 9d. Rig facts (Settings tab header) — status carries live rig facts
    r = client.get("/api/dashboard/status")
    s = r.json()
    check("status exposes wake_model",
          r.status_code == 200 and bool(s.get("wake_model")), f"wake_model={s.get('wake_model')!r}")
    check("status exposes stt_model",
          bool(s.get("stt_model")), f"stt_model={s.get('stt_model')!r}")
    check("status exposes tts_provider",
          bool(s.get("tts_provider")), f"tts_provider={s.get('tts_provider')!r}")

    # 2026-10-16 TTS health tap: tracked TTS services (bot.py TTSTracked)
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
    # Dynamic, not static: set config wake.model and re-GET status —
    # the readout must move with the config (it reads through the
    # same chain the consumer uses).
    try:
        wake_sec = config_store.read_section("wake") or {}
        wake_sec["model"] = "UNIT_TEST_WAKE_MODEL.onnx"
        config_store.write_section("wake", wake_sec)
        s2 = client.get("/api/dashboard/status").json()
        check("wake_model tracks config changes (not static)",
              s2.get("wake_model") == "UNIT_TEST_WAKE_MODEL.onnx",
              f"before={s.get('wake_model')!r} after={s2.get('wake_model')!r}")
    finally:
        # restore the section we touched (throwaway config anyway)
        pass

    # 10. REAL soco discovery on the LAN (best effort — players may nap)
    r = client.post("/api/dashboard/zones/discover")
    if r.status_code == 200:
        found = r.json()["found"]
        check("soco discovery ran", True, f"found {len(found)} player(s): "
              + ", ".join(f"{f['name']} ({f['model']})" for f in found))
    else:
        print(f"[WARN] discovery endpoint returned {r.status_code}: {r.text[:120]}")

    # 12. Guard: cross-origin mutation rejected, non-browser passes
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "Dining Room"}},
                   headers={"Origin": "https://evil.example"})
    check("guard: cross-origin mutation 403", r.status_code == 403, f"status={r.status_code}")
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "Dining Room"}})
    check("guard: no-Origin (curl) mutation passes", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/api/dashboard/status", headers={"Origin": "https://evil.example"})
    check("guard: cross-origin READ stays open", r.status_code == 200, f"status={r.status_code}")

    # 13. Token mode (env set -> mutations need the header)
    os.environ["LVI_DASHBOARD_TOKEN"] = "rig-secret"
    try:
        r = client.put("/api/dashboard/mapping",
                       json={"room_zones": {"bedroom": "Dining Room"}})
        check("guard: token mode, bare mutation 401", r.status_code == 401, f"status={r.status_code}")
        r = client.put("/api/dashboard/mapping",
                       json={"room_zones": {"bedroom": "Dining Room"}},
                       headers={"X-Dashboard-Token": "rig-secret"})
        check("guard: token mode, matching header passes", r.status_code == 200, f"status={r.status_code}")
    finally:
        del os.environ["LVI_DASHBOARD_TOKEN"]

    # 9b8. Epoch turns + wake-word barge-in: turns
    # run as background tasks (frame loop never blocks on the intelligence), a
    # new question supersedes (old reply discarded, no history write,
    # ack spoken), and the wake word twice-confirmed during LOCKED fires
    # the interrupt + reopens the gate; backends stop their clips.
    if True:  # scope block
        import os as _os
        _os.environ["LVI_HERMES_KEY"] = "suite-test-key-not-real"
        _os.environ["LVI_SESSIONS"] = str(SCRATCH / "sessions-9b8.json")
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
        from intelligence_agent import IntelligenceAgent as _Agent  # noqa: E402
        from wake_word import TurnState as _TurnState, WakeWordFilter as _WF  # noqa: E402
        _FD = _FD8

        _SPOKEN8 = []

        async def _agent_probe():
            agent = _Agent(echo_gate=_EchoGate8(), room="suite-9b8",
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

            async def say(txt):
                await agent.process_frame(_VADStart(), _FD8.DOWNSTREAM)
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
            _agent, _block_time = _asyncio.run(_agent_probe())
            check("9b8 turn: frame loop never blocks on the intelligence (was 2.5s)",
                  _block_time < 0.5, f"blocked={_block_time:.2f}s")
            check("9b8 turn: superseded reply never spoken (stale question dropped)",
                  not any("weather" in s for s in _SPOKEN8),
                  f"spoken={_SPOKEN8}")
            check("9b8 turn: supersede ack spoken",
                  any("switching" in s for s in _SPOKEN8), f"spoken={_SPOKEN8}")
            check("9b8 turn: new turn's reply spoken",
                  any("time is it" in s for s in _SPOKEN8), f"spoken={_SPOKEN8}")
            _hist = [h.get("content", "") for h in _agent._history]
            check("9b8 turn: no history write for the dead turn",
                  not any("weather" in h for h in _hist) and
                  any("time is it" in h for h in _hist), f"hist={_hist}")

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
            check("9b8 wake: one over-threshold frame does NOT fire (2-frame confirm)",
                  _one8 == 0, f"fired_after_one={_one8}")
            check("9b8 wake: two consecutive frames fire the interrupt, gate reopens",
                  len(_fired8) == 1 and _wf8._state is _TurnState.AWAKE,
                  f"fired={len(_fired8)} state={_wf8._state}")
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
            check("9b8 wake: barge_in default OFF never interrupts (bit-for-bit old)",
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
            check("9b8 wake: ASYNC on_interrupt coroutine actually runs",
                  len(_ran8c) == 1 and _wf8c._interrupt_task is not None
                  and _wf8c._interrupt_task.done(),
                  f"ran={len(_ran8c)} task={getattr(_wf8c, '_interrupt_task', 'missing')}")
            # ── backend clip stop (fake player layers, 9b7 pattern) ──
            _CANCELS8 = []

            class _FakeSw8:
                def __init__(self, ip, player_id=None):
                    pass

                async def connect(self):
                    return

                async def play_clip(self, url, volume=None):
                    await asyncio.sleep(0.05)
                    return {"clipId": "clip-9b8"}

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
                check("9b8 stop: reply clip id held before interrupt", _id8 is not None,
                      f"id={_id8}")
                if _id8 is not None:
                    _asyncio.run(_so8.stop_current_clip())
                    check("9b8 stop: sonos barge-in cancels the held clipId",
                          "clip-9b8" in _CANCELS8, f"cancels={_CANCELS8}")
                    check("9b8 stop: interrupt is idempotent (second call, no clip)",
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
                    check("9b8 stop: wiim barge-in sends setPlayerCmd:stop",
                          any("setPlayerCmd:stop" in c for c in _WIIM_CMDS8),
                          f"cmds={_WIIM_CMDS8}")
                finally:
                    _wo8_mod.wiim_api = _saved_wiim_api8
            finally:
                if _saved_ws8 is None:
                    sys.modules.pop("sonos_websocket", None)
                else:
                    sys.modules["sonos_websocket"] = _saved_ws8

            # 9b9. Frame passthrough (the "you wont wake" outage): the
            # epoch-turn _run_turn extraction lost process_frame's final push_frame —
            # the agent ate EVERY frame, StartFrame never reached the sink,
            # PipelineWorker killed the session at +20s, and the node rode a
            # reconnect loop. Every processor MUST pass frames downstream.
            async def _passthrough_probe():
                _agent2 = _Agent(echo_gate=_EchoGate8(), room="suite-9b9",
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
            check("9b9 agent: every frame passes through (StartFrame must reach the sink)",
                  all(any(x is f for x in _seen2) for f in _frames2),
                  f"forwarded={sum(1 for f in _frames2 if any(x is f for x in _seen2))}/{len(_frames2)}")

            # 9b10. Echo-gate relax on confirmed wakes and barge-ins (the
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
            check("9b10 echo: normal wake relaxes the gate (follow-up lands)",
                  _aw <= 0.6, f"remaining={_aw:.2f}s")
            check("9b10 echo: barge-in interrupt relaxes the gate",
                  _ab <= 0.6, f"remaining={_ab:.2f}s")
            check("9b10 echo: relax never extends a lapsed gate",
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
            check("9b10 echo: stop_current_clip relaxes the gate",
                  _stop_rem <= 0.6, f"remaining={_stop_rem:.2f}s")

            # 9b11. Intelligence lane: config round-trip
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
            check("9b11 PUT ok", _r.status_code == 200, _r.text[:120])
            import yaml as _yaml9
            _brain9 = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("9b11 PUT writes kind/url/agent",
                  _brain9.get("kind") == "openclaw"
                  and _brain9.get("url") == "http://127.0.0.1:18789/v1"
                  and _brain9.get("agent") == "suite-agent",
                  str(_brain9)[:120])
            check("9b11 PUT merge: fillers survive",
                  "fillers" in _brain9, str(_brain9)[:160])
            # GET round-trips
            _g = client.get("/api/dashboard/intelligence").json()
            check("9b11 GET returns kind/agent/key_set",
                  _g.get("kind") == "openclaw" and _g.get("agent") == "suite-agent"
                  and "key_set" in _g, str(_g)[:120])
            # (a2) per-kind URL memory: PUT files url under the kind; GET
            # returns the recall map; switching kinds keeps each URL.
            _r2 = client.put("/api/dashboard/intelligence", json={
                "kind": "hermes", "url": "http://127.0.0.1:8642/v1"})
            check("9b11b switch to hermes ok", _r2.status_code == 200, _r2.text[:120])
            _brain9b = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("9b11b urls map holds both kinds",
                  _brain9b.get("url") == "http://127.0.0.1:8642/v1"
                  and _brain9b.get("urls", {}).get("openclaw") == "http://127.0.0.1:18789/v1"
                  and _brain9b.get("urls", {}).get("hermes") == "http://127.0.0.1:8642/v1",
                  str(_brain9b.get("urls"))[:160])
            _g2 = client.get("/api/dashboard/intelligence").json()
            check("9b11b GET returns urls map", _g2.get("urls", {}).get("openclaw")
                  == "http://127.0.0.1:18789/v1", str(_g2.get("urls"))[:120])
            # Bare kind flip: no url in the payload — the route
            # must restore THAT kind's remembered URL, not keep the other
            # kind's (which would point the lane at the wrong intelligence).
            _r3 = client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})
            _brain9c = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("9b11b bare kind flip restores that kind's remembered URL",
                  _brain9c.get("url") == "http://127.0.0.1:18789/v1",
                  f"url={_brain9c.get('url')}")
            # restore kind for later checks: the lane probes below build
            # IntelligenceAgent against the LIVE throwaway config and expect
            # openclaw (the kind the earlier (a) block left behind)
            client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})

            # (b) invalid kind / agent / url -> 422
            _bad = client.put("/api/dashboard/intelligence", json={"kind": "claude"})
            check("9b11 retired kind 422s", _bad.status_code == 422, str(_bad.status_code))
            _bad2 = client.put("/api/dashboard/intelligence",
                               json={"kind": "openclaw", "agent": "bad agent!"})
            check("9b11 invalid agent 422s", _bad2.status_code == 422, str(_bad2.status_code))
            _bad3 = client.put("/api/dashboard/intelligence",
                               json={"kind": "openclaw", "url": "ftp://nope"})
            check("9b11 invalid url 422s", _bad3.status_code == 422, str(_bad3.status_code))

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
                for mod_name in ("intelligence_agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import intelligence_agent as _ha9
                _urq9.urlopen = _fake_open9
                # kind from the LVI_CONFIG we just PUT (openclaw)
                _a9 = _ha9.IntelligenceAgent(lull_secs=0.05, room="suite",
                                       session_mode="sticky")
                _a9._ask_intelligence("hi")
                _oc_body = _cap9[-1]["body"]
                _oc_hdr = _cap9[-1]["headers"]
                check("9b11 openclaw lane: model encodes agent, no conversation",
                      _oc_body.get("model") == "openclaw/suite-agent"
                      and "conversation" not in _oc_body, str(_oc_body)[:140])
                check("9b11 openclaw lane: sticky session-key header",
                      _oc_hdr.get("x-openclaw-session-key") == "voice-suite",
                      str(_oc_hdr.get("x-openclaw-session-key")))
                check("9b11 openclaw lane: LVI_OPENCLAW_KEY wins (naming law 2026-10-08)",
                      _oc_hdr.get("authorization") == "Bearer suite-openclaw-key-not-real",
                      str(_oc_hdr.get("authorization")))
                # strict key law (2026-10-08): NO cross-kind fallback —
                # without LVI_OPENCLAW_KEY the openclaw lane must fail with
                # a clear error naming its OWN variable, never silently
                # auth with another intelligence's token (a real wrong-token 401).
                _os.environ.pop("LVI_OPENCLAW_KEY", None)
                for mod_name in ("intelligence_agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import intelligence_agent as _ha9_fb
                _a9_fb = _ha9_fb.IntelligenceAgent(lull_secs=0.05, room="suite",
                                             session_mode="fresh")
                try:
                    _a9_fb._ask_intelligence("hi")
                    _strict_ok9 = False
                    _strict_err9 = "no error raised"
                except RuntimeError as _e9:
                    _strict_ok9 = "LVI_OPENCLAW_KEY" in str(_e9)
                    _strict_err9 = str(_e9)[:80]
                check("9b11 openclaw lane: missing LVI_OPENCLAW_KEY fails loud, names its own variable",
                      _strict_ok9, _strict_err9)
                _os.environ["LVI_OPENCLAW_KEY"] = "suite-openclaw-key-not-real"
                # fresh: header absent
                _a9f = _ha9.IntelligenceAgent(lull_secs=0.05, room="suite",
                                        session_mode="fresh")
                _a9f._ask_intelligence("hi")
                check("9b11 openclaw lane: fresh sends no session header",
                      "x-openclaw-session-key" not in _cap9[-1]["headers"], "")
                # hermes round-trip: flip kind back, conversation field returns
                client.put("/api/dashboard/intelligence", json={"kind": "hermes"})
                for mod_name in ("intelligence_agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import intelligence_agent as _ha9b
                _a9b = _ha9b.IntelligenceAgent(lull_secs=0.05, room="suite",
                                         session_mode="fresh")
                _a9b._ask_intelligence("hi")
                _hm_body = _cap9[-1]["body"]
                _hm_hdr = _cap9[-1]["headers"]
                check("9b11 hermes round-trip: conversation field + Hermes header restored",
                      _hm_body.get("conversation") and
                      _hm_hdr.get("x-hermes-session-key") == _hm_body.get("conversation"),
                      str(_hm_body)[:140])
                # Key rule: keys are KIND-AWARE — the hermes
                # lane must auth with LVI_HERMES_KEY even when LVI_BRAIN_KEY
                # (the OpenClaw token on the real rig) is also set. A kind
                # flip must never cross the tokens.
                check("9b11 hermes lane: kind-aware key (LVI_HERMES_KEY wins on hermes)",
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
                        check("9b11 no-key: ask raises (not success)", False, "no raise")
                    except RuntimeError as _e9:
                        check("9b11 no-key: RuntimeError with a clear message",
                              "no intelligence key" in str(_e9).lower(), str(_e9))
                    except KeyError as _e9:
                        check("9b11 no-key: RuntimeError with a clear message",
                              False, f"KeyError brick: {_e9}")
                except KeyError as _e9:
                    check("9b11 no-key: construction survives", False,
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
                check("9b11 key POST ok + never echoes material",
                      _kr.status_code == 200 and _kj.get("key_set") is True
                      and "suite-keymaterial-12345" not in _kr.text,
                      _kr.text[:120])
                _kbad = client.put("/api/dashboard/intelligence/key",
                                   json={"kind": "hermes", "api_key": "bad\nkey"})
                check("9b11 key POST newline 422s", _kbad.status_code == 422, "")
                # Contract pins: kind is REQUIRED (no silent
                # guess from config), only shipped lanes, extra="forbid".
                _knk = client.put("/api/dashboard/intelligence/key",
                                  json={"api_key": "suite-keymaterial-12345"})
                check("9b11c key POST without kind 422s",
                      _knk.status_code == 422, _knk.text[:120])
                _kbk = client.put("/api/dashboard/intelligence/key",
                                  json={"kind": "claude",
                                        "api_key": "suite-keymaterial-12345"})
                check("9b11c key POST unshipped kind 422s",
                      _kbk.status_code == 422, _kbk.text[:120])
                _kex = client.put("/api/dashboard/intelligence/key",
                                  json={"kind": "hermes", "apikey": "x",
                                        "api_key": "suite-keymaterial-12345"})
                check("9b11c key POST extra field 422s (forbid)",
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
                    check("9b11b key save follows payload kind, not config",
                          _wek_calls9 == ["LVI_OPENCLAW_KEY", "LVI_HERMES_KEY"],
                          f"calls={_wek_calls9}")
                    check("9b11b key response names the variable written",
                          _kx1.json().get("var") == "LVI_OPENCLAW_KEY"
                          and _kx2.json().get("var") == "LVI_HERMES_KEY",
                          f"{_kx1.text[:80]} | {_kx2.text[:80]}")
                    _cfg_kind9 = (_yaml9.safe_load(
                        Path(os.environ["LVI_CONFIG"]).read_text())
                        .get("intelligence") or {}).get("kind")
                    check("9b11b key save never touches config kind",
                          _cfg_kind9 == "openclaw", f"kind={_cfg_kind9}")
                    # GET reports key status for EVERY kind (chip follows
                    # the kind on screen).
                    _skh9 = _os.environ.pop("LVI_HERMES_KEY", None)
                    _sko9 = _os.environ.get("LVI_OPENCLAW_KEY")
                    _os.environ["LVI_OPENCLAW_KEY"] = "suite-oc-token-123"
                    try:
                        _gk9 = client.get("/api/dashboard/intelligence").json()
                        check("9b11c GET keys_set per kind",
                              _gk9.get("keys_set") == {"hermes": False, "openclaw": True}
                              and "suite-oc-token-123" not in str(_gk9),
                              str(_gk9.get("keys_set")))
                    finally:
                        if _skh9 is not None:
                            _os.environ["LVI_HERMES_KEY"] = _skh9
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

            # 9b16. Agent picker + masked key hints (2026-10-17): agents
            # discovered per provider, remembered per kind; a Hermes profile
            # routes to <base>/p/<agent>/v1 with ITS OWN key; set keys show
            # their first 4 characters, never more.
            import intelligence_target as _it12
            import threading as _th12
            import http.server as _hs12
            _h12 = _it12.key_hint
            _M12 = "\u2022" * 8
            check("9b16 key_var: hermes default -> LVI_HERMES_KEY, profile -> LVI_HERMES_KEY_<AGENT>, openclaw shares one",
                  _it12.key_var("hermes", "") == "LVI_HERMES_KEY"
                  and _it12.key_var("hermes", "default") == "LVI_HERMES_KEY"
                  and _it12.key_var("hermes", "home-helper") == "LVI_HERMES_KEY_HOME_HELPER"
                  and _it12.key_var("openclaw", "lenoxvi-test") == "LVI_OPENCLAW_KEY", "")
            check("9b16 request_url: hermes profile -> /p/<agent>/v1; default + openclaw unchanged",
                  _it12.request_url("hermes", "http://h:8642/v1", "home-helper") == "http://h:8642/p/home-helper/v1"
                  and _it12.request_url("hermes", "http://h:8642/v1/", "default") == "http://h:8642/v1"
                  and _it12.request_url("hermes", "http://h:8642/p/old/v1", "home-helper") == "http://h:8642/p/home-helper/v1"
                  and _it12.request_url("openclaw", "http://o:18789", "x") == "http://o:18789", "")
            check("9b16 legacy top-level agent never reroutes Hermes (kind hermes + agent lenoxvi-test)",
                  _it12.agent_for({"agent": "lenoxvi-test"}, "hermes") == ""
                  and _it12.agent_for({"agent": "lenoxvi-test"}, "openclaw") == "lenoxvi-test"
                  and _it12.agent_for({"agents": {"hermes": "default"}}, "hermes") == ""
                  and _it12.agent_for({"agents": {"hermes": "home-helper"}}, "hermes") == "home-helper", "")
            check("9b16 key hint: first 4 + fixed-length mask; short keys show no characters; unset -> None",
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
                check("9b16 openclaw discovery: openclaw/<agent> ids only, invalid names dropped, token sent",
                      [a["id"] for a in _oc12["agents"]] == ["default", "suite-a"]
                      and _auth12[-1] == "Bearer ockey-abcdefghijkl", str(_oc12)[:200])
                _oc12b = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}/v1", _env12)
                check("9b16 openclaw discovery: URL with or without /v1",
                      [a["id"] for a in _oc12b["agents"]] == ["default", "suite-a"], str(_oc12b)[:120])
                _hm12 = _it12.discover_agents("hermes", "http://ignored/v1", _env12,
                                              status_url=f"http://127.0.0.1:{_port12}/api/status")
                check("9b16 hermes discovery: profiles from status, default first, parked + invalid dropped",
                      [a["id"] for a in _hm12["agents"]] == ["default", "home-helper"]
                      and _hm12["source"] == "hermes-status", str(_hm12)[:200])
                _by12 = {a["id"]: a for a in _hm12["agents"]}
                check("9b16 hermes discovery: per-profile key status (default set, profile unset)",
                      _by12["default"]["key_set"] and not _by12["home-helper"]["key_set"]
                      and _by12["home-helper"]["key_hint"] is None, str(_by12)[:200])
                check("9b16 discovery never returns key material (hint = first 4 only)",
                      "abcdefghijkl" not in json.dumps([_oc12, _hm12])
                      and _by12["default"]["key_hint"] == "hmke" + _M12, "")
                _hd12 = _it12.discover_agents("hermes", "http://127.0.0.1:9/v1", _env12, timeout=1.0,
                                              status_url="http://127.0.0.1:9/api/status")
                check("9b16 hermes discovery: dashboard down -> default agent + honest note",
                      [a["id"] for a in _hd12["agents"]] == ["default"] and _hd12["source"] == "fallback"
                      and "default agent only" in (_hd12.get("note") or ""), str(_hd12)[:200])
                _nk12 = _it12.discover_agents("openclaw", f"http://127.0.0.1:{_port12}", {})
                check("9b16 openclaw discovery without a key says so",
                      _nk12["agents"] == [] and "key" in (_nk12.get("note") or ""), str(_nk12))

                # route: kind validated, URL override, hermes via the status port
                _os.environ["LVI_OPENCLAW_KEY"] = "ockey-abcdefghijkl"
                _os.environ["LVI_HERMES_KEY"] = "hmkey-abcdefghijkl"
                _it12.HERMES_STATUS_PORT = _port12
                _ra12 = client.post("/api/dashboard/intelligence/agents",
                                    json={"kind": "openclaw", "url": f"http://127.0.0.1:{_port12}"})
                check("9b16 POST /intelligence/agents openclaw: agents + selected",
                      _ra12.status_code == 200 and [a["id"] for a in _ra12.json()["agents"]] == ["default", "suite-a"]
                      and "selected" in _ra12.json(), _ra12.text[:200])
                _rh12 = client.post("/api/dashboard/intelligence/agents",
                                    json={"kind": "hermes", "url": "http://127.0.0.1:1/v1"})
                check("9b16 POST /intelligence/agents hermes: profiles via the status port, no key material",
                      _rh12.status_code == 200 and [a["id"] for a in _rh12.json()["agents"]] == ["default", "home-helper"]
                      and "abcdefghijkl" not in _rh12.text, _rh12.text[:200])
                _rb12 = client.post("/api/dashboard/intelligence/agents", json={"kind": "claude"})
                _rb12b = client.post("/api/dashboard/intelligence/agents", json={"kind": "hermes", "url": "file:///etc"})
                check("9b16 POST /intelligence/agents: unknown kind + non-http URL 422",
                      _rb12.status_code == 422 and _rb12b.status_code == 422, f"{_rb12.status_code} {_rb12b.status_code}")

                # PUT: agent filed under its kind; legacy key only for openclaw
                client.put("/api/dashboard/intelligence", json={"kind": "openclaw", "agent": "suite-a"})
                client.put("/api/dashboard/intelligence", json={"kind": "hermes", "agent": "home-helper"})
                _c12 = _yaml9.safe_load(Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
                check("9b16 PUT files the agent per kind (hermes never overwrites openclaw's)",
                      (_c12.get("agents") or {}).get("hermes") == "home-helper"
                      and (_c12.get("agents") or {}).get("openclaw") == "suite-a"
                      and _c12.get("agent") == "suite-a", str(_c12.get("agents")) + " agent=" + str(_c12.get("agent")))
                _g12 = client.get("/api/dashboard/intelligence").json()
                check("9b16 GET agents per kind + first-4 key hints, never the key",
                      _g12.get("agents") == {"hermes": "home-helper", "openclaw": "suite-a"}
                      and (_g12.get("key_hints") or {}).get("openclaw") == "ocke" + _M12
                      and "abcdefghijkl" not in json.dumps(_g12), str(_g12)[:240])

                # the voice agent: profile turn -> /p/home-helper/v1 with the profile's key
                _urls12 = []
                def _fake_open12(req, timeout=None):
                    _urls12.append((req.full_url, {k.lower(): v for k, v in req.headers.items()}))
                    if req.data is None:  # the probe's health GET
                        return _FakeResp9(b"{}")
                    return _fake_open9(req, timeout)
                _os.environ["LVI_HERMES_KEY_HOME_HELPER"] = "helper-key-not-real-123"
                if "intelligence_agent" in sys.modules:
                    del sys.modules["intelligence_agent"]
                import intelligence_agent as _ia12
                _urq9.urlopen = _fake_open12
                _a12 = _ia12.IntelligenceAgent(lull_secs=0.05, room="suite", session_mode="fresh")
                _a12._ask_intelligence("hi")
                check("9b16 hermes profile turn: /p/home-helper/v1 + LVI_HERMES_KEY_HOME_HELPER",
                      _urls12[-1][0].endswith("/p/home-helper/v1/chat/completions")
                      and _urls12[-1][1].get("authorization") == "Bearer helper-key-not-real-123",
                      f"{_urls12[-1][0]} {_urls12[-1][1].get('authorization')}")
                client.put("/api/dashboard/intelligence", json={"kind": "hermes", "agent": "default"})
                if "intelligence_agent" in sys.modules:
                    del sys.modules["intelligence_agent"]
                import intelligence_agent as _ia12b
                _a12b = _ia12b.IntelligenceAgent(lull_secs=0.05, room="suite", session_mode="fresh")
                _a12b._ask_intelligence("hi")
                check("9b16 hermes default agent: configured URL + LVI_HERMES_KEY (unchanged behaviour)",
                      "/p/" not in _urls12[-1][0]
                      and _urls12[-1][1].get("authorization") == "Bearer hmkey-abcdefghijkl", _urls12[-1][0])

                # Test probe: the saved profile's URL + key
                _urls12.clear()
                _routes9._intelligence_probe({"kind": "hermes", "url": "http://127.0.0.1:1/v1",
                                              "agents": {"hermes": "home-helper"}})
                check("9b16 Test probe uses the profile URL + its key",
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
                    check("9b16 key save: hermes profile -> its own var; openclaw ignores agent; default -> base",
                          _wek12 == ["LVI_HERMES_KEY_HOME_HELPER", "LVI_OPENCLAW_KEY", "LVI_HERMES_KEY"], str(_wek12))
                    check("9b16 key save returns the first-4 hint, never the key",
                          _kp12.json().get("key_hint") == "help" + _M12
                          and "abcdefghijkl" not in _kp12.text + _kp12b.text + _kp12c.text, _kp12.text[:160])
                    _kb12 = client.put("/api/dashboard/intelligence/key", json={
                        "kind": "hermes", "agent": "../x", "api_key": "helperkey-abcdefghijkl"})
                    check("9b16 key save: invalid agent name 422s", _kb12.status_code == 422, str(_kb12.status_code))
                finally:
                    _routes9._write_env_key = _saved_wek9
                _t12 = client.get("/api/dashboard/tts").json()
                check("9b16 TTS GET carries key hints (first 4 or null), never a key",
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
                check("9b11 test route ok path", _tr.get("ok") is True
                      and _tr.get("latency_ms") == 42, str(_tr)[:120])
            finally:
                _routes9._intelligence_probe = _saved_probe9
            _routes9._intelligence_probe = lambda cfg: {"ok": False,
                                                             "error": "unreachable: URLError"}
            try:
                _tr2 = client.post("/api/dashboard/intelligence/test").json()
                check("9b11 test route error path", _tr2.get("ok") is False
                      and "URLError" in _tr2.get("error", ""), str(_tr2)[:120])
            finally:
                _routes9._intelligence_probe = _saved_probe9
        finally:
            _os.environ.pop("LVI_HERMES_KEY", None)

    # 9b12. Dead-TTS containment (2026-10-16): with the
    # ElevenLabs websocket dead, her UNspoken weather answer still armed as
    # echo text, and the user's repeat question scored 71% bag-of-words overlap
    # (5 of 7 words, only 'springfield' content — s/the/on/today counted) —
    # dropped as an "echo". Two fixes pinned here: the echo bar 0.6 -> 0.9
    # (an echo is her words coming back: nearly ALL transcript words must
    # be hers) and no phantom arming (a dead TTS makes no sound, so its
    # texts can never echo back — they register nothing).
    import asyncio as _aio12
    import echo_gate as _eg12
    import intelligence_agent as _ha12
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
                               room="suite-9b12", session_mode="fresh")
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
    check("9b12 the repeat question lands (was dropped as an echo)",
          any("Springfield" in t for t in _a12._buffer), f"buffer={_a12._buffer}")
    check("9b12 same-topic repeat is NOT her echo (bar 0.6 -> 0.9)",
          _a12._is_own_echo("What's the weather today on Springfield?") is False,
          f"echo_texts={len(_a12._echo_texts)}")
    check("9b12 short same-topic follow-up is NOT her echo",
          _a12._is_own_echo("and what about tomorrow on Springfield") is False, "")
    check("9b12 verbatim re-transcription of her reply IS caught",
          _a12._is_own_echo(_ANS12) is True, "")
    check("9b12 clipped tail of her reply IS caught",
          _a12._is_own_echo("maybe grab a jacket if you're heading out") is True, "")
    _a_dead = _ha12.IntelligenceAgent(echo_gate=_eg12.EchoGate(),
                                room="suite-9b12", session_mode="fresh",
                                tts_is_usable=lambda: False)
    _a_dead._register_echo(_ANS12)
    _a_dead._register_echo("On it.")
    check("9b12 dead-TTS push arms NO echo texts",
          _a_dead._echo_texts == [], f"echo_texts={_a_dead._echo_texts}")
    _a_live = _ha12.IntelligenceAgent(echo_gate=_eg12.EchoGate(),
                                room="suite-9b12", session_mode="fresh")
    _a_live._register_echo(_ANS12)
    _a_live._register_echo("On it.")
    check("9b12 live/unknown TTS still arms echo texts (compat)",
          _a_live._echo_texts == [_ANS12, "On it."], f"echo_texts={_a_live._echo_texts}")
    _bot_src12 = (REPO_CORE.parent / "bot.py").read_text()
    check("9b12 bot.py wires tts_is_usable into the agent",
          "tts_is_usable" in _bot_src12 and "tts_usable_probe" in _bot_src12,
          "bot.py missing the dead-TTS wire")

    # 9b13. TTS reconnect-on-next-utterance (2026-10-16 follow-on): pipecat
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
        async def _push_tts_frames(self, src_frame, *a, **k):
            self.spokes.append(src_frame)
            return "spoke"

    class _Tracked13(_TTST13, _SpeakSink13):
        pass

    _s13 = _Tracked13()
    _s13.bind_tts_room("suite-9b13")
    _aio13.run(_s13.set_usable(False))   # pipecat's permanent death verdict
    _rc13 = _aio13.run(_s13._push_tts_frames("first speak"))
    check("9b13 dead TTS retries the websocket on the next speak",
          _s13.is_usable and _s13.connects == 1 and _rc13 == "spoke"
          and _s13.spokes == ["first speak"],
          f"usable={_s13.is_usable} connects={_s13.connects} rc={_rc13}")

    _live13 = _Tracked13()
    _rc13b = _aio13.run(_live13._push_tts_frames("live speak"))
    check("9b13 a live TTS service never pays the revive",
          _live13.connects == 0 and _rc13b == "spoke",
          f"connects={_live13.connects} rc={_rc13b}")

    _dead13 = _Tracked13()
    _dead13.bind_tts_room("suite-9b13")
    _aio13.run(_dead13.set_usable(False))
    _dead13.fail_connect = True
    _rc13c = _aio13.run(_dead13._push_tts_frames("during outage"))
    check("9b13 failed revive stays dead and never breaks the speak",
          (not _dead13.is_usable) and _dead13.connects == 1 and _rc13c == "spoke",
          f"usable={_dead13.is_usable} connects={_dead13.connects} rc={_rc13c}")
    _aio13.run(_dead13._push_tts_frames("still down"))
    check("9b13 cooldown throttles retries while the outage lasts",
          _dead13.connects == 1, f"connects={_dead13.connects}")
    _dead13._revive_last_attempt = 0.0   # simulate the cooldown expiring
    _aio13.run(_dead13._push_tts_frames("after cooldown"))
    check("9b13 a later speak gets the next retry",
          _dead13.connects == 2 and not _dead13.is_usable,
          f"connects={_dead13.connects} usable={_dead13.is_usable}")

    import dashboard.registry as _reg13
    _notes13 = []
    _orig_note13 = _reg13.note_tts
    _reg13.note_tts = lambda room, provider, usable: _notes13.append((room, provider, usable))
    try:
        _chip13 = _Tracked13()
        _chip13.bind_tts_room("suite-9b13")
        _aio13.run(_chip13.set_usable(False))
        _aio13.run(_chip13._push_tts_frames("revive me"))
        check("9b13 a revived TTS clears the dashboard tts-down chip",
              _notes13[-1] == ("suite-9b13", "elevenlabs", True) and _chip13.is_usable,
              f"notes={_notes13}")
    finally:
        _reg13.note_tts = _orig_note13

    check("9b13 the real ElevenLabs service rides the revive mixin",
          issubclass(_bot13.TrackedElevenLabsTTSService, _TTST13)
          and hasattr(_bot13.ElevenLabsTTSService, "_connect")
          and hasattr(_bot13.ElevenLabsTTSService, "_disconnect")
          and hasattr(_bot13.ElevenLabsTTSService, "_push_tts_frames"),
          "Tracked* must sit on TTSTracked over a websocket TTS service")

    # 9b14. Node-command error mapping: an in-flight duplicate is a
    # 409 CONFLICT (it used to read as a naked "Gateway Timeout" 504), and a
    # bare transport timeout (empty detail — the observed restart error shape)
    # now says half-open + self-heals instead of an empty string the UI
    # rendered as "Gateway Timeout".
    import asyncio as _aio14
    import dashboard.node_command as _nc14
    from dashboard.node_command import (CommandInFlight as _InFlight14,
                                        NoLiveSession as _NoLive14)

    registry.note_offer("pc-9b14", "suite-9b14")
    registry.mark_connected("pc-9b14")
    _real_request14 = _nc14.request

    def _raising_request14(exc):
        async def _r(room, cmd, args=None, timeout=12.0):
            raise exc
        return _r

    try:
        _nc14.request = _raising_request14(
            _InFlight14("'restart' already in flight for 'suite-9b14'"))
        _r14a = client.post("/api/dashboard/nodes/suite-9b14/restart")
        check("9b14: in-flight duplicate -> 409 Conflict (was a 504)",
              _r14a.status_code == 409 and "already in flight" in _r14a.text,
              f"status={_r14a.status_code} body={_r14a.text[:80]}")
        _nc14.request = _raising_request14(TimeoutError())
        _r14b = client.post("/api/dashboard/nodes/suite-9b14/restart")
        check("9b14: bare timeout -> 504 with half-open + self-heal wording",
              _r14b.status_code == 504 and "half-open" in _r14b.text
              and "10 min" in _r14b.text,
              f"status={_r14b.status_code} body={_r14b.text[:100]}")
        _nc14.request = _raising_request14(
            _NoLive14("no live session for room 'suite-9b14'"))
        _r14c = client.post("/api/dashboard/nodes/suite-9b14/restart")
        check("9b14: half-open session -> 504 naming the half-open shape",
              _r14c.status_code == 504 and "half-open" in _r14c.text,
              f"status={_r14c.status_code} body={_r14c.text[:100]}")
    finally:
        _nc14.request = _real_request14
        registry.mark_disconnected("pc-9b14")

    # node_command side: a pending duplicate raises CommandInFlight (the
    # route's 409 depends on the class, not on message sniffing).
    _saved_sender14 = _nc14.registry.sender_for_room

    async def _inflight_probe14():
        _nc14.registry.sender_for_room = lambda room: (object(), "pc-9b14")
        fut = _aio14.get_event_loop().create_future()
        _nc14._pending[("pc-9b14", "restart")] = fut
        try:
            await _nc14.request("suite-9b14", "restart")
            return "no-raise"
        except _InFlight14:
            return "inflight"
        except TimeoutError as e:
            return f"timeout:{str(e)[:40]}"
        finally:
            _nc14._pending.pop(("pc-9b14", "restart"), None)
            _nc14.registry.sender_for_room = _saved_sender14

    _res14 = _aio14.run(_inflight_probe14())
    check("9b14: node_command raises CommandInFlight for a pending duplicate",
          _res14 == "inflight", f"result={_res14}")

    # 9b15. Wake-model select keys on name + SOURCE: with the
    # same basename in root and models/, a name-only select always
    # resolved to the models/ copy — clicking the ROOT row silently
    # activated the other file. source=None keeps the legacy resolution.
    _l4_name = "suite-l4c.onnx"
    _l4_bytes = b"l4-placeholder-onnx-bytes"
    _l4_models = Path("models"); _l4_models.mkdir(parents=True, exist_ok=True)
    (_l4_models / _l4_name).write_bytes(_l4_bytes)
    _l4_root = Path(_l4_name); _l4_root.write_bytes(_l4_bytes)
    try:
        r = client.put("/api/dashboard/oww/select",
                       json={"model": _l4_name, "source": "root"})
        check("9b15: source=root activates the ROOT copy (collision)",
              r.status_code == 200 and r.json().get("model") == _l4_name,
              f"status={r.status_code} body={(r.json().get('model') if r.status_code == 200 else r.text[:80])!r}")
        r = client.put("/api/dashboard/oww/select",
                       json={"model": _l4_name, "source": "models"})
        check("9b15: source=models activates the MODELS copy (collision)",
              r.status_code == 200 and str(_l4_models / _l4_name) in str(r.json().get("model")),
              f"status={r.status_code} body={(r.json().get('model') if r.status_code == 200 else r.text[:80])!r}")
        r = client.put("/api/dashboard/oww/select", json={"model": _l4_name})
        check("9b15: no source keeps the legacy models-first resolution",
              r.status_code == 200 and str(_l4_models / _l4_name) in str(r.json().get("model")),
              f"status={r.status_code} body={r.text[:80]}")
        r = client.put("/api/dashboard/oww/select",
                       json={"model": "suite-l4-missing.onnx", "source": "root"})
        check("9b15: unknown name+source combo 404s naming the source",
              r.status_code == 404 and "root" in r.text,
              f"status={r.status_code} body={r.text[:80]}")
        r = client.put("/api/dashboard/oww/select",
                       json={"model": _l4_name, "source": "bogus"})
        check("9b15: source is a Literal (bogus source 422s)",
              r.status_code == 422, f"status={r.status_code}")
    finally:
        _l4_root.unlink(missing_ok=True)
        (_l4_models / _l4_name).unlink(missing_ok=True)

    # Session leak (2026-10-15) — dead sessions' pipeline graphs are one
    # big reference cycle; Python 3.14.4's incremental GC never reclaimed
    # them (10.4 GB / 406 threads after ~110 sessions). run_bot reclaims
    # deterministically when the pipeline ends, and the local STT closes the
    # unused OpenAI client itself instead of leaving it to a GC finalizer.
    import ast as _ast043
    _bot_src = (REPO_CORE.parent / "bot.py").read_text()
    _bot_ast = _ast043.parse(_bot_src)
    _run_bot = next((n for n in _ast043.walk(_bot_ast)
                     if isinstance(n, _ast043.AsyncFunctionDef) and n.name == "run_bot"), None)
    _reclaim_in_finally = False
    if _run_bot is not None:
        for _n in _ast043.walk(_run_bot):
            if isinstance(_n, _ast043.Try) and _n.finalbody:
                _body_src = _ast043.unparse(_n)
                if "runner.run(task)" in _body_src and any(
                        "_reclaim_session()" in _ast043.unparse(f) for f in _n.finalbody):
                    _reclaim_in_finally = True
    check("043a run_bot reclaims the session in a finally around runner.run",
          _reclaim_in_finally)
    _reclaim_fn = next((n for n in _ast043.walk(_bot_ast)
                        if isinstance(n, _ast043.FunctionDef) and n.name == "_reclaim_session"), None)
    check("043b _reclaim_session runs a full gc.collect()",
          _reclaim_fn is not None and "gc.collect()" in _ast043.unparse(_reclaim_fn))

    import asyncio as _aio043
    import time as _t043
    from stt_local import LocalWhisperSTTService as _LWS
    from pipecat.services.whisper.base_stt import BaseWhisperSTTService as _BWS

    class _FakeClient:
        def __init__(self, hang=False):
            self.closed = False
            self.hang = hang
        async def close(self):
            if self.hang:
                await _aio043.sleep(10)
            self.closed = True

    async def _noop_cleanup(self):
        return None

    _saved_cleanup = _BWS.cleanup
    _BWS.cleanup = _noop_cleanup  # isolate OUR cleanup from pipecat's task plumbing
    try:
        _svc = _LWS.__new__(_LWS)
        _fc = _FakeClient()
        _svc._client = _fc
        _aio043.run(_LWS.cleanup(_svc))
        check("043c local STT cleanup closes the unused OpenAI client",
              _fc.closed and _svc._client is None)
        _svc2 = _LWS.__new__(_LWS)
        _svc2._client = _FakeClient(hang=True)
        _t0 = _t043.monotonic()
        _aio043.run(_LWS.cleanup(_svc2))
        _dt = _t043.monotonic() - _t0
        check("043c hung client close is bounded (teardown never stalls)",
              _dt < 3.5 and _svc2._client is None, f"{_dt:.2f}s")
    finally:
        _BWS.cleanup = _saved_cleanup

    # RSS-step fix (2026-10-15): Whisper loads at bot STARTUP (fresh heap +
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
    check("RSS preload uses the SAME stt model/device/compute_type config as run_bot",
          _pre_call is not None and len(_pre_tr) == 3 and _pre_tr == _stt_tr,
          f"preload={_pre_tr} run_bot={_stt_tr}")
    _main_src = _astR.unparse(_botR)
    check("RSS preload runs before main() (startup, not first voice turn)",
          _pre_call is not None and _main_src.find("preload_shared_model(") < _main_src.rfind("main()"),
          "preload call missing or after main()")
    # 050 Pocket lane + fallback wiring. _main_src is ast.unparse (code only,
    # comments stripped, single-quoted literals), so a comment can't fake a
    # pass. Behavior is pinned in tests/pocket_test.py.
    check("050 bot.py: pocket lane branch exists",
          "tts_provider == 'pocket'" in _main_src)
    check("050 bot.py: pocket primary fails loud with the install hint",
          _main_src.count("INSTALL_HINT") >= 2, f"count={_main_src.count('INSTALL_HINT')}")
    check("050 bot.py: fallback wrap keyed on tts.fallback",
          "wrap_with_fallback(" in _main_src and "cfg('tts', 'fallback')" in _main_src)
    check("050 bot.py: pocket+pocket fallback ignored with a warning",
          "fallback: pocket ignored" in _main_src)
    _pk_pre = _main_src.find("tts_pocket.preload(")
    check("050 bot.py: pocket preload at startup, before main(), non-fatal",
          _pk_pre != -1 and _pk_pre < _main_src.rfind("main()")
          and "pocket preload failed" in _main_src)
    check("050 bot.py: fallback closed at session end AND on client disconnect",
          _main_src.count("close_fallback(") >= 2, f"count={_main_src.count('close_fallback(')}")
    check("050 bot.py: TTSTracked skips health notes while the fallback speaks",
          "_tts_note_suppressed" in _main_src)
    check("050 bot.py: repeated per-session Pocket warnings go through warn_once",
          _main_src.count("warn_once(") >= 2, f"count={_main_src.count('warn_once(')}")
    check("050 bot.py: the Pipeline still takes `tts` (switcher reassigns it)",
          "tts_primary = tts" in _main_src)
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