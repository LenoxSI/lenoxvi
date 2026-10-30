"""OpenClaw lane. OpenClaw Gateway; the agent rides in the model field ("openclaw/<agent>").

One Gateway token for every agent (LVI_OPENCLAW_KEY), URL unchanged. Falls
back to the legacy top-level `agent` key (it was always OpenClaw's).

Contract: intelligence/base.py.
"""

import urllib.error

from intelligence.base import AGENT_RE, Lane, get_json, key_test_via_probe


class OpenClawLane(Lane):
    kind = "openclaw"
    label = "OpenClaw"
    base_key_var = "LVI_OPENCLAW_KEY"
    legacy_agent_key = True
    url_first_setup = True

    def agent_for(self, icfg: dict) -> str:
        agents = icfg.get("agents") or {}
        agent = agents.get(self.kind) if isinstance(agents, dict) else None
        if agent is None:
            agent = icfg.get("agent")
        return str(agent or "")

    def build_request(self, text, ctx):
        headers = {}
        model = f"openclaw/{ctx.agent}" if ctx.agent else "openclaw"
        payload = {
            "model": model,
            "stream": False,
            "max_tokens": ctx.max_tokens,
            "messages": [
                {"role": "system", "content": ctx.system_prompt},
                *ctx.history,
                {"role": "user", "content": text},
            ],
        }
        # Sticky continuity rides the header (proven live:
        # seed/recall under the same key). Fresh sends nothing — and
        # the agent's own memory files may still recall earlier facts
        # (documented boundary, README row).
        if ctx.sticky and ctx.room:
            headers["x-openclaw-session-key"] = ctx.conversation
        return payload, headers

    def inspect_reply(self, reply):
        # Reasoning-preamble meter: the OpenClaw agent
        # on GLM 5.3 intermittently merges planning text into `content`
        # (measured live, 2 of 8 spike calls). A voice assistant
        # speaks content verbatim — so MEASURE, never silently strip (no
        # delimiter exists; a heuristic strip risks eating real answers).
        # Data first, fix decided later (source suppression preferred).
        if reply[:24].startswith((
                "The user", "I need to", "I should", "Let me", "Okay, the user",
                "The human", "Looking at", "First, I", "I'll ")):
            return "openclaw reasoning preamble leaked into content"
        return None

    def discover_agents(self, url, env, timeout=4.0, status_url=None, attach_key=True):
        if not url:
            return {"agents": [], "source": "none", "note": "set the OpenClaw URL first"}
        token = env.get(self.base_key_var)
        if attach_key and not token:
            return {"agents": [], "source": "none",
                    "note": "save the OpenClaw key to list its agents"}
        models_url = url + ("/models" if url.endswith("/v1") else "/v1/models")
        headers = {"Authorization": f"Bearer {token}"} if (attach_key and token) else {}
        try:
            data = get_json(models_url, headers, timeout)
        except urllib.error.HTTPError as e:
            note = f"OpenClaw answered HTTP {e.code}"
            if e.code == 401 and not attach_key:
                note += " — this URL is not saved yet; save it and the stored key will be used"
            return {"agents": [], "source": "none", "note": note}
        except Exception as e:
            return {"agents": [], "source": "none", "note": f"OpenClaw unreachable ({type(e).__name__})"}
        ids = []
        for m in (data.get("data") or []):
            mid = str((m or {}).get("id") or "")
            if mid.startswith("openclaw/"):
                name = mid[len("openclaw/"):]
                if AGENT_RE.match(name) and name not in ids:
                    ids.append(name)
        return {"agents": [self.entry(a, env) for a in ids], "source": "openclaw-models",
                "note": None if ids else "OpenClaw listed no agents"}

    def probe_body(self, agent, icfg):
        model = "openclaw/" + agent
        body = {"model": model, "stream": False, "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
        return model, body

    def key_test(self, row, key, icfg, *, probe, http, scrub):
        url = self.saved_url(icfg)
        # Setup not finished yet is not a failure: "needs_setup" says
        # what to do next instead of a red "unreachable".
        if not url:
            return {"ok": False, "status": "needs_setup",
                    "detail": "save the OpenClaw URL on the Intelligence card first"}
        agent = self.agent_for(icfg)
        if not agent:
            return {"ok": False, "status": "needs_setup",
                    "detail": "pick an OpenClaw agent on the Intelligence card first"}
        return key_test_via_probe(self, row, url, agent, icfg, probe, scrub)

    def ui_meta(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "agent_required": self.agent_required,
            "per_agent_keys": self.per_agent_keys,
            "default_agent": self.default_agent,
            "url_placeholder": '<host>:18789',
            "agent_help": 'Agents on this OpenClaw Gateway (its /v1/models).',
            "no_agent_label": None,
            "brand": {'backgroundColor': 'rgba(23, 23, 23, 0.9)', 'color': '#ef4444'},
        }
