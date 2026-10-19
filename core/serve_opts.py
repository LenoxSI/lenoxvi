"""uvicorn.run options for the appliance server.

- TLS when certs exist: plain-HTTP LAN origins are insecure contexts, so
  browsers hide getUserMedia/RTCPeerConnection and the prebuilt client dies
  with "WebRTC not supported or suppressed". The pipecat runner has no SSL
  args, so bot.py wraps uvicorn.run to inject the certfile/keyfile. Certs:
  scripts/make-cert.sh (self-signed, click through the browser warning once).
- A graceful-shutdown deadline, always: every node session runs as a
  BackgroundTask of /api/offer that never ends on its own. Without a
  deadline uvicorn waits for those tasks forever on stop, the service
  manager kills the process, and the app's shutdown hook — where the runner
  closes every peer connection, the "goodbye" a node can react to — never
  runs. With it: wait SHUTDOWN_GRACE_S, cancel the stragglers, run the
  shutdown hook, exit.
"""
import os

SHUTDOWN_GRACE_S = 2


def run_kwargs(kw: dict, cert_dir: str) -> dict:
    out = dict(kw)
    out.setdefault("timeout_graceful_shutdown", SHUTDOWN_GRACE_S)
    if os.path.exists(os.path.join(cert_dir, "cert.pem")):
        out["ssl_certfile"] = os.path.join(cert_dir, "cert.pem")
        out["ssl_keyfile"] = os.path.join(cert_dir, "key.pem")
    return out


def wrap_uvicorn_run(uvicorn, cert_dir: str) -> None:
    """Route every uvicorn.run (the pipecat runner's included) through
    run_kwargs."""
    orig = uvicorn.run

    def run(app, host=None, port=None, **kw):
        return orig(app, host=host, port=port, **run_kwargs(kw, cert_dir))

    uvicorn.run = run
