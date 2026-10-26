"""LocalAI client pins. A REAL local HTTP server stands in for LocalAI, so the
transport is exercised (URL, headers, multipart, timeouts), not mocked.

Run: cd tests && python localai_test.py <path-to-core>
The client section needs only the stdlib + loguru; the STT/TTS sections need
pipecat and run on the host (they print SKIP elsewhere).
"""
import http.server
import io
import json
import os
import socket
import sys
import threading
import time
import wave

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


KEY = "k-test-123456789"
SEEN = []        # (method, path, headers, body)
BEHAVIOUR = {}   # path -> (status, content-type, body, delay seconds)


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        SEEN.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        status, ctype, payload, delay = BEHAVIOUR.get(
            self.path.split("?")[0], (404, "application/json", b'{"error":{"message":"not found"}}', 0))
        time.sleep(delay)
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = _go


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}"


def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wav_bytes(rate=24000, ch=1, width=2, frames=2400):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01" * frames * ch * width)
    return b.getvalue()


def err(fn):
    try:
        fn()
    except la.LocalAIError as e:
        return e
    return None


import localai as la  # noqa: E402

os.environ.pop(la.KEY_VAR, None)

# ── url ──────────────────────────────────────────────────────────────
check("localai: url normalised (scheme added, trailing slash and /v1 dropped, blank -> None)",
      la.normalize_url("192.0.2.5:8080/") == "http://192.0.2.5:8080"
      and la.normalize_url("https://x.lan/v1") == "https://x.lan" and la.normalize_url("  ") is None)

# ── key is required ──────────────────────────────────────────────────
SEEN.clear()
e = err(lambda: la.transcribe(b"x", "m", "en", url=URL, key=""))
check("localai: no key -> error naming System · Keys, and NO request is sent",
      e is not None and "System · Keys" in e.reason and SEEN == [], f"{e and e.reason} {len(SEEN)}")
e = err(lambda: la.transcribe(b"x", "m", "en", url=None, key=KEY))
check("localai: no URL -> error, nothing sent", e is not None and "URL" in e.reason and SEEN == [])

# ── transcribe: what goes on the wire ────────────────────────────────
BEHAVIOUR["/v1/audio/transcriptions"] = (200, "application/json", json.dumps(
    {"segments": [], "text": " Turn on the lights.", "duration": 1.0}).encode(), 0)
t = la.transcribe(b"RIFFfakewav", "faster-whisper-small", "en", url=URL, key=KEY)
m, path, hdr, body = SEEN[-1]
check("localai: transcribe text has its leading space stripped", t == "Turn on the lights.", repr(t))
check("localai: transcribe POSTs multipart to /v1/audio/transcriptions with the Bearer key",
      m == "POST" and path == "/v1/audio/transcriptions" and hdr.get("authorization") == f"Bearer {KEY}"
      and hdr.get("content-type", "").startswith("multipart/form-data; boundary="))
check("localai: multipart carries the file, model and language",
      b'name="model"\r\n\r\nfaster-whisper-small' in body and b'name="language"\r\n\r\nen' in body
      and b'name="file"; filename="audio.wav"' in body and b"RIFFfakewav" in body)
la.transcribe(b"x", "m", None, url=URL, key=KEY)
check("localai: auto language sends no language field", b'name="language"' not in SEEN[-1][3])
os.environ[la.KEY_VAR] = KEY
la.transcribe(b"x", "m", None, url=URL)
check("localai: the key comes from LVI_LOCALAI_KEY when not passed", SEEN[-1][2].get("authorization") == f"Bearer {KEY}")
os.environ.pop(la.KEY_VAR, None)

# ── speech ───────────────────────────────────────────────────────────
BEHAVIOUR["/v1/audio/speech"] = (200, "audio/wav", wav_bytes(22050), 0)
pcm, rate = la.speech("Hello there.", "kokoro", "af_heart", 1.0, url=URL, key=KEY)
_, path, hdr, body = SEEN[-1]
check("localai: speech asks for wav and sends model, input, voice and speed",
      path == "/v1/audio/speech" and hdr.get("content-type") == "application/json"
      and json.loads(body) == {"model": "kokoro", "input": "Hello there.", "voice": "af_heart",
                               "speed": 1.0, "response_format": "wav"})
