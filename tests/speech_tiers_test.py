"""speech tiers — the catalog contract: three tiers, each provider's tier, resolve rules (host venv).

Pure data: no model loads, no network.
Run: cd tests && ~/venvs/lenoxvi/bin/python speech_tiers_test.py <path-to-core>
"""
import subprocess
import sys

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import keys_catalog
import stt_catalog
import tts_catalog

# ── catalogs ─────────────────────────────────────────────────────────
check("speech tiers: three tiers in display order — Embedded, Local, Cloud (LocalAI is a Local provider)",
      [(t["name"], t["label"]) for t in stt_catalog.TIERS]
      == [("embedded", "Embedded"), ("local", "Local"), ("cloud", "Cloud")]
      and stt_catalog.tier_of("localai") == tts_catalog.tier_of("localai") == "local", str(stt_catalog.TIERS))
check("speech tiers: one copy of the tiers — tts_catalog re-exports stt_catalog's",
      tts_catalog.TIERS is stt_catalog.TIERS and tts_catalog.TIER_NAMES is stt_catalog.TIER_NAMES)
check("speech tiers: Embedded STT is fixed — faster-whisper base on the CPU, int8",
      (stt_catalog.EMBEDDED_MODEL, stt_catalog.EMBEDDED_DEVICE, stt_catalog.EMBEDDED_COMPUTE) == ("base", "cpu", "int8"))
check("speech tiers: STT providers in card order, plain labels, one tier each",
      [(p["name"], p["label"], p["tier"]) for p in stt_catalog.PROVIDERS]
      == [("whisper", "Whisper base", "embedded"), ("localai", "LocalAI", "local"),
          ("deepgram", "Deepgram Nova-3", "cloud"), ("elevenlabs", "ElevenLabs Scribe v2", "cloud")],
      str(stt_catalog.PROVIDERS))
check("speech tiers: TTS providers in card order, plain labels, tier and key",
      [(p["name"], p["label"], p["tier"], p["key_var"]) for p in tts_catalog.PROVIDERS]
      == [("kokoro", "Kokoro", "embedded", None), ("localai", "LocalAI", "local", None),
          ("elevenlabs", "ElevenLabs", "cloud", "ELEVENLABS_API_KEY"),
          ("deepgram-flux", "Deepgram Flux", "cloud", "DEEPGRAM_API_KEY"),
          ("deepgram", "Deepgram Aura-2", "cloud", "DEEPGRAM_API_KEY")],
      str(tts_catalog.PROVIDERS))
check("speech tiers: every provider sits in a known tier, and every tier has a provider on both cards",
      all(p["tier"] in stt_catalog.TIER_NAMES for p in stt_catalog.PROVIDERS + tts_catalog.PROVIDERS)
      and {p["tier"] for p in stt_catalog.PROVIDERS} == set(stt_catalog.TIER_NAMES)
      and {p["tier"] for p in tts_catalog.PROVIDERS} == set(stt_catalog.TIER_NAMES))
check("speech tiers: only Cloud TTS providers need a key",
      all((p["key_var"] is not None) == (p["tier"] == "cloud") for p in tts_catalog.PROVIDERS))

# ── resolve: unset / unknown = Embedded ──────────────────────────────
for mod, default in ((stt_catalog, "whisper"), (tts_catalog, "kokoro")):
    name = mod.__name__
    check(f"speech tiers: {name} unset -> Embedded ({default})",
          mod.DEFAULT_PROVIDER == default and mod.resolve_provider(None) == default
          and mod.resolve_provider("") == default and mod.resolve_provider("   ") == default)
    check(f"speech tiers: {name} unknown -> Embedded ({default})",
          mod.resolve_provider("nonesuch") == default and mod.resolve_provider(42) == default)
    check(f"speech tiers: {name} known providers resolve to themselves, case and spaces ignored",
          all(mod.resolve_provider(n) == n and mod.resolve_provider(f"  {n.upper()} ") == n
              for n in mod.PROVIDER_NAMES))
check("speech tiers: is_cloud follows the tier",
      [n for n in stt_catalog.PROVIDER_NAMES if stt_catalog.is_cloud(n)] == ["deepgram", "elevenlabs"]
      and [n for n in tts_catalog.PROVIDER_NAMES if tts_catalog.is_cloud(n)] == ["elevenlabs", "deepgram-flux", "deepgram"]
      and not stt_catalog.is_cloud("nonesuch") and not tts_catalog.is_cloud("nonesuch"))
