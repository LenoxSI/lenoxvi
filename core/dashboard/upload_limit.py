"""upload_limit — pure-ASGI middleware that caps the dashboard's upload
bodies and checks the mutation guard BEFORE FastAPI parses them.

Why this exists: FastAPI reads and spools a multipart body before it
solves the route's dependencies, so the GUARD dependency and every
in-route size check run only after the whole upload is already in
memory or a temp file. A LAN client could fill the temp directory
without a token. This middleware sits in front of the parse, for POSTs
to the upload paths only:
- the guard (guard.mutation_guard — Host, Origin, optional token) runs
  on the headers alone; a refusal answers with the guard's own status
  and body, and the body is never read;
- a declared Content-Length over the path's cap is a 413, body unread;
- otherwise the bytes are counted as the app pulls them through
  `receive` (chunked bodies, or a Content-Length that lies), and the
  request ends in a 413 the moment the count passes the cap. Nothing is
  buffered here.

The in-route checks stay: they are the precise, post-parse validation
(the caps here allow for multipart framing on top of the file).
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
        cap = UPLOAD_BODY_CAPS.get(scope.get("path")) if scope.get("type") == "http" else None
        if cap is None or scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        path = scope["path"]
        try:
            mutation_guard(Request(scope))
        except HTTPException as e:
            return await _refuse(scope, receive, send, e.status_code, e.detail, e.headers)

        too_large = f"upload too large for {path} (limit {cap} bytes)"
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