check("localai: speech sample rate comes from the WAV header", rate == 22050 and len(pcm) == 2400 * 2,
      f"{rate} {len(pcm)}")
for label, payload in [("an html page", b"<html>oops</html>"), ("a stereo wav", wav_bytes(ch=2)),
                       ("a 32-bit wav", wav_bytes(width=4)), ("a truncated wav", wav_bytes()[:30]),
                       ("an empty wav", wav_bytes(frames=0))]:
    BEHAVIOUR["/v1/audio/speech"] = (200, "audio/wav", payload, 0)
    check(f"localai: speech refuses {label} (error, never noise)",
          err(lambda: la.speech("x", "kokoro", "af_heart", 1.0, url=URL, key=KEY)) is not None)

# ── errors ───────────────────────────────────────────────────────────
BEHAVIOUR["/v1/audio/transcriptions"] = (401, "application/json",
                                         json.dumps({"error": {"message": f"bad key {KEY}"}}).encode(), 0)
e = err(lambda: la.transcribe(b"x", "m", "en", url=URL, key=KEY))
check("localai: 401 -> error about the key, key never in the text",
      e is not None and e.status == 401 and "key" in e.reason.lower() and KEY not in e.reason, str(e and e.reason))
BEHAVIOUR["/v1/audio/transcriptions"] = (500, "application/json",
                                         json.dumps({"error": {"message": f"model not found {KEY}"}}).encode(), 0)
e = err(lambda: la.transcribe(b"x", "gone-model", "en", url=URL, key=KEY))
check("localai: 500 -> error carrying the server's short message, key redacted",
      e is not None and e.status == 500 and "model not found" in e.reason and KEY not in e.reason, str(e and e.reason))
t0 = time.monotonic()
e = err(lambda: la.transcribe(b"x", "m", "en", url=f"http://127.0.0.1:{dead_port()}", key=KEY))
check("localai: refused connection -> error, fast", e is not None and e.status is None and time.monotonic() - t0 < 1.5)
BEHAVIOUR["/v1/audio/transcriptions"] = (200, "application/json", b'{"text":"late"}', 7.5)
t0 = time.monotonic()
e = err(lambda: la.transcribe(b"x", "m", "en", url=URL, key=KEY))
dt = time.monotonic() - t0
check("localai: hung server -> gives up near the 6 s STT budget", e is not None and 5.5 <= dt <= 7.0, f"{dt:.2f}s")
BEHAVIOUR["/v1/audio/speech"] = (200, "audio/wav", wav_bytes(), 6.5)
t0 = time.monotonic()
e = err(lambda: la.speech("x", "kokoro", "af_heart", 1.0, url=URL, key=KEY))
dt = time.monotonic() - t0
check("localai: hung server -> speech gives up near its 5 s budget", e is not None and 4.5 <= dt <= 6.0, f"{dt:.2f}s")

# ── discovery ────────────────────────────────────────────────────────
BEHAVIOUR["/v1/models"] = (200, "application/json", json.dumps({"object": "list", "data": [
    {"id": "faster-whisper-small"}, {"id": "kokoro"}, {"id": "qwen"}, {"id": "broken"}]}).encode(), 0)
BEHAVIOUR["/api/models/config-json/faster-whisper-small"] = (200, "application/json", json.dumps(
    {"backend": "faster-whisper", "known_usecases": ["FLAG_TRANSCRIPT"]}).encode(), 0)
BEHAVIOUR["/api/models/config-json/kokoro"] = (200, "application/json", json.dumps(
    {"backend": "kokoro", "known_usecases": ["FLAG_TTS"]}).encode(), 0)
BEHAVIOUR["/api/models/config-json/qwen"] = (200, "application/json", json.dumps(
    {"backend": "llama", "known_usecases": None}).encode(), 0)
d = la.discover(url=URL, key=KEY)
check("localai: discovery sorts STT/TTS by flag and hides unflagged or unreadable models",
      d == {"stt": ["faster-whisper-small"], "tts": [{"id": "kokoro", "backend": "kokoro"}]}, str(d))
ok, ms, why = la.probe(url=URL, key=KEY)
check("localai: probe answers ok with a round-trip time", ok and ms is not None and why is None)
ok, ms, why = la.probe(url=f"http://127.0.0.1:{dead_port()}", key=KEY)
check("localai: probe of a dead box says why", not ok and ms is None and "unreachable" in (why or ""))

