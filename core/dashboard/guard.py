"""guard — browser-borne request protection for the dashboard's mutating
routes (Opus review H5, 2026-10-01).

Threat model, honestly scoped:
- The LAN is TRUSTED by ratified design (design note): anyone on it can
  read status and edit mappings. No auth for them.
- The BROWSER is not fully trusted: a malicious page the owner visits can
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
  pass — the API-first contract (design note) is unchanged.

Optional token (LVI_DASHBOARD_TOKEN in .env): if set, mutating routes
additionally require `X-Dashboard-Token` to match. For LAN-only installs
leave it unset (nothing changes); set it if you expose the dashboard
beyond the LAN (against advice) or just want belt-and-suspenders. The
dashboard page prompts for it once and remembers it in localStorage.
Read routes stay open either way.
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
        origin_host = urlsplit(origin).hostname
        # Same-origin fetches match Host (Origin includes scheme, Host
        # doesn't — compare hostnames only). "null"/garbage origins are
        # mismatches.
        if origin_host is None or origin_host != host:
            raise HTTPException(
                status_code=403,
                detail="dashboard: cross-origin mutations are not allowed",
            )

    token = os.environ.get("LVI_DASHBOARD_TOKEN", "")
    if token:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(
                status_code=401, detail="dashboard: invalid or missing token"
            )