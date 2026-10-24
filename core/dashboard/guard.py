"""guard — browser-borne request protection for the dashboard's mutating
routes.

Threat model, honestly scoped:
- The LAN is TRUSTED by design: anyone on it can
  read status and edit mappings. No auth for them.
- The BROWSER is not fully trusted: a malicious page the operator visits can
  script `fetch("https://<lan-ip>:7860/api/dashboard/firmware/upload",
  {method: "POST", body: fd})` — multipart counts as a CORS "simple
  request", so no preflight happens and the browser fires it blind. That
  is a cross-site firmware push / node restart from any tab.
- Defense: browsers ALWAYS attach an Origin header to cross-origin
  requests (even simple ones, even with fetch keepalive). Same-origin
  pages (the dashboard itself) send an Origin matching the Host. So:
  Origin present and != Host  -> 403. Classic CSRF/rebinding shapes die
  here (rebinding needs a public-DNS hostname in Host, which check 2
  rejects).
- Non-browser callers (curl, scripts, Home Assistant) send no Origin and
  pass — the API-first contract is unchanged.

Optional token (LVI_DASHBOARD_TOKEN in .env): if set, mutating routes
additionally require `X-Dashboard-Token` to match. For LAN-only installs
leave it unset (nothing changes); set it if you expose the dashboard
beyond the LAN (against advice) or just want belt-and-suspenders. The
dashboard page prompts for it once and remembers it in localStorage.
Read routes stay open either way.

Recording a room is the exception: hearing a room is not the same trust as
editing a mapping, so the recorder routes (start, stop, list, download,
delete) refuse outright unless LVI_DASHBOARD_TOKEN is set, and with it set
they require the token like every guarded route.
"""

import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

TOKEN_HEADER = "X-Dashboard-Token"


def _host_ok(hostname: str | None) -> bool:
    """Host must be an IP literal, localhost, or an mDNS .local name —
    never a public-DNS hostname (that's the DNS-rebinding vector)."""
    if not hostname:
        return False
    if hostname in ("localhost",) or hostname.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def _port(scheme: str, port: int | None) -> int | None:
    """Explicit port, else the scheme's default (None for other schemes)."""
    if port is not None:
        return port
    return {"https": 443, "http": 80}.get((scheme or "").lower())


def mutation_guard(request: Request) -> None:
    """FastAPI dependency for every mutating (POST/PUT/DELETE) route."""
    host = request.url.hostname  # Host header, parsed, lowercased
    if not _host_ok(host):
        raise HTTPException(
            status_code=403,
            detail="dashboard: Host must be the bot's LAN IP, localhost, or a .local name",
        )

    origin = request.headers.get("origin")
    if origin is not None:
        # Same-origin fetches match Host AND port: a page on another port
        # of the same IP is another origin. A missing port is its scheme's
        # default on each side (Origin carries its scheme; Host's is the
        # request's). "null"/garbage origins are mismatches.
        try:
            o = urlsplit(origin)
            origin_host, origin_port = o.hostname, _port(o.scheme, o.port)
        except ValueError:  # a malformed port
            origin_host, origin_port = None, None
        if (origin_host is None or origin_host != host
                or origin_port != _port(request.url.scheme, request.url.port)):
            raise HTTPException(
                status_code=403,
                detail="dashboard: cross-origin mutations are not allowed",
            )

    token = os.environ.get("LVI_DASHBOARD_TOKEN", "")
    if token:
        supplied = request.headers.get(TOKEN_HEADER, "")
        # Non-ASCII token or header: str/str compare_digest raises
        # TypeError on it (the caller sees a 500), so compare bytes. The
        # server decodes headers as latin-1: a browser sends "é" as the one
        # byte 0xE9 (decoded back to "é"), curl/scripts send its raw UTF-8
        # bytes (decoded to "Ã©"). Accept either against the token's UTF-8;
        # both compares always run. A wrong token is still a plain 401.
        want = token.encode("utf-8", "surrogateescape")
        as_sent = supplied.encode("latin-1", "replace")
        as_text = supplied.encode("utf-8", "surrogateescape")
        if not (hmac.compare_digest(as_sent, want) | hmac.compare_digest(as_text, want)):
            raise HTTPException(
                status_code=401, detail="dashboard: invalid or missing token"
            )


def recording_guard() -> None:
    """FastAPI dependency for the recorder routes, after mutation_guard:
    no token configured = no recording, for anyone on the LAN."""
    if not os.environ.get("LVI_DASHBOARD_TOKEN", ""):
        raise HTTPException(
            status_code=403,
            detail="recording needs a dashboard token: set LVI_DASHBOARD_TOKEN",
        )