# ── shared state + single-flight probe ───────────────────────────────
la._reset_for_tests()
la.set_cooldown(0.2)
check("localai: starts up", la.should_try() and la.down_reason() is None)
la.mark_down("LocalAI unreachable")
check("localai: down -> callers skip it", not la.should_try() and la.down_reason() == "LocalAI unreachable")
calls = []
real_probe = la.probe


def slow_probe(**kw):
    calls.append(1)
    time.sleep(0.3)
    return True, 1.0, None


la.probe = slow_probe
for _ in range(5):
    la.should_try()               # cooldown not yet over: no probe
time.sleep(0.25)
for _ in range(5):
    la.should_try()               # cooldown over: ONE probe thread for all callers
time.sleep(0.5)
check("localai: one background probe at a time, and a good probe marks it up",
      len(calls) == 1 and la.down_reason() is None, f"probes={len(calls)} down={la.down_reason()}")
la.probe = lambda **kw: (False, None, "LocalAI unreachable")
la.mark_down("LocalAI unreachable")
time.sleep(0.25)
la.should_try()
time.sleep(0.1)
check("localai: a failed probe keeps it down", la.down_reason() == "LocalAI unreachable")
la.probe = real_probe
check("localai: status never raises and has its shape", set(la.status()) == {"url", "down", "since"})
la._reset_for_tests()

# ── stt localai ─────────────────────────────────────────────────────
import stt_localai as sla  # noqa: E402


