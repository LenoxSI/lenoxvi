"""offer_tee — pure-ASGI middleware that ties offer `room` to `pc_id`.

Why this exists: the mic node's firmware identifies itself by a `room`
string inside the /api/offer JSON (set at firmware build time), but
pipecat generates the connection's pc_id server-side and never exposes
the room to the app. This middleware tees the offer request body
(room) and the answer response body (pc_id) WITHOUT consuming or
mutating them, and records the pair in the dashboard registry.

LANDMINE (measured live): the bot pipeline runs as a
Starlette BACKGROUND TASK of the offer request — `app(scope, receive,
send)` does not return until the WebRTC session ENDS (the bot sets
idle_timeout_secs=None). Correlating after `await self.app(...)`
fires at session end, tens of minutes after the offer — long after
on_client_connected already needed the room. Correlation MUST happen
at response-SEND time inside the send wrapper: the answer body (with
pc_id) flows outward before the client completes DTLS, i.e. before
the connected event fires.

Pure ASGI (not BaseHTTPMiddleware) on purpose: wrapping receive/send
directly avoids framework body-replay pitfalls, and the tee is
path-gated to POST /api/offer so every other request passes through
untouched. Failures inside the tee never break the offer path.
"""

import json

from loguru import logger

OFFER_PATH = "/api/offer"


def _extract_room(body: bytes):
    try:
        return json.loads(body or b"{}").get("room")
    except (ValueError, AttributeError):
        return None


def _extract_pc_id(body: bytes):
    try:
        answer = json.loads(body or b"{}")
        return answer.get("pc_id") or answer.get("pcId")
    except (ValueError, AttributeError):
        return None


def _correlate(pc_id, room) -> None:
    from . import registry

    registry.note_offer(pc_id, room)
    logger.debug(f"offer_tee: room={room!r} pc_id={pc_id!r}")


class OfferTeeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("path") != OFFER_PATH
            or scope.get("method") != "POST"
        ):
            return await self.app(scope, receive, send)

        req_buf = bytearray()
        resp_buf = bytearray()
        room = None
        recorded = False

        async def tee_receive():
            nonlocal room
            msg = await receive()
            if msg.get("type") == "http.request" and msg.get("body"):
                req_buf.extend(msg["body"])
                # Request body arrives at offer time — grab the room NOW
                # (it is needed the moment the response is sent).
                r = _extract_room(bytes(req_buf))
                if r is not None:
                    room = r
            return msg

        async def tee_send(msg):
            nonlocal recorded
            # The answer body carries the pc_id and flows outward BEFORE
            # the client completes DTLS (i.e. before the connected
            # event) — correlate HERE, never after the app call returns
            # (that is session end; see module docstring).
            if msg.get("type") == "http.response.body" and msg.get("body"):
                resp_buf.extend(msg["body"])
                pc_id = None
                if not recorded:
                    pc_id = _extract_pc_id(bytes(resp_buf))
                if pc_id:
                    recorded = True
                    try:
                        _correlate(pc_id, room)
                    except Exception as e:  # never break the offer path
                        logger.warning(f"offer_tee: correlation failed: {e}")
            await send(msg)

        # No finally-block correlation: the app call below does not
        # return until the WebRTC session ends (pipeline runs as this
        # request's background task).
        await self.app(scope, tee_receive, tee_send)