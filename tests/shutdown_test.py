"""Shutdown test: a stop must finish fast AND run the app's shutdown hook.

Real uvicorn, real FastAPI. The app holds a never-ending BackgroundTask
(the shape of a node's voice pipeline: pipecat runs each session as a
BackgroundTask of /api/offer). Without a graceful-shutdown deadline uvicorn
waits for that task forever ("Waiting for background tasks to complete"),
systemd SIGKILLs it, and the lifespan shutdown — where the runner closes
every peer connection (the goodbye a node can hear) — never runs.

Usage: python shutdown_test.py <core dir>
"""
import asyncio
import os
import socket
import sys
import time
import urllib.request
from contextlib import asynccontextmanager

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "core"))

import serve_opts
import uvicorn
from fastapi import BackgroundTasks, FastAPI

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[PASS] " if cond else "[FAIL] ") + name + ("" if cond else f" — {detail}"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def scenario(kwargs, budget=6.0):
    state = {"closed": False}

    @asynccontextmanager
    async def life(app):
        yield
        state["closed"] = True  # the runner closes every peer connection here

    app = FastAPI(lifespan=life)

    @app.post("/offer")
    async def offer(bg: BackgroundTasks):
        async def pipeline_forever():
            while True:
                await asyncio.sleep(0.5)
        bg.add_task(pipeline_forever)
        return {"ok": True}

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **kwargs))
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    await asyncio.to_thread(urllib.request.urlopen,
                            urllib.request.Request(f"http://127.0.0.1:{port}/offer", data=b"", method="POST"), timeout=5)
    await asyncio.sleep(0.3)  # the background task is now running
    t0 = time.monotonic()
    srv.should_exit = True  # what uvicorn's SIGTERM handler sets
    try:
        await asyncio.wait_for(asyncio.shield(task), budget)
        done = True
    except TimeoutError:
        done = False
        srv.force_exit = True
        try:
            await asyncio.wait_for(task, 3)
        except Exception:  # noqa: BLE001 — control run teardown
            task.cancel()
    return done, time.monotonic() - t0, state["closed"]


async def main():
    kw = serve_opts.run_kwargs({}, cert_dir="/nonexistent")
    check("serve options: a graceful-shutdown deadline is always set (no certs needed)",
          kw.get("timeout_graceful_shutdown") == serve_opts.SHUTDOWN_GRACE_S and "ssl_certfile" not in kw, str(kw))
    kw2 = serve_opts.run_kwargs({"timeout_graceful_shutdown": 9}, cert_dir="/nonexistent")
    check("serve options: an explicit deadline from the caller is kept", kw2["timeout_graceful_shutdown"] == 9, str(kw2))
    from loguru import logger as _lg
    _warns = []
    _sink = _lg.add(lambda m: _warns.append(str(m)), level="WARNING")
    try:
        serve_opts.run_kwargs({}, cert_dir="/nonexistent")
    finally:
        _lg.remove(_sink)
    check("serve options: no cert = a loud warning that browsers will block the mic, naming make-cert.sh",
          any("plain HTTP" in w and "microphone" in w and "make-cert.sh" in w for w in _warns), str(_warns)[:300])

    done, dt, closed = await scenario({}, budget=4.0)
    check("control: WITHOUT a deadline the stop hangs on the pipeline task and the shutdown hook never runs",
          not done and not closed, f"done={done} closed={closed} dt={dt:.1f}")
    done, dt, closed = await scenario({k: v for k, v in kw.items() if not k.startswith("ssl_")})
    check("with the bot's options: the stop finishes within ~deadline + 1 s",
          done and dt < serve_opts.SHUTDOWN_GRACE_S + 1.5, f"done={done} dt={dt:.2f}")
    check("with the bot's options: the shutdown hook runs (peer connections get closed = the goodbye)",
          closed, f"closed={closed}")


asyncio.run(main())
print(f"shutdown: pass={len(PASS)} fail={len(FAIL)} | " + ("ALL CHECKS PASSED" if not FAIL else f"{len(FAIL)} FAIL(S)"))
sys.exit(1 if FAIL else 0)
