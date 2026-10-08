"""hermes_agent — lenoxvi's real brain: the Hermes agent over the LAN API.

Replaces brain_stub.py in the pipeline (Phase 3). Same turn shape — buffers
user speech, waits for the end-of-turn lull, then gets a reply — but the reply
comes from the Hermes API server (OpenAI-compatible, bearer auth) instead of a
canned echo. The agent keeps its full toolset + memory; our system prompt
layers voice-appropriate instructions on top (short, spoken-word replies).

Config (config.yaml `brain:` section; .env holds the key only):
    brain.url / brain.model / brain.zone / brain.lull_secs /
    brain.dedup_window / brain.fillers — see config.example.yaml
    LVI_HERMES_KEY (.env) — the API server's bearer key
"""

import asyncio
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Supersede-ack rate limit (2026-10-07, the a kid session): consecutive
# supersedes in a two-person conversation said "Hold on — switching."
# three times in ten seconds. One ack per wake window: after speaking it,
# further supersedes inside this gap stay silent (the turn still
# switches — only the announcement is muted). Re-armed on every fresh
# turn. Config: brain.supersede_ack_gap / LVI_SUPERSEDE_ACK_GAP.
ACK_GAP = float(os.getenv("LVI_SUPERSEDE_ACK_GAP", "45"))

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TTSSpeakFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

VOICE_SYSTEM_PROMPT = (
    "You are the house voice assistant, currently talking to the user over speakers. "
    "Replies are spoken aloud by TTS, so keep them SHORT — one to three sentences, "
    "plain conversational English. No markdown, no bullet points, no emojis, no "
    "URLs. You have all your normal tools and memory; use them when asked, but "
    "never read out raw command output — summarize it conversationally."
)

# TTS audio tags ([laughs] etc.) render on eleven_v3 but flash_v2_5 speaks them
# literally — strip from spoken text until the TTS backend renders tags.
_TAG_RE = re.compile(r"\[[^\]]{1,20}\]")


