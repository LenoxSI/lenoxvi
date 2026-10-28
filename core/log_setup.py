"""Log level for the bot's own (loguru) logs, pipecat's included.

config.yaml `logging.level` (env fallback LVI_LOG_LEVEL), default INFO: a
fresh install logs what an operator needs; DEBUG roughly doubles the journal
and is for active development. Tracebacks never include variable values
(diagnose off), so a key held in a local can't end up in a log.
uvicorn's access lines use stdlib logging and aren't affected.
"""

import sys

from config import cfg_env
from loguru import logger

LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")
DEFAULT = "INFO"


def configure(sink=None) -> str:
    """Replace loguru's handlers with one at the configured level. Returns the
    level applied; an unknown value logs a warning and falls back to INFO."""
    raw = cfg_env("logging", "level", "LVI_LOG_LEVEL", DEFAULT)
    level = str(raw).strip().upper() if raw not in (None, "") else DEFAULT
    bad = level not in LEVELS
    if bad:
        level = DEFAULT
    logger.remove()
    logger.add(sink if sink is not None else sys.stderr, level=level, diagnose=False)
    if bad:
        logger.warning(f"log_setup: logging.level {raw!r} isn't a level ({', '.join(LEVELS)}); using {DEFAULT}")
    return level
