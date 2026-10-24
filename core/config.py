"""config — single source of truth for LenoxVI tunables (2026-09-19).

config.yaml is where humans tune the bot. Precedence for every knob:

    config.yaml value  >  LVI_* env var  >  hard-coded default

so config.yaml always wins when present, .env overrides code defaults when
a yaml section is missing, and nothing breaks if the file is absent.
Secrets NEVER live here (API keys stay in gitignored .env); this file holds
behavior only. Sonos zones/clip-server keys are consumed directly by
sonos_output.py; everything else reads through `cfg()` below.

Loaded lazily on the first cfg() call and cached for the process; pass
nothing around — modules import `cfg()` and read their section, so a
restart picks up edits.
"""

import copy
import os
import threading
from pathlib import Path

import yaml

# config.yaml lives at the REPO ROOT,
# next to bot.py — never relative to whatever cwd the process happens
# to run from. A relative "config.yaml" silently resolved to {} when
# anything ran from another directory (the suite caught it via its own
# LVI_CONFIG override; production never did — but systemd cwd drift or
# an accidental `python core/...` invocation would have tripped it).
_REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = os.getenv("LVI_CONFIG", str(_REPO_ROOT / "config.yaml"))

# Code default for a speaker zone's voice-clip volume when config has no
# `<backend>.clip_volume`. ONE value for every
# reader — sonos_output had 70, wiim_output/dashboard 35, the React UI 70,
# so a config missing the key played Sonos at 70 while the slider showed
# something else. 35 = the direct-path value, tuned by ear 2026-10-04
# (70 was the obsolete through-the-Triad-matrix value).
DEFAULT_CLIP_VOLUME = 35

# The config this process loaded first. Dashboard writes clear _load's cache
# so the API serves saved values, but settings read only at startup keep
# running the boot values until a restart; the dashboard diffs the two to
# show what a restart would apply.
_BOOT = None


# _load's cache is hand-rolled, not @lru_cache: lru_cache raced dashboard
# writes — a reader mid-parse when a write cleared the (already empty)
# cache stored its PRE-write result on return, and every cfg() then kept
# serving that stale copy until the next write. cache_clear() bumps the
# generation, and a load caches its result only if no clear happened
# while it parsed. The lock guards the bookkeeping alone — reads and
# YAML parsing stay outside it so readers never serialise on file I/O.
_CACHE_LOCK = threading.Lock()
_CACHE: dict | None = None  # the cached config; None = nothing cached yet
_GENERATION = 0  # bumped by every cache_clear


class _CachedLoad:
    """Call for the (cached) config; cache_clear() after replacing config.yaml."""

    def __call__(self) -> dict:
        global _BOOT, _CACHE
        with _CACHE_LOCK:
            cached = _CACHE
            if cached is not None:
                return cached
            gen = _GENERATION
        p = Path(CONFIG_PATH)
        if not p.is_file():
            data = {}
        else:
            try:
                data = yaml.safe_load(p.read_text()) or {}
            except yaml.YAMLError as e:
                # A broken config must be loud at boot, not a silent empty dict.
                raise RuntimeError(f"config.yaml parse error: {e}") from e
        with _CACHE_LOCK:
            # Under the lock so exactly one load ever sets _BOOT, even when
            # two parse concurrently (deep-copied, never refreshed).
            if _BOOT is None:
                _BOOT = copy.deepcopy(data)
            if _GENERATION == gen:  # no cache_clear mid-parse: safe to share
                _CACHE = data
        return data

    def cache_clear(self) -> None:
        """Drop the cached config; a load still parsing won't repopulate it."""
        global _CACHE, _GENERATION
        with _CACHE_LOCK:
            _GENERATION += 1
            _CACHE = None


_load = _CachedLoad()


def boot_config() -> dict:
    """The config as this process first loaded it (never refreshed)."""
    _load()
    return _BOOT


def cfg(section: str, key: str = None, default=None):
    """Read a section (dict) or a section.key value from config.yaml only.

    cfg("wake")              -> the whole wake: dict
    cfg("wake", "threshold") -> float value (or None); use cfg_env for env fallback
    """
    data = _load().get(section, {}) or {}
    if key is None:
        return data
    if key in data and data[key] is not None:
        return data[key]
    return default


def cfg_env(section: str, key: str, env_var: str, default=None):
    """yaml section.key, falling back to the env var, then the default.

    This is the one helper to use for knobs that already have an LVI_* env
    var — keeps .env working unchanged while letting config.yaml win.
    """
    yaml_val = cfg(section, key)
    if yaml_val is not None:
        return yaml_val
    env_val = os.getenv(env_var)
    if env_val is not None:
        return env_val
    return default