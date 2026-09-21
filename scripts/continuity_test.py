"""Headless continuity test for hermes_agent: does turn 2 know turn 1?

Sends two requests shaped exactly like hermes_agent does (system + capped
client history). The second asks what was said in the first — with working
client-side history, the reply must reference "apple".
"""

import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# URL from the same config surface the bot uses (config.yaml brain.url >
# LVI_HERMES_URL env), not env-only — .env is secrets-only by rule.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
from config import cfg_env

URL = cfg_env("brain", "url", "LVI_HERMES_URL", "http://localhost:8642/v1") + "/chat/completions"
KEY = os.environ["LVI_HERMES_KEY"]
SYSTEM = ("You are the house voice assistant. Keep replies SHORT and "
          "conversational, no markdown.")


def ask(messages):
    payload = json.dumps({"model": "hermes-agent", "stream": False, "messages": messages}).encode()
    req = urllib.request.Request(
        URL, data=payload,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


r1 = ask([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "Say the word apple."}])
print("turn1:", r1[:80])
r2 = ask([
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": "Say the word apple."},
    {"role": "assistant", "content": r1},
    {"role": "user", "content": "What did I just ask you to say?"},
])
print("turn2:", r2[:80])
assert "apple" in r2.lower(), "CONTINUITY FAILED — turn 2 did not know turn 1"
print("CONTINUITY-OK")