class FakeFW:
    """The appliance model: records what it was asked to transcribe."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language=None, beam_size=1, vad_filter=False):
        self.calls.append((audio.read(), language, beam_size, vad_filter))

        class S:
            text = " local words "

        class I:
            language = "en"
        return [S()], I()


fw = FakeFW()
la._reset_for_tests()
la.set_cooldown(60)
sla._reset_for_tests()
os.environ[la.KEY_VAR] = KEY
os.environ["LVI_LOCALAI_URL"] = URL
BEHAVIOUR["/v1/audio/transcriptions"] = (200, "application/json", b'{"text":" remote words"}', 0)
standin = sla.LocalAIWhisper("faster-whisper-small", lambda: fw)
segs, info = standin.transcribe(io.BytesIO(b"WAV1"), language="en", beam_size=1, vad_filter=False)
check("stt localai: up -> the text comes from LocalAI and the appliance model is untouched",
      [s.text for s in segs] == ["remote words"] and fw.calls == [] and sla.active() == "localai"
      and info.language == "en")
os.environ["LVI_LOCALAI_MODEL_STT"] = "faster-whisper-small"
check("stt localai: status while up says localai + the configured model",
      sla.status() == {"provider": "localai", "model": "faster-whisper-small", "active": "localai"}, str(sla.status()))
os.environ.pop("LVI_LOCALAI_MODEL_STT", None)
BEHAVIOUR["/v1/audio/transcriptions"] = (500, "application/json", b'{"error":{"message":"boom"}}', 0)
segs, info = standin.transcribe(io.BytesIO(b"WAV2"), language="en", beam_size=1, vad_filter=False)
check("stt localai: a LocalAI error sends the SAME clip to the appliance model",
      fw.calls == [(b"WAV2", "en", 1, False)] and [s.text for s in segs] == [" local words "], str(fw.calls))
check("stt localai: then LocalAI is marked down with the reason", "500" in (la.down_reason() or ""))
n = len(SEEN)
standin.transcribe(io.BytesIO(b"WAV3"), language=None)
check("stt localai: while down, later turns skip LocalAI entirely (no request sent)",
      len(SEEN) == n and fw.calls[-1][0] == b"WAV3" and sla.active() == "whisper")
st = sla.status()
check("stt localai: status says whisper fallback + the reason",
      st.get("active") == "whisper" and "500" in (st.get("fallback_reason") or "") and st.get("fallback_rooms") == [])
la.mark_up()
check("stt localai: status goes back to localai as soon as LocalAI is up", sla.status().get("active") == "localai")
la._reset_for_tests()
BEHAVIOUR["/v1/audio/transcriptions"] = (200, "application/json", b'{"text":"x"}', 0)
n = len(SEEN)
fw.calls.clear()
sla.LocalAIWhisper("", lambda: fw).transcribe(io.BytesIO(b"WAV4"), language="en")
check("stt localai: no model picked -> appliance model, nothing sent, reason says so",
      len(SEEN) == n and fw.calls and "model" in (la.down_reason() or ""))
la._reset_for_tests()
os.environ.pop(la.KEY_VAR, None)
n = len(SEEN)
fw.calls.clear()
sla.LocalAIWhisper("m", lambda: fw).transcribe(io.BytesIO(b"WAV5"), language="en")
check("stt localai: no key -> appliance model, nothing sent, reason names System · Keys",
      len(SEEN) == n and fw.calls and "System · Keys" in (la.down_reason() or ""))
la._reset_for_tests()

try:
    import pipecat  # noqa: F401
    HAVE_PIPECAT = True
except Exception:  # noqa: BLE001
    HAVE_PIPECAT = False

if HAVE_PIPECAT:
    import asyncio
    import stt_local
    os.environ[la.KEY_VAR] = KEY
    stt_local._language_hook = lambda: "en"
    _shared_before = stt_local.LocalWhisperSTTService._shared_model
    svc = stt_local.LocalWhisperSTTService(model="base", device="cpu", compute_type="int8", language="en",
                                           model_override=sla.LocalAIWhisper("faster-whisper-small", lambda: fw))
    BEHAVIOUR["/v1/audio/transcriptions"] = (200, "application/json", b'{"text":" Turn on the lights."}', 0)
    out = asyncio.run(svc._transcribe(b"RIFF....WAVE"))
    check("stt localai: through LocalWhisperSTTService the LocalAI text comes out cleaned",
          out.text == "Turn on the lights.", repr(out.text))
    check("stt localai: model_override replaces the shared model and loads nothing",
          svc._fw_model is not None and isinstance(svc._fw_model, sla.LocalAIWhisper)
          and stt_local.LocalWhisperSTTService._shared_model is _shared_before)
    BEHAVIOUR["/v1/audio/transcriptions"] = (200, "application/json", b'{"text":" Thanks for watching!"}', 0)
    out = asyncio.run(svc._transcribe(b"RIFF....WAVE"))
    check("stt localai: the phantom-transcript filter still runs on LocalAI text", out.text == "", repr(out.text))
    asyncio.run(svc.cleanup())
    os.environ.pop(la.KEY_VAR, None)
    la._reset_for_tests()
else:
    print("SKIP stt localai: pipecat not importable here (run on the host)")

# ── tts localai (host: pipecat + kokoro) ────────────────────────────
if HAVE_PIPECAT:
    import asyncio
    import tts_kokoro
    import tts_localai
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
    check("tts localai: the standby voice is the LocalAI voice when it is a Kokoro voice",
          tts_localai.standby_voice_for("af_heart", "am_adam") == "af_heart"
          and tts_localai.standby_voice_for("alba", "am_adam") == "am_adam"
          and tts_localai.standby_voice_for("alba", None) == tts_kokoro.DEFAULT_VOICE
          and tts_localai.standby_voice_for("alba", "not-a-voice") == tts_kokoro.DEFAULT_VOICE)
    assert tts_kokoro.preload(), "Kokoro must load on the host for these checks"
    NOTES = []
    import dashboard.registry as _reg
    _real_note = _reg.note_tts
    _reg.note_tts = lambda room, provider, usable: NOTES.append((room, provider, usable))

    class Mixin:
        _tts_room = "Kitchen"

    def make():
        s = tts_localai.make_service(mixin=Mixin, model="kokoro", voice="af_heart", speed=1.0,
                                     standby_voice="af_heart")
        s._sample_rate = 24000  # what StartFrame sets inside a pipeline
        return s

    async def drain(s, text):
        return [f async for f in s.run_tts(text, "ctx")]

    os.environ[la.KEY_VAR] = KEY
    la._reset_for_tests()
    la.set_cooldown(60)
    LA_WAV = wav_bytes(24000, frames=4800)
    BEHAVIOUR["/v1/audio/speech"] = (200, "audio/wav", LA_WAV, 0)
    svc = make()
    check("tts localai: is a TTS service with the provider name and a raised stop timeout",
          svc._tts_provider == "localai" and svc._stop_frame_timeout_s == tts_kokoro.STOP_FRAME_TIMEOUT_S)
    n0 = len(SEEN)
    frames = asyncio.run(drain(svc, "The first sentence is here. The second one has 3 apples."))
    reqs = [json.loads(s[3]) for s in SEEN[n0:] if s[1] == "/v1/audio/speech"]
    check("tts localai: one request per sentence, numbers spelled before sending",
          len(reqs) == 2 and "three" in reqs[1]["input"] and "3" not in reqs[1]["input"], str(reqs))
    audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
    check("tts localai: LocalAI audio comes through untouched at the same rate",
          len(audio) == 2 and audio[0].audio == LA_WAV[44:] and audio[0].sample_rate == 24000,
          f"{len(audio)} {len(audio[0].audio) if audio else 0}")
    # fail on the SECOND sentence: first from LocalAI, the rest from Kokoro
    calls = {"n": 0}
    real_speech = la.speech

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise la.LocalAIError("LocalAI error 500", 500)
        return real_speech(*a, **k)

    la.speech = flaky
    NOTES.clear()
    svc = make()
    frames = asyncio.run(drain(svc, "This is sentence number one. This is sentence number two. This is sentence number three."))
    la.speech = real_speech
    audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
    check("tts localai: a failed sentence and the rest of the reply are spoken by Kokoro (LocalAI not asked again)",
          calls["n"] == 2 and la.down_reason() == "LocalAI error 500" and audio and audio[0].audio == LA_WAV[44:]
          and len(audio) >= 3 and all(f.audio != LA_WAV[44:] for f in audio[1:]), f"calls={calls['n']} frames={len(audio)}")
    check("tts localai: the dashboard hears 'kokoro (fallback)' once", NOTES == [("Kitchen", "kokoro (fallback)", True)], str(NOTES))
    n1 = len(SEEN)
    frames = asyncio.run(drain(make(), "The next reply sentence."))
    check("tts localai: while down, later replies skip LocalAI (no request) and still speak",
          not any(s[1] == "/v1/audio/speech" for s in SEEN[n1:]) and any(isinstance(f, TTSAudioRawFrame) for f in frames))
    la.mark_up()
    NOTES.clear()
    svc = make()
    svc._noted_engine = "kokoro"
    asyncio.run(drain(svc, "LocalAI is back now."))
    check("tts localai: when LocalAI is back the dashboard hears 'localai' again", NOTES == [("Kitchen", "localai", True)], str(NOTES))
    for label, payload in [("an html page", b"<html>err</html>"), ("a stereo wav", wav_bytes(ch=2))]:
        la._reset_for_tests()
        BEHAVIOUR["/v1/audio/speech"] = (200, "audio/wav", payload, 0)
        frames = asyncio.run(drain(make(), "Garbage comes back for this one."))
        check(f"tts localai: {label} back -> Kokoro speaks it, none of LocalAI's bytes are played",
              la.down_reason() is not None and any(isinstance(f, TTSAudioRawFrame) for f in frames)
              and all(payload[:16] not in getattr(f, "audio", b"") for f in frames))
    la._reset_for_tests()
    BEHAVIOUR["/v1/audio/speech"] = (200, "audio/wav", LA_WAV, 0)
    n2 = len(SEEN)
    s0 = tts_localai.make_service(mixin=Mixin, model="", voice="af_heart", speed=1.0, standby_voice="af_heart")
    s0._sample_rate = 24000
    frames = asyncio.run(drain(s0, "No model was picked for this."))
    check("tts localai: no model picked -> Kokoro, nothing sent, reason says so",
          len(SEEN) == n2 and "model" in (la.down_reason() or "") and any(isinstance(f, TTSAudioRawFrame) for f in frames))
    la._reset_for_tests()
    _saved_k = tts_kokoro._kokoro
    tts_kokoro._kokoro = None
    la.mark_down("LocalAI unreachable")
    frames = asyncio.run(drain(make(), "Nobody can speak this."))
    tts_kokoro._kokoro = _saved_k
    check("tts localai: LocalAI down AND no Kokoro -> an error frame, never a crash",
          any(isinstance(f, ErrorFrame) for f in frames) and not any(isinstance(f, TTSAudioRawFrame) for f in frames))
    _reg.note_tts = _real_note
    os.environ.pop(la.KEY_VAR, None)
    la._reset_for_tests()
else:
    print("SKIP tts localai: pipecat not importable here (run on the host)")

srv.shutdown()
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