def _normalize(text: str) -> str:
    """Lowercase alphanumerics only — for echo containment checks."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub(" ", text).strip()


# Tiered fillers (option B, 2026-10-03): one spoken line per latency tier so
# a slow brain turn doesn't go silent after the first ack. Tool-heavy turns
# measure 20-40s+ server-side (2026-09-15); the old single-filler design
# spent its only line at 3s and left the rest as dead air.
# Tier contract (every line, any pool):
#   - <2s spoken (they ride the duck holder)
#   - >=3 words so echo containment can catch the Sonos echo (the
#     2026-09-15 double-fire lesson; 2-word "On it." is grandfathered —
#     the echo gate covers its own playback window)
#   - never command-shaped: a leaked filler reaches the brain as USER text
# Config shape (brain.fillers) — tiered list (preferred):
#     - after: 3.0    # seconds from turn start, cumulative
#       lines: [...]
# A flat string list still works = single ack tier at brain.filler_after_secs
# (legacy configs keep behaving exactly as before). This default is the
# fallback when config has no fillers key.
_DEFAULT_TIERS = [
    (3.0, ["On it.", "Working on it…", "Give me a sec.", "Let me check…", "One moment."]),
    (12.0, ["Still on it — this one's a slow one.", "Still working — it's a deep one.",
            "Still with you — taking a minute."]),
    (25.0, ["Still here — it's a big one.", "Still here — give me a few more seconds.",
            "Not done yet — hang tight."]),
]

_LAST_FILLER = ""


def _filler_tiers() -> list[tuple[float, list[str]]]:
    """Normalized filler tiers [(after_secs, [lines...]), ...], sorted.

    Accepts the tiered dict shape or a legacy flat string list (one ack
    tier at brain.filler_after_secs); falls back to _DEFAULT_TIERS.
    """
    from config import cfg, cfg_env
    raw = cfg("brain", "fillers")
    tiers: list[tuple[float, list[str]]] = []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        for t in raw:
            try:
                after = float(t.get("after", 0) or 0)
                lines = [str(x).strip() for x in (t.get("lines") or []) if str(x).strip()]
            except (TypeError, ValueError):
                continue
            if lines and after > 0:
                tiers.append((after, lines))
        tiers.sort(key=lambda x: x[0])
    elif isinstance(raw, list) and raw and all(isinstance(x, str) for x in raw):
        # legacy flat pool -> one ack tier (timing knob: filler_after_secs)
        after = float(cfg_env("brain", "filler_after_secs", "LVI_FILLER_AFTER_SECS", 3.0) or 3.0)
        tiers.append((after, [x.strip() for x in raw if x.strip()]))
    return tiers or _DEFAULT_TIERS


def _pick_filler(pool: list[str]) -> str:
    """One line from `pool`, never the same line twice in a row."""
    global _LAST_FILLER
    choice = random.choice(pool)
    # avoid the same filler twice in a row (still possible on a 1-item pool)
    if len(pool) > 1 and choice == _LAST_FILLER:
        choice = random.choice([f for f in pool if f != choice])
    _LAST_FILLER = choice
    return choice


# Standalone wake-phrase transcriptions (the wake word repeated after the
# gate opened — observed live 2026-09-15: "Hey Linux." went to the brain).
# If the utterance is just the wake phrase, it's not a command: drop it.
_WAKE_PHRASE_RE = re.compile(
    r"^\s*(hey|ok|okay|hi)?\s*,?\s*(willow|linux|yellow)\s*[.!?]?\s*$", re.IGNORECASE
)


def _is_wake_phrase_only(text: str) -> bool:
    return bool(_WAKE_PHRASE_RE.match(text))


class _SessionStash:
    """Disk stash for sticky-room conversation history (data/sessions.json).

    Written after every successful turn (crash-safe — no teardown hook
    needed: reconnects AND bot restarts both restore from disk). The file
    holds plain conversation text and is gitignored; sticky rooms opt in
    per node via the dashboard checkbox (config `session_modes`).
    """

    def __init__(self, path: str | None = None):
        self._path = Path(path or os.getenv("LVI_SESSIONS", "data/sessions.json"))

    def load(self, room: str) -> list:
        try:
            data = json.loads(self._path.read_text())
            hist = data.get(room) or []
            return hist if isinstance(hist, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, room: str, history: list) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
            data[room] = history
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"hermes_agent: session stash write failed: {e}")


_session_stash = _SessionStash()

# Reasoning-preamble meter (design note): [count] as a mutable cell so the
# lane can count leaks without rebinding a module global per hit.
_PREAMBLE_HITS = [0]


class HermesAgent(FrameProcessor):
    """Buffers user speech; on end-of-turn lull, asks the Hermes API for a reply."""

    def __init__(self, lull_secs: float = 0.6, max_history: int = 10, echo_gate=None,
                 room: str = None, session_mode: str = "fresh", **kwargs):
        super().__init__(**kwargs)
        from config import cfg_env

        self._url = cfg_env("brain", "url", "LVI_HERMES_URL", "http://<brain-host>:8642/v1")
        # Intelligence lane (design note): brain.kind picks the request
        # shape. hermes = today's api_server contract; openclaw = OpenClaw
        # Gateway (model "openclaw/<agent>", x-openclaw-session-key, no
        # `conversation` field). The UI calls this "Intelligence" — the
        # word "brain" never reaches the dashboard.
        self._kind = str(cfg_env("brain", "kind", "LVI_BRAIN_KIND", "hermes") or "hermes").lower()
        self._agent = ""
        if self._kind == "openclaw":
            self._agent = str(cfg_env("brain", "agent", "LVI_BRAIN_AGENT", "") or "")
        # Key resolution (design note): LVI_BRAIN_KEY first, LVI_HERMES_KEY
        # fallback — zero migration for existing installs. Missing key is
        # NOT a construction brick (audit finding #4 class, the provider-
        # switch-without-key incident): the turn fails with a clear error
        # and the dashboard Test button reports it.
        # Bug-7 law (2026-10-07): the key is KIND-AWARE. Resolution
        # (renamed 2026-10-08, the owner's ruling — "each brain's key names
        # what it is"): hermes → LVI_HERMES_KEY (with LVI_BRAIN_KEY legacy
        # fallback); openclaw → LVI_OPENCLAW_KEY (LVI_BRAIN_KEY legacy
        # fallback — the old generic-slot name — then LVI_HERMES_KEY,
        # original note-032 order). Future lanes follow the law:
        # LVI_CLAUDE_KEY, LVI_CUSTOM_KEY — a lane's key names its brain.
        if self._kind == "hermes":
            self._key = (os.getenv("LVI_HERMES_KEY")
                         or os.getenv("LVI_BRAIN_KEY")
                         or None)
        else:
            self._key = (os.getenv("LVI_OPENCLAW_KEY")
                         or os.getenv("LVI_BRAIN_KEY")
                         or os.getenv("LVI_HERMES_KEY")
                         or None)
        if not self._key and self._kind != "stub":
            logger.warning(
                "hermes_agent: no brain key set (LVI_BRAIN_KEY / LVI_HERMES_KEY) "
                f"— kind={self._kind}; turns will fail until one is set")
        self._model = cfg_env("brain", "model", "LVI_HERMES_MODEL", "hermes-agent")
        # Output cap (2026-10-04, the 16k-degenerate-reply incident): the
        # brain request carried no max_tokens, so a runaway model loop
        # could monologue until the context window died (measured: one
        # 16,145-token reply "degenerated into garbage" mid-session).
        # ~300 tokens ≈ 220 words ≈ 90s of speech — generous for sass,
        # impossible for a runaway. Honored by every OpenAI-compat
        # endpoint (Hermes, Anthropic, OpenClaw — forward-compatible
        # with the brain.kind arc). Config-gated per house law.
        self._max_tokens = int(cfg_env("brain", "max_tokens", "LVI_BRAIN_MAX_TOKENS", 300) or 300)
        # Turn-shape tunables — the knobs closest to conversation UX. Same
        # precedence as every other knob: config.yaml > LVI_* env > default.
        self._lull_secs = float(cfg_env("brain", "lull_secs", "LVI_LULL_SECS", lull_secs))
        self._max_history = max_history
        self._echo_gate = echo_gate
        self._buffer: list[str] = []
        self._history: list[dict] = []
        self._last_transcription: float = 0.0
        self._speaking = False
        self._flushed = False
        # Dedup: same utterance re-heard inside this window is treated as an
        # echo/repeat (observed live: the lull-timer can double-fire while the
        # bot is still speaking a slow reply).
        self._last_user_text: str = ""
        self._last_user_time: float = 0.0
        self._dedup_window: float = float(cfg_env("brain", "dedup_window", "LVI_DEDUP_WINDOW", 10.0))
        # B3 epoch turns (design note): one live turn per pipeline,
        # spawned as a background task; a new question supersedes.
        self._turn_task: asyncio.Task | None = None
        self._turn_epoch: int = 0
        self._turn_started: float = 0.0
        # Supersede-ack rate limit (2026-10-07, the a kid session): the ack
        # ("Hold on — switching.") is useful for one deliberate interrupt,
        # but in a two-person conversation consecutive utterances supersede
        # repeatedly and she says it three times in ten seconds — sounds
        # broken. Once per wake window is plenty: the state flip from the
        # old turn dying still tells the user something changed. Reset on
        # a fresh turn start after a quiet gap (no live turn at supersede
        # time = the window rolled over).
        self._supersede_ack_ts: float = 0.0
        # Echo containment: her last spoken texts (reply + filler) — a
        # transcription that contains a chunk of these IS her echo, whenever
        # it arrives (duration gates lose the race when her Sonos reply talks
        # over the mic and whisper transcribes late; measured live 2026-09-15).
        self._echo_texts: list[str] = []
        # Session modes (fresh | sticky, per node — dashboard checkbox,
        # 2026-10-03): sticky rooms keep conversation memory across node
        # reconnects AND bot restarts via a disk stash; fresh rooms start
        # every session clean (privacy default — a bedroom mic must not
        # accumulate memory nobody asked for).
        self._room = room
        self._sticky = session_mode == "sticky"
        if self._sticky and room:
            self._conversation = f"voice-{room}"
            self._history = _session_stash.load(room)[: self._max_history]
            logger.info(
                f"hermes_agent: sticky session '{self._conversation}' "
                f"restored {len(self._history)} history entries"
            )
        else:
            if self._sticky and not room:
                # Sticky without a correlated room (offer-tee race, shouldn't
                # happen): fail SAFE, not silent — fresh beats guessing which
                # room's stash to load.
                logger.warning("hermes_agent: sticky mode without a room — falling back to fresh")
            # Server-side conversation chain (per-connection): "voice-<zone>-<id>"
            self._conversation = os.getenv(
                "LVI_CONVERSATION",
                f"voice-{cfg_env('brain', 'zone', 'LVI_ZONE', 'zone')}-{uuid.uuid4().hex[:8]}"
            )
        logger.info(f"hermes_agent: conversation '{self._conversation}'"
                    f"{' (sticky)' if self._sticky and room else ''}")

    def _is_own_echo(self, text: str) -> bool:
        """True if `text` contains a meaningful chunk of her own last words.

        An echo is her words coming back out of the mic — so containment beats
        timing. True when >= 60% of the words (minimum 3) appear in one of
        her recent spoken texts, in any order.
        """
        norm = _normalize(text)
        if not norm or not self._echo_texts:
            return False
        words = norm.split()
        if len(words) < 3:
            return False
        for spoken in self._echo_texts:
            spoken_words = set(_normalize(spoken).split())
            hits = sum(1 for w in words if w in spoken_words)
            if words and hits / len(words) >= 0.6:
                return True
        return False

    def _ask_hermes(self, text: str) -> str:
        """One chat completion; returns the reply text.

        Intelligence lane (design note): kind=openclaw reshapes the request —
        model "openclaw/<agent>" addresses the agent, x-openclaw-session-key
        carries sticky continuity, and the Hermes-only `conversation` field
        is omitted. kind=hermes keeps today's contract (conversation +
        X-Hermes-Session-Key). The messages array, output caps, client-side
        history and fillers are shared — the lanes differ only in envelope.
        """
        if not self._key:
            raise RuntimeError("no brain key set (LVI_BRAIN_KEY / LVI_HERMES_KEY)")
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        if self._kind == "openclaw":
            model = f"openclaw/{self._agent}" if self._agent else "openclaw"
            payload = {
                "model": model,
                "stream": False,
                "max_tokens": self._max_tokens,
                "messages": [
                    {"role": "system", "content": VOICE_SYSTEM_PROMPT},
                    *self._history,
                    {"role": "user", "content": text},
                ],
            }
            # Sticky continuity rides the header (proven live 2026-10-05:
            # seed/recall under the same key). Fresh sends nothing — and
            # the agent's own memory files may still recall earlier facts
            # (documented boundary, README row).
            if self._sticky and self._room:
                headers["x-openclaw-session-key"] = self._conversation
        else:
            payload = {
                "model": self._model,
                "stream": False,
                "max_tokens": self._max_tokens,
                "conversation": self._conversation,
                "messages": [
                    {"role": "system", "content": VOICE_SYSTEM_PROMPT},
                    *self._history,
                    {"role": "user", "content": text},
                ],
            }
            # X-Hermes-Session-Key (2026-10-04): the api_server keys
            # dashboard-visible session continuity on THIS header, not
            # the body's `conversation` field — the label in the body
            # alone left voice sessions invisible in the Hermes admin
            # dash (the owner's audit need). With the header, sticky rooms
            # merge into ONE long-lived dashboard session per room and
            # fresh pipelines get one session per connection — both
            # visible and auditable. Requires API_SERVER_KEY on the
            # brain (present on this install).
            headers["X-Hermes-Session-Key"] = self._conversation
        req = urllib.request.Request(
            f"{self._url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        reply = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        # Reasoning-preamble meter (design note §5 layer 2): the OpenClaw agent
        # on GLM 5.3 intermittently merges planning text into `content`
        # (measured live 2026-10-05, 2 of 8 spike calls). A voice assistant
        # speaks content verbatim — so MEASURE, never silently strip (no
        # delimiter exists; a heuristic strip risks eating real answers).
        # Data first, fix decided later (source suppression preferred).
        if self._kind == "openclaw" and reply[:24].startswith((
                "The user", "I need to", "I should", "Let me", "Okay, the user",
                "The human", "Looking at", "First, I", "I'll ")):
            _PREAMBLE_HITS[0] += 1
            logger.warning(
                "hermes_agent: openclaw reasoning preamble leaked into content "
                f"({_PREAMBLE_HITS[0]} this process) — reply[:80]={reply[:80]!r}")
        # Client-side output cap (2026-10-04): Hermes's api_server IGNORES
        # the request's max_tokens (verified live: max_tokens:5 returned a
        # 360-char reply with finish_reason stop; api_server.py has zero
        # references). Real OpenAI-compat endpoints honor the field, but
        # our PRIMARY brain doesn't — so the client enforces too. Char
        # budget ≈ 4 chars/token × max_tokens; truncate at the last word
        # boundary so she never mid-words the speaker.
        char_cap = self._max_tokens * 4
        if len(reply) > char_cap:
            logger.warning(
                f"hermes_agent: reply {len(reply)} chars exceeded cap "
                f"{char_cap} (max_tokens={self._max_tokens}) — truncated"
            )
            reply = reply[:char_cap].rsplit(maxsplit=1)[0].rstrip(",;:-—") + "."
        logger.info(
            f"hermes_agent: tokens in={usage.get('prompt_tokens', '?')} "
            f"out={usage.get('completion_tokens', '?')}"
        )
        return reply

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            self._flushed = False
        elif isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                if self._echo_gate and self._echo_gate.blocked():
                    logger.debug(f"hermes_agent: echo-gate dropped [{frame.text.strip()}]")
                elif self._is_own_echo(frame.text):
                    logger.info(f"hermes_agent: containment dropped echo [{frame.text.strip()[:60]}]")
                else:
                    self._buffer.append(frame.text.strip())
                    self._last_transcription = time.monotonic()

        if (
            self._speaking
            and self._flushed is False
            and self._buffer
            and self._last_transcription > 0
            and (time.monotonic() - self._last_transcription) >= self._lull_secs
        ):
            self._speaking = False
            self._flushed = True
            text = " ".join(self._buffer).strip()
            self._buffer.clear()
            if text:
                if _is_wake_phrase_only(text):
                    logger.info(f"hermes_agent: dropped wake-phrase-only [{text}]")
                else:
                    now = time.monotonic()
                    if text == self._last_user_text and (now - self._last_user_time) < self._dedup_window:
                        logger.info(f"hermes_agent: dedup dropped repeat [{text[:60]}]")
                        self._speaking = False
                        self._flushed = True
                    else:
                        self._last_user_text = text
                        self._last_user_time = now
                        # ── B3 epoch turns (design note) ──
                        # The brain call runs as a BACKGROUND task; the
                        # frame loop returns immediately and never
                        # blocks on a slow brain (the 12:12 collision
                        # and the 09-30 stuck-LED class share this root
                        # cause). A new question supersedes: old task
                        # cancelled, its late reply discarded by the
                        # epoch check at every await, no history write.
                        superseded = False
                        if self._turn_task is not None and not self._turn_task.done():
                            superseded = True
                            self._turn_task.cancel()
                            try:
                                await asyncio.shield(self._turn_task)
                            except (asyncio.CancelledError, Exception):
                                pass
                            logger.info(
                                f"hermes_agent: turn superseded after "
                                f"{time.monotonic() - self._turn_started:.1f}s — "
                                f"reply discarded, no history write")
                        self._turn_epoch += 1
                        epoch = self._turn_epoch
                        self._turn_started = time.monotonic()
                        self._turn_task = asyncio.create_task(
                            self._run_turn(text, epoch))
                        if superseded:
                            from config import cfg_env as _cfg_env
                            ack = str(_cfg_env("brain", "supersede_ack",
                                               "LVI_SUPERSEDE_ACK",
                                               "Hold on — switching.") or "")
                            now_ack = time.monotonic()
                            if ack and (now_ack - self._supersede_ack_ts) >= ACK_GAP:
                                self._supersede_ack_ts = now_ack
                                await self.push_frame(TTSSpeakFrame(text=ack))
                                self._echo_texts.append(ack)
                                self._echo_texts = self._echo_texts[-4:]
                            # else: silent supersede — the turn still
                            # switches; only the announcement is rate-
                            # limited (or the ack is disabled via config).
                        else:
                            # Fresh turn (no live task to supersede) → new
                            # conversational turn: re-arm so a deliberate
                            # interrupt here still gets its announcement.
                            self._supersede_ack_ts = 0.0

        # EVERY frame passes through (B3 regression, 2026-10-05 17:09):
        # this line was lost in the _run_turn extraction churn — the agent
        # ate StartFrame, PipelineWorker timed out at +20s, and the node
        # rode a reconnect loop ("you wont wake"). Non-negotiable.
        await self.push_frame(frame, direction)

    async def _run_turn(self, text: str, epoch: int) -> None:
        """One turn as a background task (design note). Every await
        checks the epoch — a superseded turn discards its reply, skips
        the history write, and dies quietly. CancelledError propagates
        (task.cancel from the supersede path); everything else is the H3
        broad-catch: the user never gets dead silence on a brain failure."""
        filler_spoken = False
        try:
            ask = asyncio.create_task(asyncio.to_thread(self._ask_hermes, text))
            failed = False
            reply = None
            # Tiered fillers (option B, 2026-10-03): wait to each tier's
            # cumulative `after` mark; on timeout speak ONE line from
            # that tier's pool and keep waiting.
            tiers = _filler_tiers()
            turn_start = time.monotonic()
            for i, (after, pool) in enumerate(tiers):
                remaining = turn_start + after - time.monotonic()
                if remaining > 0:
                    try:
                        reply = await asyncio.wait_for(
                            asyncio.shield(ask), timeout=remaining
                        )
                        break
                    except TimeoutError:
                        if epoch != self._turn_epoch:
                            ask.cancel()
                            return  # superseded mid-filler — die quietly
                        filler = _pick_filler(pool)
                        await self.push_frame(TTSSpeakFrame(text=filler))
                        self._echo_texts.append(filler)
                        filler_spoken = True
                        logger.info(
                            f"hermes_agent: filler tier {i + 1}/{len(tiers)} -> [{filler}]"
                        )
            if reply is None:
                # every tier spent, brain still working — wait it out
                reply = await ask
            if epoch != self._turn_epoch:
                return  # superseded while thinking — discard, no history
            reply = _strip_tags(reply)
            self._echo_texts.append(reply)
            self._echo_texts = self._echo_texts[-4:]
            # A failed call never reached the brain — don't write the
            # exchange into server-side history (Opus rider).
            if not failed:
                self._history.append({"role": "user", "content": text})
                self._history.append({"role": "assistant", "content": reply})
                self._history = self._history[-self._max_history:]
                # Sticky rooms persist after every successful turn.
                if self._sticky and self._room:
                    _session_stash.save(self._room, self._history)
            logger.info(f"hermes_agent: [{text}] -> [{reply[:120]}]")
            await self.push_frame(TTSSpeakFrame(text=reply))
        except asyncio.CancelledError:
            raise  # supersede path — die quietly, no user-facing output
        except Exception as e:
            # H3 (Opus review 2026-09-30): the brain can fail at ANY
            # point; ONE broad catch guards the whole turn — the last
            # defense before user-facing silence. A superseded turn that
            # fails is still superseded: no history write either way.
            logger.error(f"hermes_agent: brain call failed"
                         f"{' after filler' if filler_spoken else ''}: {e}")
            if epoch != self._turn_epoch:
                return
            await self.push_frame(
                TTSSpeakFrame(text="Sorry — I can't reach my brain right now."))