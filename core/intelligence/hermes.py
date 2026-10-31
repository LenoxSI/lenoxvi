"""Hermes lane. Hermes API server; named agents are profiles on a multiplexing gateway.

Agent "" or "default" -> the configured URL, LVI_HERMES_KEY. Any other agent
is a profile: <base>/p/<agent>/v1 with that profile's OWN key,
LVI_HERMES_KEY_<AGENT> (Hermes binds each /p/<profile>/ prefix to that
profile's API_SERVER_KEY; the default key gets 401 there).

Contract: intelligence/base.py.
"""

import re
import urllib.parse

from intelligence.base import AGENT_RE, Lane, get_json, key_test_via_probe


class HermesLane(Lane):
    kind = "hermes"
    label = "Hermes"
    base_key_var = "LVI_HERMES_KEY"
    per_agent_keys = True
    default_agent = "default"
    status_url_key = "hermes_status_url"

    def agent_for(self, icfg: dict) -> str:
        # Hermes reads ONLY agents.hermes — the legacy top-level `agent` key
        # was always OpenClaw's (the Hermes lane never read it).
        agent = super().agent_for(icfg)
        if agent == "default":
            return ""
        return agent

    def key_var(self, agent: str = "") -> str:
        from intelligence.target import is_default_agent

        base = self.base_key_var
        if not is_default_agent(agent):
            return base + "_" + re.sub(r"[^A-Z0-9]", "_", agent.upper())
        return base

    def request_url(self, url: str, agent: str = "") -> str:
        from intelligence.target import is_default_agent

        url = (url or "").rstrip("/")
        if not is_default_agent(agent):
            root = url[:-3] if url.endswith("/v1") else url
            root = re.sub(r"/p/[^/]+$", "", root)  # a saved profile URL, normalised
            return f"{root}/p/{agent}/v1"
        return url

    def build_request(self, text, ctx):
        payload = {
            "model": ctx.model,
            "stream": False,
            "max_tokens": ctx.max_tokens,
            "conversation": ctx.conversation,
            "messages": [
                {"role": "system", "content": ctx.system_prompt},
                *ctx.history,
                {"role": "user", "content": text},
            ],
        }
        headers = {}
        # X-Hermes-Session-Key: the api_server keys
        # dashboard-visible session continuity on THIS header, not
        # the body's `conversation` field — the label in the body
        # alone left voice sessions invisible in the Hermes admin
        # dash (the operator needs to audit them). With the header, sticky rooms
        # merge into ONE long-lived dashboard session per room and
        # fresh pipelines get one session per connection — both
        # visible and auditable. Requires API_SERVER_KEY on the
        # intelligence (present on this install).
        headers["X-Hermes-Session-Key"] = ctx.conversation
        # X-Hermes-Session-Id names the session itself. Without it the
        # api_server derives the session from the opening (system prompt +
        # first user line), so every fresh room that opens with the same
        # question lands on ONE stored session and is refused (409
        # admission_conflict). With it, each conversation is its own session
        # and the server keeps its history.
        headers["X-Hermes-Session-Id"] = ctx.conversation
        return payload, headers

    def discover_agents(self, url, env, timeout=4.0, status_url=None, attach_key=True):
        # Read at call time: the suite swaps intelligence_target.HERMES_STATUS_PORT.
        import intelligence.target as _it

        fallback = {"agents": [self.entry("default", env)], "source": "fallback"}
        if not status_url:
            p = urllib.parse.urlsplit(url)
            if not p.hostname:
                return {**fallback, "note": "set the Hermes URL first"}
            status_url = f"{p.scheme or 'http'}://{p.hostname}:{_it.HERMES_STATUS_PORT}/api/status"
        try:
            data = get_json(status_url, {}, timeout)
        except Exception as e:
            return {**fallback, "note": f"Hermes dashboard not reachable ({type(e).__name__}) "
                                        f"— showing the default agent only"}
        if not isinstance(data, dict):
            data = {}
        raw_parked, raw_ids = data.get("parked_profiles"), data.get("profiles")
        parked = {p for p in raw_parked if isinstance(p, str)} if isinstance(raw_parked, list) else set()
        ids = [p for p in (raw_ids if isinstance(raw_ids, list) else [])
               if isinstance(p, str) and AGENT_RE.match(p) and p not in parked]
        if "default" in ids:  # default first: it is the main agent
            ids = ["default"] + [p for p in ids if p != "default"]
        if not ids:
            return {**fallback, "note": "Hermes listed no profiles — showing the default agent only"}
        return {"agents": [self.entry(a, env) for a in ids], "source": "hermes-status", "note": None}

    def probe_body(self, agent, icfg):
        model = icfg.get("model") or "hermes-agent"
        body = {"model": model, "stream": False, "max_tokens": 16,
                "conversation": "dashboard-test",
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
        return model, body

    def key_test(self, row, key, icfg, *, probe, http, scrub):
        url = self.saved_url(icfg)
        agent = row.get("agent") or "default"
        return key_test_via_probe(self, row, url, agent, icfg, probe, scrub)

    def ui_meta(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "agent_required": self.agent_required,
            "per_agent_keys": self.per_agent_keys,
            "default_agent": self.default_agent,
            "url_placeholder": '<host>:8642',
            "agent_help": 'Agents (profiles) on this Hermes gateway. Each one needs its own API key.',
            "no_agent_label": None,
            "brand": {'backgroundColor': 'rgba(255, 255, 255, 0.92)', 'color': '#2563eb'},
        }
