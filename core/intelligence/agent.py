"""intelligence_agent — LenoxVI's intelligence step: turns speech into a reply.

The real counterpart of intelligence/stub.py, same turn shape: buffers user
speech, waits for the end-of-turn lull, plays fillers while it waits, filters
echo/dedup/wake-phrase-only turns, keeps per-room history, and asks the
configured intelligence over its OpenAI-compatible chat API (bearer auth).
`intelligence.kind` picks the lane (core/intelligence/: `hermes`, `openclaw`,
`nanobot`, ...); the lane owns its request shape and quirks, and everything
else here is shared. The agent behind the API keeps its own
tools + memory; our system prompt layers voice-appropriate instructions on
top (short, spoken-word replies) — except on nanobot, which accepts no
system message: its agent's own instructions set the spoken style.

Config (config.yaml `intelligence:` section; .env holds the key only):
    intelligence.kind / url / agent / model / zone / lull_secs /
    dedup_window / fillers — see config.example.yaml
    Key: LVI_HERMES_KEY (kind hermes), LVI_OPENCLAW_KEY (kind openclaw) or
    LVI_NANOBOT_KEY (kind nanobot)
"""

import asyncio
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    Frame,
    InterimTranscriptionFrame,
    TTSSpeakFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import intelligence
from paths import sessions_file

VOICE_SYSTEM_PROMPT = (
    "You are the house voice assistant, currently talking to the user over speakers. "
    "Replies are spoken aloud by TTS, so keep them SHORT — one to three sentences, "
    "plain conversational English. No markdown, no bullet points, no emojis, no "
    "URLs. You have all your normal tools and memory; use them when asked, but "
    "never read out raw command output — summarize it conversationally."
)

