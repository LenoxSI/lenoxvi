"""Where LenoxVI keeps its runtime files.

One anchor (the repo, or LVI_STATE_DIR) instead of the working directory:
clips, uploaded firmware, wake models and sticky sessions used to be
written relative to the cwd while the firmware server read from the repo,
so they only lined up because systemd starts the bot in the repo. Every
call reads the environment at call time (tests point LVI_STATE_DIR at a
scratch folder). The older per-file overrides still win: LVI_MODELS_DIR,
LVI_SESSIONS (and LVI_CAPTURES_DIR in room_recorder).
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def state_root() -> Path:
    """Base for every runtime folder: LVI_STATE_DIR, else the repo."""
    env = os.getenv("LVI_STATE_DIR")
    return Path(env) if env else REPO


def clips_dir() -> Path:
    """Reply/test clips the speakers fetch over HTTP."""
    return state_root() / "clips"


def firmware_dir() -> Path:
    """Uploaded node firmware, one sub-folder per track."""
    return state_root() / "firmware"


def models_dir() -> Path:
    """Dashboard-managed wake models (LVI_MODELS_DIR wins)."""
    env = os.getenv("LVI_MODELS_DIR")
    return Path(env) if env else state_root() / "models"


def wake_root() -> Path:
    """Where legacy wake models sit loose (the repo root)."""
    return state_root()


def sessions_file() -> Path:
    """Sticky-session history (LVI_SESSIONS wins)."""
    env = os.getenv("LVI_SESSIONS")
    return Path(env) if env else state_root() / "data" / "sessions.json"
