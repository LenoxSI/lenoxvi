"""dashboard — web UI for mapping, node status, and zone management.

Served by the bot process itself (the pipecat runner's FastAPI app on
:7860) — same registration pattern as core/firmware_server.py. No
extra process, no new ports, no build step.

Design contract:
- The page is the prebuilt React app in dashboard-ui/dist. The old
  page.html thin JSON skin is deleted (dead since the
  HeroUI port, actively wrong vs the API). Endpoints are the stable
  contract (they are also what a future host-side integration would
  consume).
- No auth: LAN-only page by design. Users who expose it to the
  internet do so at their own risk.
- Config edits write the SAME config.yaml a human would hand-edit,
  comment-preserving and atomic (config_store); they apply on the
  next bot restart (the bot caches config at boot — core/config.py).
- Kill switch: set `dashboard.enabled: false` in config.yaml and the
  routes never mount.

Import note: this package assumes the repo's `core/` dir is on
sys.path (bot.py inserts it), so `import config` and
`import dashboard` both resolve — same mechanism as every other core
module.
"""

from pathlib import Path

from loguru import logger

# Files at the dist root the built page links with './' paths (served at /
# and /dashboard/). Name only what the build ships; the suite pins that.
DIST_ROOT_FILES = frozenset({
    "lenoxvi-horizontal-lime-on-dark.svg",
    "lenoxvi-horizontal-lime-on-light.svg",
    "favicon.ico",
    "apple-touch-icon.png",
})
_ROOT_MEDIA = {".svg": "image/svg+xml", ".ico": "image/x-icon", ".png": "image/png"}


def register_dashboard(app) -> None:
    """Mount the dashboard (page + API) on the runner's FastAPI app.

    Call once at process start, before the app begins serving.
    Fail-safe: if the dashboard's optional deps (ruamel.yaml, soco)
    are missing on a fresh clone, log and skip — the voice pipeline
    must never depend on dashboard dependencies.
    """
    from config import cfg

    if not bool(cfg("dashboard", "enabled", True)):
        logger.info("dashboard: disabled in config.yaml — not mounting")
        return

    # Optional deps FIRST and alone: a missing soco/ruamel really is the
    # fresh-clone case that message was written for.
    try:
        import ruamel.yaml  # noqa: F401
        import soco  # noqa: F401
    except ImportError as e:
        logger.warning(
            f"dashboard: optional deps missing ({e}) — not mounting. "
            "pip install soco ruamel.yaml to enable it."
        )
        return

    # Everything past here is OUR code, not a dependency: a broken
    # dashboard import used to fall into the same quiet "optional deps"
    # warning and silently unmount the dashboard. Loud, with the
    # traceback — but still never a crash: the voice pipeline mounts
    # without its UI.
    try:
        from . import registry
        from .offer_tee import OfferTeeMiddleware
        from .routes import router
    except Exception:  # noqa: BLE001 — any failure: loud, never a crash
        logger.exception("dashboard: failed to load — not mounting")
        return

    registry.mark_boot()

    # Tee /api/offer so node room labels can be tied to connections
    # (the firmware sends `room` in the offer; pipecat generates the
    # pc_id server-side — see offer_tee.py).
    app.add_middleware(OfferTeeMiddleware)
    app.include_router(router)

    # HeroUI rebuild: FastAPI serves the
    # prebuilt React app from dashboard-ui/dist — a fresh clone needs
    # zero node toolchain (dist/ is committed; the build stays dev-side,
    # npm run build in dashboard-ui/).
    # The legacy page.html fallback is DELETED —
    # dead since the dist/ commit and actively wrong vs the API (its
    # intelligence key save omits `kind`; extra=forbid 422s it). A
    # clone without dist/ gets an honest 404 naming the build step,
    # never a stale page that lies about the API.
    ui_index = Path(__file__).resolve().parents[2] / "dashboard-ui" / "dist" / "index.html"

    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page():
        if ui_index.is_file():
            return FileResponse(ui_index)
        raise _HTTPException(status_code=404,
                              detail="dashboard UI not built — run npm run build in dashboard-ui/")

    ui_assets = ui_index.parent / "assets"
    if ui_assets.is_dir():
        app.mount("/assets", StaticFiles(directory=ui_assets), name="ui-assets")

    # Dist-root files (wordmark SVGs, favicon.ico, apple-touch-icon) — the built page
    # references them with './' paths that resolve against the SITE
    # root from /dashboard (no trailing slash). Serve each one at both
    # root and /dashboard/ so the lockups load either way (bug 1 fix,
    # 404s on /lenoxvi-horizontal-on-dark.svg).
    from fastapi import Response as _Resp

    @app.get("/{root_file}", include_in_schema=False)
    async def ui_root_file(root_file: str):
        if root_file in DIST_ROOT_FILES:
            f = ui_index.parent / root_file
            if f.is_file():
                media = _ROOT_MEDIA.get(f.suffix, "application/octet-stream")
                return _Resp(content=f.read_bytes(), media_type=media)
        raise _HTTPException(status_code=404, detail="not found")

    logger.info("dashboard: serving /dashboard (HeroUI build; no dist → 404 with the build step)")