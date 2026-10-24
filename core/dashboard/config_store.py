"""config_store — comment-preserving, atomic config.yaml writes for the
dashboard.

The dashboard edits the SAME config.yaml a human would hand-edit:
same keys, same precedence, developer hand-edits stay
possible and authoritative. ruamel.yaml round-trip mode preserves
comments and formatting; writes are atomic (temp file + os.replace)
and preceded by a single rolling backup (config.backup.yaml, gitignored).

Writes apply on the next bot restart — the bot caches config at boot
(core/config.py lru_cache). The dashboard says so in its UI; it never
restarts the bot on its own (that is systemd's job, deliberately kept
out of the web path for now).
"""

import copy
import os
import shutil
import threading
from pathlib import Path

from loguru import logger
from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter

from config import CONFIG_PATH

_YAML = YAML()  # round-trip mode: comments and layout survive
_YAML.preserve_quotes = True

# The bot reads config.yaml with PyYAML (YAML 1.1: on/off/yes/no/true/
# false/null — and y/n/~, any case — resolve to bool/None), but this
# writer is YAML 1.2, which emits those words PLAIN. A room named "on"
# was written as a bare `on:` key and read back as key True, so it
# played nowhere. Any plain scalar (key OR value) that YAML 1.1 would
# resolve to something else is written double-quoted; every other string
# stays plain, exactly as today. ruamel has no per-instance registry
# (add_representer mutates the representer class) — fine, this module
# is the process's only ruamel writer.
_YAML11_PLAIN = frozenset({"on", "off", "yes", "no", "y", "n", "true", "false", "null", "~"})


def _represent_str(dumper, data):
    if str(data).lower() in _YAML11_PLAIN:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return RoundTripRepresenter.represent_str(dumper, data)


_YAML.representer.add_representer(str, _represent_str)

# One lock for every read and write. The ruamel YAML() instance keeps its
# parser/emitter state on itself (not thread-safe) and every write shares one
# tmp file, so a write from a worker thread racing a loop-side read or write
# could garble config.yaml — and a garbled config fails the next boot.
# Re-entrant: read-modify-write callers hold it across both steps.
LOCK = threading.RLock()


def _merge_into(dst, src: dict) -> None:
    """Update the loaded mapping `dst` IN PLACE to equal `src`.

    Replacing a section object drops every comment ruamel stored on it -
    its own notes and the comment block above the NEXT top-level key - and
    most callers pass plain dict copies. Merging keeps existing keys (and
    their comments) where they are: changed values update, new keys
    append, keys missing from `src` are removed."""
    for k in [k for k in dst if k not in src]:
        del dst[k]
    for k, v in src.items():
        cur = dst.get(k) if k in dst else None
        if isinstance(cur, dict) and isinstance(v, dict):
            _merge_into(cur, v)
        elif k not in dst or cur != v:
            dst[k] = v


def _set_section(data, section: str, value) -> None:
    value = value or {}
    cur = data.get(section)
    if isinstance(cur, dict) and isinstance(value, dict):
        _merge_into(cur, value)
    else:
        data[section] = value


def _path() -> Path:
    # Same resolution as core/config.py — one process, one config file.
    return Path(os.getenv("LVI_CONFIG", CONFIG_PATH))


def read_section(section: str, default=None):
    with LOCK:
        p = _path()
        if not p.is_file():
            return default
        data = _YAML.load(p.read_text()) or {}
        return data.get(section) or default


def read_sections(sections: list[str]) -> dict:
    with LOCK:
        p = _path()
        if not p.is_file():
            return {s: None for s in sections}
        data = _YAML.load(p.read_text()) or {}
        return {s: data.get(s) for s in sections}


def write_section(section: str, value: dict) -> dict:
    """Replace `section` with `value` in config.yaml.

    Returns {"before": ..., "after": ...} for the API response/log.
    Raises FileNotFoundError if config.yaml is absent (fresh clone that
    skipped `cp config.example.yaml config.yaml`) or ValueError on a
    bad key.
    """
    with LOCK:
        p = _path()
        if not p.is_file():
            raise FileNotFoundError(
                f"{p} not found — copy config.example.yaml to config.yaml first"
            )
        data = _YAML.load(p.read_text()) or {}
        # deep copy: the merge below updates nested maps in place
        before = copy.deepcopy(dict(data.get(section) or {}))
        _set_section(data, section, value)
        _atomic_write(p, data)
        logger.info(f"config_store: rewrote '{section}' via dashboard ({len(value)} entries)")
        return {"before": before, "after": dict(value)}


def write_sections(sections: dict) -> dict:
    """Write MULTIPLE sections in ONE
    atomic pass — one backup, one tmp write, one cache refresh. The
    zone-delete flow used to do two write_section calls back-to-back; a
    crash between them left config.yaml with the zone gone but its
    dangling room mappings still pointing at it (and two backups deep
    of history churn). Callers that need the before/after diff read the
    sections first via read_sections.
    """
    with LOCK:
        p = _path()
        if not p.is_file():
            raise FileNotFoundError(
                f"{p} not found — copy config.example.yaml to config.yaml first"
            )
        data = _YAML.load(p.read_text()) or {}
        before = {k: copy.deepcopy(dict(data.get(k) or {})) for k in sections}
        for section, value in sections.items():
            _set_section(data, section, value)
        _atomic_write(p, data)
        for section in sections:
            logger.info(f"config_store: rewrote '{section}' via dashboard "
                        f"({len(sections[section])} entries, multi-section write)")
        return {"before": before, "after": {k: dict(v) for k, v in sections.items()}}


def _atomic_write(p: Path, data) -> None:
    backup = p.with_name(p.stem + ".backup" + p.suffix)
    if p.exists():
        shutil.copy2(p, backup)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as f:
        _YAML.dump(data, f)
    # keep the original file's permissions (config.yaml may be 600)
    os.chmod(tmp, p.stat().st_mode & 0o777)
    os.replace(tmp, p)
    # The process caches config at boot (core/config.py lru_cache) —
    # a dashboard write must refresh it or the API keeps serving the
    # pre-write values (and new sessions would miss just-added zones).
    try:
        from config import _load

        _load.cache_clear()
    except Exception:  # pragma: no cover — config import is core's job
        pass