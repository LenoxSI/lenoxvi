"""Intelligence lane registry: one list of kinds, and a new lane needs only
its own module + one registry line.

Registers a fake 4th lane ("echo") BEFORE anything else imports the registry,
then proves every consumer picks it up with no other edit: the facade, the
dashboard routes (key save, discovery, config save, GET /intelligence, the
lane list), the key catalog and the voice agent. Also pins the real lanes'
contract and their UI metadata against the dashboard's built-in fallback.

Usage (host venv): python intelligence_registry_test.py <repo>/core
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_CORE = Path(sys.argv[1]).resolve()
SCRATCH = Path(tempfile.mkdtemp(prefix="lvi-lanes-registry-"))
FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=None):
        return self._body


CALLS = []


def _fake_urlopen(req, timeout=None):
    CALLS.append((req.full_url, {k.lower(): v for k, v in req.header_items()},
                  req.data.decode() if req.data else None))
    if req.full_url.endswith("/chat/completions"):
        return _Resp({"choices": [{"message": {"content": "echo reply"}}]})
    if req.full_url.endswith("/models"):
        return _Resp({"data": [{"id": "echo-1"}]})
    raise urllib.error.URLError("unreachable")


def main():
    sys.path.insert(0, str(REPO_CORE))
    cfg_test = SCRATCH / "config.yaml"
    shutil.copy2(Path(os.environ.get("LVI_TEST_CONFIG") or REPO_CORE.parent / "tests" / "fixtures" / "config.yaml"), cfg_test)  # tracked fixture; LVI_TEST_CONFIG overrides
    cfg_test.write_text(cfg_test.read_text().replace("port: 8765", "port: 8766"))
    os.environ["LVI_CONFIG"] = str(cfg_test)
    env_file = SCRATCH / ".env"
    env_file.write_text("LVI_ECHO_KEY=echo-key-abcdefghijkl\n")
    os.chmod(env_file, 0o600)
    os.environ["LVI_ENV_FILE"] = str(env_file)
    os.environ["LVI_ECHO_KEY"] = "echo-key-abcdefghijkl"
    urllib.request.urlopen = _fake_urlopen

    import intelligence
    from intelligence.base import Lane, TurnContext, get_json

    # ── the real lanes ──────────────────────────────────────────────
    check("lanes registry: KINDS is the one ordered list (hermes, openclaw, nanobot)",
          intelligence.KINDS == ("hermes", "openclaw", "nanobot"), str(intelligence.KINDS))
    check("lanes registry: DEFAULT_KIND is a registered lane",
          intelligence.DEFAULT_KIND in intelligence.KINDS, intelligence.DEFAULT_KIND)
    check("lanes registry: get() is case-insensitive and None for stub/unknown/empty",
          intelligence.get("OpenClaw") is intelligence.LANES["openclaw"]
          and intelligence.get("stub") is None and intelligence.get("") is None
          and intelligence.get(None) is None)
    # ── a fake 4th lane: one class + one registry line ──────────────
    class EchoLane(Lane):
        kind, label, base_key_var = "echo", "Echo", "LVI_ECHO_KEY"
        agent_required = False

        def agent_for(self, icfg):
            return str((icfg.get("agents") or {}).get("echo") or "")

        def key_var(self, agent=""):
            return self.base_key_var

        def request_url(self, url, agent=""):
            return (url or "").rstrip("/")

        def build_request(self, text, ctx):
            return {"echo": text, "sid": ctx.conversation}, {"X-Echo": "1"}

        def discover_agents(self, url, env, timeout=4.0, status_url=None, attach_key=True):
            ids = [m["id"] for m in get_json(url + "/models", {}, timeout)["data"]]
            return {"agents": [self.entry(a, env) for a in ids], "source": "echo-models", "note": None}

        def probe_body(self, agent, icfg):
            return "echo", {"echo": "ok"}

        def key_test(self, row, key, icfg, *, probe, http, scrub):
            return {"ok": True, "status": "ok", "detail": "Echo accepted the key"}

        def ui_meta(self):
            return {"kind": "echo", "label": "Echo", "agent_required": False, "per_agent_keys": False,
                    "default_agent": None, "url_placeholder": "http://<echo-host>/v1",
                    "agent_help": "Echo agents.", "no_agent_label": "echo's own", "brand": None}

    intelligence.LANES["echo"] = EchoLane()   # the "one registry line"
    intelligence.KINDS = tuple(intelligence.LANES)
    # Registered before ANY lane method runs: a lane may import the facade
    # lazily, and the facade's BASE_KEY_VARS is built at its import.


    meta_keys = {"kind", "label", "agent_required", "per_agent_keys", "default_agent",
                 "url_placeholder", "agent_help", "no_agent_label", "brand"}
    REAL = ("hermes", "openclaw", "nanobot")
    for kind in REAL:
        lane = intelligence.LANES[kind]
        m = lane.ui_meta()
        check(f"lanes registry: {kind}: a Lane subclass whose kind matches its registry key",
              isinstance(lane, Lane) and lane.kind == kind and lane.label and lane.base_key_var)
        check(f"lanes registry: {kind}: ui_meta has exactly the frozen fields",
              set(m) == meta_keys and m["kind"] == kind and m["label"] == lane.label
              and m["agent_required"] is lane.agent_required and m["per_agent_keys"] is lane.per_agent_keys
              and m["default_agent"] == lane.default_agent, str(sorted(m)))
        check(f"lanes registry: {kind}: ui_meta brand is null or a colour pair; no secrets",
              (m["brand"] is None or set(m["brand"]) == {"backgroundColor", "color"})
              and not any("LVI_" in str(v) or v == lane.base_key_var for v in m.values())
              and (m["no_agent_label"] is None) == lane.agent_required, json.dumps(m)[:200])
        ctx = TurnContext(agent="a1", model="m", max_tokens=9, conversation="c", sticky=True,
                          room="r", system_prompt="sys", history=[])
        try:
            payload, headers = lane.build_request("hi", ctx)
            lane.key_var("a1"), lane.request_url("http://x.test/v1", "a1"), lane.agent_for({})
            lane.health_url("http://x.test/v1"), lane.probe_body("a1", {}), lane.inspect_reply("ok")
            ok = isinstance(payload, dict) and isinstance(headers, dict) and "authorization" not in {
                k.lower() for k in headers}
            detail = ""
        except NotImplementedError as e:
            ok, detail = False, f"NotImplementedError {e}"
        check(f"lanes registry: {kind}: implements the turn + probe contract; never sets Authorization",
              ok, detail)

    # The dashboard's built-in fallback copy must match what the bot serves.
    app_src = (REPO_CORE.parent / "dashboard-ui" / "src" / "App.jsx").read_text()
    mb = re.search(r"const BUILTIN_LANES = \[(.*?)\n\]", app_src, re.DOTALL)
    block = mb.group(1) if mb else ""
    for kind in REAL:
        m = intelligence.LANES[kind].ui_meta()
        strings = [m["kind"], m["label"], m["url_placeholder"], m["agent_help"]] + \
            ([m["no_agent_label"]] if m["no_agent_label"] else []) + \
            (list(m["brand"].values()) if m["brand"] else [])
        missing = [s for s in strings if s not in block]
        check(f"lanes registry: {kind}: App.jsx BUILTIN_LANES matches the bot's ui_meta", bool(block) and not missing,
              f"missing {missing}")
    check("lanes registry: App.jsx has no other hardcoded lane names",
          not re.findall(r"'(hermes|openclaw|nanobot)'", app_src.replace(block, "")),
          str(re.findall(r".{30}'(?:hermes|openclaw|nanobot)'.{10}", app_src.replace(block, ""))[:3]))

    # bot.py dispatch reads the registry (pinned on code, never comments).
    bot_src = ast.unparse(ast.parse((REPO_CORE.parent / "bot.py").read_text()))
    check("lanes registry: bot.py dispatches every registered kind to IntelligenceAgent",
          "in intelligence_lanes.KINDS" in bot_src and "('hermes', 'openclaw', 'nanobot')" not in bot_src)

    import dashboard
    import intelligence_agent as ia
    import intelligence_target as it
    import keys_catalog
    from dashboard import config_store
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    dashboard.register_dashboard(app)
    client = TestClient(app, base_url="http://192.0.2.10", raise_server_exceptions=False)
    base = dict(config_store.read_section("intelligence") or {})

    check("lanes registry: echo: the facade knows the new lane (key var, URL, agent)",
          it.BASE_KEY_VARS.get("echo") == "LVI_ECHO_KEY" and it.key_var("echo") == "LVI_ECHO_KEY"
          and it.request_url("echo", "http://e.test/v1/") == "http://e.test/v1"
          and it.agent_for({"agents": {"echo": "e1"}}, "echo") == "e1")
    r = client.put("/api/dashboard/intelligence/key", json={"kind": "echo", "api_key": "echo-new-key-123"})
    check("lanes registry: echo: PUT /intelligence/key accepts the new kind and writes its own variable",
          r.status_code == 200 and r.json().get("var") == "LVI_ECHO_KEY", f"{r.status_code} {r.text[:160]}")
    r = client.put("/api/dashboard/intelligence", json={"kind": "echo", "url": "http://e.test/v1", "agent": ""})
    check("lanes registry: echo: PUT /intelligence saves the new kind (agent optional, like its flag says)",
          r.status_code == 200 and (config_store.read_section("intelligence") or {}).get("kind") == "echo",
          f"{r.status_code} {r.text[:160]}")
    r = client.post("/api/dashboard/intelligence/agents", json={"kind": "echo"})
    check("lanes registry: echo: agent discovery runs the new lane's own discover_agents",
          r.status_code == 200 and r.json().get("source") == "echo-models"
          and [a["id"] for a in r.json().get("agents", [])] == ["echo-1"], f"{r.status_code} {r.text[:160]}")
    g = client.get("/api/dashboard/intelligence").json()
    check("lanes registry: echo: GET /intelligence reports the new lane's key status",
          "echo" in g.get("keys_set", {}) and "echo" in g.get("agents", {}), str(g.get("keys_set")))
    lanes = client.get("/api/dashboard/intelligence/lanes")
    kinds = [x.get("kind") for x in lanes.json().get("lanes", [])] if lanes.status_code == 200 else []
    check("lanes registry: GET /intelligence/lanes serves every lane's ui_meta in registry order",
          kinds == ["hermes", "openclaw", "nanobot", "echo"]
          and lanes.json()["lanes"][0] == intelligence.LANES["hermes"].ui_meta(), f"{lanes.status_code} {kinds}")
    r = client.put("/api/dashboard/mapping", json={"room_agents": {"echo": {}}})
    check("lanes registry: echo: room overrides accept the new kind (Literal from the registry)",
          r.status_code != 422 or "literal_error" not in r.text, f"{r.status_code} {r.text[:160]}")
    check("lanes registry: echo: the key catalog lists the new lane's key",
          any(row.get("var") == "LVI_ECHO_KEY" and row.get("kind") == "echo" for row in keys_catalog.STATIC))
    config_store.write_section("intelligence", {**base, "kind": "echo", "url": "http://e.test/v1"})
    CALLS.clear()
    agent = ia.IntelligenceAgent(room="reg-room", session_mode="sticky")
    reply = agent._ask_intelligence("hello")
    url, headers, body = CALLS[-1] if CALLS else (None, {}, None)
    check("lanes registry: echo: the voice agent sends the new lane's own envelope",
          reply == "echo reply" and url == "http://e.test/v1/chat/completions"
          and json.loads(body or "{}") == {"echo": "hello", "sid": "voice-reg-room"}
          and headers.get("x-echo") == "1" and headers.get("authorization") == "Bearer echo-key-abcdefghijkl",
          f"{url} {body} {headers}")
    # ── host:port is enough (Lane.normalize_url, applied before validation) ──
    cases = {"1.2.3.4:8642": "http://1.2.3.4:8642/v1", " 1.2.3.4:8642/ ": "http://1.2.3.4:8642/v1",
             "http://h.test:1": "http://h.test:1/v1", "http://h.test:1/v1": "http://h.test:1/v1",
             "http://h.test:1/v1/": "http://h.test:1/v1/", "https://h.test/api/v1": "https://h.test/api/v1",
             "http://h.test:8642/p/kids-agent/v1": "http://h.test:8642/p/kids-agent/v1",
             "h.test": "http://h.test/v1", "": ""}
    for kind in REAL:
        got = {raw: intelligence.LANES[kind].normalize_url(raw) for raw in cases}
        check(f"lanes registry: {kind}: host:port gets http:// and /v1; a full URL is kept as typed",
              got == cases, str({k: v for k, v in got.items() if v != cases[k]}))
    r = client.put("/api/dashboard/intelligence", json={"kind": "hermes", "url": "hm.test:8642", "agent": "default"})
    sec = dict(config_store.read_section("intelligence") or {})
    check("lanes registry: url: PUT /intelligence saves host:port as the full URL (url + the kind's recall map)",
          r.status_code == 200 and sec.get("url") == "http://hm.test:8642/v1"
          and (sec.get("urls") or {}).get("hermes") == "http://hm.test:8642/v1", f"{r.status_code} {r.text[:120]} {sec.get('url')}")
    r = client.put("/api/dashboard/intelligence", json={"kind": "hermes", "url": "http://hm.test:8642/v1/", "agent": "default"})
    sec = dict(config_store.read_section("intelligence") or {})
    check("lanes registry: url: a full URL is saved byte for byte, as before",
          r.status_code == 200 and sec.get("url") == "http://hm.test:8642/v1/", f"{r.status_code} {sec.get('url')}")
    CALLS.clear()
    r = client.post("/api/dashboard/intelligence/agents", json={"kind": "nanobot", "url": "nb.test:8900"})
    check("lanes registry: url: agent discovery accepts host:port and probes the full URL",
          r.status_code == 200 and any(c[0] == "http://nb.test:8900/v1/models" for c in CALLS),
          f"{r.status_code} {[c[0] for c in CALLS]}")
    bad = [client.put("/api/dashboard/intelligence", json={"kind": "hermes", "url": u}).status_code
           for u in ("hm.test:99999", "hm.test:86 42", "javascript:alert(1)")]
    check("lanes registry: url: a bad port, a space or a non-http scheme is still a 422", bad == [422, 422, 422], str(bad))
    print(f"{len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        import traceback
        traceback.print_exc()
        print("[FAIL] lanes registry: suite crashed")
        rc = 1
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(rc)
