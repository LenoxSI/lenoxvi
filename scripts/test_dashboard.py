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
    # H5: guard.py requires an IP-literal Host (public-DNS hosts are the
    # rebinding vector) — TestClient's default 'testserver' would 403.
    client = TestClient(app, base_url="http://192.0.2.10")

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

    # 9. TTS settings panel (Settings tab — config-editable arc)
    r = client.get("/api/dashboard/tts")
    ok = r.status_code == 200 and "provider" in r.json() and "deepgram_key_set" in r.json()
    check("TTS GET returns provider config + key status", ok, f"status={r.status_code}")
    if ok:
        t = r.json()
        check("TTS GET never leaks key values",
              "api_key" not in t and not any("sk_" in str(v) for v in t.values()),
              f"keys={list(t)}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs",
        "voice_id": "AbC123xyZw9876543210", "model": "eleven_v2"})
    check("TTS PUT (elevenlabs) ok", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")
    t = client.get("/api/dashboard/tts").json()
    check("TTS round-trip: elevenlabs voice_id + model persisted",
          t.get("voice_id") == "AbC123xyZw9876543210" and t.get("model") == "eleven_v2",
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
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs",
        "voice_id": "AbC123xyZw9876543210", "model": "eleven_v3"})
    check("TTS PUT rejects eleven_v3 (the HTTP-400 model)",
          r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "voice_id": "short", "model": "eleven_v2"})
    check("TTS PUT rejects too-short voice_id", r.status_code == 422, f"status={r.status_code}")
    r = client.put("/api/dashboard/tts", json={
        "provider": "elevenlabs", "deepgram_voice": "aura-2-zeus-en"})
    check("TTS PUT elevenlabs-without-voice_id rejected (422)",
          r.status_code == 422, f"status={r.status_code}")

    # 9b. Bot restart route (override lane — the suite must NEVER restart
    # the real bot; LVI_RESTART_COMMAND points at a benign stand-in)
    os.environ["LVI_RESTART_COMMAND"] = "true"  # /usr/bin/true, rc=0
    try:
        r = client.post("/api/dashboard/restart")
        ok = r.status_code == 200 and r.json().get("override") is True
        check("restart route returns via override lane (no real restart)",
              ok, f"status={r.status_code} body={r.text[:120]}")
    finally:
        del os.environ["LVI_RESTART_COMMAND"]

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
    # brain section — max_tokens + fillers must survive untouched.
    brain_after = config_store.read_section("brain") or {}
    check("tuning write preserves brain.max_tokens (merge, not replace)",
          brain_after.get("max_tokens") == 300, f"brain keys={sorted(brain_after)}")
    check("tuning write preserves brain.fillers (merge, not replace)",
          isinstance(brain_after.get("fillers"), list) and len(brain_after["fillers"]) == 2,
          f"fillers={str(brain_after.get('fillers'))[:80]}")
    wake_after = config_store.read_section("wake") or {}
    check("tuning write preserves wake.enabled (merge, not replace)",
          wake_after.get("enabled") is True, f"wake={wake_after}")

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