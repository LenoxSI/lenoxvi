"""Turn-path fixes from the 10-24 review (host venv).

- the wake gate holds LOCKED until a Sonos/WiiM clip really ends, then
  counts the follow-up window from there (review A #1)
- a transcript that arrives after the turn closed is dropped, never glued
  onto the next question (A #3)
- after a rename, the room-agent lookup uses the room's current name (A #4)
- pins: the duck holder skips silence loads while a reply is loading (A #2);
  bot.py lowercases the lane kind, parses wake.enabled like everything
  else, and registers the echo gate per session

Run: cd tests && ~/venvs/lenoxvi/bin/python turn_fixes_test.py <path-to-core>
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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
os.environ["LVI_HERMES_KEY"] = "hm-key"

import config
from echo_gate import EchoGate
from intelligence_agent import IntelligenceAgent
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from wake_word import TurnState, WakeWordFilter

D = FrameDirection.DOWNSTREAM
SILENCE = b"\x00" * 2560


def gate(score=0.0, timeout=0.6, barge=False):
    wf = WakeWordFilter(echo_gate=EchoGate(), on_wake=lambda: None)
    wf._oww = SimpleNamespace(predict=lambda a: {"probe": score})
    wf._name = "probe"
    wf._frame_size = 2560
    wf._awake_timeout = timeout
    wf._barge_in = barge

    async def _noop(*a, **k):
        return None
    wf.push_frame = _noop
    return wf


async def audio(wf, seconds, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await wf.process_frame(InputAudioRawFrame(SILENCE, 16000, 1), D)
        await asyncio.sleep(step)


# ── A #1: hold LOCKED until the speaker clip ends ────────────────
async def hold_probe():
    out = {}
    wf = gate()
    await wf.process_frame(BotStartedSpeakingFrame(), D)
    wf.note_speech(0.5)                       # clip loads now, sounds 0.5 s
    await wf.process_frame(BotStoppedSpeakingFrame(), D)  # transport done first
    out["after_stop"] = wf._state
    await audio(wf, 0.3)
    out["mid_clip"] = wf._state
    await audio(wf, 0.35)                     # past the clip end
    out["after_clip"] = wf._state
    t_rel = time.monotonic()
    await audio(wf, 0.4)                      # 0.4 s after the clip end < 0.6 timeout
    out["follow_up"] = wf._state
    out["follow_up_age"] = time.monotonic() - t_rel
    await audio(wf, 0.4)
    out["timed_out"] = wf._state
    return out


o = asyncio.run(hold_probe())
check("gate: the transport finishing first keeps LOCKED while the speaker clip still sounds",
      o["after_stop"] is TurnState.LOCKED and o["mid_clip"] is TurnState.LOCKED, f"{o['after_stop']} {o['mid_clip']}")
check("gate: released to AWAKE when the clip really ends",
      o["after_clip"] is TurnState.AWAKE, str(o["after_clip"]))
check("gate: the follow-up window counts from the clip end, not from the transport's stop",
      o["follow_up"] is TurnState.AWAKE and o["timed_out"] is TurnState.IDLE, f"{o['follow_up']} {o['timed_out']}")


async def no_clip_probe():
    wf = gate()
    await wf.process_frame(BotStartedSpeakingFrame(), D)
    await wf.process_frame(BotStoppedSpeakingFrame(), D)
    a = wf._state
    wf2 = gate()
    await wf2.process_frame(BotStartedSpeakingFrame(), D)
    wf2.note_speech(0.05)
    await asyncio.sleep(0.1)                  # clip already over when the transport stops
    await wf2.process_frame(BotStoppedSpeakingFrame(), D)
    return a, wf2._state


a, b = asyncio.run(no_clip_probe())
check("gate: no speaker clip (node speaker / unmapped) -> AWAKE at the transport's stop, as before",
      a is TurnState.AWAKE, str(a))
check("gate: a clip that already ended -> AWAKE at the transport's stop", b is TurnState.AWAKE, str(b))


async def barge_probe():
    fired = []
    wf = gate(score=0.99, barge=True)
    wf._on_interrupt = lambda: fired.append(1)
    await wf.process_frame(BotStartedSpeakingFrame(), D)
    wf.note_speech(5.0)
    await wf.process_frame(BotStoppedSpeakingFrame(), D)
    held = wf._state
    for _ in range(2):
        await wf.process_frame(InputAudioRawFrame(SILENCE, 16000, 1), D)
    return held, wf._state, fired, getattr(wf, "_clip_until", None)


held, after, fired, cu = asyncio.run(barge_probe())
check("gate: the wake phrase during the held clip tail still barges in",
      held is TurnState.LOCKED and after is TurnState.AWAKE and fired == [1] and cu == 0.0,
      f"{held} {after} fired={fired} clip_until={cu}")

# ── A #3: a late transcript is never glued onto the next question ─
LIVE = {"intelligence": {"kind": "hermes", "url": "http://hm.test:8642/v1"}}
config._load = lambda: LIVE
config._BOOT = LIVE


async def late_probe():
    ag = IntelligenceAgent(echo_gate=EchoGate(), room="suite-late", session_mode="fresh")
    ag._lull_secs = 0.2
    turns = []

    async def rec(text, epoch):
        turns.append(text)
    ag._run_turn = rec

    async def tick(n, gap=0.05):
        for _ in range(n):
            await ag.process_frame(TranscriptionFrame("", "", 0), D)
            await asyncio.sleep(gap)
    await ag.process_frame(VADUserStartedSpeakingFrame(), D)
    await ag.process_frame(TranscriptionFrame("turn on the lights", "", 0), D)
    await ag.process_frame(VADUserStoppedSpeakingFrame(), D)
    await tick(8)
    first = list(turns)
    await ag.process_frame(TranscriptionFrame("in the kitchen", "", 0), D)  # late tail
    await tick(4)
    await ag.process_frame(VADUserStartedSpeakingFrame(), D)
    await ag.process_frame(TranscriptionFrame("what's the weather", "", 0), D)
    await ag.process_frame(VADUserStoppedSpeakingFrame(), D)
    await tick(8)
    return first, turns


first, turns = asyncio.run(late_probe())
check("agent: a transcript after the turn closed is dropped, the next question stands alone",
      first == ["turn on the lights"] and turns == ["turn on the lights", "what's the weather"], str(turns))

# ── A #4: the room-agent lookup follows a rename ───────────────────
LIVE.clear()
LIVE.update({"intelligence": {"kind": "hermes", "url": "http://hm.test:8642/v1"},
             "room_agents": {"hermes": {"study": "kids-agent"}}})
config._BOOT = LIVE
os.environ["LVI_HERMES_KEY_KIDS_AGENT"] = "kids-key"
name = {"room": "office"}
ag = IntelligenceAgent(echo_gate=EchoGate(), room="office", room_resolver=lambda: name["room"])
name["room"] = "study"                       # dashboard rename before the first turn
ag._ensure_route()
check("agent: after a rename the room agent is looked up under the NEW name",
      ag._agent == "kids-agent" and ag._key_var == "LVI_HERMES_KEY_KIDS_AGENT", f"{ag._agent!r} {ag._key_var}")

# ── pins ──────────────────────────────────────────────────────────
src = Path(CORE, "sonos_output.py").read_text()
check("sonos pin: the duck holder skips loads while a reply clip is loading (counter held over connect+load)",
      "if self._reply_loading or time.monotonic() < self._clip_active_until:" in src
      and "self._reply_loading += 1\n        try:\n            await asyncio.wait_for(sw.connect(), timeout=5.0)" in src
      and "        finally:\n            self._reply_loading -= 1\n" in src)
bot = Path(CORE).parent.joinpath("bot.py").read_text()
check("bot pin: the lane kind is lowercased and an unknown kind is logged, not silently stubbed",
      ").strip().lower()" in bot and "answers from the offline stub" in bot
      and "if _intel_kind in intelligence_lanes.KINDS" in bot)
check("bot pin: wake.enabled / LVI_WAKE_WORD accept 1/true/yes/on in any case",
      'in ("1", "true", "yes", "on")' in bot and '"LVI_WAKE_WORD", "1") in (True, 1, "1", "true")' not in bot)
check("bot pin: each session registers its echo gate under its pc_id",
      "dash_registry.set_gate(gate, pc_id=pc_id)" in bot)

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
