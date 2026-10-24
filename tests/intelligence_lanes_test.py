"""Intelligence lanes: golden contract — every outgoing request and every
answer the lanes produce, compared against a recording.

What it pins, per lane (hermes, openclaw, nanobot):
  - voice turns: URL, headers, raw JSON body (key order included), reply
    post-processing, error texts, warning lines;
  - intelligence_target: agent_for / key_var / request_url / discover_agents;
  - dashboard routes: GET/PUT intelligence, key save, Test probe, agent
    discovery, key test, the rig label, the house default agent, payload
    validation;
  - the key catalog rows.

The network is a fake urlopen (nothing leaves the process); config and .env
are throwaway copies.

Usage (host venv — pipecat must import):
  python intelligence_lanes_test.py <repo>/core            # compare
  python intelligence_lanes_test.py <repo>/core --record   # rewrite the golden
The golden file is recorded from a known-good tree; a refactor must reproduce
it exactly.
"""

import io
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
RECORD = "--record" in sys.argv
GOLDEN = Path(__file__).resolve().parent / "intelligence_lanes_golden.json"
SCRATCH = Path(tempfile.mkdtemp(prefix="lvi-lanes-golden-"))
FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ── fake network ─────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=None):
        return self._body if n is None else self._body[:n]


class FakeNet:
    """rules: [(substring, spec)], first match wins. spec:
    ("json", obj[, status]) | ("http", code, body_text) | ("urlerror",) | ("oserror",)"""

    def __init__(self):
        self.calls = []
        self.rules = []

    def urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        data = getattr(req, "data", None)
        self.calls.append({
            "method": req.get_method() if hasattr(req, "get_method") else "GET",
            "url": url,
            "headers": sorted([k.lower(), v] for k, v in req.header_items()) if hasattr(req, "header_items") else [],
            "body": data.decode() if isinstance(data, bytes) else data,
            "timeout": timeout,
        })
        for sub, spec in self.rules:
            if sub in url:
                break
        else:
            spec = ("urlerror",)
        kind = spec[0]
        if kind == "json":
            return _Resp(spec[2] if len(spec) > 2 else 200, json.dumps(spec[1]).encode())
        if kind == "http":
            raise urllib.error.HTTPError(url, spec[1], "err", {}, io.BytesIO(spec[2].encode()))
        if kind == "oserror":
            raise OSError("boom")
        raise urllib.error.URLError("unreachable")


NET = FakeNet()
urllib.request.urlopen = NET.urlopen

_NORM = [
    (re.compile(r"lenoxvi-test-[0-9a-f]{12}"), "lenoxvi-test-<ID>"),
    (re.compile(r'"latency_ms": \d+'), '"latency_ms": 0'),
]


def norm(obj):
    s = json.dumps(obj, default=str)
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return json.loads(s)


def exc_text(e):
    return f"{type(e).__name__}: {e}"


# ── scenario recorder ────────────────────────────────────────────────

RESULTS = {}


def scenario(name, fn, rules=()):
    NET.calls.clear()
    NET.rules = list(rules)
    LOGS.clear()
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 — an exception is a recorded result
        result = {"raised": exc_text(e)}
    RESULTS[name] = norm({"result": result, "calls": list(NET.calls), "logs": list(LOGS)})


LOGS = []


