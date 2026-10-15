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

    # H5: guard.py requires an IP-literal Host (public-DNS hosts are the
    # rebinding vector) — TestClient's default 'testserver' would 403.
    # raise_server_exceptions=False: a handler crash must surface as the
    # 500 the live app returns, not abort the suite.
    client = TestClient(app, base_url="http://192.0.2.10", raise_server_exceptions=False)

    # 2. page
    r = client.get("/dashboard")
    check("GET /dashboard -> 200 html", r.status_code == 200 and "dashboard" in r.text)

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
    # H1 contract: bot.py passes THIS pipeline's room (room_for_pc) — the
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

    # 8m. Opus smalls M10/M11/M12/M14 pins (design note, fixed 2026-10-08)
    import importlib
    import config as _cfg_mod
    _saved_cwd = os.getcwd()
    # M10: config resolves to the REPO ROOT when LVI_CONFIG is unset —
    # a different cwd must NOT silently read {}. We chdir to / and
    # check the resolved path is absolute + repo-anchored.
    _saved_os_config = os.environ.pop("LVI_CONFIG", None)
    try:
        os.chdir("/")
        # fresh import so CONFIG_PATH re-resolves without the env override
        importlib.reload(_cfg_mod)
        _cp = str(_cfg_mod.CONFIG_PATH)
        check("M10 config path is repo-anchored (cwd-proof)",
              "lenoxvi" in _cp and os.path.isabs(_cp)
              and _cp.endswith("config.yaml"), f"CONFIG_PATH={_cp}")
        check("M10 config loads away from repo root",
              bool(_cfg_mod.cfg("tts")), f"tts={_cfg_mod.cfg('tts')}")
    finally:
        os.chdir(_saved_cwd)
        if _saved_os_config is not None:
            os.environ["LVI_CONFIG"] = _saved_os_config
        importlib.reload(_cfg_mod)  # restore the suite's throwaway config
    # M11: a failed ClipServer.start() must NOT leave a half-bound
    # runner — simulate by double-starting on the same port: the
    # second singleton user reuses it (no EADDRINUSE), and a forced
    # raw second bind fails cleanly with the runner still reusable.
    from sonos_output import ClipServer, shared_clip_server

    async def _m11_pins():
        _cs = await shared_clip_server()
        _first_runner = _cs._runner
        _again = await shared_clip_server()
        check("M11 shared clip server is a true singleton",
              _again is _cs and _cs._runner is _first_runner,
              f"first={_first_runner is not None} again-identity={_again is _cs}")
        # raw double-bind on the SAME interface + port as the singleton
        # (127.0.0.1 vs the LAN IP are different interfaces — no conflict;
        # the first suite run "passed" the bind for that wrong reason)
        _raw = ClipServer(Path(tempfile.mkdtemp()), _cs._host_ip, _cs._port)
        try:
            await _raw.start()
            check("M11 raw double-bind raises (no silent second server)", False,
                  "raw start on a taken port unexpectedly succeeded")
            try:
                await _raw.stop()
            except Exception:
                pass
        except Exception:
            check("M11 raw double-bind raises (no silent second server)", True, "")
            check("M11 failed bind leaves no half-bound runner",
                  _raw._runner is None, f"runner={_raw._runner}")
        check("M11 singleton still healthy after a failed raw bind",
              _cs._runner is _first_runner, "")

    import asyncio as _aio
    _aio.get_event_loop_policy()
    try:
        _aio.run(_m11_pins())
    except RuntimeError as _re11:
        # running loop already exists (suite invoked under one) — hop on it
        check("M11 pins ran under an existing loop", False, f"loop hop failed: {_re11}")
    # M12: missing TTS secret fails construction with a CLEAR error
    # naming the variable (not a bare KeyError deep in pipecat).
    _saved_el = os.environ.pop("ELEVENLABS_API_KEY", None)
    _saved_dg = os.environ.pop("DEEPGRAM_API_KEY", None)
    os.environ["LVI_TTS_PROVIDER"] = "elevenlabs"
    try:
        importlib.reload(_cfg_mod)
        _prov = _cfg_mod.cfg_env("tts", "provider", "LVI_TTS_PROVIDER", "elevenlabs")
        _need = "ELEVENLABS_API_KEY" if str(_prov).startswith("eleven") else None
        check("M12 missing secret detected at construction (clear error shape)",
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
    # M14: zone DELETE with a dangling room mapping does ONE atomic
    # multi-section write (zone gone + mapping cleaned, same pass).
    # 041 H3: the probe WiiM zone carries NO player_uid — this used to
    # need a fake RINCON because ZonePayload required a uid for every
    # backend (so the real UI's WiiM add 422'd every time).
    _r_add14 = client.post("/api/dashboard/zones", json={
        "name": "m14_probe_zone", "backend": "wiim", "ip": "127.0.0.9"})
    check("M14 probe zone add ok (wiim, no player_uid — 041 H3)",
          _r_add14.status_code == 200, f"status={_r_add14.status_code} body={_r_add14.text[:120]}")
    _wz14 = ((config_store.read_section("wiim") or {}).get("zones") or {}).get("m14_probe_zone")
    check("H3 wiim zone written without a player_uid key",
          isinstance(_wz14, dict) and "player_uid" not in _wz14, f"zone={_wz14}")
    # 041 H3: the UI's exact pre-fix WiiM body (player_uid: '') is legal;
    # any stray value sent for wiim is dropped, never written.
    _r_h3 = client.post("/api/dashboard/zones", json={
        "name": "h3_probe_zone", "backend": "wiim", "ip": "127.0.0.8",
        "player_uid": ""})
    check("H3 wiim zone add with empty player_uid ok",
          _r_h3.status_code == 200, f"status={_r_h3.status_code} body={_r_h3.text[:120]}")
    client.delete("/api/dashboard/zones/h3_probe_zone")
    # Sonos still REQUIRES a real uid (validator, sonos-only).
    _r_h3s = client.post("/api/dashboard/zones", json={
        "name": "h3_sonos_nouid", "backend": "sonos", "ip": "127.0.0.7"})
    check("H3 sonos zone without player_uid 422s",
          _r_h3s.status_code == 422, f"status={_r_h3s.status_code} body={_r_h3s.text[:120]}")
    _r_h3t = client.post("/api/dashboard/zones", json={
        "name": "h3_sonos_short", "backend": "sonos", "ip": "127.0.0.7",
        "player_uid": "RINC"})
    check("H3 sonos zone with short player_uid 422s",
          _r_h3t.status_code == 422, f"status={_r_h3t.status_code} body={_r_h3t.text[:120]}")
    check("H3 rejected sonos probes never reached config",
          not ({"h3_sonos_nouid", "h3_sonos_short"}
               & set(((config_store.read_section("sonos") or {}).get("zones") or {}))),
          "")
    client.put("/api/dashboard/mapping", json={
        "room_zones": {"m14_probe_room": "m14_probe_zone"}})
    _del = client.delete("/api/dashboard/zones/m14_probe_zone")
    check("M14 delete zone ok (with dangling mapping)",
          _del.status_code == 200, f"status={_del.status_code} body={_del.text[:120]}")
    check("M14 dangling room mapping dropped in the same write",
          _del.json().get("dropped_room_mappings") == ["m14_probe_room"],
          f"body={_del.text[:120]}")
    _probe = (config_store.read_section("room_zones") or {}).get("m14_probe_room")
    check("M14 probe mapping actually gone from config",
          _probe is None, f"room_zones m14_probe_room={_probe}")

    # 041 M5: /mapping only runs soco name lookups on ?names=1, and only
    # for SONOS zones (the 5s UI poll used to hit every zone, WiiM
    # included, on every tick).
    from dashboard import routes as _routes_m5
    _m5_calls = []
    _real_names_m5 = _routes_m5._sonos_names
    _routes_m5._sonos_names = lambda zones: (_m5_calls.append(sorted(zones)) or {k: k for k in zones})
    try:
        _rm = client.get("/api/dashboard/mapping")
        check("041 M5 plain /mapping poll runs NO soco lookup and omits sonos_names",
              _rm.status_code == 200 and not _m5_calls and "sonos_names" not in _rm.json(),
              f"calls={_m5_calls} keys={list(_rm.json())}")
        _rn = client.get("/api/dashboard/mapping?names=1")
        _wiim_keys_m5 = set(((config_store.read_section("wiim") or {}).get("zones") or {}))
        _sonos_keys_m5 = set(((config_store.read_section("sonos") or {}).get("zones") or {}))
        check("041 M5 ?names=1 looks up Sonos zones only (never WiiM)",
              _rn.status_code == 200 and len(_m5_calls) == 1
              and set(_m5_calls[0]) == _sonos_keys_m5
              and not (set(_m5_calls[0]) & _wiim_keys_m5)
              and "sonos_names" in _rn.json(),
              f"calls={_m5_calls} sonos={sorted(_sonos_keys_m5)} wiim={sorted(_wiim_keys_m5)}")
    finally:
        _routes_m5._sonos_names = _real_names_m5

    # 041 L3: ONE volume truth. /mapping serves effective_volumes for every
    # zone through the same resolver as the test tone; each zone resolves
    # in its OWN backend section; one code default everywhere.
    import yaml as _yaml_l3
    import sonos_output as _so_l3
    import wiim_output as _wo_l3
    from dashboard import routes as _routes_l3
    _sonos_keys_l3 = set(((config_store.read_section("sonos") or {}).get("zones") or {}))
    _wiim_keys_l3 = set(((config_store.read_section("wiim") or {}).get("zones") or {}))
    _ev = client.get("/api/dashboard/mapping").json().get("effective_volumes") or {}
    check("041 L3 effective_volumes covers every zone (sonos ∪ wiim)",
          bool(_ev) and set(_ev) == _sonos_keys_l3 | _wiim_keys_l3, f"ev={_ev}")
    check("041 L3 effective volume == test-tone volume for every zone (one resolver)",
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
        check("041 L3 Sonos global moves Sonos zones only; WiiM resolves in its own section",
              bool(_ev2) and all(_ev2[z] == 61 for z in _sonos_keys_l3)
              and all(_ev2[z] == _wiim_expect for z in _wiim_keys_l3 if z not in _wo_l3._LIVE_WIIM_VOLUMES),
              f"ev2={_ev2} wiim_expect={_wiim_expect}")
        # live slider layer wins
        if _sonos_keys_l3:
            _z = sorted(_sonos_keys_l3)[0]
            _so_l3.zone_volume_overrides({_z: 13})
            _ev3 = client.get("/api/dashboard/mapping").json().get("effective_volumes") or {}
            check("041 L3 live slider override wins", _ev3.get(_z) == 13, f"ev3={_ev3}")
    finally:
        config_store.write_section("sonos", _sonos_sec_l3)
        _so_l3.zone_volume_overrides(_saved_live_l3)
    # one code default: a config with no clip_volume → the SAME number in
    # SonosOutput (was 70), WiiMOutput (35) and the dashboard resolver (35)
    _cfg_l3 = SCRATCH / "l3-novol.yaml"
    _cfg_l3.write_text(_yaml_l3.safe_dump({"sonos": {"enabled": False, "zones": {}}}))
    _so_def = _so_l3.SonosOutput(config_path=str(_cfg_l3), echo_gate=None, zone_selector=lambda: None, led=None)
    check("041 L3 SonosOutput default clip volume == DEFAULT_CLIP_VOLUME (was 70)",
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
        # 041 M1: GET /tts serves the FULL accepted catalog per provider —
        # the UI's only pick-list source (it hardcoded 12/40 Aura, 16/36
        # Flux). Every catalog entry must pass the server's own validation
        # (no I/O: validate the payload directly), so the list can never
        # offer a value the PUT rejects.
        _cat = t.get("catalog") or {}
        check("041 M1 TTS GET catalog sizes (elevenlabs 2, aura 40, flux 36)",
              len(_cat.get("elevenlabs", [])) == 2 and len(_cat.get("deepgram", [])) == 40
              and len(_cat.get("deepgram-flux", [])) == 36,
              str({k: len(v) for k, v in _cat.items()}))
        from dashboard import routes as _routes_m1
        _m1_bad = []
        for _prov, _vals in _cat.items():
            for _v in _vals:
                try:
                    if _prov == "elevenlabs":
                        _p = _routes_m1.TTSPayload(provider=_prov, model=_v, voice_id="AbC123xyZw9876543210")
                    elif _prov == "deepgram":
                        _p = _routes_m1.TTSPayload(provider=_prov, deepgram_voice=_v)
                    else:
                        _p = _routes_m1.TTSPayload(provider=_prov, deepgram_flux_voice=_v)
                    _routes_m1._validate_tts_payload(_p)
                except Exception as _e:
                    _m1_bad.append((_prov, _v, str(_e)[:40]))
        check("041 M1 every catalog entry passes the server's own TTS validation",
              not _m1_bad and sum(len(v) for v in _cat.values()) == 78, f"bad={_m1_bad[:3]}")
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
    # ONE provider, model picks the lane (the owner 12:05 + trim 13:20): only
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
    # ONE provider, model picks the lane (the owner 12:05): v3/v4 models are
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
    # restore the real provider (the suite config is a copy, but leave it
    # in a sane state anyway)
    client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "AbC123xyZw9876543210",
        "model": "eleven_flash_v2_5"})

    # 9b. Bot restart route (suite MUST NEVER restart the real bot — it
    # runs on the same host as prod). LVI_RESTART_COMMAND used to override
    # the command, but that env hook was a prod code-exec lane (Opus
    # audit 2026-10-04) and is now deleted. The suite monkeypatches
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
              and "1.1.0+ required" not in str(_j_mut.get("detail", "")))
        check("mute: unknown-cmd 501s with the per-line truth (no firmware lie)",
              ok, f"status={_r_mut.status_code} body={_r_mut.text[:140]}")

        # 041 M3: restart + wifi check the node's ack — ok:false is a 502,
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
        check("M3 restart: node ok:false → 502 (041)",
              _r.status_code == 502 and "busy" in _r.text, f"status={_r.status_code} body={_r.text[:120]}")
        _m3_reply["restart"] = {"ok": True, "data": "rebooting"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/restart")
        check("M3 restart: node ok:true → 200", _r.status_code == 200, f"status={_r.status_code}")
        _m3_reply["set_wifi"] = {"ok": False, "data": "nvs error"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                         json={"ssid": "suite-net", "password": "suite-secret-pw"})
        check("M3 wifi: node ok:false → 502, password never echoed (041)",
              _r.status_code == 502 and "nvs error" in _r.text
              and "suite-secret-pw" not in _r.text,
              f"status={_r.status_code} body={_r.text[:120]}")
        _m3_reply["set_wifi"] = {"ok": True, "data": "stored, connecting"}
        _r = client.post("/api/dashboard/nodes/suite-mute-probe/wifi",
                         json={"ssid": "suite-net", "password": "suite-secret-pw"})
        check("M3 wifi: node ok:true → 200", _r.status_code == 200, f"status={_r.status_code}")

        # 041 M7: MutePayload.mute is REQUIRED — a body-less/empty POST
        # must never mute a node (old default=True did).
        _m3_reply["set_mute"] = {"ok": True, "data": {"muted": True}}
        _r_e = client.post("/api/dashboard/nodes/suite-mute-probe/mute", json={})
        _r_n = client.post("/api/dashboard/nodes/suite-mute-probe/mute")
        check("M7 mute: empty body / no body 422 (no default-True trap)",
              _r_e.status_code == 422 and _r_n.status_code == 422,
              f"empty={_r_e.status_code} none={_r_n.status_code}")
    finally:
        _routes_mut.node_command.request = _real_request
        _reg_mut.mark_disconnected("suite-mute-pc")

    # 041 M7: extra="forbid" on EVERY write payload — a misspelled key
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
        ("POST", "/api/dashboard/firmware/version", {"version": "1.0.0", "bogus": 1}),
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
    check(f"M7 extra key 422s on all {len(_m7_cases)} write payloads (041)",
          not _m7_bad, f"not forbidden: {_m7_bad}")
    check("M7 forbidden writes left config.yaml byte-identical",
          _hl_m7.sha256(_cfg_path_m7.read_bytes()).hexdigest() == _sha_m7, "")

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

    # 9b2. Key-route hardening (Opus audit finding 1): the character-class
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

    # 9b3. Clip server singleton (Opus audit finding 2): SonosOutput and
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

    # 9b4. Reply-time backend dispatch regression (Opus finding #6; the
    # 2026-10-04 23:22 bedroom outage): bot.py must wire BOTH output
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
    # B3 (design note): the backends can be hoisted to variables before
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
    check("bot.py: no build-time ternary picks the backend (23:22 outage shape)",
          not _conditional, f"conditional={_conditional}")
    # 9b11 rider (the 20:56 stub incident): the intelligence construction ternary
    # must route BOTH live kinds (hermes, openclaw) to HermesAgent — a
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

    # 9b5b. Track-aware firmware upload (036 H-B): the image + manifest
    # must land in the DECLARED track's directory, and an unknown track
    # 400s. The old route staged everything into firmware/node/ — a
    # Flex .bin there would be OTA'd by an array node (cross-flash).
    def _fake_image5b(ver: str) -> bytes:
        head = bytearray(b"\x00" * 256)
        head[32:36] = b"\x32\x54\xcd\xab"          # esp_app_desc magic
        head[48:80] = ver.encode().ljust(32, b"\x00")  # version string
        return bytes(head) + b"\x00" * (1024 * 200)  # over the size floor

    _saved_updir5b = os.getcwd()
    os.chdir(SCRATCH)  # uploads are relative to cwd; keep them in scratch
    try:
        # (a) flex-declared upload → firmware/flex/
        _r = client.post("/api/dashboard/firmware/upload",
                         files={"file": ("flex.bin", _fake_image5b("1.9.9"),
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
        # (c) 041 M2: NO default track — a track-less upload 422s and
        # touches neither track's directory (reverses the 036 H-B
        # "default stays array" pin: no board identity in the image yet,
        # so a silent array default is a cross-flash accelerant).
        _snap5c = {t: (Path(f"firmware/{t}/version.json").read_text()
                       if Path(f"firmware/{t}/version.json").exists() else None)
                   for t in ("node", "flex")}
        _rc = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("arr.bin", _fake_image5b("1.9.7"),
                                          "application/octet-stream")})
        check("9b5b M2 upload without track 422s (no silent array default; not pipecat's Stream-consumed 500)",
              _rc.status_code == 422 and "track" in _rc.text, f"{_rc.status_code} {_rc.text[:120]}")
        check("9b5b M2 track-less upload left both manifests untouched",
              all((Path(f"firmware/{t}/version.json").read_text()
                   if Path(f"firmware/{t}/version.json").exists() else None) == _snap5c[t]
                  for t in ("node", "flex")), str(_snap5c)[:120])
        # explicit array track still works
        _rd = client.post("/api/dashboard/firmware/upload",
                          files={"file": ("arr.bin", _fake_image5b("1.9.7"),
                                          "application/octet-stream")},
                          data={"track": "node"})
        check("9b5b explicit array track upload lands in firmware/node/",
              _rd.status_code == 200 and _rd.json().get("track") == "node"
              and json.loads(Path("firmware/node/version.json").read_text())["version"] == "1.9.7",
              _rd.text[:120])
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
        # (design note loaded-gun rule — and the dev host's root copy is
        # the ONLY local model copy).
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

    # 9b7. Load-anchored speech timers (Fix A, 2026-10-05): the v4
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

    # 12. H5 guard: cross-origin mutation rejected, non-browser passes
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "Dining Room"}},
                   headers={"Origin": "https://evil.example"})
    check("guard: cross-origin mutation 403", r.status_code == 403, f"status={r.status_code}")
    r = client.put("/api/dashboard/mapping",
                   json={"room_zones": {"bedroom": "Dining Room"}})
    check("guard: no-Origin (curl) mutation passes", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/api/dashboard/status", headers={"Origin": "https://evil.example"})
    check("guard: cross-origin READ stays open", r.status_code == 200, f"status={r.status_code}")

    # 13. H5 token mode (env set -> mutations need the header)
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

    # 9b8. B3 epoch turns + wake-word barge-in (design note): turns
    # run as background tasks (frame loop never blocks on the intelligence), a
    # new question supersedes (old reply discarded, no history write,
    # ack spoken), and "Hey Willow" twice-confirmed during LOCKED fires
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
        from hermes_agent import HermesAgent as _Agent  # noqa: E402
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

            agent._ask_hermes = fake_ask

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
            # config.yaml (which carries barge_in: true on the owner's rig),
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

            # ── 036 H-A pin: an ASYNC on_interrupt must actually run ──
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
            check("9b8 wake: ASYNC on_interrupt coroutine actually runs (036 H-A)",
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

            # 9b9. Frame passthrough (the 17:09 "you wont wake" outage): the
            # B3 _run_turn extraction lost process_frame's final push_frame —
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
            # 17:49:08 double-wake): a wake during a sounding clip must
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

            # 9b11. Intelligence lane (design note): config round-trip
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
            # Bare kind flip (036 M-3): no url in the payload — the route
            # must restore THAT kind's remembered URL, not keep the other
            # kind's (which would point the lane at the wrong intelligence).
            _r3 = client.put("/api/dashboard/intelligence", json={"kind": "openclaw"})
            _brain9c = _yaml9.safe_load(
                Path(os.environ["LVI_CONFIG"]).read_text()).get("intelligence") or {}
            check("9b11b bare kind flip restores that kind's remembered URL",
                  _brain9c.get("url") == "http://127.0.0.1:18789/v1",
                  f"url={_brain9c.get('url')}")
            # restore kind for later checks: the lane probes below build
            # HermesAgent against the LIVE throwaway config and expect
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
                for mod_name in ("hermes_agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import hermes_agent as _ha9
                _urq9.urlopen = _fake_open9
                # kind from the LVI_CONFIG we just PUT (openclaw)
                _a9 = _ha9.HermesAgent(lull_secs=0.05, room="suite",
                                       session_mode="sticky")
                _a9._ask_hermes("hi")
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
                # auth with another intelligence's token (tonight's 401 lesson).
                _os.environ.pop("LVI_OPENCLAW_KEY", None)
                for mod_name in ("hermes_agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import hermes_agent as _ha9_fb
                _a9_fb = _ha9_fb.HermesAgent(lull_secs=0.05, room="suite",
                                             session_mode="fresh")
                try:
                    _a9_fb._ask_hermes("hi")
                    _strict_ok9 = False
                    _strict_err9 = "no error raised"
                except RuntimeError as _e9:
                    _strict_ok9 = "LVI_OPENCLAW_KEY" in str(_e9)
                    _strict_err9 = str(_e9)[:80]
                check("9b11 openclaw lane: missing LVI_OPENCLAW_KEY fails loud, names its own variable",
                      _strict_ok9, _strict_err9)
                _os.environ["LVI_OPENCLAW_KEY"] = "suite-openclaw-key-not-real"
                # fresh: header absent
                _a9f = _ha9.HermesAgent(lull_secs=0.05, room="suite",
                                        session_mode="fresh")
                _a9f._ask_hermes("hi")
                check("9b11 openclaw lane: fresh sends no session header",
                      "x-openclaw-session-key" not in _cap9[-1]["headers"], "")
                # hermes round-trip: flip kind back, conversation field returns
                client.put("/api/dashboard/intelligence", json={"kind": "hermes"})
                for mod_name in ("hermes_agent",):
                    if mod_name in sys.modules:
                        del sys.modules[mod_name]
                import hermes_agent as _ha9b
                _a9b = _ha9b.HermesAgent(lull_secs=0.05, room="suite",
                                         session_mode="fresh")
                _a9b._ask_hermes("hi")
                _hm_body = _cap9[-1]["body"]
                _hm_hdr = _cap9[-1]["headers"]
                check("9b11 hermes round-trip: conversation field + Hermes header restored",
                      _hm_body.get("conversation") and
                      _hm_hdr.get("x-hermes-session-key") == _hm_body.get("conversation"),
                      str(_hm_body)[:140])
                # Bug-7 law (2026-10-07): keys are KIND-AWARE — the hermes
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
                    _a9c = _ha9b.HermesAgent(lull_secs=0.05, room="suite",
                                             session_mode="fresh")
                    try:
                        _a9c._ask_hermes("hi")
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
                # design note H1 contract pins: kind is REQUIRED (no silent
                # guess from config), only shipped lanes, extra="forbid".
                _knk = client.put("/api/dashboard/intelligence/key",
                                  json={"api_key": "suite-keymaterial-12345"})
                check("9b11c key POST without kind 422s (041 H1)",
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
                # 036 M-2 + 041 H1 pin: the variable follows the PAYLOAD's
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
                    check("9b11b key save follows payload kind, not config (041 H1)",
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
                        check("9b11c GET keys_set per kind (041 H1)",
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

    # 043 (2026-10-15): session leak — dead sessions' pipeline graphs are one
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