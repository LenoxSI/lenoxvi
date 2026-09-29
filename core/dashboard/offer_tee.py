"""offer_tee — pure-ASGI middleware that ties offer `room` to `pc_id`.

Why this exists: the mic node's firmware identifies itself by a `room`
string inside the /api/offer JSON (set at firmware build time), but
pipecat generates the connection's pc_id server-side and never exposes
the room to the app. This middleware tees the offer request body
(room) and the answer response body (pc_id) WITHOUT consuming or
mutating them, and records the pair in the dashboard registry.

Pure ASGI (not BaseHTTPMiddleware) on purpose: wrapping receive/send
directly avoids any framework body-replay pitfalls, and the tee is
path-gated to POST /api/offer so every other request passes through
untouched. Failures inside the tee never break the offer path.
"""

import json
from collections.abc import Awaitable, Callable

from loguru import logger

OFFER_PATH = "/api/offer"


class OfferTeeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != OFFER_PATH:
            return await self.app(scope, receive, send)

        req_body = bytearray()
        resp_body = bytearray()

        async def tee_receive():
            msg = await receive()
            if msg.get("type") == "http.request" and msg.get("body"):
                req_body.extend(msg["body"])
            return msg

        async def tee_send(msg):
            if msg.get("type") == "http.response.body" and msg.get("body"):
                resp_body.extend(msg["body"])
            await send(msg)

        try:
            await self.app(scope, tee_receive, tee_send)
        finally:
            # Never let a tee failure take down the offer path.
            try:
                room = None
                pc_id = None
                try:
                    offer = json.loads(bytes(req_body) or b"{}")
                    room = offer.get("room")
                except (ValueError, AttributeError):
                    pass
                try:
                    answer = json.loads(bytes(resp_body) or b"{}")
                    # key name varies across pipecat versions — try both
                    pc_id = answer.get("pc_id") or answer.get("pcId")
                except (ValueError, AttributeError):
                    pass
                from . import registry

                registry.note_offer(pc_id, room)
                logger.debug(f"offer_tee: room={room!r} pc_id={pc_id!r}")
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(f"offer_tee: failed to record offer: {e}")