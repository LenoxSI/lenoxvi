"""The Lane contract: everything LenoxVI knows about one intelligence system.

A lane is one OpenAI-compatible chat backend (Hermes, OpenClaw, Nanobot, ...).
Each lane lives in its own module next to this one and overrides only what
makes it different; the defaults here are the plain OpenAI shape. The
registry (`intelligence/__init__.py`) is the one list of kinds: the voice
agent, the facade (`intelligence_target`), the dashboard routes, the key
catalog and the dashboard UI (through `ui_meta`) all read it.

Lanes are stateless singletons: every method takes what it needs as
arguments, and nothing reads config at import time (the suite reloads
modules and swaps config between checks).

Adding a lane = one new module with a Lane subclass + one registry line.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class TurnContext:
    """What one voice turn knows when it builds its request."""
    agent: str            # the routed agent ('' = none / the lane's default)
    model: str            # intelligence.model (hermes-agent by default)
    max_tokens: int
    conversation: str     # voice-<room> (sticky) or this connection's id (fresh)
    sticky: bool
    room: str | None
    system_prompt: str    # VOICE_SYSTEM_PROMPT; lanes that take none ignore it
    history: list = field(default_factory=list)  # prior turns (user/assistant dicts)


class Lane:
    # ── identity ────────────────────────────────────────────────────
    kind: str = ""            # config value: intelligence.kind
    label: str = ""           # display name: "Hermes"
    base_key_var: str = ""    # the lane's one key variable: LVI_<KIND>_KEY

    # ── behaviour flags (read by routes / keys_catalog / ui_meta) ──
    agent_required: bool = True     # False = runs with no agent picked (nanobot)
    per_agent_keys: bool = False    # True = each agent has its own key var (hermes)
    default_agent: str | None = None  # name the UI shows for "no agent" (hermes: "default")
    legacy_agent_key: bool = False  # reads/writes the legacy top-level `agent` (openclaw)
    url_first_setup: bool = False   # PUT saves the URL alone until an agent exists (openclaw)
    status_url_key: str | None = None  # icfg key handed to discovery as status_url (hermes)

    # ── config / routing ────────────────────────────────────────────
    def agent_for(self, icfg: dict) -> str:
        """The live agent from an intelligence config section
        (`agents.<kind>`). '' = none."""
        agents = icfg.get("agents") or {}
        agent = agents.get(self.kind) if isinstance(agents, dict) else None
        return str(agent or "")

    def key_var(self, agent: str = "") -> str:
        """The ONE env variable this lane reads for `agent`."""
        return self.base_key_var

    def request_url(self, url: str, agent: str = "") -> str:
        """The base URL a turn posts to (callers append /chat/completions).
        `url` may carry a trailing slash."""
        return (url or "").rstrip("/")

    def normalize_url(self, raw: str) -> str:
        """What the user typed -> the URL to save. Host:port is enough: a
        missing scheme becomes http://, a missing path becomes /v1. A full
        URL (https, a profile or any other path) is kept as typed."""
        url = (raw or "").strip()
        if not url:
            return url
        if "://" not in url:
            url = "http://" + url
        scheme, rest = url.split("://", 1)
        if "/" not in rest.rstrip("/"):
            url = f"{scheme}://{rest.rstrip('/')}/v1"
        return url

    # ── voice turn (called by IntelligenceAgent._ask_intelligence) ──
    def build_request(self, text: str, ctx: TurnContext) -> tuple[dict, dict]:
        """(payload, extra_headers) for one chat completion. The caller adds
        Authorization + Content-Type itself; extra_headers carries only the
        lane's own headers (session keys)."""
        raise NotImplementedError

    def on_http_error(self, err, key: str | None) -> Exception | None:
        """An HTTPError from a turn. None = re-raise it unchanged; an
        exception = raise that instead (never carrying the key)."""
        return None

    def inspect_reply(self, reply: str) -> str | None:
        """A warning label when the reply shows a known lane defect (logged
        and counted by the agent), else None. Never changes the reply."""
        return None

    # ── dashboard (called through intelligence_target / routes) ────
    def discover_agents(self, url: str, env, timeout: float = 4.0,
                        status_url: str | None = None,
                        attach_key: bool = True) -> dict:
        """-> {"agents": [entry...], "source": str, "note": str|None}.
        Never raises; never returns a key. `url` arrives rstripped of '/'."""
        raise NotImplementedError

    def health_url(self, url: str) -> str:
        """The no-auth liveness URL the Test probe GETs first."""
        return url + "/health"

    def probe_body(self, agent: str, icfg: dict) -> tuple[str, dict]:
        """(model label for the result, request body) for the Test probe's
        one minimal chat completion."""
        raise NotImplementedError

    def probe_error(self, err, key: str | None) -> str:
        """Error text for an HTTPError from the Test probe."""
        return f"gateway error: HTTP {err.code}"

    def key_test(self, row: dict, key: str, icfg: dict, *, probe, http, scrub) -> dict:
        """Check a SAVED key against this lane -> {"ok", "status", "detail"}.
        probe(probe_cfg) = routes._intelligence_probe; http(method, url,
        headers) -> (code, body) = routes._keys_http; scrub(text) removes the
        key and caps length. `icfg` is the saved intelligence section."""
        raise NotImplementedError

    def ui_meta(self) -> dict:
        """Static, public UI facts for GET /api/dashboard/intelligence/lanes.
        No secrets, no config. Shape (frozen; App.jsx depends on it):
          {"kind": str, "label": str,
           "agent_required": bool, "per_agent_keys": bool,
           "default_agent": str|None,
           "url_placeholder": str, "agent_help": str,
           "no_agent_label": str|None,   # option text for '' when agent_required is False
           "brand": {"backgroundColor": str, "color": str} | None}
        """
        raise NotImplementedError

    # ── shared helpers for subclasses ───────────────────────────────
    def saved_url(self, icfg: dict) -> str:
        """This lane's SAVED URL: urls.<kind>, else url when this lane is
        the configured kind, else ''."""
        return (icfg.get("urls") or {}).get(self.kind) or \
            (icfg.get("url") if (icfg.get("kind") or "hermes") == self.kind else None) or ""

    def entry(self, agent_id: str, env) -> dict:
        var = self.key_var(agent_id)
        value = env.get(var) if var else None
        return {"id": agent_id, "key_set": bool(value), "key_hint": key_hint(value)}


