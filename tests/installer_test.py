"""Installer suite: scripts/install.sh, scripts/installer/*, the lenoxvi CLI.

Everything runs in dry-run mode (LVI_DRY_RUN=1 prints each system command
instead of running it) inside a scratch LVI_PREFIX, with stub commands on PATH
for the parts of the machine a test has to fake (uname, dpkg-query,
nvidia-smi, ss, systemctl, id, ip, ...). Nothing here needs root or touches
the real system. Real files are written only inside the scratch folder.

    python tests/installer_test.py
"""
import hashlib
import re
import http.server
import os
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts" / "installer"
sys.path.append(str(REPO / "core"))  # appended, never first: no core module may shadow a stdlib one
# Pure data (no pipecat): the Embedded defaults the installer relies on.
import stt_catalog

try:
    import tts_catalog
except ImportError:  # an older tree (the red probe): the checks that need it fail, the suite still runs
    tts_catalog = None
EMB = getattr(stt_catalog, "EMBEDDED_MODEL", None)
STT_DEFAULT = getattr(stt_catalog, "DEFAULT_PROVIDER", None)
TTS_DEFAULT = getattr(tts_catalog, "DEFAULT_PROVIDER", None)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="lvi-installer-test-"))
STUBS = TMP / "stubs"
STUBS.mkdir()

# Stub commands. Each reads its behaviour from STUB_* env vars.
_STUB_SRC = {
    "uname": 'if [ "$1" = -m ]; then echo "${STUB_ARCH:-x86_64}"; else /usr/bin/uname "$@"; fi',
    "dpkg-query": 'pkg="${@: -1}"; case " ${STUB_DPKG:-} " in *" $pkg "*) printf "install ok installed";; *) exit 1;; esac',
    "nvidia-smi": '[ -n "${STUB_GPU:-}" ] && echo "$STUB_GPU" || exit 9',
    "lspci": '[ -n "${STUB_LSPCI:-}" ] && echo "$STUB_LSPCI"; exit 0',
    "ss": 'printf "%b" "${STUB_SS:-}"',
    "systemctl": 'case "$1 $2" in "is-active --quiet") [ "${STUB_ACTIVE:-no}" = yes ];; "is-active lenoxvi") [ "${STUB_ACTIVE:-no}" = yes ] && echo active || echo inactive;; *) exit 0;; esac',
    "id": 'if [ "$1" = -u ] && [ "${2:-}" = lenoxvi ]; then [ "${STUB_USER:-no}" = yes ] && echo 999 || exit 1; else /usr/bin/id "$@"; fi',
    "getent": 'case "$2" in video|render) echo "$2:x:44:";; *) exit 2;; esac',
    "ip": 'if [ "$2" = route ]; then [ -n "${STUB_SRC:-}" ] && echo "1.1.1.1 via 192.0.2.1 dev eth0 src $STUB_SRC uid 0"; else [ -n "${STUB_SRC:-}" ] && echo "2: eth0    inet $STUB_SRC/24 brd 192.0.2.255 scope global eth0"; fi; exit 0',
    "free": 'echo "              total        used        free"; echo "Mem:   ${STUB_RAM:-8000}   1000   7000"',
    "df": 'echo "Filesystem 1M-blocks Used Available Capacity Mounted"; echo "/dev/x 100000 1000 ${STUB_DISK:-50000} 2% /"',
}
# Safety net: if a broken dry-run ever executed for real, these land on a stub
# that does nothing, never on the machine running the suite.
for _cmd in ("apt-get", "useradd", "usermod", "userdel", "runuser", "journalctl", "chown"):
    _STUB_SRC[_cmd] = "exit 0"
for name, body in _STUB_SRC.items():
    f = STUBS / name
    f.write_text("#!/usr/bin/env bash\n" + body + "\n")
    f.chmod(0o755)

ALL_PKGS = "ca-certificates curl git openssl qrencode iproute2 whiptail"


def make_prefix(os_id="ubuntu", version="24.04", pretty=None):
    p = Path(tempfile.mkdtemp(prefix="pfx-", dir=TMP))
    (p / "etc").mkdir()
    (p / "opt").mkdir()
    (p / "etc" / "os-release").write_text(
        f'ID={os_id}\nVERSION_ID="{version}"\nPRETTY_NAME="{pretty or os_id + " " + version}"\n')
    return p


def env_for(prefix=None, **stub):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("LVI_", "STUB_"))}
    e.update({"PATH": f"{STUBS}:{os.environ['PATH']}", "NO_COLOR": "1", "LVI_DRY_RUN": "1", "LVI_YES": "1",
              "STUB_DPKG": ALL_PKGS})
    if prefix is not None:
        e["LVI_PREFIX"] = str(prefix)
    e.update({k: str(v) for k, v in stub.items()})
    return e


LIBS = ["banner", "lib", "preflight", "deps", "app", "state", "service"]


def sh(script, prefix=None, **stub):
    src = "; ".join(f"source {SCRIPTS / n}.sh" for n in LIBS)
    full = f"set -euo pipefail; LVI_SCRIPT_DIR={REPO / 'scripts'}; {src}; {script}"
    return subprocess.run(["setsid", "bash", "-c", full], capture_output=True, text=True,
                          env=env_for(prefix, **stub), timeout=60, check=False, stdin=subprocess.DEVNULL)


def out(r):
    return r.stdout + r.stderr


