"""make-cert.sh suite: the long-lived bot CA and the leaf it issues.

The script runs against a scratch LVI_STATE_DIR with the system's real openssl — venv-free by
design: a fresh run makes the CA pair and the leaf, a re-run re-issues only the leaf against the
same CA, and a half-missing CA pair is refused with the survivor left byte for byte.

    python3 tests/make_cert_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAKE_CERT = REPO / "scripts" / "make-cert.sh"
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="lvi-make-cert-test-"))
STATE = TMP / "state"
STATE.mkdir()
CA_KEY, CA_CERT = "secrets/tls-ca-key.pem", "certs/ca.pem"


def run(ip):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LVI_")}
    env["LVI_STATE_DIR"] = str(STATE)
    return subprocess.run(["bash", str(MAKE_CERT), ip], capture_output=True, text=True,
                          env=env, timeout=30, check=False, stdin=subprocess.DEVNULL)


def ossl(*args):
    return subprocess.run(["openssl", *args], capture_output=True, text=True,
                          timeout=30, check=False, stdin=subprocess.DEVNULL)


def out(r):
    return f"rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def mode(path):
    return oct(path.stat().st_mode & 0o777)


def fp(path):
    return ossl("x509", "-in", str(path), "-noout", "-fingerprint", "-sha256").stdout.strip()


def ext(path, name):
    return ossl("x509", "-in", str(path), "-noout", "-ext", name).stdout


# ── a fresh run ───────────────────────────────────────────────────────────
leaf, ca = STATE / "certs" / "cert.pem", STATE / CA_CERT
r = run("192.0.2.10")
check("make-cert: a fresh run exits 0 and writes the CA pair, the leaf and the leaf key",
      r.returncode == 0 and ca.is_file() and leaf.is_file()
      and (STATE / "certs" / "key.pem").is_file() and (STATE / CA_KEY).is_file(), out(r))
check("make-cert: both keys land 0600 and secrets/ is 0700",
      mode(STATE / "certs" / "key.pem") == "0o600" and mode(STATE / CA_KEY) == "0o600"
      and mode(STATE / "secrets") == "0o700",
      f"key={mode(STATE / 'certs' / 'key.pem')} ca_key={mode(STATE / CA_KEY)} "
      f"secrets={mode(STATE / 'secrets')}")

v = ossl("verify", "-CAfile", str(ca), str(leaf))
check("make-cert: the leaf verifies against certs/ca.pem", v.returncode == 0, v.stdout + v.stderr)

check("make-cert: the leaf carries the LAN IP and DNS:lenoxvi in its SAN, and is not a CA",
      "IP Address:192.0.2.10" in ext(leaf, "subjectAltName")
      and "DNS:lenoxvi" in ext(leaf, "subjectAltName")
      and "CA:FALSE" in ext(leaf, "basicConstraints"),
      ext(leaf, "subjectAltName") + ext(leaf, "basicConstraints"))
check("make-cert: the CA cert says CA:TRUE", "CA:TRUE" in ext(ca, "basicConstraints"),
      ext(ca, "basicConstraints"))

# ── a re-run re-issues only the leaf ──────────────────────────────────────
ca_fp, leaf_fp = fp(ca), fp(leaf)
r2 = run("192.0.2.44")
v2 = ossl("verify", "-CAfile", str(ca), str(leaf))
check("make-cert: a re-run with a new IP keeps the CA and re-issues a leaf that still verifies",
      r2.returncode == 0 and fp(ca) == ca_fp and fp(leaf) != leaf_fp and v2.returncode == 0,
      out(r2) + "\n" + v2.stdout + v2.stderr)
check("make-cert: the re-issued leaf carries the new IP and drops the old one",
      "IP Address:192.0.2.44" in ext(leaf, "subjectAltName")
      and "192.0.2.10" not in ext(leaf, "subjectAltName"), ext(leaf, "subjectAltName"))
check("make-cert: a run leaves exactly ca.pem, cert.pem and key.pem in certs/ and the CA key in secrets/",
      sorted(p.name for p in (STATE / "certs").iterdir()) == ["ca.pem", "cert.pem", "key.pem"]
      and sorted(p.name for p in (STATE / "secrets").iterdir()) == ["tls-ca-key.pem"],
      f"certs/{sorted(p.name for p in (STATE / 'certs').iterdir())} "
      f"secrets/{sorted(p.name for p in (STATE / 'secrets').iterdir())}")
lines = [ln for ln in r2.stdout.splitlines() if ln.strip()]
check("make-cert: stdout ends with CERT-OK and a CA-OK line naming the CA fingerprint",
      len(lines) >= 2 and lines[-2].startswith("CERT-OK ") and lines[-1] == f"CA-OK {fp(ca)}",
      out(r2))

# ── a half CA pair is refused ──────────────────────────────────────────────
key_bytes, ca_bytes = (STATE / CA_KEY).read_bytes(), ca.read_bytes()
ca.unlink()
r3 = run("192.0.2.10")
check("make-cert: losing only certs/ca.pem fails the run, names both CA paths, and keeps the CA key bytes",
      r3.returncode != 0 and CA_CERT in r3.stderr and CA_KEY in r3.stderr
      and (STATE / CA_KEY).read_bytes() == key_bytes, out(r3))
ca.write_bytes(ca_bytes)
(STATE / CA_KEY).unlink()
r4 = run("192.0.2.10")
check("make-cert: losing only the CA key fails the run, names both CA paths, and keeps ca.pem",
      r4.returncode != 0 and CA_CERT in r4.stderr and CA_KEY in r4.stderr
      and ca.read_bytes() == ca_bytes, out(r4))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