def is_default_agent(agent) -> bool:
    return not agent or agent == "default"


def get_json(url: str, headers: dict, timeout: float):
    """GET -> parsed JSON. Module-level so the suite can patch urlopen."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def key_hint(value):
    """Set keys show their first 4 characters so a wrong key is recognisable;
    short keys show none (4 of 9 characters would be most of the secret).
    Fixed-length mask: the hint never reveals the key's length."""
    if not value:
        return None
    if len(value) < 12:
        return "\u2022" * 8
    return value[:4] + "\u2022" * 8


def key_test_via_probe(lane: Lane, row: dict, url: str, agent: str, icfg: dict,
                       probe, scrub) -> dict:
    """Lane.key_test for lanes whose Test probe proves the key (hermes,
    openclaw): one probe through the exact request a turn sends. `url` is
    the lane's saved URL, `agent` the agent whose key `row` is."""
    kind = lane.kind
    # Build the config the way the probe READS it (agents.<kind>), then
    # refuse unless the variable it will send is exactly this row's.
    probe_cfg = {"kind": kind, "url": url, "agents": {kind: agent}, "agent": agent,
                 "model": icfg.get("model")}
    if lane.key_var(lane.agent_for(probe_cfg)) != row["var"]:
        return {"ok": False, "status": "unreachable", "detail": "refused: that test would use a different key"}
    r = probe(probe_cfg)
    if r.get("ok"):
        return {"ok": True, "status": "ok", "detail": f"{r.get('model') or row['label']} answered"}
    err = scrub(r.get("error") or "failed")
    status = "rejected" if ("401" in err or "403" in err) else "unreachable"
    if "HTTP 404" in err and not url.rstrip("/").endswith("/v1"):
        err += " — the saved URL may need /v1 at the end (Intelligence card)"
    return {"ok": False, "status": status, "detail": err}


def discover_via_models(lane: Lane, url: str, env, timeout: float, attach_key: bool, *,
                        key_note: str, empty_note: str, agent_id) -> dict:
    """Lane.discover_agents for lanes that list their agents on
    GET <url>/models with the lane's one key (openclaw, nanobot).
    `agent_id(model_id)` names the agent for one listed model id, or ''
    to skip it; `key_note` is the no-key note, `empty_note` the note for
    an empty list."""
    if not url:
        return {"agents": [], "source": "none", "note": f"set the {lane.label} URL first"}
    token = env.get(lane.base_key_var)
    if attach_key and not token:
        return {"agents": [], "source": "none", "note": key_note}
    models_url = url + ("/models" if url.endswith("/v1") else "/v1/models")
    headers = {"Authorization": f"Bearer {token}"} if (attach_key and token) else {}
    try:
        data = get_json(models_url, headers, timeout)
    except urllib.error.HTTPError as e:
        note = f"{lane.label} answered HTTP {e.code}"
        if e.code == 401 and not attach_key:
            note += " — this URL is not saved yet; save it and the stored key will be used"
        return {"agents": [], "source": "none", "note": note}
    except Exception as e:
        return {"agents": [], "source": "none", "note": f"{lane.label} unreachable ({type(e).__name__})"}
    ids = []
    models = data.get("data") if isinstance(data, dict) else None
    for m in (models if isinstance(models, list) else []):
        mid = str(m.get("id") or "") if isinstance(m, dict) else ""
        name = agent_id(mid)
        if AGENT_RE.match(name) and name not in ids:
            ids.append(name)
    return {"agents": [lane.entry(a, env) for a in ids], "source": f"{lane.kind}-models",
            "note": None if ids else empty_note}
