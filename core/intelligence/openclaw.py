"""OpenClaw lane. OpenClaw Gateway; the agent rides in the model field ("openclaw/<agent>").

One Gateway token for every agent (LVI_OPENCLAW_KEY), URL unchanged. Falls
back to the legacy top-level `agent` key (it was always OpenClaw's).

Contract: intelligence/base.py.
"""

from intelligence.base import Lane, discover_via_models, key_test_via_probe


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
        return discover_via_models(
            self, url, env, timeout, attach_key,
            key_note="save the OpenClaw key to list its agents",
            empty_note="OpenClaw listed no agents",
            agent_id=lambda mid: mid[len("openclaw/"):] if mid.startswith("openclaw/") else "")

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
