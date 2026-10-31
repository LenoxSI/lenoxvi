"""Scripts suite: scripts/restart.sh and scripts/deploy_firmware.sh argument handling.

Every external command the scripts could reach (systemctl, journalctl, sleep, git, idf.py, ssh, scp,
curl, rsync) is a stub on a scratch PATH that records its call, and the helper scripts deploy_firmware.sh
runs from LVI_LOCAL are stubs in a scratch tree. Nothing here touches the real systemd, network or repo.

    python tests/scripts_test.py [core]   # the core path is accepted and ignored
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESTART = REPO / "scripts" / "restart.sh"
DEPLOY = REPO / "scripts" / "deploy_firmware.sh"
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="lvi-scripts-test-"))
STUBS = TMP / "stubs"
STUBS.mkdir()
CALLS = TMP / "calls.log"

# Each stub logs "<name> <args>" to CALLS, then behaves per its STUB_* env vars.
_STUB_SRC = {
    "systemctl": 'case "$2" in '
                 'restart) exit "${STUB_RESTART_RC:-0}";; '
                 'is-active) if [ -n "${STUB_ACTIVE_SEQ:-}" ]; then '
                 'n=$(grep -c " is-active " "$STUB_CALLS"); set -- $STUB_ACTIVE_SEQ; '
                 '[ "$n" -gt "$#" ] && n=$#; eval "v=\\${$n}"; [ "$v" = yes ]; '
                 'else [ "${STUB_ACTIVE:-yes}" = yes ]; fi;; '
                 '*) exit 0;; esac',
    "git": 'case "$3" in '
           'symbolic-ref) echo main;; '
           'status) ;; '
           'rev-parse) echo 0abc123;; '
           '*) exit 0;; esac',
}
# Expected to do nothing (sleep, idf.py: the build output is pre-made) or never to run at all (network).
for _cmd in ("sleep", "journalctl", "idf.py", "ssh", "scp", "curl", "rsync"):
    _STUB_SRC[_cmd] = "exit 0"
for name, body in _STUB_SRC.items():
    f = STUBS / name
    f.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$STUB_CALLS"\n{body}\n')
    f.chmod(0o755)

NETWORK = ("ssh", "scp", "curl", "rsync")


def run(script, *args, **env_extra):
    CALLS.write_text("")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LVI_", "STUB_", "FW_", "WIFI_", "PIPECAT_", "DEPLOY_"))}
    env.update({"PATH": f"{STUBS}:{os.environ['PATH']}", "STUB_CALLS": str(CALLS)})
    env.update({k: str(v) for k, v in env_extra.items()})
    r = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env,
                       timeout=30, check=False, stdin=subprocess.DEVNULL)
    return r, CALLS.read_text().splitlines()


def out(r):
    return f"rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def ran(calls, name):
    return [c for c in calls if c.split(" ", 1)[0] == name]


# ── restart.sh ───────────────────────────────────────────────────────────
SUCCESS = "lenoxvi-bot restarted"

r, calls = run(RESTART, STUB_RESTART_RC=1)
check("scripts: restart.sh — a failed systemctl restart exits non-zero with the failure on stderr, never the "
      "success line",
      r.returncode != 0 and SUCCESS not in r.stdout + r.stderr and "journalctl --user -u lenoxvi-bot" in r.stderr
      and "systemctl --user status lenoxvi-bot" in r.stderr, out(r))

r, calls = run(RESTART, STUB_ACTIVE="no")
is_active = [c for c in calls if " is-active " in f" {c} "]
check("scripts: restart.sh — restart returns 0 but the unit never comes up = non-zero exit, no success line",
      r.returncode != 0 and SUCCESS not in r.stdout + r.stderr and r.stderr.strip(), out(r))
check("scripts: restart.sh — waiting for the unit is bounded (a handful of is-active polls, stub sleep)",
      1 <= len(is_active) <= 15 and len(ran(calls, "sleep")) <= 15, "\n".join(calls))

r, calls = run(RESTART, STUB_ACTIVE="yes")
check("scripts: restart.sh — restart and an active unit = exit 0 with the success line",
      r.returncode == 0 and SUCCESS in r.stdout and not r.stderr.strip()
      and any(c.startswith("systemctl --user restart lenoxvi-bot") for c in calls), out(r))

# Type=simple units report active the moment the process starts: a bot that dies on a bad
# config edit a second later must not be reported as restarted.
r, calls = run(RESTART, STUB_ACTIVE_SEQ="yes no")
check("scripts: restart.sh — a unit that comes up and then drops = non-zero exit, no success line",
      r.returncode != 0 and SUCCESS not in r.stdout + r.stderr and "journalctl --user -u lenoxvi-bot" in r.stderr,
      out(r) + "\n" + "\n".join(calls))
r, calls = run(RESTART, STUB_ACTIVE_SEQ="no no yes")
check("scripts: restart.sh — a slow start that then stays up = exit 0 with the success line",
      r.returncode == 0 and SUCCESS in r.stdout and not r.stderr.strip(), out(r))

# ── deploy_firmware.sh ───────────────────────────────────────────────────
# Scratch firmware checkout with a pre-made build (idf.py is a no-op stub), scratch HOME for the
# esp-idf export script, and a scratch LVI_LOCAL whose helper scripts are stubs.
FW = TMP / "fw"
BUILD = FW / "build" / "array"
BUILD.mkdir(parents=True)
(BUILD / "array_esp32.bin").write_bytes(b"\0" * 64)
(BUILD / "compile_commands.json").write_text(json.dumps(
    [{"command": 'cc -DWIFI_SSID=\\"\\" -DWIFI_PASSWORD=\\"\\" -c main.c'}]))
HOME = TMP / "home"
(HOME / "esp-idf").mkdir(parents=True)
(HOME / "esp-idf" / "export.sh").write_text("")
LVI = TMP / "lvi"
(LVI / "scripts").mkdir(parents=True)
(LVI / "scripts" / "app_bin_version.py").write_text(
    "import os, sys\n"
    "print(os.environ['STUB_BIN_TRACK'] if sys.argv[1] == '--track' else os.environ['STUB_BIN_VER'])\n")
# The first step after the version gate: a refusal here stops the script before anything is shipped.
(LVI / "scripts" / "flash_manifest.py").write_text(
    "import os\n"
    "open(os.environ['STUB_CALLS'], 'a').write('flash_manifest.py\\n')\n"
    "raise SystemExit(1)\n")
DEPLOY_ENV = {"FW_REPO": FW, "LVI_LOCAL": LVI, "LVI_HOST": "appliance.invalid", "HOME": HOME,
              "PIPECAT_SMALLWEBRTC_URL": "https://192.0.2.10:7860/api/offer",
              "STUB_BIN_TRACK": "array", "STUB_BIN_VER": "1.2.3"}

r, calls = run(DEPLOY, "--track", "array", "--version", **DEPLOY_ENV)
both = r.stdout + r.stderr
check("scripts: deploy_firmware.sh — --version with no value = a usage message naming the option, not the "
      "shell's unbound-variable crash",
      r.returncode != 0 and "--version" in both and "unbound variable" not in both, out(r))
check("scripts: deploy_firmware.sh — --version with no value ships nothing (no network command ran)",
      not any(ran(calls, n) for n in NETWORK) and not (LVI / "firmware" / "array" / "version.json").exists(),
      "\n".join(calls))

r, calls = run(DEPLOY, "--track", "array", "--version", "1.2.3", **DEPLOY_ENV)
check("scripts: deploy_firmware.sh — --version X.Y.Z still parses and reaches the version gate",
      "==> version 0.0.0 -> 1.2.3" in r.stdout and "flash_manifest.py" in calls
      and "unbound variable" not in r.stdout + r.stderr, out(r) + "\n" + "\n".join(calls))
check("scripts: deploy_firmware.sh — the stubbed build ran and nothing was shipped",
      ran(calls, "idf.py") and not any(ran(calls, n) for n in NETWORK), "\n".join(calls))

r, calls = run(DEPLOY, "--track", **DEPLOY_ENV)
check("scripts: deploy_firmware.sh — --track with no value still refuses with the track message",
      r.returncode != 0 and "--track array|flex|waveshare is required" in r.stdout + r.stderr
      and not calls, out(r) + "\n" + "\n".join(calls))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
