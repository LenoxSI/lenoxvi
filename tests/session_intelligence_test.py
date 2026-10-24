"""Intelligence settings a new session can use before the restart (host venv).

Keys load only at restart, but dashboard config edits reach every new
session at once. A lane/agent switch saved with its NEW key must keep the
boot settings until the restart (keys_catalog.session_tts_provider's rule),
while a key that is simply missing still fails loudly.

Run: cd tests && ~/venvs/lenoxvi/bin/python session_intelligence_test.py <path-to-core>
"""
import os
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


for k in list(os.environ):
    if k.startswith(("LVI_HERMES", "LVI_OPENCLAW", "LVI_NANOBOT", "LVI_INTELLIGENCE")):
        del os.environ[k]

import config
import intelligence_agent as ia

HM, OC = "http://hm.test:8642/v1", "http://oc.test:18789/v1"


def s(**kv):
    return ia._intel_settings(kv)


# ── the rule itself ──────────────────────────────────────────────
hermes, openclaw = s(kind="hermes", url=HM), s(kind="openclaw", url=OC, agent="main")
env_hm = {"LVI_HERMES_KEY": "k"}
use, warn = ia.session_intelligence(hermes, hermes, env_hm)
check("session: saved == boot with its key -> saved settings, no warning", use == hermes and warn is None)
use, warn = ia.session_intelligence(openclaw, hermes, env_hm)
check("session: switched to openclaw, LVI_OPENCLAW_KEY not loaded yet, boot key loaded -> boot hermes + warning",
      use == hermes and warn and "LVI_OPENCLAW_KEY" in warn and "restart" in warn, f"{use['kind']} {warn}")
use, warn = ia.session_intelligence(openclaw, openclaw, env_hm)
check("session: openclaw at boot too and its key missing -> openclaw (fails loudly, no silent fallback)",
      use == openclaw and warn is None)
use, warn = ia.session_intelligence(openclaw, hermes, {})
check("session: neither key loaded -> the saved settings (fails loudly)", use == openclaw and warn is None)
use, warn = ia.session_intelligence(openclaw, hermes, {"LVI_HERMES_KEY": "k", "LVI_OPENCLAW_KEY": "k"})
check("session: the new key is already loaded -> the switch applies now", use == openclaw and warn is None)
hm_new_url = s(kind="hermes", url="http://other.test:8642/v1")
use, warn = ia.session_intelligence(hm_new_url, hermes, env_hm)
check("session: a URL-only change with the same key still goes live", use == hm_new_url and warn is None)
kids = s(kind="hermes", url=HM, agents={"hermes": "kids-agent"})
use, warn = ia.session_intelligence(kids, hermes, env_hm)
check("session: a new Hermes profile agent whose LVI_HERMES_KEY_KIDS_AGENT isn't loaded -> boot default agent",
      use == hermes and "LVI_HERMES_KEY_KIDS_AGENT" in (warn or ""), str(warn))
check("session: _intel_settings keeps the cfg_env rule (yaml, then env, then default)",
      ia._intel_settings({})["kind"] == "hermes" and ia._intel_settings({"kind": "OpenClaw"})["kind"] == "openclaw"
      and ia._intel_settings({})["model"] == "hermes-agent")

# ── real sessions: boot config vs the saved (live) config ────────
LIVE, BOOT = {}, {}
config._load = lambda: LIVE
config._BOOT = BOOT


def agent(room="kitchen"):
    a = ia.IntelligenceAgent(room=room, room_resolver=lambda: room)
    a._ensure_route()
    return a


BOOT.clear(); BOOT.update({"intelligence": {"kind": "hermes", "url": HM}})
LIVE.clear(); LIVE.update({"intelligence": {"kind": "openclaw", "url": OC, "agent": "main"}})
os.environ["LVI_HERMES_KEY"] = "hm-key"
a = agent()
check("agent: a room reconnecting after an unloaded-key switch keeps the boot lane, URL and key",
      a._kind == "hermes" and a._base_url == HM and a._key_var == "LVI_HERMES_KEY" and a._key == "hm-key",
      f"{a._kind} {a._base_url} {a._key_var}")
os.environ["LVI_OPENCLAW_KEY"] = "oc-key"
a = agent()
check("agent: once LVI_OPENCLAW_KEY is loaded the saved lane applies",
      a._kind == "openclaw" and a._key_var == "LVI_OPENCLAW_KEY" and a._key == "oc-key", f"{a._kind} {a._key_var}")
del os.environ["LVI_OPENCLAW_KEY"]

# per-room override saved with a new profile key
BOOT.clear(); BOOT.update({"intelligence": {"kind": "hermes", "url": HM}})
LIVE.clear(); LIVE.update({"intelligence": {"kind": "hermes", "url": HM},
                           "room_agents": {"hermes": {"kitchen": "kids-agent"}}})
a = agent()
check("agent: a room override whose LVI_HERMES_KEY_KIDS_AGENT isn't loaded keeps the room's boot agent",
      a._agent == "" and a._key_var == "LVI_HERMES_KEY" and a._key == "hm-key", f"{a._agent!r} {a._key_var}")
os.environ["LVI_HERMES_KEY_KIDS_AGENT"] = "kids-key"
a = agent()
check("agent: with the profile key loaded the room override applies",
      a._agent == "kids-agent" and a._key_var == "LVI_HERMES_KEY_KIDS_AGENT" and a._key == "kids-key",
      f"{a._agent!r} {a._key_var}")
del os.environ["LVI_HERMES_KEY_KIDS_AGENT"]
BOOT["room_agents"] = {"hermes": {"kitchen": "kids-agent"}}
a = agent()
check("agent: the same override at boot with its key missing stays put (fails loudly)",
      a._agent == "kids-agent" and a._key is None, f"{a._agent!r} {a._key!r}")
a = agent(room="office")
check("agent: other rooms are untouched by a kitchen override", a._agent == "" and a._key == "hm-key")

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
