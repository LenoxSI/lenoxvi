"""intelligence_target — where a turn goes, which key it carries, and which
agents a provider offers.

One module answers "kind + url + agent -> request URL + key variable" for
every caller (the voice agent, the dashboard Test probe, key status, key
save, agent discovery), so they can never disagree.

hermes   agent "" or "default" -> the configured URL, LVI_HERMES_KEY.
         Any other agent is a profile on a multiplexing Hermes gateway:
         <base>/p/<agent>/v1 with that profile's OWN key,
         LVI_HERMES_KEY_<AGENT>. Hermes binds each /p/<profile>/ prefix to
         that profile's API_SERVER_KEY; the default key gets 401 there.
openclaw One Gateway token for every agent (LVI_OPENCLAW_KEY); the agent
         rides in the model field ("openclaw/<agent>"), URL unchanged.

Agents are remembered per kind (intelligence.agents: {hermes, openclaw}),
like intelligence.urls. Hermes reads ONLY agents.hermes — the legacy
top-level `agent` key was always OpenClaw's (the Hermes lane never read
it), so honouring it for Hermes would reroute an existing install to a
profile that does not exist. OpenClaw falls back to the legacy key.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
BASE_KEY_VARS = {"hermes": "LVI_HERMES_KEY", "openclaw": "LVI_OPENCLAW_KEY"}
# The Hermes web dashboard's public status route lists the gateway's
# profiles (the API server itself has no list endpoint).
HERMES_STATUS_PORT = 9119


def is_default_agent(agent) -> bool:
    return not agent or agent == "default"


def agent_for(cfg: dict, kind: str) -> str:
    """The live agent for `kind` from an intelligence config section."""
    agents = cfg.get("agents") or {}
    agent = agents.get(kind) if isinstance(agents, dict) else None
    if agent is None and kind == "openclaw":
        agent = cfg.get("agent")
    agent = str(agent or "")
    if kind == "hermes" and agent == "default":
        return ""
    return agent


def key_var(kind: str, agent: str = ""):
    """The ONE env variable a (kind, agent) pair reads; None for unknown kinds."""
    base = BASE_KEY_VARS.get(kind)
    if base is None:
        return None
    if kind == "hermes" and not is_default_agent(agent):
        return base + "_" + re.sub(r"[^A-Z0-9]", "_", agent.upper())
    return base


def request_url(kind: str, url: str, agent: str = "") -> str:
    """The base URL a turn posts to (callers append /chat/completions)."""
    url = (url or "").rstrip("/")
    if kind == "hermes" and not is_default_agent(agent):
        root = url[:-3] if url.endswith("/v1") else url
        root = re.sub(r"/p/[^/]+$", "", root)  # a saved profile URL, normalised
        return f"{root}/p/{agent}/v1"
    return url


def key_hint(value):
    """Set keys show their first 4 characters so a wrong key is recognisable;
    short keys show none (4 of 9 characters would be most of the secret).
    Fixed-length mask: the hint never reveals the key's length."""
    if not value:
        return None
    if len(value) < 12:
        return "\u2022" * 8
    return value[:4] + "\u2022" * 8


def _get_json(url: str, headers: dict, timeout: float):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _entry(kind: str, agent_id: str, env) -> dict:
    var = key_var(kind, agent_id)
    value = env.get(var) if var else None
    return {"id": agent_id, "key_set": bool(value), "key_hint": key_hint(value)}


def discover_agents(kind: str, url: str, env, timeout: float = 4.0,
                    status_url: "str | None" = None) -> dict:
    """List the agents a provider offers. Never raises; never returns a key.

    -> {"agents": [{"id", "key_set", "key_hint"}], "source": str, "note": str|None}
    """
    url = (url or "").rstrip("/")
    if kind == "openclaw":
        if not url:
            return {"agents": [], "source": "none", "note": "set the OpenClaw URL first"}
        token = env.get("LVI_OPENCLAW_KEY")
        if not token:
            return {"agents": [], "source": "none",
                    "note": "save the OpenClaw key to list its agents"}
        models_url = url + ("/models" if url.endswith("/v1") else "/v1/models")
        try:
            data = _get_json(models_url, {"Authorization": f"Bearer {token}"}, timeout)
        except urllib.error.HTTPError as e:
            return {"agents": [], "source": "none", "note": f"OpenClaw answered HTTP {e.code}"}
        except Exception as e:
            return {"agents": [], "source": "none", "note": f"OpenClaw unreachable ({type(e).__name__})"}
        ids = []
        for m in (data.get("data") or []):
            mid = str((m or {}).get("id") or "")
            if mid.startswith("openclaw/"):
                name = mid[len("openclaw/"):]
                if AGENT_RE.match(name) and name not in ids:
                    ids.append(name)
        return {"agents": [_entry(kind, a, env) for a in ids], "source": "openclaw-models",
                "note": None if ids else "OpenClaw listed no agents"}

    if kind == "hermes":
        fallback = {"agents": [_entry(kind, "default", env)], "source": "fallback"}
        if not status_url:
            p = urllib.parse.urlsplit(url)
            if not p.hostname:
                return {**fallback, "note": "set the Hermes URL first"}
            status_url = f"{p.scheme or 'http'}://{p.hostname}:{HERMES_STATUS_PORT}/api/status"
        try:
            data = _get_json(status_url, {}, timeout)
        except Exception as e:
            return {**fallback, "note": f"Hermes dashboard not reachable ({type(e).__name__}) "
                                        f"— showing the default agent only"}
        parked = set(data.get("parked_profiles") or [])
        ids = [p for p in (data.get("profiles") or [])
               if isinstance(p, str) and AGENT_RE.match(p) and p not in parked]
        if "default" in ids:  # default first: it is the main agent
            ids = ["default"] + [p for p in ids if p != "default"]
        if not ids:
            return {**fallback, "note": "Hermes listed no profiles — showing the default agent only"}
        return {"agents": [_entry(kind, a, env) for a in ids], "source": "hermes-status", "note": None}

    return {"agents": [], "source": "none", "note": f"unknown kind {kind!r}"}
