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
nanobot  One server key (LVI_NANOBOT_KEY), URL unchanged. Its "agent" is the
         one model the server is configured for (its /v1/models), sent as
         the model field; no agent = the field is omitted.

Agents are remembered per kind (intelligence.agents: {hermes, openclaw, nanobot}),
like intelligence.urls. Hermes reads ONLY agents.hermes — the legacy
top-level `agent` key was always OpenClaw's (the Hermes lane never read
it), so honouring it for Hermes would reroute an existing install to a
profile that does not exist. OpenClaw falls back to the legacy key.

The per-lane rules above live in the lane classes (core/intelligence/);
this module is the facade every caller keeps importing, plus the kind-free
helpers (key_hint, http_error_message, same_target).
"""

import json
import re
import urllib.parse

import intelligence
from intelligence.base import AGENT_RE, get_json  # noqa: F401 — re-exported

# The Hermes web dashboard's public status route lists the gateway's
# profiles (the API server itself has no list endpoint).
HERMES_STATUS_PORT = 9119


def _lane(kind):
    """The lane for EXACTLY `kind`: no case folding, so a spelling this
    module never knew stays unknown."""
    try:
        return intelligence.LANES.get(kind)
    except TypeError:  # unhashable: no lane
        return None


def is_default_agent(agent) -> bool:
    return not agent or agent == "default"


def agent_for(cfg: dict, kind: str) -> str:
    """The live agent for `kind` from an intelligence config section."""
    lane = _lane(kind)
    if lane is not None:
        return lane.agent_for(cfg)
    agents = cfg.get("agents") or {}
    agent = agents.get(kind) if isinstance(agents, dict) else None
    return str(agent or "")


def key_var(kind: str, agent: str = ""):
    """The ONE env variable a (kind, agent) pair reads; None for unknown kinds."""
    lane = intelligence.LANES.get(kind)
    if lane is None:
        return None
    return lane.key_var(agent)


def request_url(kind: str, url: str, agent: str = "") -> str:
    """The base URL a turn posts to (callers append /chat/completions)."""
    lane = _lane(kind)
    if lane is None:
        return (url or "").rstrip("/")
    return lane.request_url(url, agent)


def key_hint(value):
    """Set keys show their first 4 characters so a wrong key is recognisable;
    short keys show none (4 of 9 characters would be most of the secret).
    Fixed-length mask: the hint never reveals the key's length."""
    if not value:
        return None
    if len(value) < 12:
        return "\u2022" * 8
    return value[:4] + "\u2022" * 8


def http_error_message(err, key=None) -> str:
    """The server's own error text from an HTTPError (OpenAI shape:
    {"error": {"message"}}), short, with the key scrubbed should a server
    ever echo it."""
    try:
        raw = err.read(2000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — no body is still an answer
        raw = ""
    msg = raw
    try:
        body = json.loads(raw)
        inner = body.get("error") if isinstance(body, dict) else None
        if isinstance(inner, dict) and inner.get("message"):
            msg = str(inner["message"])
        elif isinstance(inner, str):
            msg = inner
    except (ValueError, TypeError):
        pass
    if key:
        msg = msg.replace(key, "***")
    return " ".join(msg.split())[:200] or str(getattr(err, "reason", "") or "no detail")


def same_target(a: str, b: str) -> bool:
    """True when two base URLs point at the same endpoint. Scheme, host and
    port must match exactly; a trailing /v1 and trailing slashes are ignored
    (with or without /v1 is the same gateway). Anything else — a different
    host, port, or scheme — is a different target. A URL whose port can't be
    read (out of range, not a number) matches nothing, so the key is never
    attached to it."""
    def _n(u):
        p = urllib.parse.urlsplit((u or "").rstrip("/"))
        return (p.scheme.lower(), (p.hostname or "").lower(), p.port,
                re.sub(r"/v1$", "", p.path or "").rstrip("/"))
    try:
        return _n(a) == _n(b)
    except ValueError:
        return False


def discover_agents(kind: str, url: str, env, timeout: float = 4.0,
                    status_url: "str | None" = None,
                    attach_key: bool = True) -> dict:
    """List the agents a provider offers. Never raises; never returns a key.

    attach_key=False probes WITHOUT credentials: the dashboard sets it when
    the URL is not the saved config URL, so a typed-but-unsaved host never
    receives the stored token — the saved key is only ever sent to the
    saved URL. The OpenClaw lane then reports the server's own answer
    (usually HTTP 401) instead of the agent list.

    -> {"agents": [{"id", "key_set", "key_hint"}], "source": str, "note": str|None}
    """
    url = (url or "").rstrip("/")
    lane = _lane(kind)
    if lane is None:
        return {"agents": [], "source": "none", "note": f"unknown kind {kind!r}"}
    return lane.discover_agents(url, env, timeout, status_url, attach_key)
