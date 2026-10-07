"""dashboard — web UI for mapping, node status, and zone management.

Served by the bot process itself (the pipecat runner's FastAPI app on
:7860) — same registration pattern as core/firmware_server.py. No
extra process, no new ports, no build step.

Design contract (design note, ratified 2026-09-29):
- The HTML page (page.html) is a thin, unstyled skin over JSON
  /api/... endpoints. A later restyle swaps the page only; endpoints
  are the stable contract (they are also what a future host-side
  integration would consume).
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

    try:
        from . import registry
        from .offer_tee import OfferTeeMiddleware
        from .routes import router
    except ImportError as e:
        logger.warning(
            f"dashboard: optional deps missing ({e}) — not mounting. "
            "pip install soco ruamel.yaml to enable it."
        )
        return

    registry.mark_boot()

    # Tee /api/offer so node room labels can be tied to connections
    # (the firmware sends `room` in the offer; pipecat generates the
    # pc_id server-side — see offer_tee.py).
    app.add_middleware(OfferTeeMiddleware)
    app.include_router(router)

    # HeroUI rebuild (2026-10-07, design note): FastAPI serves the
    # prebuilt React app from dashboard-ui/dist — a fresh clone needs
    # zero node toolchain (dist/ is committed; the build stays dev-side,
    # npm run build in dashboard-ui/). The legacy page.html stays as a
    # fallback for a bare clone without dist.
    ui_index = Path(__file__).resolve().parents[2] / "dashboard-ui" / "dist" / "index.html"
    legacy_page = (Path(__file__).parent / "page.html").read_text()

    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page():
        if ui_index.is_file():
            return FileResponse(ui_index)
        return HTMLResponse(legacy_page)

    ui_assets = ui_index.parent / "assets"
    if ui_assets.is_dir():
        app.mount("/assets", StaticFiles(directory=ui_assets), name="ui-assets")

    # Dist-root files (favicon.svg, wordmark SVGs) — the built page
    # references them with './' paths that resolve against the SITE
    # root from /dashboard (no trailing slash). Serve each one at both
    # root and /dashboard/ so the lockups load either way (bug 1 fix,
    # 2026-10-07: 404s on /lenoxvi-horizontal-on-dark.svg).
    from fastapi import HTTPException as _HTTPException
    from fastapi import Response as _Resp

    @app.get("/{root_file}", include_in_schema=False)
    async def ui_root_file(root_file: str):
        if root_file in {"favicon.svg", "icons.svg",
                         "lenoxvi-horizontal-on-dark.svg",
                         "lenoxvi-horizontal-on-light.svg"}:
            f = ui_index.parent / root_file
            if f.is_file():
                media = "image/svg+xml" if f.suffix == ".svg" else "application/octet-stream"
                return _Resp(content=f.read_bytes(), media_type=media)
        raise _HTTPException(status_code=404, detail="not found")

    logger.info("dashboard: serving /dashboard (HeroUI build if present, legacy fallback)")