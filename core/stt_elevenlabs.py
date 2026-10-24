"""ElevenLabs Scribe that holds a socket only while a room is talking.

pipecat 1.13 connects ElevenLabsRealtimeSTTService at setup and, after 10 s
without audio, sends 100 ms of silence every 5 s to keep it open — and Scribe
bills that silence as audio. The wake gate only lets audio reach STT while a
room is AWAKE, so "open on the first audio, close after a quiet spell" is
connect-on-wake without STT knowing about the wake word:

- no socket at setup, no keepalive (no silence is ever sent);
- the first audio starts ONE connect and is held (bounded) until Scribe says
  session_started, then sent in order, then audio streams live; a VAD stop
  that arrives meanwhile is held in its place, after the audio it ends;
- IDLE_CLOSE_SECS without audio closes the socket; the next audio reopens it.
  Scribe also hangs up a quiet socket by itself (~15 s): that stays closed
  too, instead of pipecat reconnecting a socket nobody is talking to.

Failure moves the room to Whisper (stt_fallback) at once: a connect that
gets no session (bad key, dead network, timeout) and an account error
(quota, auth, terms) are reported as permanent, so the room never sits
deaf through pipecat's retries or on a provider that refuses every turn.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque

from loguru import logger
from pipecat.frames.frames import ErrorFrame, VADUserStoppedSpeakingFrame
from pipecat.services.elevenlabs.stt import (
    ElevenLabsRealtimeSTTService,
    audio_format_from_sample_rate,
)
from websockets.protocol import State

# Shorter than Scribe's own quiet-socket hang-up (~15 s, seen live), so we
# usually close first; a longer gap only costs one reconnect (~175 ms, held
# audio). Audio flows the whole time a room is awake, so this counts from
# the room going quiet (or the bot's reply muting the mic).
IDLE_CLOSE_SECS = 10.0
# A server close this long after the last audio is Scribe hanging up a quiet
# socket, not a failure: leave it closed, the next audio reopens it.
SERVER_IDLE_SECS = 5.0
# Audio held while the socket opens; overflow drops the oldest.
HOLD_SECS = 10.0
# Handshake + session_started; well inside HOLD_SECS, so a connect can't overflow it.
CONNECT_TIMEOUT_SECS = 5.0
# Scribe refuses a commit with under ~0.3 s of audio since the last one and
# then drops the socket ("Commit request ignored: only 0.26s ..."). A shorter
# turn (a noise blip right after the wake word) is not committed: its audio
# rides along with the next words' commit.
MIN_COMMIT_SECS = 0.35

_COMMIT = object()                        # a held VAD stop, in its place among the audio
# Account-level refusals: no retry can fix these, so Whisper takes the room.
ACCOUNT_ERRORS = frozenset({"auth_error", "quota_exceeded", "unaccepted_terms"})


class OnDemandElevenLabsSTTService(ElevenLabsRealtimeSTTService):
    """ElevenLabsRealtimeSTTService that connects on the first audio and
    closes after IDLE_CLOSE_SECS without it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # pipecat hardcodes a 10 s keepalive for Scribe: billed silence. Off.
        self._keepalive_timeout = None
        self._held: deque = deque()
        self._held_bytes = 0
        self._held_dropped = 0
        self._opening = False
        self._open_task = None
        self._idle_task = None
        self._opened_at = 0.0
        self._session_started = False
        self._session_event = asyncio.Event()
        self._account_error = False
        # The idle close and a new connect take turns: a wake that lands
        # while the close is still tearing down waits for it, then opens a
        # fresh socket (never one the teardown is about to wipe).
        self._conn_lock = asyncio.Lock()
        self._closing = False
        self._uncommitted_bytes = 0          # audio streamed since the last commit

    async def setup(self, setup):
        # The parent's setup connects; skip it and keep the rest.
        await super(ElevenLabsRealtimeSTTService, self).setup(setup)
        self._audio_format = audio_format_from_sample_rate(self.sample_rate)

    async def _websocket_connect(self, uri, **kwargs):
        kwargs.setdefault("open_timeout", CONNECT_TIMEOUT_SECS)
        return await super()._websocket_connect(uri, **kwargs)

    def _socket_open(self) -> bool:
        return self._websocket is not None and self._websocket.state is State.OPEN

    def session_ready(self) -> bool:
        """Scribe answered this connect with a session and no account error
        (recover_stt only switches a room back to a session that is up)."""
        return self._session_started and not self._account_error and self._socket_open()

    def _ms(self, nbytes) -> int:
        return round(nbytes * 1000 / (max(self.sample_rate, 1) * 2))

    def _hold(self, item) -> None:
        self._held.append(item)
        if item is _COMMIT:
            return
        self._held_bytes += len(item)
        cap = HOLD_SECS * max(self.sample_rate, 1) * 2
        while self._held_bytes > cap:
            old = self._held.popleft()
            if old is not _COMMIT:
                self._held_bytes -= len(old)
                if not self._held_dropped:
                    logger.warning(f"stt: elevenlabs still connecting after {HOLD_SECS:g} s of audio "
                                   f"— dropping the oldest")
                self._held_dropped += len(old)

    def _drop_held(self) -> int:
        dropped = self._held_bytes
        self._held.clear()
        self._held_bytes = 0
        return dropped

    async def run_stt(self, audio):
        if self._opening:
            self._hold(audio)
        elif self._closing or self._receive_task is None or self._receive_task.done():
            # No socket (never opened, closed idle, or the server let it go),
            # or the idle close is tearing it down: open a fresh one.
            self._hold(audio)
            self._opening = True
            self._open_task = self.create_task(self._open(), name="elevenlabs_open")
        else:
            # Open: stream. (While pipecat's receive loop reconnects a dropped
            # socket, its run_stt drops the audio, as it always has.)
            async for frame in self._stream(audio):
                yield frame
            return
        yield None

    async def _stream(self, audio):
        """pipecat's send, counting what the open socket actually got."""
        if self._socket_open():
            self._uncommitted_bytes += len(audio)
        async for frame in super().run_stt(audio):
            yield frame

    async def _open(self):
        t0 = time.monotonic()
        try:
            self._held_dropped = 0
            async with self._conn_lock:       # after any idle close still tearing down
                if self._receive_task is not None and self._receive_task.done():
                    self._receive_task = None  # the old loop ended: _connect starts a new one
                await self._connect()
            try:
                await asyncio.wait_for(self._session_event.wait(),
                                       max(CONNECT_TIMEOUT_SECS - (time.monotonic() - t0), 0.01))
            except TimeoutError:
                pass
            ms = round((time.monotonic() - t0) * 1000)
            if not self._session_started:
                # Rejected key, dead network or no answer. Waiting out pipecat's
                # reconnect ladder would leave the room deaf for ~25 s: hand it
                # to Whisper now (stt_fallback's recovery brings Scribe back).
                # This turn's held audio is lost.
                logger.warning(f"stt: elevenlabs no session after {ms} ms "
                               f"({self._ms(self._drop_held())} ms of audio dropped)")
                if not self._account_error:   # that one has reported itself
                    await self._report_error(ErrorFrame(f"ElevenLabs STT: no session after {ms} ms"),
                                             force_treat_as_permanent=True)
                async with self._conn_lock:
                    await ElevenLabsRealtimeSTTService._disconnect(self)   # stop the retries
                return
            logger.info(f"stt: elevenlabs connected in {ms} ms ({self._ms(self._held_bytes)} ms of audio buffered)")
            # Audio (and a VAD stop) arriving during the sends below is held
            # behind them, so the loop drains it in order.
            while self._held:
                item = self._held.popleft()
                if item is _COMMIT:
                    await self._send_commit()
                else:
                    self._held_bytes -= len(item)
                    await self.process_generator(self._stream(item))
        finally:
            self._drop_held()                 # empty unless the open broke off: never stream stale audio later
            self._opening = False
            self._open_task = None

    async def _send_commit(self):
        # Every manual commit comes through here (the parent's own commit on a
        # VAD stop is skipped in process_frame).
        if not self._socket_open():
            return
        secs = self._uncommitted_bytes / (2 * max(self.sample_rate, 1))
        if secs < MIN_COMMIT_SECS:
            logger.debug(f"stt: elevenlabs commit skipped: {secs:.2f} s of audio is under Scribe's minimum")
            return
        try:
            await self._websocket.send(json.dumps({"message_type": "input_audio_chunk", "audio_base_64": "",
                                                   "commit": True, "sample_rate": self.sample_rate}))
            self._uncommitted_bytes = 0
        except Exception as e:  # noqa: BLE001 — the receive loop handles a dead socket
            logger.warning(f"stt: elevenlabs commit not sent: {e}")

    async def process_frame(self, frame, direction):
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            held = self._opening
            if held:
                # Held before any await, so it can't slip past the audio still held.
                self._hold(_COMMIT)
            # Skip the parent's commit (it has no minimum-length check).
            await super(ElevenLabsRealtimeSTTService, self).process_frame(frame, direction)
            if not held:
                await self._send_commit()
            return
        await super().process_frame(frame, direction)

    async def _process_response(self, data):
        kind = data.get("message_type")
        if kind == "session_started":
            self._session_started = True
            self._session_event.set()
        elif kind in ACCOUNT_ERRORS:
            # pipecat reports these as ordinary errors, which never fail over:
            # out of credits, every room would stay on Scribe and hear nothing.
            # The server's text can echo request details: report the kind only.
            self._account_error = True
            self._session_event.set()
            await self._report_error(ErrorFrame(f"ElevenLabs STT refused the account ({kind})"),
                                     force_treat_as_permanent=True)
            return
        await super()._process_response(data)

    async def _maybe_try_reconnect(self, error_message, report_error, error=None):
        if self._account_error:
            return False                      # reported; retrying can't fix it
        if (not self._opening and not self._disconnecting and self._session_started
                and time.monotonic() - self._last_audio_time >= SERVER_IDLE_SECS):
            # Scribe hung up a quiet socket: not a failure, and no reason to
            # reconnect until someone talks (run_stt reopens on the next audio).
            logger.info("stt: elevenlabs closed by the server while quiet — reopens on the next audio")
            return False
        return await super()._maybe_try_reconnect(error_message, report_error, error)

    async def _receive_messages(self):
        try:
            await super()._receive_messages()
        finally:
            self._session_event.set()         # socket gone: stop waiting for session_started

    async def _connect(self):
        # Every connect (on demand or recover_stt's) starts unanswered.
        self._session_started = False
        self._uncommitted_bytes = 0          # a new socket has nothing to commit
        self._account_error = False
        self._session_event.clear()
        await super()._connect()
        self._opened_at = time.monotonic()
        if self._idle_task is None:
            self._idle_task = self.create_task(self._close_when_idle(), name="elevenlabs_idle")

    def _idle_for(self) -> float:
        return time.monotonic() - max(self._last_audio_time, self._opened_at)

    def _may_close(self) -> bool:
        # Never while a connect or pipecat's reconnect is running: a key or
        # network that keeps failing must reach the permanent error (Whisper).
        return self._idle_for() >= IDLE_CLOSE_SECS and not (self._opening or self._reconnect_in_progress)

    async def _close_when_idle(self):
        while True:
            if self._may_close():
                async with self._conn_lock:
                    if self._may_close():     # still quiet now that it's our turn
                        idle = self._idle_for()
                        self._idle_task = None    # this task is the one closing
                        self._closing = True      # audio meanwhile opens a fresh socket after us
                        try:
                            if self._socket_open():
                                logger.info(f"stt: elevenlabs idle {idle:.0f} s — closed until the next audio")
                            # pipecat's teardown only: the held audio and any
                            # connect waiting on the lock belong to the next wake.
                            await ElevenLabsRealtimeSTTService._disconnect(self)
                        finally:
                            self._closing = False
                        return
            await asyncio.sleep(max(IDLE_CLOSE_SECS - self._idle_for(), 0.25))

    async def _disconnect(self):
        for task in (self._open_task, self._idle_task):
            if task is not None:
                await self.cancel_task(task)
        self._open_task = self._idle_task = None
        self._opening = False
        self._closing = False
        self._drop_held()
        await super()._disconnect()