# Question log: replaces lib.sh's prompts AFTER sourcing. Each question goes to stderr and is answered with
# its default, so a check can list exactly which questions a step asks (ask_choice is a tripwire: lib.sh has none).
ASK_LOG = ('ask_input() { printf "ASKED input: %s\\n" "$1" >&2; printf "%s\\n" "$2"; }; '
           'ask_yes() { printf "ASKED yes: %s\\n" "$1" >&2; [[ $2 == y ]]; }; '
           'ask_choice() { printf "ASKED choice: %s\\n" "$1" >&2; printf "%s\\n" "$2"; }; ')


def asked(r):
    return [ln[len("ASKED "):] for ln in r.stderr.splitlines() if ln.startswith("ASKED ")]


# ── lib: dry-run runner, prompts, tags ───────────────────────────────────
marker = TMP / "should-not-exist"
r = sh(f"run touch {marker}")
check("installer lib: dry-run prints the command and runs nothing",
      r.stdout.strip() == f"+ touch {marker}" and not marker.exists(), out(r))
r = sh(f"printf 'secret' | write_file {TMP}/wf 0600 -")
check("installer lib: dry-run write_file prints one line and writes nothing",
      r.stdout.strip() == f"+ write {TMP}/wf (0600 -)" and not (TMP / "wf").exists(), out(r))
r = sh(f"LVI_DRY_RUN=0; printf 'a\\nb\\n' | write_file {TMP}/wf2 0600 -; stat -c %a {TMP}/wf2; ls {TMP} | grep -c 'wf2\\.' || true")
check("installer lib: write_file lands the file with its mode and leaves no temp file",
      (TMP / "wf2").read_text() == "a\nb\n" and r.stdout.split() == ["600", "0"], out(r))
r = sh('ask_input "IP?" 192.0.2.7; ask_yes "ok?" y && echo Y; ask_yes "no?" n || echo N')
check("installer lib: --yes answers every question with its default",
      r.stdout.split("\n")[:3] == ["192.0.2.7", "Y", "N"], out(r))
r = sh('LVI_YES=0; UI=; ask_input "This machine\'s address" 192.0.2.7')
check("installer lib: no terminal and no --yes = a clear error naming --yes, never a hang",
      r.returncode != 0 and "--yes" in r.stderr and "This machine" in r.stderr, out(r))
r = sh("printf 'v0.6.9\\nv0.7.0\\nv0.10.0\\nv0.9.1\\nv0.11.0-rc.1\\narchive/x\\n' | newest_tag")
check("installer lib: newest release tag sorts by version and skips pre-releases",
      r.stdout.strip() == "v0.10.0", out(r))

# ── banner (degrades cleanly) ────────────────────────────────────────────
for cols in (30, 50, 100):
    r = sh(f"COLUMNS={cols} banner")
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    check(f"installer banner: {cols} columns, no colour codes off a terminal, nothing wider than the screen",
          "\x1b[" not in r.stdout and lines and max(len(ln) for ln in lines) <= cols
          and ("installer v" in r.stdout), f"widest={max((len(x) for x in lines), default=0)}\n{r.stdout}")

# ── preflight ────────────────────────────────────────────────────────────
for os_id, ver in (("ubuntu", "24.04"), ("ubuntu", "22.04"), ("ubuntu", "26.04"), ("debian", "12"), ("debian", "13")):
    r = sh("preflight; echo OS=$OS_ID ARCH=$ARCH", make_prefix(os_id, ver))
    check(f"installer preflight: {os_id} {ver} passes", r.returncode == 0 and f"OS={os_id}" in r.stdout, out(r))
r = sh("preflight", make_prefix("debian", "12"), STUB_ARCH="aarch64")
check("installer preflight: 64-bit ARM (Raspberry Pi OS 64-bit reports debian) passes", r.returncode == 0, out(r))
r = sh("preflight", make_prefix("debian", "12"), STUB_ARCH="armv7l")
check("installer preflight: a 32-bit system stops with 'install the 64-bit' advice",
      r.returncode != 0 and "64-bit" in r.stderr, out(r))
r = sh("preflight", make_prefix("raspbian", "12"), STUB_ARCH="aarch64")
check("installer preflight: 32-bit Raspberry Pi OS (ID=raspbian) stops", r.returncode != 0 and "64-bit" in r.stderr, out(r))
r = sh("preflight", make_prefix("fedora", "41"))
check("installer preflight: an untested distro stops and mentions --force",
      r.returncode != 0 and "--force" in r.stderr, out(r))
r = sh("LVI_FORCE=1; preflight", make_prefix("fedora", "41"))
check("installer preflight: --force continues on an untested distro with a warning",
      r.returncode == 0 and "not tested" in r.stderr, out(r))
busy = "LISTEN 0 128 0.0.0.0:7860 0.0.0.0:* users:((\\\"python\\\",pid=4242,fd=7))\\n"
r = sh("preflight", make_prefix(), STUB_SS=busy)
check("installer preflight: port 7860 taken = stop before touching anything, naming who has it",
      r.returncode != 0 and "7860" in r.stderr and "pid=4242" in r.stderr, out(r))
pfx_inst = make_prefix()
(pfx_inst / "opt" / "lenoxvi" / "app" / ".git").mkdir(parents=True)
r = sh("preflight; echo EXISTING=$EXISTING", pfx_inst, STUB_SS=busy, STUB_ACTIVE="yes")
check("installer preflight: on a box where LenoxVI already runs, its own ports aren't an error",
      r.returncode == 0 and "EXISTING=yes" in r.stdout and "in use by LenoxVI" in r.stdout, out(r))
