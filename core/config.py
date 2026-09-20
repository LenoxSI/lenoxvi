"""config — single source of truth for lenoxvi tunables (2026-09-19).

config.yaml is where humans tune the bot. Precedence for every knob:

    config.yaml value  >  TK_* env var  >  hard-coded default

so config.yaml always wins when present, .env overrides code defaults when
a yaml section is missing, and nothing breaks if the file is absent.
Secrets NEVER live here (API keys stay in gitignored .env); this file holds
behavior only. Sonos zones/clip-server keys are consumed directly by
sonos_output.py; everything else reads through `cfg()` below.

Load once at bot startup (bot.py), pass nothing around — modules import
`cfg()` and read their section lazily so a restart picks up edits.
"""

import os
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = os.getenv("LVI_CONFIG", "config.yaml")


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
    """Read a section (dict) or a section.key value with env fallback.

    cfg("wake")             -> the whole wake: dict
    cfg("wake", "threshold")-> float value (or None); env fallback below
    """
    data = _load().get(section, {}) or {}
    if key is None:
        return data
    if key in data and data[key] is not None:
        return data[key]
    return default


def cfg_env(section: str, key: str, env_var: str, default=None):
    """yaml section.key, falling back to the env var, then the default.

    This is the one helper to use for knobs that already have a TK_* env
    var — keeps .env working unchanged while letting config.yaml win.
    """
    yaml_val = cfg(section, key)
    if yaml_val is not None:
        return yaml_val
    env_val = os.getenv(env_var)
    if env_val is not None:
        return env_val
    return default