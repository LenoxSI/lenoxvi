"""Nanobot lane. nanobot API: one user message + session_id; the server keeps the history.

One server key (LVI_NANOBOT_KEY). Its "agent" is the one model the server is
configured for (its /v1/models), sent as the model field; no agent = the
field is omitted.

Contract: intelligence/base.py.
"""

import urllib.error
import urllib.parse
import uuid

from intelligence.base import AGENT_RE, Lane, get_json


class NanobotLane(Lane):
    kind = "nanobot"
    label = "Nanobot"
    base_key_var = "LVI_NANOBOT_KEY"
    agent_required = False

    def request_url(self, url: str, agent: str = "") -> str:
        url = (url or "").rstrip("/")
        if urllib.parse.urlsplit(url).path in ("", "/"):
            # A bare http://host:8900 is what users type; nanobot's API lives
            # under /v1. Discovery already tolerated both forms, so turns, the
            # Test probe and the key Test must too (a model listed, then every
            # turn a 404, is the worst way to find out).
            return url + "/v1"
        return url

    def build_request(self, text, ctx):
        # nanobot's contract (measured against v0.3.5): exactly ONE
        # user message — a system message or prior turns get HTTP 400 —
        # because it keeps the history itself, per session_id. So no
        # VOICE_SYSTEM_PROMPT and no local history here. session_id is
        # ALWAYS sent: without it every client shares one default
        # session. Sticky -> voice-<room>, fresh -> this connection's id.
        # model must equal the server's configured model, so it is sent
        # only when an agent is picked.
        payload = {
            "stream": False,
            "max_tokens": ctx.max_tokens,
            "messages": [{"role": "user", "content": text}],
            "session_id": ctx.conversation,
        }
        if ctx.agent:
            payload["model"] = ctx.agent
        return payload, {}

    def on_http_error(self, err, key):
        # nanobot explains a refusal in its body ("Only a single user
        # message is supported", "Only configured model ... is
        # available"); put that in the failure log, never the key.
        import intelligence_target as _it
        return RuntimeError(f"nanobot answered HTTP {err.code}: "
                            f"{_it.http_error_message(err, key)}")

    def discover_agents(self, url, env, timeout=4.0, status_url=None, attach_key=True):
        # nanobot serves exactly one model (its configured one); the picker
        # lists it. Same key rule as OpenClaw: only the saved URL gets it.
        if not url:
            return {"agents": [], "source": "none", "note": "set the Nanobot URL first"}
        token = env.get(self.base_key_var)
        if attach_key and not token:
            return {"agents": [], "source": "none",
                    "note": "save the Nanobot key to list its agent"}
        models_url = url + ("/models" if url.endswith("/v1") else "/v1/models")
        headers = {"Authorization": f"Bearer {token}"} if (attach_key and token) else {}
        try:
            data = get_json(models_url, headers, timeout)
        except urllib.error.HTTPError as e:
            note = f"Nanobot answered HTTP {e.code}"
            if e.code == 401 and not attach_key:
                note += " — this URL is not saved yet; save it and the stored key will be used"
            return {"agents": [], "source": "none", "note": note}
        except Exception as e:  # noqa: BLE001 — any failure is a discovery note, never a 500
            return {"agents": [], "source": "none", "note": f"Nanobot unreachable ({type(e).__name__})"}
        ids = []
        models = data.get("data") if isinstance(data, dict) else None
        for m in (models if isinstance(models, list) else []):
            mid = str(m.get("id") or "") if isinstance(m, dict) else ""
            if AGENT_RE.match(mid) and mid not in ids:
                ids.append(mid)
        return {"agents": [self.entry(a, env) for a in ids], "source": "nanobot-models",
                "note": None if ids else "Nanobot listed no model"}

    def health_url(self, url):
        # nanobot's /health lives at the server root, beside /v1.
        return (url[:-3] if url.endswith("/v1") else url) + "/health"

    def probe_body(self, agent, icfg):
        # A throwaway session: nanobot stores every turn under session_id,
        # so a test must never land in a room's voice session (or in the
        # shared default session an omitted id falls into).
        model = agent or "nanobot"
        body = {"stream": False, "max_tokens": 16,
                "session_id": f"lenoxvi-test-{uuid.uuid4().hex[:12]}",
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
        if agent:
            body["model"] = agent
        return model, body

    def probe_error(self, err, key):
        import intelligence_target as _it
        return (f"gateway error: HTTP {err.code}: "
                f"{_it.http_error_message(err, key)}")

    def key_test(self, row, key, icfg, *, probe, http, scrub):
        # GET <saved url>/models answers 401 to a wrong key. The key only
        # goes to nanobot's SAVED URL, never a typed one.
        url = self.saved_url(icfg).rstrip("/")
        url = self.request_url(url) if url else url
        if not url:
            return {"ok": False, "status": "needs_setup",
                    "detail": "save the Nanobot URL on the Intelligence card first"}
        try:
            code, _body = http("GET", url + "/models", {"Authorization": f"Bearer {key}"})
        except OSError as e:
            return {"ok": False, "status": "unreachable", "detail": scrub(e)}
        if 200 <= code < 300:
            return {"ok": True, "status": "ok", "detail": "Nanobot accepted the key"}
        if code in (401, 403):
            return {"ok": False, "status": "rejected", "detail": f"Nanobot rejected the key (HTTP {code})"}
        detail = f"Nanobot answered HTTP {code}"
        if code == 404 and not url.endswith("/v1"):
            detail += " — the saved URL may need /v1 at the end (Intelligence card)"
        return {"ok": False, "status": "unreachable", "detail": detail}

    def ui_meta(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "agent_required": self.agent_required,
            "per_agent_keys": self.per_agent_keys,
            "default_agent": self.default_agent,
            "url_placeholder": '<host>:8900',
            "agent_help": 'The model this nanobot server runs (its /v1/models). Optional: with none picked, nanobot uses its own.',
            "no_agent_label": "nanobot's own model",
            "brand": None,
        }
