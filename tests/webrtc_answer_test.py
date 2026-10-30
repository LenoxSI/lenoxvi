"""Offline node-handshake check: the SDP answer our bot gives an ESP32 node.

The nodes run libpeer: no trickle ICE, so the ANSWER is the only place they
learn the bot's ICE candidates — an answer without `a=candidate` lines means
the node never starts connectivity checks and never connects (pipecat 1.13's
MAX_BUNDLE default did exactly that: every node offline). libpeer
also copies each SDP line into a 256-byte buffer, so no line may reach 256.

No network peer needed: aiortc answers a libpeer-shaped offer in-process,
then the runner's --esp32 munging runs on it, same as /api/offer.
Run: cd tests && ~/venvs/lenoxvi/bin/python webrtc_answer_test.py <path-to-core>
"""
import asyncio
import os
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, os.path.abspath(CORE))
sys.path.insert(0, os.path.abspath(os.path.join(CORE, "..")))

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else f" — {detail}"))
    fails += 0 if cond else 1


# The libpeer offer shape (deps/libpeer src/sdp.c + peer_connection.c):
# opus audio + data channel, one host candidate, no trickle.
FP = ":".join(["AB"] * 32)
OFFER = "\r\n".join([
    "v=0",
    "o=- 1495799811084970 1495799811084970 IN IP4 0.0.0.0",
    "s=-",
    "t=0 0",
    "a=msid-semantic: iot",
    "a=group:BUNDLE 0 2",
    "a=ice-ufrag:abcd",
    "a=ice-pwd:abcdefghijklmnopqrstuv",
    f"a=fingerprint:sha-256 {FP}",
    "a=setup:passive",
    "m=audio 9 UDP/TLS/RTP/SAVP 111",
    "c=IN IP4 0.0.0.0",
    "a=rtpmap:111 opus/48000/2",
    "a=ssrc:6 cname:webrtc-opus",
    "a=sendrecv",
    "a=mid:2",
    "a=rtcp-mux",
    "m=application 50712 UDP/DTLS/SCTP webrtc-datachannel",
    "c=IN IP4 0.0.0.0",
    "a=mid:0",
    "a=sctp-port:5000",
    "a=max-message-size:262144",
    "a=candidate:1 1 UDP 2122260223 192.0.2.10 50712 typ host",
]) + "\r\n"


async def answer_sdp():
    import contextlib

    from pipecat.runner.utils import smallwebrtc_sdp_munging
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    import bot  # noqa: F401 — production import path applies our transport compat
    conn = SmallWebRTCConnection()
    await conn.initialize(sdp=OFFER, type="offer")
    sdp = conn.get_answer()["sdp"]
    with contextlib.suppress(Exception):  # teardown of a peer that never connected
        await conn.disconnect()
    return smallwebrtc_sdp_munging(sdp, None)


sdp = asyncio.run(answer_sdp())
lines = [ln for ln in sdp.split("\r\n") if ln]
cands = [ln for ln in lines if ln.startswith("a=candidate:")]
conns = [ln for ln in lines if ln.startswith("c=")]
check("webrtc answer: carries ICE candidates (libpeer has no trickle ICE)",
      len(cands) >= 1, f"candidates={len(cands)}")
check("webrtc answer: every m-line has its own candidate",
      len(cands) >= sum(ln.startswith("m=") for ln in lines),
      f"candidates={len(cands)} m-lines={sum(ln.startswith('m=') for ln in lines)}")
check("webrtc answer: connection address is a real IP, not 0.0.0.0",
      conns and all("0.0.0.0" not in c for c in conns), f"c-lines={conns}")
check("webrtc answer: audio + data channel both answered",
      any(ln.startswith("m=audio") for ln in lines)
      and any(ln.startswith("m=application") for ln in lines), f"m-lines={[l for l in lines if l.startswith('m=')]}")
check("webrtc answer: no line reaches libpeer's 256-byte line buffer",
      max(len(ln) for ln in lines) < 256, f"longest={max(len(ln) for ln in lines)}")
check("webrtc answer: sha-256 fingerprint present (libpeer reads only sha-256)",
      any(ln.startswith("a=fingerprint:sha-256 ") for ln in lines), "no sha-256 fingerprint")

print(f"\n{'ALL CHECKS PASSED' if not fails else str(fails) + ' FAIL(S)'}")
sys.exit(1 if fails else 0)