old_py = STUBS.parent / "oldpy"
old_py.mkdir()
(old_py / "python3").write_text("#!/usr/bin/env bash\nexit 1\n")
(old_py / "python3").chmod(0o755)
r = subprocess.run(["setsid", "bash", "-c", f"source {SCRIPTS}/lib.sh; source {SCRIPTS}/preflight.sh; check_python; echo PY_OK=$PY_OK"],
                   capture_output=True, text=True, env={**env_for(), "PATH": f"{old_py}:{STUBS}:{os.environ['PATH']}"},
                   timeout=30, check=False)
check("installer preflight: Python older than 3.12 = a private 3.12 via uv (not an error)",
      "PY_OK=no" in r.stdout and "3.12" in r.stdout, out(r))
r = sh("preflight", make_prefix(), STUB_DISK="2000")
check("installer preflight: under 4 GB of free disk stops", r.returncode != 0 and "disk" in r.stderr, out(r))

# ── deps ─────────────────────────────────────────────────────────────────
r = sh("apt_missing git curl qrencode", STUB_DPKG="git ca-certificates")
check("installer deps: apt_missing lists only what isn't installed", r.stdout.split() == ["curl", "qrencode"], out(r))
r = sh("install_apt", STUB_DPKG="ca-certificates curl openssl iproute2 whiptail")
installs = [ln for ln in r.stdout.splitlines() if "apt-get install" in ln]
check("installer deps: one apt install with only the missing packages (git qrencode), never an upgrade",
      len(installs) == 1 and installs[0].endswith("--no-install-recommends git qrencode")
      and "upgrade" not in r.stdout, out(r))
r = sh("install_apt")
check("installer deps: everything present = no apt at all", "apt-get" not in r.stdout and "all present" in r.stdout, out(r))
r = sh("for v in GUM_SHA256_x86_64 GUM_SHA256_aarch64 UV_SHA256_x86_64 UV_SHA256_aarch64; do echo ${!v}; done")
check("installer deps: gum and uv are pinned to a sha256 per CPU",
      len([x for x in r.stdout.split() if len(x) == 64]) == 4, out(r))

# fetch_pinned against a tiny local server
srv_dir = TMP / "srv"
srv_dir.mkdir()
(srv_dir / "blob").write_bytes(b"lenoxvi test blob\n")
good = hashlib.sha256(b"lenoxvi test blob\n").hexdigest()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(srv_dir), **k)

    def log_message(self, *a):
        pass


httpd = socketserver.TCPServer(("127.0.0.1", 0), _Quiet)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
dl = TMP / "dl"
dl.mkdir()
r = sh(f"LVI_DRY_RUN=0; fetch_pinned http://127.0.0.1:{port}/blob {'0' * 64} {dl}/bad && echo GOT || echo REFUSED; ls -A {dl}")
check("installer deps: a download with the wrong sha256 is refused and leaves no file",
      "REFUSED" in r.stdout and "bad" not in r.stdout.split("REFUSED")[-1], out(r))
r = sh(f"LVI_DRY_RUN=0; fetch_pinned http://127.0.0.1:{port}/blob {good} {dl}/good && echo GOT")
check("installer deps: a download with the right sha256 lands", "GOT" in r.stdout and (dl / "good").is_file(), out(r))
gp = make_prefix()
r = sh(f"LVI_DRY_RUN=0; ARCH=x86_64; GUM_URL_BASE=http://127.0.0.1:{port}/nope; UI=whiptail; install_gum; echo UI=$UI", gp)
check("installer deps: gum can't be fetched = simple prompts, the install goes on",
      r.returncode == 0 and "UI=whiptail" in r.stdout and "simple prompts" in r.stdout
      and not (gp / "opt/lenoxvi/bin/gum").exists(), out(r))
httpd.shutdown()