check("speech tiers: keys_catalog.tts_secret reads the TTS catalog",
      {n: keys_catalog.tts_secret(n) for n in tts_catalog.PROVIDER_NAMES}
      == {"kokoro": None, "localai": None, "elevenlabs": "ELEVENLABS_API_KEY",
          "deepgram-flux": "DEEPGRAM_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}
      and keys_catalog.tts_secret(None) is None and keys_catalog.tts_secret("nonesuch") is None)
check("speech tiers: validate_language — auto, listed codes and None pass; anything else is refused",
      stt_catalog.validate_language("auto") is None and stt_catalog.validate_language("en") is None
      and stt_catalog.validate_language(None) is None
      and "language" in (stt_catalog.validate_language("xx") or ""))

# ── both catalogs stay pure data ─────────────────────────────────────
_light = subprocess.run(
    [sys.executable, "-c",
     ("import sys; sys.path.insert(0, sys.argv[1]); import stt_catalog, tts_catalog; "
      "print(sorted(m for m in ('pipecat', 'faster_whisper', 'kokoro_onnx') if m in sys.modules))"), CORE],
    capture_output=True, text=True, timeout=60, check=False)
check("speech tiers: importing both catalogs loads no pipecat, Whisper or Kokoro",
      _light.returncode == 0 and _light.stdout.strip() == "[]", (_light.stdout + _light.stderr)[-300:])

# ── bot.py: Embedded STT is fixed (code only: ast.unparse, comments stripped) ──
import ast
from pathlib import Path

_BOT_TREE = ast.parse((Path(CORE).resolve().parent / "bot.py").read_text())
_BOT_SRC = ast.unparse(_BOT_TREE)
_RUN_BOT = next((n for n in ast.walk(_BOT_TREE) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_bot"), None)
_RB_SRC = ast.unparse(_RUN_BOT) if _RUN_BOT else ""
_MAIN = next((n for n in _BOT_TREE.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)), None)


def _calls(node, name):
    """Calls of `name` (a plain name or an attribute) under node."""
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and name in (getattr(n.func, "id", None), getattr(n.func, "attr", None))] if node else []


def _config_reads(node):
    """(section, key) of every cfg/cfg_env call under node with a literal section and key."""
    return [(c.args[0].value, c.args[1].value) for c in ast.walk(node)
            if isinstance(c, ast.Call) and getattr(c.func, "id", "") in ("cfg", "cfg_env", "_cfg", "_cfg_env")
            and len(c.args) >= 2 and all(isinstance(a, ast.Constant) for a in c.args[:2])]


def _path_to(root, target):
    """The nodes from root (exclusive) down to target (exclusive), or None."""
    for child in ast.iter_child_nodes(root):
        if child is target:
            return []
        sub = _path_to(child, target)
        if sub is not None:
            return [child, *sub]
    return None


_stt_new = _calls(_RUN_BOT, "LocalWhisperSTTService")
check("speech tiers: run_bot builds Whisper once, with no model, device or compute_type (Embedded is fixed)",
      len(_stt_new) == 1 and not _stt_new[0].args
      and not ({k.arg for k in _stt_new[0].keywords} & {"model", "device", "compute_type"}),
      ast.unparse(_stt_new[0]) if _stt_new else "no LocalWhisperSTTService(...) in run_bot")
_pre = _calls(_MAIN, "preload_shared_model")
_pre_path = _path_to(_MAIN, _pre[0]) if len(_pre) == 1 else None
check("speech tiers: startup preloads Whisper with ONE argument-free call for every provider (no provider branch)",
      len(_pre) == 1 and not _pre[0].args and not _pre[0].keywords and _pre_path is not None
      and not any(isinstance(n, ast.If) for n in _pre_path), str([ast.unparse(c) for c in _pre]))
_stt_read = {r[1] for r in _config_reads(_BOT_TREE) if r[0] == "stt"}
_stt_env = {n.value for n in ast.walk(_BOT_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.startswith("LVI_STT_")}
check("speech tiers: bot.py reads only stt.provider, language and revive_cooldown_secs (and their LVI_STT_ variables)",
      _stt_read <= {"provider", "language", "revive_cooldown_secs"}
      and _stt_env <= {"LVI_STT_PROVIDER", "LVI_STT_LANGUAGE", "LVI_STT_REVIVE_COOLDOWN"},
      f"keys={sorted(_stt_read)} env={sorted(_stt_env)}")
check("speech tiers: stt.provider goes through stt_catalog.resolve_provider; an unknown name keeps its warning",
      "_stt_raw = cfg_env('stt', 'provider', 'LVI_STT_PROVIDER', None)" in _RB_SRC
      and "stt_provider = stt_catalog.resolve_provider(_stt_raw)" in _RB_SRC
      and "stt: unknown provider {_stt_raw!r} — using local Whisper" in _RB_SRC
      and "validate_provider" not in _RB_SRC, "")
check("speech tiers: the cloud STT switcher wraps that one Embedded Whisper service as its standby",
      _RB_SRC.count("stt = LocalWhisperSTTService(") == 1
      and "stt_fallback.wrap_stt_with_fallback(_cloud, stt, room=_room" in _RB_SRC, "")

# ── bot.py: TTS tiers (code only) ────────────────────────────────────
import tts_catalog

# The tts keys that stay (spec: config after). An allow-list, so the test never
# has to spell a deleted key.
_TTS_KEYS = {"provider", "kokoro_voice", "kokoro_speed", "revive_cooldown_secs", "voice_id", "model",
             "elevenlabs_speed", "elevenlabs_stability", "elevenlabs_text_normalization", "deepgram_voice",
             "deepgram_flux_voice", "deepgram_flux_speed", "deepgram_flux_expressivity"}
_prov_reads = [c for c in ast.walk(_BOT_TREE) if isinstance(c, ast.Call)
               and getattr(c.func, "id", "") in ("cfg_env", "_cfg_env") and len(c.args) >= 4
               and all(isinstance(a, ast.Constant) for a in c.args[:2])
               and (c.args[0].value, c.args[1].value) in (("stt", "provider"), ("tts", "provider"))]
check("speech tiers: every stt/tts.provider read in bot.py leaves the default to the catalogs (no fallback name of its own)",
      len(_prov_reads) >= 4 and all(isinstance(c.args[3], ast.Constant) and c.args[3].value is None for c in _prov_reads),
      str([ast.unparse(c) for c in _prov_reads]))
check("speech tiers: run_bot reads tts.provider once, through tts_catalog.resolve_provider; an unknown name warns and means Kokoro",
      _RB_SRC.count("'tts', 'provider'") == 1
      and "tts_provider = tts_catalog.resolve_provider(_tts_raw)" in _RB_SRC
      and "tts: unknown provider {_tts_raw!r} — using Kokoro" in _RB_SRC, "")
check("speech tiers: the boot TTS provider goes through tts_catalog too (unset = kokoro)",
      "_BOOT_TTS_PROVIDER = tts_catalog.resolve_provider(" in (ast.unparse(_MAIN) if _MAIN else ""), "")
_tts_branches = sorted({c.comparators[0].value for c in ast.walk(_RUN_BOT) if isinstance(c, ast.Compare)
                        and isinstance(c.left, ast.Name) and c.left.id == "tts_provider"
                        and len(c.ops) == 1 and isinstance(c.ops[0], ast.Eq)
                        and isinstance(c.comparators[0], ast.Constant)}) if _RUN_BOT else []
check("speech tiers: run_bot dispatches exactly the TTS catalog's providers — one branch each, nothing else",
      _tts_branches == sorted(tts_catalog.PROVIDER_NAMES), str(_tts_branches))
_tts_read = {r[1] for r in _config_reads(_BOT_TREE) if r[0] == "tts"}
check("speech tiers: bot.py reads only the tts keys that stay (no deleted key is read)",
      _tts_read <= _TTS_KEYS, str(sorted(_tts_read - _TTS_KEYS)))
check("speech tiers: a provider with no branch fails loud instead of falling into ElevenLabs",
      "elif tts_provider == 'elevenlabs':" in _RB_SRC and "has no TTS dispatch in bot.py" in _RB_SRC, "")
_kk_pre = [c for c in _calls(_MAIN, "preload") if ast.unparse(c.func) == "tts_kokoro.preload"]
_kk_ifs = ([n for n in (_path_to(_MAIN, _kk_pre[0]) or []) if isinstance(n, ast.If)]
           if len(_kk_pre) == 1 else None)
check("speech tiers: startup preloads Kokoro for every provider (only kokoro_available() guards it)",
      _kk_ifs is not None and all(ast.unparse(n.test) == "tts_kokoro.kokoro_available()" for n in _kk_ifs),
      f"guards={[ast.unparse(n.test) for n in (_kk_ifs or [])]}")

# ── run_bot probe: one session built for real, stopped at PipelineWorker ──
# run_bot runs with the real config loader (a scratch copy of the fixture),
# the real provider dispatch, the real TTS services and switcher. Only what
# would load a model, open a socket or start a pipeline is stubbed in bot's
# namespace; PipelineWorker(...) is the stop: it captures run_bot's locals.
import asyncio
import importlib.util
import os
import tempfile
import types

import numpy as np
import yaml

_REPO = Path(CORE).resolve().parent
_SCRATCH = Path(tempfile.mkdtemp(prefix="speech-tiers-"))
_FIXTURE = Path(os.environ.get("LVI_TEST_CONFIG") or _REPO / "tests" / "fixtures" / "config.yaml")
_CFG = _SCRATCH / "config.yaml"
_CFG.write_text(_FIXTURE.read_text())
(_SCRATCH / "suite.env").write_text("")
os.environ.update({"LVI_CONFIG": str(_CFG), "LVI_ENV_FILE": str(_SCRATCH / "suite.env"),
                   "LVI_STATE_DIR": str(_SCRATCH)})
if importlib.util.find_spec("kokoro_onnx") is None:  # pipecat's Kokoro module imports it
    _kk_mod = types.ModuleType("kokoro_onnx")
    _kk_mod.Kokoro = object
    sys.modules["kokoro_onnx"] = _kk_mod
sys.path.insert(0, str(_REPO))
import config
import tts_fallback
import tts_kokoro
from loguru import logger
from pipecat.pipeline.service_switcher import ServiceSwitcher

import bot

config.CONFIG_PATH = str(_CFG)


class _FakeKokoro:
    """The shared kokoro-onnx model's surface (no 325 MB load)."""

    def __init__(self):
        self.voices = {v: object() for v in tts_kokoro.KOKORO_VOICES}

    async def create_stream(self, text, voice, speed=1.0, lang="en-us", **kw):
        yield np.zeros(240, dtype=np.float32), 24000


def _fake_kokoro():
    tts_kokoro._reset_for_tests()
    tts_kokoro._kokoro = _FakeKokoro()


_fake_kokoro()
_real_kokoro_available = tts_kokoro.kokoro_available
tts_kokoro.kokoro_available = lambda: True


class _Stub:
    def __init__(self, *a, **k):
        pass


class _Transport:
    def input(self):
        return _Stub()

    def output(self):
        return _Stub()


class _Stop(Exception):
    pass


class _EnvSpy(dict):
    """os.environ stand-in that remembers every key looked up."""

    def __init__(self, base):
        super().__init__(base)
        self.seen = set()

    def __getitem__(self, k):
        self.seen.add(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.seen.add(k)
        return super().get(k, default)

    def __contains__(self, k):
        self.seen.add(k)
        return super().__contains__(k)


_STUBBED = ("IntelligenceAgent", "StubIntelligence", "SileroVADAnalyzer", "WakeWordFilter",
            "SonosOutput", "WiiMOutput", "VADProcessor", "Pipeline")
_RUNNER_ARGS = types.SimpleNamespace(webrtc_connection=types.SimpleNamespace(pc_id="suite-pc-tiers"))
_KEYS = {"ELEVENLABS_API_KEY": "suite-key-not-real", "DEEPGRAM_API_KEY": "suite-key-not-real",
         "LVI_TTS_PROVIDER": None, "LVI_STT_PROVIDER": None}


def _probe(edit=None, env=None, patches=None, spy=False):
    """Build one session with run_bot up to PipelineWorker(...).

    The config is the fixture with `edit` applied (read by the real loader);
    `env` sets (str) or unsets (None) variables for this probe only; `patches`
    replaces bot attributes. Returns locals (run_bot's, at the stop), warnings
    (WARNING+ log lines), error (why run_bot died first, or None) and env_seen
    (keys looked up in os.environ, when spy)."""
    data = yaml.safe_load(_FIXTURE.read_text()) or {}
    data.setdefault("stt", {})
    data.setdefault("tts", {})
    if edit:
        edit(data)
    _CFG.write_text(yaml.safe_dump(data, sort_keys=False))
    config._load.cache_clear()
    seen, lines = {}, []

    def _stop(*a, **k):
        seen.update(sys._getframe(1).f_locals)
        raise _Stop

    saved_bot = {n: getattr(bot, n) for n in (*_STUBBED, "PipelineWorker", *(patches or {}))}
    saved_env = {k: os.environ.get(k) for k in (env or {})}
    real_env = os.environ
    for n in _STUBBED:
        setattr(bot, n, _Stub)
    bot.PipelineWorker = _stop
    for n, v in (patches or {}).items():
        setattr(bot, n, v)
    for k, v in (env or {}).items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    env_spy = _EnvSpy(os.environ) if spy else None
    if env_spy is not None:
        os.environ = env_spy  # noqa: B003 — a read spy, swapped back in finally
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    error = None
    try:
        asyncio.run(bot.run_bot(_Transport(), _RUNNER_ARGS))
    except _Stop:
        pass
    # A probe that dies says why.
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        logger.remove(sink)
        os.environ = real_env  # noqa: B003 — the real mapping back
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, v in saved_bot.items():
            setattr(bot, n, v)
    if isinstance(seen.get("tts"), ServiceSwitcher):
        tts_fallback.close_fallback(seen["tts"])
    return types.SimpleNamespace(locals=seen, warnings=lines, error=error,
                                 env_seen=env_spy.seen if env_spy is not None else None)


def _tts(provider, **extra):
    """A config written after this release (only the tts keys that stay), with
    tts.provider set (None = no key at all) plus `extra` keys."""
    def edit(data):
        for k in [k for k in data["tts"] if k not in _TTS_KEYS]:
            data["tts"].pop(k)
        if provider is None:
            data["tts"].pop("provider", None)
        else:
            data["tts"]["provider"] = provider
        data["tts"].update(extra)
    return edit


def _kind(svc):
    return getattr(svc, "_tts_provider", type(svc).__name__)


# Review focus: an unknown value from the env with no config key.
_built = []


class _NoElevenLabs:
    def __init__(self, *a, **k):
        _built.append(k)
        raise AssertionError("suite: ElevenLabs was constructed")


_p = _probe(_tts(None), env={**_KEYS, "LVI_TTS_PROVIDER": "nonesuch", "ELEVENLABS_API_KEY": None},
            patches={"TrackedElevenLabsTTSService": _NoElevenLabs, "TrackedElevenLabsDialogueTTSService": _NoElevenLabs},
            spy=True)
_unknown = [ln for ln in _p.warnings if "unknown provider" in ln]
check("speech tiers: LVI_TTS_PROVIDER=nonesuch with no config key -> Kokoro speaks, one warning names the value",
      _p.error is None and _p.locals.get("tts_provider") == "kokoro" and _kind(_p.locals.get("tts")) == "kokoro"
      and len(_unknown) == 1 and "'nonesuch'" in _unknown[0] and "Kokoro" in _unknown[0],
      f"error={_p.error} provider={_p.locals.get('tts_provider')} warnings={_unknown}")
check("speech tiers: the unknown TTS path never builds ElevenLabs and never looks up ELEVENLABS_API_KEY",
      _p.error is None and not _built and _p.env_seen is not None and "ELEVENLABS_API_KEY" not in _p.env_seen,
      f"error={_p.error} built={len(_built)} looked_up={sorted(k for k in (_p.env_seen or ()) if 'ELEVEN' in k)}")

# Every cloud voice gets the Kokoro standby — no fallback key needed.
for _prov in [p for p in tts_catalog.PROVIDER_NAMES if tts_catalog.is_cloud(p)]:
    _p = _probe(_tts(_prov), env=_KEYS)
    _sw, _prim, _stand = (_p.locals.get(k) for k in ("tts", "tts_primary", "tts_standby"))
    check(f"speech tiers: cloud TTS {_prov} gets the Kokoro standby with no fallback key in config",
          _p.error is None and _p.locals.get("tts_provider") == _prov and isinstance(_sw, ServiceSwitcher)
          and list(_sw.services) == [_prim, _stand] and _kind(_stand) == "kokoro"
          and getattr(_sw, "_lenoxvi_standby_name", None) == "kokoro",
          f"error={_p.error} tts={type(_sw).__name__} standby={_kind(_stand)}")

_p = _probe(_tts("elevenlabs", kokoro_voice="bm_george", kokoro_speed=1.2), env=_KEYS)
_stand = _p.locals.get("tts_standby")
_want = (tts_kokoro.resolve_voice(config.cfg_env("tts", "kokoro_voice", "LVI_TTS_KOKORO_VOICE", tts_kokoro.DEFAULT_VOICE)),
         float(config.cfg_env("tts", "kokoro_speed", "LVI_TTS_KOKORO_SPEED", tts_kokoro.DEFAULT_SPEED)))
_got = (getattr(getattr(_stand, "_settings", None), "voice", None), getattr(getattr(_stand, "_settings", None), "speed", None))
check("speech tiers: the standby speaks with tts.kokoro_voice / kokoro_speed (the keys Kokoro uses as the main voice)",
      _p.error is None and _stand is not None and _got == _want, f"error={_p.error} got={_got} want={_want}")

_p = _probe(_tts("elevenlabs", fallback="off"), env=_KEYS)
check("speech tiers: an old config's tts.fallback: off is not read — the Kokoro standby stays armed",
      _p.error is None and isinstance(_p.locals.get("tts"), ServiceSwitcher)
      and _kind(_p.locals.get("tts_standby")) == "kokoro", f"error={_p.error} tts={type(_p.locals.get('tts')).__name__}")

for _prov in ("kokoro", "localai"):
    _p = _probe(_tts(_prov), env=_KEYS)
    check(f"speech tiers: {_prov} TTS runs without a switcher (Kokoro needs no standby; LocalAI has its own)",
          _p.error is None and _p.locals.get("tts_standby") is None
          and _p.locals.get("tts") is _p.locals.get("tts_primary") and _kind(_p.locals.get("tts")) == _prov,
          f"error={_p.error} tts={_kind(_p.locals.get('tts'))}")

# Review focus: odd case and spaces mean that provider.
_p = _probe(_tts(" ElevenLabs "), env=_KEYS)
check("speech tiers: tts.provider ' ElevenLabs ' means ElevenLabs (case and spaces ignored) — Kokoro standby, no warning",
      _p.error is None and _p.locals.get("tts_provider") == "elevenlabs"
      and _kind(_p.locals.get("tts_primary")) == "elevenlabs" and isinstance(_p.locals.get("tts"), ServiceSwitcher)
      and not [ln for ln in _p.warnings if "unknown provider" in ln],
      f"error={_p.error} provider={_p.locals.get('tts_provider')!r}")

# Review focus: Kokoro's files missing on a cloud-voice box.
tts_kokoro._reset_for_tests()
_saved_files_problem = tts_kokoro._files_problem
tts_kokoro._files_problem = lambda: f"{_SCRATCH}/kokoro-v1.0.onnx is missing"
try:
    _p = _probe(_tts("elevenlabs"), env=_KEYS)
finally:
    tts_kokoro._files_problem = _saved_files_problem
    _fake_kokoro()
_without = [ln for ln in _p.warnings if "running WITHOUT a fallback" in ln]
check("speech tiers: Kokoro files missing -> the cloud voice still builds, no standby, ONE 'running WITHOUT a fallback' "
      "warning naming scripts/fetch_kokoro_models.sh",
      _p.error is None and _p.locals.get("tts_standby") is None and _p.locals.get("tts") is _p.locals.get("tts_primary")
      and _kind(_p.locals.get("tts")) == "elevenlabs" and len(_without) == 1
      and "scripts/fetch_kokoro_models.sh" in _without[0], f"error={_p.error} warnings={_without}")
tts_kokoro.kokoro_available = _real_kokoro_available

if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