def main():
    sys.path.insert(0, str(REPO_CORE))
    cfg_src = REPO_CORE.parent / "config.yaml"
    cfg_test = SCRATCH / "config.yaml"
    shutil.copy2(cfg_src, cfg_test)
    cfg_test.write_text(cfg_test.read_text().replace("port: 8765", "port: 8766"))
    os.environ["LVI_CONFIG"] = str(cfg_test)
    env_file = SCRATCH / ".env"
    keys = {
        "LVI_HERMES_KEY": "hm-golden-key-abcdefghijkl",
        "LVI_HERMES_KEY_KIDS_AGENT": "hk-golden-key-abcdefghijkl",
        "LVI_OPENCLAW_KEY": "oc-golden-key-abcdefghijkl",
        "LVI_NANOBOT_KEY": "nb-golden-key-abcdefghijkl",
    }
    env_file.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    os.chmod(env_file, 0o600)
    os.environ["LVI_ENV_FILE"] = str(env_file)
    for k in list(os.environ):
        if k.startswith(("LVI_HERMES", "LVI_OPENCLAW", "LVI_NANOBOT", "LVI_INTELLIGENCE")) or k == "LVI_CONVERSATION":
            del os.environ[k]
    os.environ.update(keys)
    os.environ["LVI_CONVERSATION"] = "voice-golden-fresh"

    from loguru import logger
    logger.add(lambda m: LOGS.append(m.record["message"])
               if any(w in m.record["message"] for w in ("preamble", "truncated", "no key", "no intelligence key"))
               else None, level="DEBUG")

    import dashboard
    import intelligence_agent as ia
    import intelligence_target as it
    import keys_catalog
    from dashboard import config_store, routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    dashboard.register_dashboard(app)
    client = TestClient(app, base_url="http://192.0.2.10", raise_server_exceptions=False)

    HM, OC, NB = "http://hm.test:8642/v1", "http://oc.test:18789/v1", "http://nb.test:8900/v1"
    base_icfg = dict(config_store.read_section("intelligence") or {})
    for k in ("kind", "url", "urls", "agent", "agents", "model", "max_tokens", "hermes_status_url"):
        base_icfg.pop(k, None)

    def put_cfg(room_agents=None, **kv):
        config_store.write_section("intelligence", {**base_icfg, **kv})
        config_store.write_section("room_agents", room_agents or {})

    def http(method, path, **kw):
        r = getattr(client, method)(path, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text[:200]
        return {"status": r.status_code, "json": body}

    chat_ok = ("/chat/completions", ("json", {"choices": [{"message": {"content": "ok reply"}}],
                                              "usage": {"prompt_tokens": 5, "completion_tokens": 2}}))
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "before"}]

    # ── voice turns ─────────────────────────────────────────────────
    def turn(text="hi", session_mode="fresh", room="golden-room", hist=True):
        def run():
            a = ia.IntelligenceAgent(room=room, session_mode=session_mode,
                                     room_resolver=(lambda: room) if room else None)
            if hist:
                a._history = list(history)
            reply = a._ask_intelligence(text)
            return {"reply": reply, "agent": a._agent, "url": a._url, "key_var": a._key_var,
                    "conversation": a._conversation}
        return run

    turn_cfgs = {
        "hermes-default": dict(kind="hermes", url=HM, urls={"hermes": HM}, model="hermes-agent"),
        "hermes-profile": dict(kind="hermes", url=HM, urls={"hermes": HM}, agents={"hermes": "kids-agent"}),
        "hermes-default-named": dict(kind="hermes", url=HM + "/", agents={"hermes": "default"}),
        "openclaw-agent": dict(kind="openclaw", url=OC, urls={"openclaw": OC}, agents={"openclaw": "main"}),
        "openclaw-legacy": dict(kind="openclaw", url=OC, agent="legacy-main"),
        "openclaw-none": dict(kind="openclaw", url=OC),
        "nanobot-none": dict(kind="nanobot", url=NB, urls={"nanobot": NB}),
        "nanobot-agent": dict(kind="nanobot", url="http://nb.test:8900", agents={"nanobot": "glm-5"}),
    }
    for name, c in turn_cfgs.items():
        for mode in ("fresh", "sticky"):
            put_cfg(**c)
            scenario(f"turn/{name}/{mode}", turn(session_mode=mode), [chat_ok])
    put_cfg(**turn_cfgs["hermes-default"])
    scenario("turn/hermes-default/no-history", turn(hist=False), [chat_ok])
    put_cfg(**turn_cfgs["hermes-default"])
    scenario("turn/hermes-default/no-room-sticky", turn(session_mode="sticky", room=None), [chat_ok])
    # room overrides
    put_cfg(room_agents={"hermes": {"golden-room": "kids-agent"}}, **turn_cfgs["hermes-default"])
    scenario("turn/room-override/hermes", turn(session_mode="sticky"), [chat_ok])
    put_cfg(room_agents={"openclaw": {"golden-room": "kidbot"}}, **turn_cfgs["openclaw-agent"])
    scenario("turn/room-override/openclaw", turn(session_mode="sticky"), [chat_ok])
    put_cfg(room_agents={"nanobot": {"golden-room": "glm-kids"}}, **turn_cfgs["nanobot-none"])
    scenario("turn/room-override/nanobot", turn(session_mode="sticky"), [chat_ok])
    put_cfg(room_agents={"openclaw": {"golden-room": "kidbot"}}, **turn_cfgs["hermes-default"])
    scenario("turn/room-override/other-system-ignored", turn(session_mode="sticky"), [chat_ok])
    put_cfg(room_agents={"hermes": {"golden-room": "bad name!"}}, **turn_cfgs["hermes-default"])
    scenario("turn/room-override/invalid-name", turn(session_mode="sticky"), [chat_ok])
    # errors + reply post-processing
    nb_400 = ("/chat/completions", ("http", 400, json.dumps(
        {"error": {"message": "Only a single user message is supported nb-golden-key-abcdefghijkl"}})))
    for name in ("hermes-default", "openclaw-agent", "nanobot-none"):
        put_cfg(**turn_cfgs[name])
        scenario(f"turn/{name}/http-400", turn(), [nb_400])
        put_cfg(**turn_cfgs[name])
        scenario(f"turn/{name}/http-401", turn(), [("/chat/completions", ("http", 401, "nope"))])
        put_cfg(**turn_cfgs[name])
        scenario(f"turn/{name}/empty-reply", turn(),
                 [("/chat/completions", ("json", {"choices": [{"message": {"content": "  "}}]}))])
        put_cfg(max_tokens=10, **turn_cfgs[name])
        scenario(f"turn/{name}/char-cap", turn(), [("/chat/completions", ("json", {"choices": [{"message": {
            "content": "This reply is far too long for a ten token cap, so it must be cut at a word."}}]}))])
        put_cfg(**turn_cfgs[name])
        scenario(f"turn/{name}/preamble", turn(), [("/chat/completions", ("json", {"choices": [{"message": {
            "content": "The user is asking about the time. It is noon."}}]}))])
    os.environ.pop("LVI_NANOBOT_KEY")
    put_cfg(**turn_cfgs["nanobot-none"])
    scenario("turn/nanobot-none/no-key", turn(), [chat_ok])
    os.environ["LVI_NANOBOT_KEY"] = keys["LVI_NANOBOT_KEY"]
    put_cfg(kind="bogus", url=HM)
    scenario("turn/unknown-kind", turn(), [chat_ok])

    # ── intelligence_target ─────────────────────────────────────────
    kinds = ("hermes", "openclaw", "nanobot", "stub", "bogus", "")
    cfgs = [{}, {"agent": "legacy"}, {"agents": {"hermes": "default", "openclaw": "oc", "nanobot": "nb"}},
            {"agents": {"hermes": "kids-agent"}, "agent": "x"}, {"agents": "not-a-dict", "agent": None}]
    scenario("target/agent_for", lambda: [[k, i, it.agent_for(c, k)] for k in kinds for i, c in enumerate(cfgs)])
    scenario("target/key_var", lambda: [[k, a, it.key_var(k, a)] for k in kinds
                                         for a in ("", "default", "kids-agent", "ha.butler_2")])
    urls = ["http://h:8642/v1", "http://h:8642/v1/", "http://h:8642", "http://h:8642/", "http://h:8642/p/old/v1",
            "http://h:8900/v1", "", "https://h/api/v1"]
    scenario("target/request_url", lambda: [[k, u, a, it.request_url(k, u, a)] for k in kinds for u in urls
                                             for a in ("", "default", "kids-agent")])
    scenario("target/constants", lambda: {"BASE_KEY_VARS": it.BASE_KEY_VARS, "port": it.HERMES_STATUS_PORT,
                                          "agent_re": it.AGENT_RE.pattern,
                                          "is_default": [it.is_default_agent(a) for a in ("", None, "default", "x")]})
    env = dict(keys)
    models_oc = ("/models", ("json", {"data": [{"id": "openclaw/main"}, {"id": "openclaw/kidbot"}, {"id": "gpt-x"},
                                                {"id": "openclaw/bad name"}, {"id": "openclaw/main"}, None]}))
    models_nb = ("/models", ("json", {"data": [{"id": "glm-5"}, {"id": "bad id!"}, "junk", {"id": "glm-5"}]}))
    status_hm = ("/api/status", ("json", {"profiles": ["kids-agent", "default", "parked-one", "bad name!", 3],
                                          "parked_profiles": ["parked-one"]}))
    disc = {
        "openclaw/ok": ("openclaw", OC, env, None, True, [models_oc]),
        "openclaw/bare-url": ("openclaw", "http://oc.test:18789", env, None, True, [models_oc]),
        "openclaw/no-url": ("openclaw", "", env, None, True, []),
        "openclaw/no-key": ("openclaw", OC, {}, None, True, []),
        "openclaw/401-unsaved": ("openclaw", OC, env, None, False, [("/models", ("http", 401, "x"))]),
        "openclaw/401-saved": ("openclaw", OC, env, None, True, [("/models", ("http", 401, "x"))]),
        "openclaw/unreachable": ("openclaw", OC, env, None, True, []),
        "openclaw/empty": ("openclaw", OC, env, None, True, [("/models", ("json", {"data": []}))]),
        "nanobot/ok": ("nanobot", NB, env, None, True, [models_nb]),
        "nanobot/bare-url": ("nanobot", "http://nb.test:8900/", env, None, True, [models_nb]),
        "nanobot/no-url": ("nanobot", "", env, None, True, []),
        "nanobot/no-key": ("nanobot", NB, {}, None, True, []),
        "nanobot/401-unsaved": ("nanobot", NB, env, None, False, [("/models", ("http", 401, "x"))]),
        "nanobot/unreachable": ("nanobot", NB, env, None, True, []),
        "nanobot/bad-shape": ("nanobot", NB, env, None, True, [("/models", ("json", {"data": "nope"}))]),
        "hermes/ok": ("hermes", HM, env, None, True, [status_hm]),
        "hermes/status-url": ("hermes", HM, env, "http://other.test:9119/api/status", True, [status_hm]),
        "hermes/no-url": ("hermes", "", env, None, True, []),
        "hermes/unreachable": ("hermes", HM, env, None, True, []),
        "hermes/no-profiles": ("hermes", HM, env, None, True, [("/api/status", ("json", {"profiles": []}))]),
        "bogus/any": ("bogus", HM, env, None, True, []),
    }
    for name, (k, u, e, su, ak, rules) in disc.items():
        scenario(f"target/discover/{name}", lambda k=k, u=u, e=e, su=su, ak=ak: it.discover_agents(k, u, e, 4.0, su, ak),
                 rules)
    _port = it.HERMES_STATUS_PORT
    it.HERMES_STATUS_PORT = 19119
    scenario("target/discover/hermes/patched-port", lambda: it.discover_agents("hermes", HM, env), [status_hm])
    it.HERMES_STATUS_PORT = _port

    # ── dashboard routes ────────────────────────────────────────────
    route_cfgs = {**turn_cfgs, "bogus": dict(kind="bogus", url=HM)}
    for name, c in route_cfgs.items():
        put_cfg(**c)
        scenario(f"routes/get-intelligence/{name}", lambda: http("get", "/api/dashboard/intelligence"))
        put_cfg(**c)
        scenario(f"routes/label/{name}", lambda: routes._intelligence_label())
        put_cfg(**c)
        scenario(f"routes/agent-default/{name}", lambda: routes._agent_default())
    os.environ["LVI_INTELLIGENCE_AGENT"] = "env-agent"
    put_cfg(kind="openclaw", url=OC)
    scenario("routes/label/openclaw-env-agent", lambda: routes._intelligence_label())
    del os.environ["LVI_INTELLIGENCE_AGENT"]

    def put_then_read(body, **start):
        def run():
            put_cfg(**start)
            r = http("put", "/api/dashboard/intelligence", json=body)
            sec = dict(config_store.read_section("intelligence") or {})
            return {"resp": r, "saved": {k: sec.get(k) for k in ("kind", "url", "urls", "agent", "agents")}}
        return run

    puts = {
        "hermes-url-agent": ({"kind": "hermes", "url": HM, "agent": "kids-agent"}, {}),
        "hermes-empty-agent": ({"kind": "hermes", "url": HM, "agent": ""}, {}),
        "openclaw-empty-agent": ({"kind": "openclaw", "url": OC, "agent": ""}, {}),
        "nanobot-empty-agent": ({"kind": "nanobot", "url": NB, "agent": ""},
                                {"kind": "nanobot", "url": NB, "agents": {"nanobot": "glm-5"}}),
        "nanobot-agent": ({"kind": "nanobot", "url": NB, "agent": "glm-5"}, {}),
        "openclaw-url-first": ({"kind": "openclaw", "url": OC}, {"kind": "hermes", "url": HM, "urls": {"hermes": HM}}),
        "openclaw-url-first-live": ({"kind": "openclaw", "url": OC + "/x"}, {"kind": "openclaw", "url": OC}),
        "openclaw-with-agent": ({"kind": "openclaw", "url": OC, "agent": "main"}, {"kind": "hermes", "url": HM}),
        "openclaw-flip-no-agent": ({"kind": "openclaw"}, {"kind": "hermes", "url": HM, "urls": {"openclaw": OC}}),
        "openclaw-flip-kept-agent": ({"kind": "openclaw"}, {"kind": "hermes", "url": HM, "urls": {"openclaw": OC},
                                                           "agents": {"openclaw": "main"}}),
        "flip-no-url": ({"kind": "nanobot"}, {"kind": "hermes", "url": HM}),
        "flip-remembered-url": ({"kind": "nanobot"}, {"kind": "hermes", "url": HM, "urls": {"nanobot": NB}}),
        "same-kind-no-url": ({"kind": "hermes"}, {"kind": "hermes", "url": HM}),
        "bogus-kind": ({"kind": "bogus", "url": HM}, {}),
        "extra-field": ({"kind": "hermes", "url": HM, "zzz": 1}, {}),
    }
    for name, (body, start) in puts.items():
        scenario(f"routes/put-intelligence/{name}", put_then_read(body, **start))

    def key_put(body):
        def run():
            r = http("put", "/api/dashboard/intelligence/key", json=body)
            env_file.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
            return r
        return run
    for name, body in {
        "hermes": {"kind": "hermes", "api_key": "new-key-abcdefgh"},
        "hermes-profile": {"kind": "hermes", "agent": "kids-agent", "api_key": "new-key-abcdefgh"},
        "hermes-default": {"kind": "hermes", "agent": "default", "api_key": "new-key-abcdefgh"},
        "openclaw-agent-ignored": {"kind": "openclaw", "agent": "main", "api_key": "new-key-abcdefgh"},
        "nanobot": {"kind": "nanobot", "api_key": "new-key-abcdefgh"},
        "bogus": {"kind": "bogus", "api_key": "new-key-abcdefgh"},
    }.items():
        scenario(f"routes/put-key/{name}", key_put(body))

    probe_rules = [("/health", ("http", 404, "nf")), chat_ok]
    probes = {
        "hermes": dict(kind="hermes", url=HM, model="my-model"),
        "hermes-profile": dict(kind="hermes", url=HM, agents={"hermes": "kids-agent"}),
        "openclaw": dict(kind="openclaw", url=OC, agents={"openclaw": "main"}),
        "nanobot": dict(kind="nanobot", url=NB),
        "nanobot-agent": dict(kind="nanobot", url=NB, agents={"nanobot": "glm-5"}),
        "bogus": dict(kind="bogus", url=HM),
    }
    for name, c in probes.items():
        put_cfg(**c)
        scenario(f"routes/test/{name}/ok", lambda: http("post", "/api/dashboard/intelligence/test"), probe_rules)
        put_cfg(**c)
        scenario(f"routes/test/{name}/http-400", lambda: http("post", "/api/dashboard/intelligence/test"),
                 [("/health", ("json", {})), nb_400])
        put_cfg(**c)
        scenario(f"routes/test/{name}/unreachable", lambda: http("post", "/api/dashboard/intelligence/test"), [])
    put_cfg(kind="hermes")
    scenario("routes/test/no-url", lambda: http("post", "/api/dashboard/intelligence/test"))

    agents_cases = {
        "openclaw-saved": ({"kind": "openclaw"}, dict(kind="openclaw", url=OC), [models_oc]),
        "openclaw-typed-other": ({"kind": "openclaw", "url": "http://elsewhere.test:18789/v1"},
                                 dict(kind="openclaw", url=OC), [("/models", ("http", 401, "x"))]),
        "openclaw-same-target": ({"kind": "openclaw", "url": OC + "/"}, dict(kind="hermes", url=HM, urls={"openclaw": OC}),
                                 [models_oc]),
        "nanobot-saved": ({"kind": "nanobot"}, dict(kind="nanobot", url=NB, agents={"nanobot": "glm-5"}), [models_nb]),
        "hermes-saved": ({"kind": "hermes"}, dict(kind="hermes", url=HM), [status_hm]),
        "hermes-status-url": ({"kind": "hermes"}, dict(kind="hermes", url=HM, hermes_status_url="http://s.test:9119/api/status"),
                              [status_hm]),
        "bogus": ({"kind": "bogus"}, dict(kind="hermes", url=HM), []),
    }
    for name, (body, c, rules) in agents_cases.items():
        put_cfg(**c)
        scenario(f"routes/agents/{name}", lambda body=body: http("post", "/api/dashboard/intelligence/agents", json=body),
                 rules)

    kt = {
        "hermes": ("LVI_HERMES_KEY", dict(kind="hermes", url=HM), [("/health", ("json", {})), chat_ok]),
        "hermes-401": ("LVI_HERMES_KEY", dict(kind="hermes", url="http://hm.test:8642"),
                       [("/health", ("json", {})), ("/chat/completions", ("http", 404, "nf"))]),
        "hermes-profile": ("LVI_HERMES_KEY_KIDS_AGENT", dict(kind="hermes", url=HM), [("/health", ("json", {})), chat_ok]),
        "openclaw": ("LVI_OPENCLAW_KEY", dict(kind="openclaw", url=OC, agents={"openclaw": "main"}),
                     [("/health", ("json", {})), chat_ok]),
        "openclaw-no-url": ("LVI_OPENCLAW_KEY", dict(kind="hermes", url=HM), []),
        "openclaw-no-agent": ("LVI_OPENCLAW_KEY", dict(kind="openclaw", url=OC), []),
        "openclaw-401": ("LVI_OPENCLAW_KEY", dict(kind="openclaw", url=OC, agents={"openclaw": "main"}),
                         [("/health", ("json", {})), ("/chat/completions", ("http", 401, "no"))]),
        "nanobot": ("LVI_NANOBOT_KEY", dict(kind="nanobot", url=NB), [("/models", ("json", {"data": []}))]),
        "nanobot-bare": ("LVI_NANOBOT_KEY", dict(kind="hermes", url=HM, urls={"nanobot": "http://nb.test:8900"}),
                         [("/models", ("http", 404, "nf"))]),
        "nanobot-401": ("LVI_NANOBOT_KEY", dict(kind="nanobot", url=NB), [("/models", ("http", 401, "no"))]),
        "nanobot-no-url": ("LVI_NANOBOT_KEY", dict(kind="hermes", url=HM), []),
        "nanobot-unreachable": ("LVI_NANOBOT_KEY", dict(kind="nanobot", url=NB), []),
        "unknown-var": ("LVI_HERMES_KEY_NOPE", dict(kind="hermes", url=HM), []),
    }
    for name, (var, c, rules) in kt.items():
        put_cfg(**c)
        scenario(f"routes/key-test/{name}", lambda var=var: http("post", "/api/dashboard/keys/test", json={"var": var}),
                 rules)

    scenario("routes/mapping-bad-kind", lambda: http("put", "/api/dashboard/mapping",
                                                     json={"room_agents": {"bogus": {"r": "a"}}}))
    scenario("keys_catalog/static", lambda: keys_catalog.STATIC)
    scenario("routes/key-vars", lambda: routes.INTELLIGENCE_KEY_VARS)

    # ── compare / record ────────────────────────────────────────────
    if RECORD:
        GOLDEN.write_text(json.dumps(RESULTS, indent=1) + "\n")
        print(f"recorded {len(RESULTS)} scenarios -> {GOLDEN}")
        return 0
    golden = json.loads(GOLDEN.read_text())
    check("lanes golden: scenario set unchanged", set(golden) == set(RESULTS),
          f"missing={sorted(set(golden) - set(RESULTS))[:5]} new={sorted(set(RESULTS) - set(golden))[:5]}")
    for name in sorted(golden):
        got = RESULTS.get(name)
        same = json.dumps(got) == json.dumps(golden[name])
        detail = ""
        if not same and got is not None:
            for part in ("result", "calls", "logs"):
                if json.dumps(got.get(part)) != json.dumps(golden[name].get(part)):
                    detail = f"{part} differs: got {json.dumps(got.get(part))[:300]} | want {json.dumps(golden[name].get(part))[:300]}"
                    break
        check(f"lanes golden: {name}", same, detail)
    print(f"{len(FAILS)} failed of {len(golden) + 1}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(rc)