def _normalize(text: str) -> str:
    """Lowercase alphanumerics only — for echo containment checks."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# Tiered fillers (option B): one spoken line per latency tier so
# a slow intelligence turn doesn't go silent after the first ack. Tool-heavy turns
# measure 20-40s+ server-side; the old single-filler design
# spent its only line at 3s and left the rest as dead air.
# Tier contract (every line, any pool):
#   - <2s spoken (they ride the duck holder); the last tier's long-wait
#     line, which promises a report, may run ~3s
#   - >=3 words so echo containment can catch the Sonos echo (the
# double-fire lesson; 2-word "On it." is grandfathered —
#     the echo gate covers its own playback window)
#   - never command-shaped: a leaked filler reaches the intelligence as USER text
# Config shape (intelligence.fillers) — tiered list (preferred):
#     - after: 3.0    # seconds from turn start, cumulative
#       lines: [...]
# A flat string list still works = single ack tier at intelligence.filler_after_secs
# (legacy configs keep behaving exactly as before). This default is the
# fallback when config has no fillers key.
_DEFAULT_TIERS = [
    (3.0, ["On it.", "Working on it…", "Give me a sec.", "Let me check…", "One moment."]),
    (12.0, ["Almost there now.", "Still working on that.", "Taking a minute."]),
    # Long wait: past here a tool-heavy turn can run for minutes. If the
    # user moves on meanwhile, a call already running reports late
    # (intelligence.late_reply_prefix), so the line promises that report.
    (30.0, ["This one's taking a while. I'll tell you when it's done.",
            "Still on it. I'll let you know when it's done.",
            "This is a long one. I'll tell you when it's ready."]),
]

_LAST_FILLER = ""


# Intelligence calls get their own threads. A superseded or hung call can't
# be cancelled (blocking urlopen, up to the 120 s timeout); on the shared
# default pool (cpu+4 threads) a pile of them starved wake-word scoring in
# every room.
_INTEL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lvi-intel")

# Segmented STT: after VAD stop + lull, how much longer to wait for a
# stretch's transcript before closing on what's already buffered.
SEGMENT_TRANSCRIPT_GRACE = 1.5
# A transcript that lands after the turn closed, while that turn is still
# thinking (no reply yet) and within this window, is the rest of the same
# question (a slow STT stretch outlived the grace): the turn restarts on the
# joined text, silently. Later than this, or once she has replied, it's
# dropped as before.
LATE_JOIN_WINDOW = 20.0


def _filler_tiers() -> list[tuple[float, list[str]]]:
    """Normalized filler tiers [(after_secs, [lines...]), ...], sorted.

    Accepts the tiered dict shape or a legacy flat string list (one ack
    tier at intelligence.filler_after_secs); falls back to _DEFAULT_TIERS.
    """
    from config import cfg, cfg_env
    raw = cfg("intelligence", "fillers")
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
        after = float(cfg_env("intelligence", "filler_after_secs", "LVI_FILLER_AFTER_SECS", 3.0) or 3.0)
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
# gate opened — observed live: a bare, mis-spelled "Hey <name>." went to the
# intelligence). If the utterance is just the wake phrase, it's not a command:
# drop it. The name, its sound-alike spellings and the whole-utterance match
# live in wake_name (config-driven, applies live).
def _is_wake_phrase_only(text: str) -> bool:
    import wake_name
    return wake_name.is_wake_phrase_only(text)


class _SessionStash:
    """Disk stash for sticky-room conversation history (data/sessions.json).

    Written after every successful turn (crash-safe — no teardown hook
    needed: reconnects AND bot restarts both restore from disk). The file
    holds plain conversation text and is gitignored; sticky rooms opt in
    per node via the dashboard checkbox (config `session_modes`).
    """

    def __init__(self, path: str | None = None):
        # explicit path wins; else paths anchors it (LVI_SESSIONS, then
        # repo/LVI_STATE_DIR) instead of the cwd
        self._path = Path(path) if path else sessions_file()
        # Saves run in worker threads (off the event loop): two rooms saving
        # at once must not lose each other's read-modify-write or share the
        # temp file mid-write. Re-entrant: a live session holds it while it
        # resolves its room name and saves (IntelligenceAgent._save_history).
        self._lock = threading.RLock()

    def load(self, room: str) -> list:
        try:
            data = json.loads(self._path.read_text())
            hist = data.get(room) or []
            return hist if isinstance(hist, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, room: str, history: list) -> None:
        with self._lock:
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
                logger.warning(f"intelligence_agent: session stash write failed: {e}")

    def rename(self, old: str, new: str, then=None) -> bool:
        """Move `old`'s history to `new`. `then` runs inside the lock after
        the move (or the refusal), so a live session's save lands wholly
        before both or wholly after both."""
        with self._lock:
            try:
                try:
                    data = json.loads(self._path.read_text())
                except (OSError, json.JSONDecodeError):
                    return False
                if not isinstance(data, dict) or old not in data or new in data:
                    return False
                data[new] = data.pop(old)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                os.replace(tmp, self._path)
                return True
            finally:
                if then is not None:
                    then()


_session_stash = _SessionStash()


def rename_session_history(old: str, new: str, then=None) -> bool:
    """Move a sticky room's saved conversation to its new name (node rename).
    Never overwrites history already stored under `new`. `then` (the
    registry relabel) runs under the stash lock, right after the move."""
    return _session_stash.rename(old, new, then)

# Reasoning-preamble meter: [count] as a mutable cell so the
# lane can count leaks without rebinding a module global per hit.
_PREAMBLE_HITS = [0]


def _intel_settings(sec: dict) -> dict:
    """intelligence.* as a session uses it: config.yaml wins, then the LVI_*
    env var, then the default (the cfg_env rule)."""
    sec = sec if isinstance(sec, dict) else {}

    def pick(key, env_var, default):
        val = sec.get(key)
        if val is None:
            val = os.getenv(env_var)
        return default if val is None else val

    return {"url": pick("url", "LVI_HERMES_URL", "http://<intelligence-host>:8642/v1"),
            "kind": str(pick("kind", "LVI_INTELLIGENCE_KIND", "hermes") or "hermes").lower(),
            "agent": pick("agent", "LVI_INTELLIGENCE_AGENT", ""),
            "agents": sec.get("agents") or {},
            "model": pick("model", "LVI_HERMES_MODEL", "hermes-agent")}


def _settings_key_var(s: dict) -> str | None:
    lane = intelligence.get(s["kind"])
    if lane is None:
        return None
    return lane.key_var(lane.agent_for({"agent": s["agent"], "agents": s["agents"]}))


def session_intelligence(live: dict, boot: dict, environ) -> tuple[dict, str | None]:
    """The intelligence settings a new session can actually use, plus a warning.

    Keys load only at restart. When the saved lane/agent needs a key this
    process doesn't have yet and the boot settings' key IS loaded, keep the
    boot settings until the restart. Otherwise the live settings, even
    without a key: a key that is simply missing still fails loudly."""
    need = _settings_key_var(live)
    if not need or environ.get(need) or live == boot:
        return live, None
    boot_need = _settings_key_var(boot)
    if intelligence.get(boot["kind"]) is not None and (not boot_need or environ.get(boot_need)):
        return boot, (f"intelligence_agent: the saved {live['kind']} settings need {need}, which "
                      f"loads at restart; keeping the boot settings ({boot['kind']}) until then")
    return live, None


def _room_agent(overrides, room: str, default: str) -> tuple[str, str]:
    """room_agents.<kind>.<room> when it names a valid agent, else the house default."""
    agent = str(overrides.get(room) or "").strip() if isinstance(overrides, dict) else ""
    if not agent or not intelligence.AGENT_RE.match(agent):
        return default, "default"
    return agent, "override"


class IntelligenceAgent(FrameProcessor):
    """Buffers user speech; on end-of-turn lull, asks the Hermes API for a reply."""

    def __init__(self, lull_secs: float = 0.6, max_history: int = 10, echo_gate=None,
                 room: str = None, session_mode: str = "fresh",
                 tts_is_usable=None, room_resolver=None, **kwargs):
        super().__init__(**kwargs)
        from config import boot_config, cfg, cfg_env

        # Keys load only at restart, but dashboard config edits reach every
        # new session at once (a room that reconnects). A lane or agent
        # switch saved with its NEW key would make that room fail every turn
        # until the restart, so until then keep what the bot booted with
        # (same rule as keys_catalog.session_tts_provider).
        _live = _intel_settings(cfg("intelligence") or {})
        _boot = _intel_settings(boot_config().get("intelligence") or {})
        _use, _warn = session_intelligence(_live, _boot, os.environ)
        if _warn:
            logger.warning(_warn)
        self._boot_room_agents = boot_config().get("room_agents") or {}
        self._url = _use["url"]
        # Intelligence lane: intelligence.kind picks the request
        # shape. hermes = today's api_server contract; openclaw = OpenClaw
        # Gateway (model "openclaw/<agent>", x-openclaw-session-key, no
        # `conversation` field). The UI calls this "Intelligence" — the
        # word "intelligence" never reaches the dashboard.
        self._kind = _use["kind"]
        # Agent per kind (intelligence_target.agent_for): openclaw addresses
        # it in the model field; hermes routes a named profile to
        # <base>/p/<agent>/v1 with that profile's own key; nanobot sends it
        # as the model field (its one configured model), or nothing.
        # The lane (core/intelligence/<kind>.py) owns the request shape;
        # None = an unknown kind (turns fail with "no key set").
        self._lane = intelligence.get(self._kind)
        _icfg = {"agent": _use["agent"], "agents": _use["agents"]}
        self._agent = self._lane.agent_for(_icfg) if self._lane else ""
        # Per-room override (room_agents.<kind>.<room>) is resolved at the
        # first turn (_ensure_route): the room can still be unknown here.
        self._base_url = self._url
        self._default_agent = self._agent
        self._room_resolver = room_resolver
        self._routed_room = None
        if self._lane:
            self._url = self._lane.request_url(self._url, self._agent)
        # Missing key is NOT a construction brick (the provider-
        # switch-without-key incident): the turn fails with a clear error
        # and the dashboard Test button reports it.
        # Key rule (strict): each lane reads
        # EXACTLY ONE variable — hermes → LVI_HERMES_KEY, openclaw →
        # LVI_OPENCLAW_KEY. No cross-kind fallback: a fallback chain once
        # turned a missing LVI_HERMES_KEY into a silent
        # wrong-token 401 outage instead of one honest error line
        # (fail-loud rule, same as the mapping-wipe fix). A new lane
        # follows the same rule: its key names its intelligence
        # (LVI_<KIND>_KEY), and it reads only its own.
        # A named Hermes profile reads its own variable,
        # LVI_HERMES_KEY_<AGENT> (Hermes rejects the default key on /p/<x>/).
        self._key_var = self._lane.key_var(self._agent) if self._lane else None
        self._key = (os.getenv(self._key_var)
                     if self._key_var else None)
        if not self._key and self._kind != "stub":
            logger.warning(
                f"intelligence_agent: no intelligence key set ({self._key_var}) "
                f"— kind={self._kind}; turns will fail until it is set")
        self._model = _use["model"]
        # Output cap (the 16k-degenerate-reply incident): the
        # intelligence request carried no max_tokens, so a runaway model loop
        # could monologue until the context window died (measured: one
        # 16,145-token reply "degenerated into garbage" mid-session).
        # ~300 tokens ≈ 220 words ≈ 90s of speech — generous for sass,
        # impossible for a runaway. Honored by every OpenAI-compat
        # endpoint (Hermes, Anthropic, OpenClaw — forward-compatible
        # with the intelligence.kind arc). Config-gated per house law.
        self._max_tokens = int(cfg_env("intelligence", "max_tokens", "LVI_INTELLIGENCE_MAX_TOKENS", 300) or 300)
        # Turn-shape tunables — the knobs closest to conversation UX. Same
        # precedence as every other knob: config.yaml > LVI_* env > default.
        self._lull_secs = float(cfg_env("intelligence", "lull_secs", "LVI_LULL_SECS", lull_secs))
        self._max_history = max_history
        self._echo_gate = echo_gate
        # Direct LED wire (led_state.LedSender), set by bot.py after the
        # transport exists; None = no ring to drive (tests, stub lanes).
        self._led = None
        self._buffer: list[str] = []
        self._history: list[dict] = []
        self._last_transcription: float = 0.0
        self._speaking = False
        self._flushed = False
        # Turn gate: a streaming STT (Deepgram) sends finals WHILE you talk,
        # so "lull since the last transcript" alone can close a turn
        # mid-sentence. The turn closes only once VAD says you stopped; the
        # lull counts from whichever came last (transcript or stop), and a
        # finalized transcript after the stop (the STT's flush) closes it at
        # once. Whisper only transcribes after the stop, so it's unchanged.
        self._vad_active = False
        self._vad_stopped_at = 0.0
        # "finalized" means two different things: a streaming STT (Deepgram,
        # which sends interims while you talk) finalizes the UTTERANCE, so
        # that closes the turn at once; a segmented STT (Whisper: no interims)
        # stamps EVERY VAD stretch finalized, so a breath pause would close
        # the turn mid-thought. For segmented finals the lull counts from VAD
        # stop and waits for that stretch's transcript (grace for a stretch
        # that yields no text).
        self._saw_interim = False
        self._segmented_final = False
        self._last_flushed_text = ""  # what the last closed turn asked (late-tail join)
        self._last_flush_at = 0.0
        self._joining = False
        self._last_flush_wake_only = False  # the last close was a bare "Hey <name>"
        self._last_close_segmented = False  # closed by the segmented-STT grace (Whisper/LocalAI)
        # Dedup: same utterance re-heard inside this window is treated as an
        # echo/repeat (observed live: the lull-timer can double-fire while the
        # bot is still speaking a slow reply).
        self._last_user_text: str = ""
        self._last_user_time: float = 0.0
        self._dedup_window: float = float(cfg_env("intelligence", "dedup_window", "LVI_DEDUP_WINDOW", 10.0))
        # Epoch turns: one live turn per pipeline,
        # spawned as a background task; a new question supersedes.
        self._turn_task: asyncio.Task | None = None
        self._turn_epoch: int = 0
        self._turn_started: float = 0.0
        # Supersede-ack rate limit (a two-person session): the ack
        # ("Hold on — switching.") is useful for one deliberate interrupt,
        # but in a two-person conversation consecutive utterances supersede
        # repeatedly and the assistant says it three times in ten seconds — sounds
        # broken. Once per wake window is plenty: the state flip from the
        # old turn dying still tells the user something changed. Reset on
        # a fresh turn start after a quiet gap (no live turn at supersede
        # time = the window rolled over). After an ack, further supersedes
        # inside this gap stay silent.
        self._supersede_ack_ts: float = 0.0
        self._ack_gap = float(cfg_env("intelligence", "supersede_ack_gap", "LVI_SUPERSEDE_ACK_GAP", 45.0))
        # Late results: a turn superseded by a NEW question whose call was
        # already running keeps going (the agent finishes the work anyway),
        # so its reply is spoken after the current one with this lead-in —
        # the user hears what got done instead of asking again and having
        # it done twice. "" = off: the late result is dropped.
        self._late_prefix = str(cfg_env("intelligence", "late_reply_prefix", "LVI_LATE_REPLY_PREFIX",
                                        "About your earlier request:") or "").strip()
        # (epoch, by a new question) of the turn being superseded; False = a
        # join (same question, more words: its late result is dropped).
        self._superseded: tuple[int, bool] | None = None
        # The live turn's epoch once it has pushed its reply (or apology):
        # held late results wait until then.
        self._answered_epoch = -1
        self._late_pending: list[tuple[str, str]] = []  # (question, reply), in the order they finished
        self._late_tasks: set[asyncio.Task] = set()
        self._late_flusher: asyncio.Task | None = None
        # Session end (cleanup / CancelFrame) drops every late result. The
        # epoch alone can't tell this from a supersede: both bump it.
        self._session_ended = False
        # Echo containment: her last spoken texts (reply + filler) — a
        # transcription that contains a chunk of these IS her echo, whenever
        # it arrives (duration gates lose the race when her Sonos reply talks
        # over the mic and whisper transcribes late; measured live).
        self._echo_texts: list[str] = []
        # Dead-TTS phantom arming: echo texts
        # exist so a late transcript of her SPEECH can be dropped. A dead
        # TTS service makes no sound — nothing handed to it can echo back —
        # so bot.py wires the tracked service's is_usable here and those
        # pushes register nothing. None = no signal = arm always (stub /
        # suite default); a service that dies mid-clip after speaking
        # partially still arms — defense-in-depth.
        self._tts_is_usable = tts_is_usable
        # Session modes (fresh | sticky, per node — dashboard checkbox):
        # sticky rooms keep conversation memory across node
        # reconnects AND bot restarts via a disk stash; fresh rooms start
        # every session clean (privacy default — a bedroom mic must not
        # accumulate memory nobody asked for).
        self._room = room
        # The name sticky history is saved under: follows a rename of the
        # live session (see _stash_room).
        self._stash_name = room
        self._sticky = session_mode == "sticky"
        if self._sticky and room:
            self._conversation = f"voice-{room}"
            self._history = _session_stash.load(room)[: self._max_history]
            logger.info(
                f"intelligence_agent: sticky session '{self._conversation}' "
                f"restored {len(self._history)} history entries"
            )
        else:
            if self._sticky and not room:
                # Sticky without a correlated room (offer-tee race, shouldn't
                # happen): fail SAFE, not silent — fresh beats guessing which
                # room's stash to load.
                logger.warning("intelligence_agent: sticky mode without a room — falling back to fresh")
            # Server-side conversation chain (per-connection): "voice-<zone>-<id>"
            self._conversation = os.getenv(
                "LVI_CONVERSATION",
                f"voice-{cfg_env('intelligence', 'zone', 'LVI_ZONE', 'zone')}-{uuid.uuid4().hex[:8]}"
            )
        logger.info(f"intelligence_agent: conversation '{self._conversation}'"
                    f"{' (sticky)' if self._sticky and room else ''}")

    def _is_own_echo(self, text: str) -> bool:
        """True if `text` is essentially her own words coming back.

        An echo is her reply re-transcribed by the mic — so nearly ALL of
        the transcript's words must be words she actually spoke. The old
        0.6 bar was stopword-blind: a user's
        repeat "What's the weather today on <town>?" scored 71% against
        her weather ANSWER — 5 of 7 words, only the town name content; s,
        the, on, today all counted as echo evidence — and the question
        died as an "echo" while TTS was down. 0.9 keeps verbatim and
        clipped-tail echoes caught (they are ~100% subsets of her words)
        and lets same-topic follow-ups through (their own question words
        are the missing percentage).
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
            if words and hits / len(words) >= 0.9:
                return True
        return False

    def _register_echo(self, text: str) -> None:
        """Arm `text` for echo containment — unless the TTS service is
        known-dead: it makes no sound, so its texts can never echo back,
        and their words kept eating real follow-ups (2026-10-16: three
        phantom texts held after a websocket death and swallowed the
        user's repeat question)."""
        if self._tts_is_usable is not None and not self._tts_is_usable():
            return
        self._echo_texts.append(text)
        self._echo_texts = self._echo_texts[-4:]

    def _ensure_route(self) -> None:
        """Route this session to its room's agent: room_agents.<kind>.<room>
        overrides the house default (the Intelligence card's agent). Runs at
        every turn but only acts once the room is known — the pipeline's
        room can still be unknown when the session is built, and resolving
        early would send a racing session to the default agent."""
        if self._lane is None:
            return
        # The room's CURRENT name (a dashboard rename relabels the live
        # session, and the carry moves its room_agents entry to the new name).
        room = self._stash_room()
        if not room or room == "unknown" or room == self._routed_room:
            return
        from config import cfg

        agent, how = _room_agent(cfg("room_agents", self._kind), room, self._default_agent)
        key_var = self._lane.key_var(agent)
        if key_var and not os.getenv(key_var):
            # A per-room agent saved with its new key: keep the room's boot
            # agent until the restart loads the key (when that one has one).
            boot_kinds = self._boot_room_agents if isinstance(self._boot_room_agents, dict) else {}
            boot_agent, boot_how = _room_agent(boot_kinds.get(self._kind), room, self._default_agent)
            boot_var = self._lane.key_var(boot_agent)
            if boot_agent != agent and (not boot_var or os.getenv(boot_var)):
                logger.warning(f"intelligence_agent: room '{room}' agent '{agent or 'default'}' needs "
                               f"{key_var}, which loads at restart; keeping '{boot_agent or 'default'}' until then")
                agent, how = boot_agent, boot_how
        if agent != self._agent:
            # Build the whole route first, then swap it in; _routed_room is
            # set LAST, so an overlapping (superseded) turn's thread that
            # sees it set also sees the complete route.
            url = self._lane.request_url(self._base_url, agent)
            key_var = self._lane.key_var(agent)
            key = os.getenv(key_var) if key_var else None
            self._agent, self._url, self._key_var, self._key = agent, url, key_var, key
            if not key:
                logger.warning(f"intelligence_agent: no key set ({key_var}) for room '{room}' agent '{agent}'")
        self._routed_room = room
        logger.info(f"intelligence_agent: room '{room}' -> agent '{agent or 'default'}' ({how})")

    def _stash_room(self) -> str | None:
        """The room's CURRENT name. A dashboard rename relabels the live
        session without a reconnect; saving under the name captured at
        construction would freeze the moved history and leave a copy under
        a name another node could take later. Keeps the last known name
        once the session is gone (a save can land after the disconnect)."""
        if self._room_resolver is not None:
            try:
                room = self._room_resolver()
            except Exception:  # noqa: BLE001 — keep the last known name
                room = None
            if room and room != "unknown":
                self._stash_name = room
        return self._stash_name

    def _save_history(self, history: list) -> None:
        """Sticky save (worker thread). The name is resolved under the
        stash lock: a rename moves the history and relabels the session
        under that same lock, so this save never lands in between."""
        with _session_stash._lock:
            _session_stash.save(self._stash_room(), history)

    def _ask_intelligence(self, text: str) -> str:
        """One chat completion; returns the reply text.

        The lane builds the request envelope (payload + its own headers:
        session keys) and explains its own HTTP errors; output caps,
        fillers and history are shared — the lanes differ only in envelope.
        """
        self._ensure_route()
        if not self._key:
            raise RuntimeError(
                f"no intelligence key set ({self._key_var}) — set it in .env "
                f"(a lane's key names its intelligence; no cross-kind fallback)")
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        payload, lane_headers = self._lane.build_request(text, intelligence.TurnContext(
            agent=self._agent, model=self._model, max_tokens=self._max_tokens,
            conversation=self._conversation, sticky=self._sticky, room=self._room,
            system_prompt=VOICE_SYSTEM_PROMPT, history=list(self._history)))
        headers.update(lane_headers)
        req = urllib.request.Request(
            f"{self._url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # A lane may explain a refusal from the server's body (never
            # the key); otherwise the error propagates unchanged.
            err = self._lane.on_http_error(e, self._key)
            if err is None:
                raise
            raise err from None
        reply = data["choices"][0]["message"]["content"].strip()
        if not reply:
            # e.g. a run that ended on a tool call with no closing text. A
            # failed turn, not a silent one: the user hears the apology, no
            # empty assistant message enters history, and a retry isn't
            # dedup-dropped.
            raise RuntimeError("the intelligence returned an empty reply")
        usage = data.get("usage", {})
        # Lane defect meter (e.g. a reasoning preamble merged into
        # `content`): the lane names it, we count and log it. MEASURE,
        # never silently strip — a voice assistant speaks content verbatim.
        leak = self._lane.inspect_reply(reply)
        if leak:
            _PREAMBLE_HITS[0] += 1
            logger.warning(
                f"intelligence_agent: {leak} "
                f"({_PREAMBLE_HITS[0]} this process) — reply[:80]={reply[:80]!r}")
        # Client-side output cap: Hermes's api_server IGNORES
        # the request's max_tokens (verified live: max_tokens:5 returned a
        # 360-char reply with finish_reason stop; api_server.py has zero
        # references). Real OpenAI-compat endpoints honor the field, but
        # our PRIMARY intelligence doesn't — so the client enforces too. Char
        # budget ≈ 4 chars/token × max_tokens; truncate at the last word
        # boundary so she never mid-words the speaker.
        char_cap = self._max_tokens * 4
        if len(reply) > char_cap:
            logger.warning(
                f"intelligence_agent: reply {len(reply)} chars exceeded cap "
                f"{char_cap} (max_tokens={self._max_tokens}) — truncated"
            )
            reply = reply[:char_cap].rsplit(maxsplit=1)[0].rstrip(",;:-—") + "."
        logger.info(
            f"intelligence_agent: tokens in={usage.get('prompt_tokens', '?')} "
            f"out={usage.get('completion_tokens', '?')}"
        )
        return reply

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        final_now = False
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            self._flushed = False
            self._vad_active = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_active = False
            self._vad_stopped_at = time.monotonic()
        elif isinstance(frame, InterimTranscriptionFrame):
            self._saw_interim = True
        elif isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                if self._echo_gate and self._echo_gate.blocked():
                    logger.debug(f"intelligence_agent: echo-gate dropped [{frame.text.strip()}]")
                elif self._is_own_echo(frame.text):
                    logger.info(f"intelligence_agent: containment dropped echo [{frame.text.strip()[:60]}]")
                elif (self._flushed and not self._speaking and self._last_flushed_text
                      and self._last_close_segmented and not self._saw_interim
                      and (self._last_flush_wake_only
                           or (self._turn_task is not None and not self._turn_task.done()))
                      and time.monotonic() - self._last_flush_at < LATE_JOIN_WINDOW):
                    # The rest of the question the turn closed on (a slow
                    # STT stretch): reopen it with both parts; the close
                    # below supersedes the thinking turn without the ack.
                    logger.info(f"intelligence_agent: late transcript joins the open turn "
                                f"[{frame.text.strip()[:60]}]")
                    self._buffer[:] = [self._last_flushed_text, frame.text.strip()]
                    self._speaking = True
                    self._flushed = False
                    self._joining = True
                    self._last_user_text = ""  # the joined text is new, never a dedup repeat
                    self._last_transcription = time.monotonic()
                    final_now = True
                elif self._flushed and not self._speaking:
                    # The turn already closed: a late STT tail (busy GPU,
                    # Deepgram finalize). Kept, it would sit in the buffer
                    # and be glued onto the front of the NEXT question,
                    # however much later that came.
                    logger.info(f"intelligence_agent: dropped late transcript after the turn closed "
                                f"[{frame.text.strip()[:60]}]")
                else:
                    self._buffer.append(frame.text.strip())
                    self._last_transcription = time.monotonic()
                    if getattr(frame, "finalized", False):
                        if self._saw_interim:
                            final_now = True
                        else:
                            self._segmented_final = True

        now_t = time.monotonic()
        if self._segmented_final:
            since_stop = now_t - self._vad_stopped_at
            lull_done = since_stop >= self._lull_secs and (
                self._last_transcription >= self._vad_stopped_at
                or since_stop >= self._lull_secs + SEGMENT_TRANSCRIPT_GRACE)
        else:
            lull_done = (now_t - max(self._last_transcription, self._vad_stopped_at)
                         >= self._lull_secs)
        if (
            self._speaking
            and self._flushed is False
            and self._buffer
            and self._last_transcription > 0
            and not self._vad_active
            and (final_now or lull_done)
        ):
            self._last_close_segmented = (self._segmented_final and not final_now) or self._joining
            self._speaking = False
            self._flushed = True
            self._saw_interim = False
            self._segmented_final = False
            text = " ".join(self._buffer).strip()
            self._buffer.clear()
            joining, self._joining = self._joining, False
            self._last_flushed_text, self._last_flush_at = text, time.monotonic()
            self._last_flush_wake_only = bool(text) and _is_wake_phrase_only(text)
            if text:
                if _is_wake_phrase_only(text):
                    logger.info(f"intelligence_agent: dropped wake-phrase-only [{text}]")
                else:
                    now = time.monotonic()
                    if text == self._last_user_text and (now - self._last_user_time) < self._dedup_window:
                        logger.info(f"intelligence_agent: dedup dropped repeat [{text[:60]}]")
                        self._speaking = False
                        self._flushed = True
                    else:
                        self._last_user_text = text
                        self._last_user_time = now
                        # ── Epoch turns ──
                        # The intelligence call runs as a BACKGROUND task; the
                        # frame loop returns immediately and never
                        # blocks on a slow intelligence (filler/reply
                        # collisions and stuck LEDs share this root
                        # cause). A new question supersedes: old task
                        # cancelled, nothing spoken in its place, no
                        # history write. A call still queued is never
                        # sent; one already running finishes, and if a
                        # NEW question superseded it (not a join) its
                        # reply is spoken late, after the current one
                        # (_watch_late).
                        superseded = False
                        if self._turn_task is not None and not self._turn_task.done():
                            superseded = True
                            self._superseded = (self._turn_epoch, not joining)
                            self._turn_task.cancel()
                            try:
                                await asyncio.shield(self._turn_task)
                            except (asyncio.CancelledError, Exception):
                                pass
                            self._superseded = None
                            logger.info(
                                f"intelligence_agent: turn superseded after "
                                f"{time.monotonic() - self._turn_started:.1f}s — "
                                f"no reply in its place, no history write "
                                f"(a call already running may report late)")
                        self._turn_epoch += 1
                        epoch = self._turn_epoch
                        self._turn_started = time.monotonic()
                        self._turn_task = asyncio.create_task(
                            self._run_turn(text, epoch))
                        if superseded and joining:
                            pass  # same question, more words: no "switching" ack
                        elif superseded:
                            from config import cfg_env as _cfg_env
                            ack = str(_cfg_env("intelligence", "supersede_ack",
                                               "LVI_SUPERSEDE_ACK",
                                               "Hold on — switching.") or "")
                            now_ack = time.monotonic()
                            if ack and (now_ack - self._supersede_ack_ts) >= self._ack_gap:
                                self._supersede_ack_ts = now_ack
                                await self.push_frame(TTSSpeakFrame(text=ack))
                                self._register_echo(ack)
                            # else: silent supersede — the turn still
                            # switches; only the announcement is rate-
                            # limited (or the ack is disabled via config).
                        else:
                            # Fresh turn (no live task to supersede) → new
                            # conversational turn: re-arm so a deliberate
                            # interrupt here still gets its announcement.
                            self._supersede_ack_ts = 0.0

        if isinstance(frame, CancelFrame):
            await self._end_turn()

        # EVERY frame passes through (a past regression):
        # this line was lost in the _run_turn extraction churn — the agent
        # ate StartFrame, PipelineWorker timed out at +20s, and the node
        # rode a reconnect loop ("you wont wake"). Non-negotiable.
        await self.push_frame(frame, direction)

    async def cleanup(self):
        # Session end, whatever ended it: a turn still waiting on the
        # intelligence would otherwise push into the dead pipeline and write
        # the sticky stash over the next session's history.
        await self._end_turn()
        await super().cleanup()

    async def _end_turn(self) -> None:
        """Session end: cancel the in-flight turn for good. The epoch bump
        makes every epoch check fail, so even a turn that resumes pushes
        nothing and writes no history. The blocking call's thread runs on;
        its result is dropped — and so is every late result, held or still
        to come (_session_ended: unlike a supersede, nothing reports late)."""
        self._session_ended = True
        self._turn_epoch += 1
        for text, _ in self._late_pending:
            logger.info(f"intelligence_agent: late result dropped (the session ended) [{text[:60]}]")
        self._late_pending.clear()
        for t in [*self._late_tasks, self._late_flusher]:
            if t is not None and not t.done():
                t.cancel()
        task, self._turn_task = self._turn_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 — a dying turn stays quiet
                pass

    def set_led(self, led) -> None:
        """Bind this pipeline's LED sender (bot.py, once per session)."""
        self._led = led

    async def _run_turn(self, text: str, epoch: int) -> None:
        """One turn as a background task. Every await
        checks the epoch — a superseded turn speaks nothing in its place,
        skips the history write, and dies quietly; a call it already had
        running may still report late (_watch_late). CancelledError
        propagates (task.cancel from the supersede path); everything else
        is the broad-catch: the user never gets dead silence on an
        intelligence failure."""
        filler_spoken = False
        ask = call = None
        settled = False  # the call answered or failed while the turn still waited on it
        # The turn is now with the intelligence: show "thinking" on the ring
        # until speech (filler or reply) takes over. Cosmetic and
        # fire-and-forget: LedSender.send never raises.
        if self._led is not None and epoch == self._turn_epoch:
            self._led.send("bot-thinking")
        try:
            # The pool's own future too: the finally tells a call still
            # queued (cancel succeeds) from one already running.
            call = _INTEL_POOL.submit(self._ask_intelligence, text)
            ask = asyncio.wrap_future(call)
            failed = False
            reply = None
            # Tiered fillers (option B): wait to each tier's
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
                        # Never talk over the user: hold the filler while
                        # VAD says they're speaking (it would also arm the
                        # echo gate and swallow what they're saying).
                        while (self._vad_active and not ask.done()
                               and epoch == self._turn_epoch):
                            await asyncio.sleep(0.05)
                        if epoch != self._turn_epoch:
                            ask.cancel()
                            return
                        if ask.done():
                            reply = await ask
                            break
                        filler = _pick_filler(pool)
                        await self.push_frame(TTSSpeakFrame(text=filler))
                        self._register_echo(filler)
                        filler_spoken = True
                        logger.info(
                            f"intelligence_agent: filler tier {i + 1}/{len(tiers)} -> [{filler}]"
                        )
            if reply is None:
                # every tier spent, intelligence still working — wait it out
                reply = await ask
            settled = True
            if epoch != self._turn_epoch:
                return  # ended while thinking — discard, no history
            self._register_echo(reply)
            # A failed call never reached the intelligence — don't write the
            # exchange into server-side history.
            if not failed:
                self._history.append({"role": "user", "content": text})
                self._history.append({"role": "assistant", "content": reply})
                self._history = self._history[-self._max_history:]
            logger.info(f"intelligence_agent: [{text}] -> [{reply[:120]}]")
            await self.push_frame(TTSSpeakFrame(text=reply))
            # Sticky rooms persist after every successful turn. File I/O runs
            # off the event loop, after the reply is on its way (no new await
            # between the history write and the push); a copy, as the next
            # turn may append meanwhile.
            if not failed and self._sticky and self._room:
                await asyncio.to_thread(self._save_history, list(self._history))
            # Held late results go right after this reply — after its save,
            # so a late result's own save (both exchanges) lands last.
            self._answered_epoch = epoch
            self._kick_late()
        except asyncio.CancelledError:
            raise  # supersede path — die quietly, no user-facing output
        except Exception as e:
            # The intelligence can fail at ANY
            # point; ONE broad catch guards the whole turn — the last
            # defense before user-facing silence. A superseded turn that
            # fails is still superseded: no history write either way.
            settled = True
            logger.error(f"intelligence_agent: intelligence call failed"
                         f"{' after filler' if filler_spoken else ''}: {e}")
            # A failed turn must not dedup its own retry: the user hears the
            # apology and repeats the question. Dedup exists for double-fires
            # of ONE utterance, not for a deliberate second try.
            if self._last_user_text == text:
                self._last_user_text = ""
            if epoch != self._turn_epoch:
                return
            await self.push_frame(
                TTSSpeakFrame(text="Sorry — I can't reach my intelligence right now."))
            self._answered_epoch = epoch
            self._kick_late()
        finally:
            # Superseded, ended or failed: the shield kept the call alive
            # through the filler waits, so drop it here. One still queued
            # for a pool thread is never sent (a stale question must not
            # reach the agent later or hold a slot); one already running
            # finishes, and _watch_late decides whether it reports late.
            if call is not None and not settled and not call.cancel():
                self._watch_late(text, call, epoch)
            if ask is not None and not ask.done():
                ask.cancel()

    def _watch_late(self, text: str, call, epoch: int) -> None:
        """A turn ended while its call was already running (it can't be
        recalled: the agent finishes the work). Superseded by a NEW question,
        the result is spoken late; otherwise it is dropped."""
        sup = self._superseded
        if self._session_ended:
            why = "the session ended"
        elif sup is None or sup[0] != epoch:
            why = "the turn was cancelled"
        elif not sup[1]:
            why = "the rest of the question joined it"
        elif not self._late_prefix:
            why = "intelligence.late_reply_prefix is off"
        else:
            task = asyncio.create_task(self._late_reply(text, call))
            self._late_tasks.add(task)
            task.add_done_callback(self._late_tasks.discard)
            return
        logger.info(f"intelligence_agent: late result dropped ({why}) [{text[:60]}]")

    async def _late_reply(self, text: str, call) -> None:
        """Wait out a superseded turn's running call; queue a non-empty
        reply to be spoken. A failure or an empty reply says nothing."""
        try:
            reply = await asyncio.wrap_future(call)
        except asyncio.CancelledError:
            logger.info(f"intelligence_agent: late result dropped (the session ended) [{text[:60]}]")
            raise
        except Exception as e:  # noqa: BLE001 — a late failure stays quiet
            logger.info(f"intelligence_agent: late result dropped (the call failed: {e}) [{text[:60]}]")
            return
        reply = str(reply or "").strip()
        if not reply:
            logger.info(f"intelligence_agent: late result dropped (empty reply) [{text[:60]}]")
            return
        if self._session_ended:
            logger.info(f"intelligence_agent: late result dropped (the session ended) [{text[:60]}]")
            return
        self._late_pending.append((text, reply))
        if self._reply_due() or self._vad_active:
            until = "the current reply" if self._reply_due() else "the user stops talking"
            logger.info(f"intelligence_agent: late result held until {until} [{text[:60]}]")
        self._kick_late()

    def _reply_due(self) -> bool:
        """A live turn hasn't pushed its reply (or apology) yet."""
        return (self._turn_task is not None and not self._turn_task.done()
                and self._answered_epoch != self._turn_epoch)

    def _kick_late(self) -> None:
        """Start the one late-result speaker, if anything is held."""
        if self._late_pending and (self._late_flusher is None or self._late_flusher.done()):
            self._late_flusher = asyncio.create_task(self._speak_late())

    async def _speak_late(self) -> None:
        """Speak held late results in the order they finished, each with
        the lead-in. Never before a live turn's own reply (that turn kicks
        this again once it has pushed it — frame order plays this after),
        never while the user is talking, never after the session ended."""
        while self._late_pending and not self._session_ended:
            if self._reply_due():
                return
            if self._vad_active:
                await asyncio.sleep(0.05)
                continue
            text, reply = self._late_pending.pop(0)
            line = f"{self._late_prefix} {reply}"
            # Into history like any answered turn, so a re-ask has context
            # and isn't silently done again.
            self._history.append({"role": "user", "content": text})
            self._history.append({"role": "assistant", "content": reply})
            self._history = self._history[-self._max_history:]
            self._register_echo(line)
            logger.info(f"intelligence_agent: late result spoken [{text}] -> [{reply[:120]}]")
            await self.push_frame(TTSSpeakFrame(text=line))
            if self._sticky and self._room:
                await asyncio.to_thread(self._save_history, list(self._history))