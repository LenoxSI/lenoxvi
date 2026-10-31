"""upload_limit — pure-ASGI middleware that caps the dashboard's request
bodies and checks the mutation guard BEFORE FastAPI parses them.

Why this exists: FastAPI reads the whole body (spooling a multipart
upload, buffering a JSON one) before it solves the route's dependencies,
so the GUARD dependency and every in-route size check run only after
the body is already in memory or a temp file, and nothing else bounds
it. A LAN client could fill memory or the temp directory without a
token. This middleware sits in front of the parse, for every
POST/PUT/PATCH/DELETE under /api/dashboard/ (matched on the exact,
case-sensitive path the router matches on):
- the guard (guard.mutation_guard — Host, Origin, optional token) runs
  on the headers alone; a refusal answers with the guard's own status
  and body, and the body is never read;
- a declared Content-Length over the path's cap is a 413, body unread;
- otherwise the bytes are counted as the app pulls them through
  `receive` (chunked bodies, or a Content-Length that lies), and the
  request ends in a 413 the moment the count passes the cap. Nothing is
  buffered here.
The two upload paths have caps sized to their files; every other route
gets DEFAULT_BODY_CAP. Reads (GET/HEAD/OPTIONS) and paths outside
/api/dashboard/ (the node offer, the page and its assets) pass through
untouched.

The in-route checks stay: they are the precise, post-parse validation
(the upload caps here allow for multipart framing on top of the file).
"""

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.requests import Request

from .guard import mutation_guard

# The OTA app partition: an image larger than it can never be flashed.
FIRMWARE_IMAGE_MAX = 0x3D0000
WAKE_MODEL_MAX = 50 * 1024 * 1024
# Boundaries, part headers and small form fields around the file.
MULTIPART_OVERHEAD = 64 * 1024

UPLOAD_BODY_CAPS = {
    "/api/dashboard/firmware/upload": FIRMWARE_IMAGE_MAX + MULTIPART_OVERHEAD,
    "/api/dashboard/oww/models": WAKE_MODEL_MAX + MULTIPART_OVERHEAD,
}
# Every other dashboard write is a small JSON body: the largest the UI
# sends, a full room mapping with per-room agents, is at most about 200
# bytes a room.
DEFAULT_BODY_CAP = 256 * 1024

GUARDED_PREFIX = "/api/dashboard/"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _BodyTooLarge(Exception):
    pass


def _declared_length(scope) -> int | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == b"content-length":
            try:
                return int(v)
            except ValueError:
                return None  # the server rejects these; count instead
    return None


async def _refuse(scope, receive, send, status: int, detail: str,
                  headers: dict | None = None) -> None:
    # Connection: close — the client may still be sending the body we refused.
    resp = JSONResponse({"detail": detail}, status_code=status,
                        headers={**(headers or {}), "connection": "close"})
    await resp(scope, receive, send)


class UploadLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path") or ""
        if (scope.get("type") != "http" or scope.get("method") not in MUTATING_METHODS
                or not path.startswith(GUARDED_PREFIX)):
            return await self.app(scope, receive, send)

        try:
            mutation_guard(Request(scope))
        except HTTPException as e:
            return await _refuse(scope, receive, send, e.status_code, e.detail, e.headers)

        # A trailing slash only earns the router's redirect to the real path:
        # size it like that path so the redirect isn't a 413 instead.
        cap = UPLOAD_BODY_CAPS.get(path.rstrip("/"), DEFAULT_BODY_CAP)
        too_large = f"request body too large for {path} (limit {cap} bytes)"
        declared = _declared_length(scope)
        if declared is not None and declared > cap:
            logger.warning(f"upload_limit: refused {path}: declared {declared} bytes, cap {cap}")
            return await _refuse(scope, receive, send, 413, too_large)

        received = 0
        over = False
        started = False

        async def capped_receive():
            nonlocal received, over
            if over:
                raise _BodyTooLarge
            msg = await receive()
            if msg.get("type") == "http.request":
                received += len(msg.get("body") or b"")
                if received > cap:
                    over = True
                    raise _BodyTooLarge
            return msg

        async def capped_send(msg):
            nonlocal started
            if over and not started:
                return  # the app's answer to the aborted read; the 413 replaces it
            if msg.get("type") == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, capped_receive, capped_send)
        except Exception:
            if not over:
                raise
        if over and not started:
            logger.warning(f"upload_limit: refused {path}: body passed the {cap}-byte cap mid-stream")
            await _refuse(scope, receive, send, 413, too_large)
