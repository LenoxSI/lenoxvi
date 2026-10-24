"""Intelligence lanes: the ONE list of kinds.

Every caller that needs to know which intelligence systems exist reads this
registry: the voice agent, intelligence_target, the dashboard routes, the
key catalog, bot.py dispatch and (through GET /intelligence/lanes) the UI.
Order matters: it is the dashboard's display order and the Keys card order.

Adding a lane: write `<kind>.py` with a Lane subclass, add it below.
"""

from intelligence.base import AGENT_RE, Lane, TurnContext  # noqa: F401
from intelligence.hermes import HermesLane
from intelligence.nanobot import NanobotLane
from intelligence.openclaw import OpenClawLane

LANES: dict[str, Lane] = {lane.kind: lane for lane in (
    HermesLane(),
    OpenClawLane(),
    NanobotLane(),
)}
KINDS: tuple[str, ...] = tuple(LANES)
# The lane an unset or unknown intelligence.kind falls back to (every
# `kind or "hermes"` default reads this).
DEFAULT_KIND = "hermes"


def get(kind) -> Lane | None:
    """The lane for `kind`, or None for an unknown kind (incl. 'stub')."""
    return LANES.get(str(kind or "").lower()) if kind else None
