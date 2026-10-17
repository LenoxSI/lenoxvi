"""config — single source of truth for lenoxvi tunables (2026-09-19).

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

import os
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _load() -> dict:
    p = Path(CONFIG_PATH)
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        # A broken config must be loud at boot, not a silent empty dict.
        raise RuntimeError(f"config.yaml parse error: {e}") from e


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

    This is the one helper to use for knobs that already have a LVI_* env
    var — keeps .env working unchanged while letting config.yaml win.
    """
    yaml_val = cfg(section, key)
    if yaml_val is not None:
        return yaml_val
    env_val = os.getenv(env_var)
    if env_val is not None:
        return env_val
    return default