# ── app ──────────────────────────────────────────────────────────────────
src = TMP / "src"
subprocess.run(["git", "init", "-q", str(src)], check=True)
for cmd in (["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
            ["commit", "-q", "--allow-empty", "-m", "x"], ["tag", "v0.6.9"], ["tag", "v0.10.0"],
            ["tag", "v0.9.0"], ["tag", "v0.11.0-rc.1"],
            ["remote", "add", "origin", "https://example.com/lenoxvi.git"]):
    subprocess.run(["git", "-C", str(src), *cmd], check=True)
r = sh(f"LVI_SOURCE={src}; resolve_source; echo TAG=$LVI_TAG ORIGIN=$LVI_ORIGIN")
check("installer app: the newest release tag of the source is installed, and upgrades follow its origin",
      "TAG=v0.10.0 ORIGIN=https://example.com/lenoxvi.git" in r.stdout, out(r))
p = make_prefix()
r = sh("ensure_user", p)
check("installer app: a fresh box gets a system user with no login, home = the data folder",
      "+ useradd --system --home-dir /var/lib/lenoxvi --no-create-home --shell /usr/sbin/nologin lenoxvi" in r.stdout
      and "usermod" not in r.stdout, out(r))
r = sh("ensure_user", p, STUB_USER="yes")
check("installer app: an existing user isn't re-created or added to any group",
      r.returncode == 0 and "useradd" not in r.stdout and "usermod" not in r.stdout and "(exists)" in r.stdout, out(r))
r = sh(f"LVI_SOURCE={src}; LVI_TAG=v0.10.0; LVI_ORIGIN=https://example.com/o.git; install_code", p)
check("installer app: code is a shallow clone of the release tag, origin kept for upgrades",
      f"clone --quiet --depth 1 --branch v0.10.0 file://{src} {p}/opt/lenoxvi/app" in r.stdout
      and "remote set-url origin https://example.com/o.git" in r.stdout, out(r))
check("installer app: the code folder is marked safe for git, so the bot (not root) can read its version",
      "+ git config --system --add safe.directory /opt/lenoxvi/app" in r.stdout, out(r))
r = sh(f"LVI_SOURCE={src}; LVI_TAG=v0.10.0; LVI_ORIGIN=x; install_code", pfx_inst)
check("installer app: an existing install's code is left alone (upgrades are lenoxvi upgrade)",
      "clone" not in r.stdout and "lenoxvi upgrade" in r.stdout, out(r))
r = sh("PY_OK=no; install_venv", p)
check("installer app: old system Python = uv installs a private 3.12 under /opt (not root's home)",
      "python install --quiet 3.12" in r.stdout and f"UV_PYTHON_INSTALL_DIR={p}/opt/lenoxvi/python" in r.stdout
      and "UV_COMPILE_BYTECODE=1" in r.stdout and "requirements-gpu" not in r.stdout, out(r))
r = sh("PY_OK=yes; PY_BIN=/usr/bin/python3; install_venv", p)
_pip = [ln for ln in r.stdout.splitlines() if " pip install " in ln]
check("installer app: a new-enough system Python is used as is; packages come from requirements.txt alone",
      "venv --quiet -p /usr/bin/python3" in r.stdout and "python install" not in r.stdout and len(_pip) == 1
      and _pip[0].endswith(f"--python {p}/opt/lenoxvi/venv/bin/python -r {p}/opt/lenoxvi/app/requirements.txt")
      and "requirements-gpu" not in r.stdout, out(r))
r = sh("fetch_models", p)
check("installer app: both speech models download up front as the service user, into its home",
      "runuser -u lenoxvi -- env HOME=/var/lib/lenoxvi" in r.stdout and EMB is not None
      and "fetch_kokoro_models.sh" in r.stdout and f"download_model\\(\\'{EMB}\\'\\)" in r.stdout
      and f"speech model (Whisper {EMB}): ready" in r.stdout, out(r))

# ── questions + keyless state ────────────────────────────────────────────
r = sh(ASK_LOG + "ask_questions; echo IP=$HOST_IP TOKEN=$MAKE_TOKEN", make_prefix(), STUB_SRC="192.0.2.44")
check("installer questions: two questions — the LAN address (default-route guess) and a token; speech needs none",
      r.returncode == 0 and [a.split(":")[0] for a in asked(r)] == ["input", "yes"] and "LAN address" in asked(r)[0]
      and "IP=192.0.2.44 TOKEN=yes" in r.stdout and "speech: Embedded (Whisper base + Kokoro)" in r.stdout
      and "Two questions" in r.stdout, out(r))
r = sh("ask_questions", make_prefix())
check("installer questions: no default route and no --host = stop and ask for --host",
      r.returncode != 0 and "--host" in r.stderr, out(r))
r = sh("HOST_IP=10.0.0.300x; ask_questions", make_prefix())
check("installer questions: a malformed --host is refused", r.returncode != 0 and "IPv4" in r.stderr, out(r))
pe = make_prefix()
(pe / "var/lib/lenoxvi").mkdir(parents=True)
(pe / "var/lib/lenoxvi/.env").write_text("ELEVENLABS_API_KEY=x\n")
r = sh("HOST_IP=192.0.2.5; ask_questions; echo TOKEN=$MAKE_TOKEN", pe)
check("installer questions: an existing keys file = no token question, nothing replaced", "TOKEN=no" in r.stdout, out(r))

# write_config.py, real files
ex = REPO / "config.example.yaml"
cfgdir = TMP / "cfg"
cfgdir.mkdir()
dest = cfgdir / "config.yaml"
WC = [sys.executable, str(SCRIPTS / "write_config.py")]
r = subprocess.run([*WC, str(ex), str(dest), "--host", "192.0.2.10"], capture_output=True, text=True, check=False)
if not dest.exists():  # an older write_config refused the call: the check below reports it; later sections need a config
    shutil.copy(ex, dest)
try:
    from ruamel.yaml import YAML
    c = YAML(typ="safe").load(dest.read_text())
except Exception as e:  # noqa: BLE001
    c = {"error": repr(e)}
check("installer config: keyless values — kokoro voice, echo intelligence, CPU Whisper base, no rooms, the clip address",
      r.returncode == 0 and TTS_DEFAULT is not None and c.get("tts", {}).get("provider") == TTS_DEFAULT
      and STT_DEFAULT is not None and c.get("stt", {}).get("provider") == STT_DEFAULT
      and not {"model", "device", "compute_type"} & set(c.get("stt") or {})
      and not {"fallback", "pocket_voice"} & set(c.get("tts") or {})
      and c.get("intelligence", {}).get("kind") == "stub"
      and c.get("room_zones") == {} and c.get("clip_server", {}).get("host_ip") == "192.0.2.10"
      and c.get("wake", {}).get("enabled") is True, f"{r.stderr[-300:]} {str(c)[:300]}")
check("installer config: the example's comments survive (ruamel round-trip)",
      "the Rooms tab sets these" in dest.read_text() and "Web dashboard" in dest.read_text())
before = dest.read_bytes()
r = subprocess.run([*WC, str(ex), str(dest), "--host", "192.0.2.99"], capture_output=True, text=True, check=False)
check("installer config: an existing config is never overwritten", dest.read_bytes() == before and "kept" in r.stdout, r.stdout)
r = subprocess.run([*WC, "--update", str(dest), "--host", "192.0.2.11"], capture_output=True, text=True, check=False)
c2 = YAML(typ="safe").load(dest.read_text())
check("installer config: --update changes only the address",
      r.returncode == 0 and c2["clip_server"]["host_ip"] == "192.0.2.11"
      and {k: v for k, v in c2.items() if k != "clip_server"} == {k: v for k, v in c.items() if k != "clip_server"}
      and not (cfgdir / "config.yaml.tmp").exists(), r.stderr[-200:])
r = subprocess.run([*WC, "--get", str(dest)], capture_output=True, text=True, check=False)
check("installer config: --get prints only the address", r.returncode == 0 and r.stdout == "192.0.2.11\n", repr(r.stdout))

me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
pt = make_prefix()
(pt / "var/lib/lenoxvi").mkdir(parents=True)
r = sh(f"LVI_DRY_RUN=0; LVI_USER={me}; MAKE_TOKEN=yes; write_env; echo TOKEN=$DASH_TOKEN", pt)
envf = pt / "var/lib/lenoxvi/.env"
tok = r.stdout.split("TOKEN=")[-1].strip()
body = envf.read_text() if envf.exists() else ""
check("installer state: the keys file is 0600 and holds only the generated dashboard token (no keys asked)",
      envf.exists() and oct(envf.stat().st_mode & 0o777) == "0o600" and len(tok) == 64
      and [ln for ln in body.splitlines() if ln and not ln.startswith("#")] == [f"LVI_DASHBOARD_TOKEN={tok}"], out(r))
r = sh(f"LVI_DRY_RUN=0; LVI_USER={me}; MAKE_TOKEN=yes; write_env; echo TOKEN=[$DASH_TOKEN]", pt)
check("installer state: an existing keys file is kept byte for byte (no new token)",
      envf.read_text() == body and "TOKEN=[]" in r.stdout, out(r))
r = sh("HOST_IP=192.0.2.10; MAKE_TOKEN=no; write_state", make_prefix())
check("installer state: fresh box = config written as the service user, empty keys file, cert for the address",
      "write_config.py /opt/lenoxvi/app/config.example.yaml /var/lib/lenoxvi/config.yaml --host 192.0.2.10\n" in r.stdout
      and "--stt" not in r.stdout
      and "/var/lib/lenoxvi/.env (0600 lenoxvi:lenoxvi)" in r.stdout
      and "LVI_STATE_DIR=/var/lib/lenoxvi bash /opt/lenoxvi/app/scripts/make-cert.sh 192.0.2.10" in r.stdout, out(r))

# ── service ──────────────────────────────────────────────────────────────
UT = f"LVI_UNIT_TEMPLATE={REPO}/deploy/lenoxvi.service.in"
r = sh(f"{UT}; render_unit 192.0.2.10")
check("installer service: CPU unit runs the bot from /opt as lenoxvi with the state dir, no CUDA path",
      "ExecStart=/opt/lenoxvi/venv/bin/python bot.py --host 192.0.2.10 --port 7860 --esp32" in r.stdout
      and "User=lenoxvi" in r.stdout and "Environment=LVI_STATE_DIR=/var/lib/lenoxvi" in r.stdout
      and "Environment=LVI_CONFIG=/var/lib/lenoxvi/config.yaml" in r.stdout
      and "ReadWritePaths=/var/lib/lenoxvi" in r.stdout and "ProtectSystem=strict" in r.stdout
      and "LD_LIBRARY_PATH" not in r.stdout and "@" not in r.stdout, r.stdout)
GPU_BOX = {"STUB_GPU": "NVIDIA RTX A2000 12GB", "STUB_LSPCI": "01:00.0 VGA compatible controller: NVIDIA Corporation"}
wheels = make_prefix()
for _lib in ("cublas", "cudnn", "cuda_nvrtc"):      # CUDA wheels a venv may still hold
    (wheels / f"opt/lenoxvi/venv/lib/python3.12/site-packages/nvidia/{_lib}/lib").mkdir(parents=True)
r = sh(f"{UT}; render_unit 192.0.2.10", wheels, **GPU_BOX)
check("installer service: the unit carries no CUDA lib dirs, even with an NVIDIA GPU and its wheels present",
      r.returncode == 0 and "ExecStart=" in r.stdout
      and not any(w in r.stdout.lower() for w in ("ld_library_path", "nvidia", "cuda", "@gpu")), r.stdout)
pu = make_prefix()
(pu / "etc/systemd/system").mkdir(parents=True)
r = sh(f"{UT}; install_unit 192.0.2.10; echo CHANGED=$UNIT_CHANGED", pu)
check("installer service: a new unit is written and systemd reloaded",
      "CHANGED=yes" in r.stdout and "+ systemctl daemon-reload" in r.stdout, out(r))
(pu / "etc/systemd/system/lenoxvi.service").write_text(sh(f"{UT}; render_unit 192.0.2.10").stdout)
r = sh(f"{UT}; HOST_IP=192.0.2.10; install_service", pu, STUB_ACTIVE="yes")
check("installer service: same unit + running = no restart",
      "restart" not in r.stdout.replace("restarted", "") and "+ write" not in r.stdout and "nothing changed" in r.stdout, out(r))
r = sh(f"{UT}; HOST_IP=192.0.2.20; install_service", pu, STUB_ACTIVE="yes")
check("installer service: a changed address rewrites the unit and restarts once",
      "+ systemctl restart lenoxvi" in r.stdout and r.stdout.count("systemctl restart") == 1, out(r))
r = sh(f"{UT}; HOST_IP=192.0.2.10; install_service", make_prefix())
check("installer service: not running = enable --now, and the lenoxvi command is linked",
      "+ systemctl enable --now --quiet lenoxvi" in r.stdout
      and "ln -sfn /opt/lenoxvi/app/scripts/installer/lenoxvi" in r.stdout, out(r))
r = sh("HOST_IP=192.0.2.10; DASH_TOKEN=abc123; handoff")
check("installer handoff: dashboard address, the certificate warning, the token once, how to manage it",
      "https://192.0.2.10:7860/dashboard" in r.stdout and "certificate" in r.stdout and r.stdout.count("abc123") == 1
      and "lenoxvi status" in r.stdout, r.stdout)

# ── whole install.sh, dry-run: fresh box vs an installed box ─────────────
INST = REPO / "scripts" / "install.sh"


def install(prefix, *args, **stub):
    return subprocess.run(["setsid", "bash", str(INST), "--dry-run", "--yes", "--prefix", str(prefix), *args],
                          capture_output=True, text=True, env=env_for(prefix, **stub), timeout=120, check=False,
                          stdin=subprocess.DEVNULL)


fresh = make_prefix()
r = install(fresh, "--source", str(src), "--host", "192.0.2.10", STUB_DPKG="ca-certificates curl")
o = r.stdout
order = ["apt-get install", "fetch https://github.com/charmbracelet/gum", "fetch https://github.com/astral-sh/uv",
         "useradd --system", "clone --quiet --depth 1 --branch v0.10.0", "pip install", "fetch_kokoro_models.sh",
         "write_config.py", "/var/lib/lenoxvi/.env (0600", "make-cert.sh 192.0.2.10", "lenoxvi.service",
         "systemctl enable --now"]
pos = [o.find(x) for x in order]
check("installer dry-run: a fresh box runs every step in order (apt, gum, uv, user, code, packages, models, config, keys, cert, unit, start)",
      r.returncode == 0 and -1 not in pos and pos == sorted(pos),
      f"rc={r.returncode} missing={[order[i] for i, x in enumerate(pos) if x == -1]}\n{out(r)[-600:]}")
check("installer dry-run: nothing was written to the scratch root", not any(fresh.joinpath(d).exists()
      for d in ("opt/lenoxvi", "var/lib/lenoxvi", "etc/systemd")), str(list(fresh.rglob("*"))[:10]))
p_cpu, p_gpu = make_prefix(), make_prefix()


def _no_token(s):
    """The dashboard token is random per install: mask it before comparing two dry-runs."""
    return re.sub(r"(?m)^  [0-9a-f]{64}$", "  <token>", s)


r_cpu = install(p_cpu, "--source", str(src), "--host", "192.0.2.10")
r_gpu = install(p_gpu, "--source", str(src), "--host", "192.0.2.10", **GPU_BOX)
u_gpu = sh(f"{UT}; render_unit 192.0.2.10", p_gpu, **GPU_BOX).stdout
check("installer dry-run: a machine with an NVIDIA GPU installs exactly like one without — no GPU question, no "
      "requirements-gpu, no CUDA lib dirs in the unit",
      r_cpu.returncode == 0 and r_gpu.returncode == 0
      and _no_token(r_gpu.stdout.replace(str(p_gpu), "PFX")) == _no_token(r_cpu.stdout.replace(str(p_cpu), "PFX"))
      and "requirements-gpu" not in r_gpu.stdout
      and "gpu" not in out(r_gpu).replace(str(p_gpu), "PFX").replace(str(TMP), "TMP").lower()  # random dir names
      and "speech: Embedded (Whisper base + Kokoro)" in r_gpu.stdout and EMB is not None
      and f"download_model\\(\\'{EMB}\\'\\)" in r_gpu.stdout
      and "ExecStart=" in u_gpu and not any(w in u_gpu.lower() for w in ("ld_library_path", "nvidia", "cuda")),
      out(r_gpu)[-600:] + u_gpu[-300:])
r = install(make_prefix(), "--stt", "gpu")
rh = subprocess.run(["bash", str(INST), "--help"], capture_output=True, text=True, timeout=30, check=False,
                    stdin=subprocess.DEVNULL)
check("installer dry-run: --stt is gone — '--stt gpu' is a usage error naming it, and --help no longer lists it",
      r.returncode == 2 and "unknown option: --stt" in r.stderr and "Usage:" in r.stderr
      and rh.returncode == 0 and "--host IP" in rh.stdout and "--stt" not in rh.stdout, out(r) + rh.stdout)

# an installed box: re-running changes nothing it doesn't own
done = make_prefix()
app_dir = done / "opt/lenoxvi/app"
subprocess.run(["git", "clone", "-q", "--branch", "v0.10.0", str(src), str(app_dir)], check=True,
               capture_output=True)
(app_dir / "deploy").mkdir()
shutil.copy(REPO / "deploy/lenoxvi.service.in", app_dir / "deploy/lenoxvi.service.in")
(done / "opt/lenoxvi/venv/bin").mkdir(parents=True)
(done / "opt/lenoxvi/venv/bin/python").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
(done / "opt/lenoxvi/venv/bin/python").chmod(0o755)
bindir = done / "opt/lenoxvi/bin"
bindir.mkdir()
(bindir / "uv").write_text("#!/usr/bin/env bash\necho 'uv 0.13.6'\n")
(bindir / "gum").write_text("#!/usr/bin/env bash\necho 'gum version v2.0.2'\n")
for f in ("uv", "gum"):
    (bindir / f).chmod(0o755)
st = done / "var/lib/lenoxvi"
(st / "certs").mkdir(parents=True)
shutil.copy(dest, st / "config.yaml")
(st / ".env").write_text("LVI_DASHBOARD_TOKEN=x\n")
(st / "certs/cert.pem").write_text("x")
(done / "etc/systemd/system").mkdir(parents=True)
unit = subprocess.run(["bash", "-c", f"source {SCRIPTS}/lib.sh; source {SCRIPTS}/service.sh; LVI_UNIT_TEMPLATE={REPO}/deploy/lenoxvi.service.in; "
                       "render_unit 192.0.2.10"],
                      capture_output=True, text=True, check=False).stdout
(done / "etc/systemd/system/lenoxvi.service").write_text(unit)
(done / "usr/local/bin").mkdir(parents=True)
os.symlink("/opt/lenoxvi/app/scripts/installer/lenoxvi", done / "usr/local/bin/lenoxvi")
r = install(done, "--source", str(src), "--host", "192.0.2.10", STUB_USER="yes", STUB_ACTIVE="yes",
            STUB_SS="LISTEN 0 1 0.0.0.0:7860 x users:((\\\"python\\\"))\\n")
o = r.stdout
bad = [x for x in ("useradd", "apt-get", "clone", "write_config.py", "+ write", "make-cert", "systemctl restart",
                   "systemctl enable", "daemon-reload", "ln -sfn", "fetch https") if x in o]
check("installer dry-run: re-running on an installed box changes nothing it doesn't own (no apt, user, code, config, keys, cert, unit or restart)",
      r.returncode == 0 and not bad and "kept your" in o and "nothing changed" in o, f"rc={r.returncode} bad={bad}\n{out(r)[-800:]}")
check("installer dry-run: the re-run only re-checks the Python packages (a no-op when they're met)",
      "pip install" in o, o[-400:])

(app_dir / "scripts/installer").mkdir(parents=True)
shutil.copy(SCRIPTS / "write_config.py", app_dir / "scripts/installer/write_config.py")
r = sh("ask_questions; echo IP=$HOST_IP TOKEN=$MAKE_TOKEN", done, STUB_SRC="192.0.2.99")
check("installer questions: on an installed box today's address is the default (a re-run never moves it)",
      "IP=192.0.2.11 TOKEN=no" in r.stdout, out(r))

# ── the lenoxvi command ───────────────────────────────────────────────
CLI = SCRIPTS / "lenoxvi"


def cli(prefix, *args, **stub):
    return subprocess.run(["setsid", "bash", str(CLI), "--dry-run", "--yes", "--prefix", str(prefix), *args],
                          capture_output=True, text=True, env=env_for(prefix, **stub), timeout=60, check=False,
                          stdin=subprocess.DEVNULL)


r = cli(done, "status", STUB_ACTIVE="yes")
check("lenoxvi status: service, version and dashboard address; no speech-to-text profile line",
      r.returncode == 0 and "service:   active" in r.stdout and "version:   v0.10.0" in r.stdout
      and "https://192.0.2.11:7860/dashboard" in r.stdout and "stt:" not in r.stdout, out(r))
os.chmod(st / "config.yaml", 0)
r = cli(done, "status", STUB_ACTIVE="yes")
os.chmod(st / "config.yaml", 0o644)
check("lenoxvi status: works without sudo (no access to the settings = a hint instead of an error)",
      r.returncode == 0 and "version:   v0.10.0" in r.stdout and "sudo lenoxvi status" in r.stdout, out(r))
r = cli(done, "uninstall", STUB_USER="yes")
o = r.stdout
check("lenoxvi uninstall: removes the service, command and code, keeps the data folder",
      f"+ rm -f {done}/etc/systemd/system/lenoxvi.service" in o and f"+ rm -rf {done}/opt/lenoxvi" in o
      and f"rm -rf {done}/var/lib/lenoxvi\n" not in o + "\n" and "userdel" not in o and "kept" in o, out(r))
r = cli(done, "uninstall", "--purge", STUB_USER="yes")
check("lenoxvi uninstall --purge asks with 'no' as the default (so --yes alone doesn't wipe data)",
      r.returncode != 0 and "nothing removed" in r.stderr, out(r))
r = subprocess.run(["setsid", "bash", str(CLI), "--dry-run", "--prefix", str(done), "uninstall", "--purge"],
                   capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL,
                   env={**env_for(done, STUB_USER="yes"), "LVI_YES": "0"})
check("lenoxvi uninstall --purge without a terminal refuses instead of guessing",
      r.returncode != 0 and "--yes" in r.stderr, out(r))
_cli_src = CLI.read_text()
_purge_blk = _cli_src[_cli_src.index("if [[ $purge == yes ]]; then\n    run rm -rf"):]
_purge_blk = _purge_blk[:_purge_blk.index("else")]
check("lenoxvi uninstall --purge also deletes the install log",
      'run rm -f "$LVI_LOG"' in _purge_blk, _purge_blk[:200])
_user_text = "".join((SCRIPTS / n).read_text() for n in ("lib.sh", "preflight.sh", "deps.sh", "app.sh", "state.sh", "service.sh", "lenoxvi"))
_user_text += (SCRIPTS.parent / "install.sh").read_text()
check("installer copy: says 'intelligence', never 'brain' (naming law)",
      "brain" not in _user_text.lower(), [l.strip() for l in _user_text.splitlines() if "brain" in l.lower()][:3])
r = cli(done, "upgrade", "--to", "v0.9.0", "--source", str(src))
check("lenoxvi upgrade: going back to an older release needs --force",
      r.returncode != 0 and "older" in r.stderr and "--force" in r.stderr, out(r))
r = cli(done, "upgrade", "--source", str(src), STUB_ACTIVE="yes")
check("lenoxvi upgrade: already on the newest release = nothing to do", r.returncode == 0 and "already on v0.10.0" in r.stdout, out(r))
subprocess.run(["git", "-C", str(src), "tag", "v0.12.0"], check=True)
r = cli(done, "upgrade", "--source", str(src), STUB_ACTIVE="yes")
o = r.stdout
check("lenoxvi upgrade: fetches the newest tag, reinstalls packages, restarts, waits for the dashboard",
      r.returncode == 0 and "refs/tags/v0.12.0:refs/tags/v0.12.0" in o and "checkout --quiet v0.12.0" in o
      and o.find("pip install") > o.find("checkout") and "+ systemctl restart lenoxvi" in o
      and "wait for https://192.0.2.11:7860/dashboard" in o, out(r))
tf = make_prefix()
(tf / "var/lib/lenoxvi").mkdir(parents=True)
(tf / "var/lib/lenoxvi/.env").write_text("ELEVENLABS_API_KEY=keep-me\nLVI_DASHBOARD_TOKEN=old\n# note\n")
r = subprocess.run(["bash", "-c", f"set -euo pipefail; source {SCRIPTS}/lib.sh; "
                    f"eval \"$(sed -n '/^set_env_token()/,/^}}/p' {CLI})\"; LVI_DRY_RUN=0; set_env_token newtok; stat -c %a {tf}/var/lib/lenoxvi/.env"],
                   capture_output=True, text=True, timeout=30, check=False, env={**env_for(tf), "LVI_USER": me, "LVI_DRY_RUN": "0"})
envt = (tf / "var/lib/lenoxvi/.env").read_text()
check("lenoxvi reconfigure: a new token replaces only that line; keys and comments stay, file stays 0600",
      "ELEVENLABS_API_KEY=keep-me" in envt and "# note" in envt and "LVI_DASHBOARD_TOKEN=newtok" in envt
      and "=old" not in envt and r.stdout.strip() == "600", out(r) + envt)
# reconfigure's questions: source the CLI (it only prints its usage with no command), log the prompts, run it dry.
r = subprocess.run(["setsid", "bash", "-c", f"set -euo pipefail; source {CLI} >/dev/null; {ASK_LOG}cmd_reconfigure"],
                   capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL, env=env_for(done))
check("lenoxvi reconfigure: one question (the LAN address) plus the offer of a new token; no speech-to-text choice, "
      "no packages, no groups",
      r.returncode == 0 and [a.split(":")[0] for a in asked(r)] == ["input", "yes"] and "LAN address" in asked(r)[0]
      and not any(w in r.stdout.lower() for w in ("pip install", "usermod", "nvidia")), out(r))

# ── README matches the scripts ───────────────────────────────────────────
readme = (REPO / "README.md").read_text()
cli_src = CLI.read_text()
table_cmds = sorted(set(re.findall(r"^\| `(?:sudo )?lenoxvi (\w+)", readme, re.M)))
cli_cmds = sorted(set(re.findall(r"^  (\w+)\)", cli_src, re.M)) - {"help"})
check("installer README: the command table lists exactly the lenoxvi commands",
      table_cmds and table_cmds == cli_cmds, f"readme={table_cmds} cli={cli_cmds}")
inst_flags = set(re.findall(r"^    (--[a-z-]+)", (REPO / "scripts/install.sh").read_text(), re.M))
check("installer README: the install steps and the flags it names exist",
      "sudo bash scripts/install.sh" in readme and {"--yes", "--host", "--dry-run"} <= inst_flags, str(inst_flags))
_aq = (SCRIPTS / "state.sh").read_text()
_aq = _aq[_aq.index("ask_questions() {"):]
_aq = _aq[:_aq.index("\n}\n")]
_n_q = len(re.findall(r"\bask_(?:input|yes|choice)\b", _aq))
_flat = " ".join(readme.split())
check("installer README: says how many questions the installer asks, and nothing about a speech-to-text profile",
      _n_q == 2 and "asks two questions" in _flat and "speech-to-text profile" not in _flat
      and "GPU or the CPU" not in _flat, f"ask_questions asks {_n_q}")

# ── hygiene ──────────────────────────────────────────────────────────────
shell_files = [REPO / "scripts/install.sh", CLI, *sorted(SCRIPTS.glob("*.sh"))]
modes = [f.name for f in (REPO / "scripts/install.sh", CLI) if not os.stat(f).st_mode & stat.S_IXUSR]
check("installer: install.sh and the lenoxvi command are executable", not modes, str(modes))
syn = [subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True, check=False).returncode for f in shell_files]
check("installer: every shell file parses (bash -n)", not any(syn), str(list(zip([f.name for f in shell_files], syn))))
_gpu_src = {f.relative_to(REPO).as_posix(): f.read_text().lower()
            for f in (*shell_files, SCRIPTS / "write_config.py", REPO / "deploy/lenoxvi.service.in")}
_gpu_hits = sorted(f"{n}: {w}" for n, s in _gpu_src.items()
                   for w in ("--stt", "stt_profile", "stt_model", "check_gpu", "usermod_gpu", "requirements-gpu",
                             "nvidia", "cuda", "ld_library_path", "video,render", "gpu", "profile")
                   if w in s)
check("installer: no GPU path left — no --stt, GPU probe, CUDA package or library dir, or speech profile in any "
      "installer file or the unit template", not _gpu_hits, str(_gpu_hits))
code = "\n".join(ln.split("#", 1)[0] if not ln.lstrip().startswith(("printf", "note", "warn", "die", "ok")) else ""
                 for f in shell_files for ln in f.read_text().splitlines())
check("installer: never installs drivers, never reboots, never upgrades the system, no ffmpeg",
      not re.search(r"\breboot\b|shutdown -r|apt-get (dist-)?upgrade|nvidia-driver|ubuntu-drivers|ffmpeg", code),
      str(re.findall(r".{20}(?:reboot|upgrade|nvidia-driver|ubuntu-drivers|ffmpeg).{20}", code))[:300])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
