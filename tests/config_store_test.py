"""config_store.update — every dashboard save as one locked read-modify-write (#77).

- update() returns what fn returns, runs fn on a worker thread holding
  config_store.LOCK for the whole call, and lets fn's exceptions reach the
  caller unchanged with config.yaml untouched
- 20 concurrent read-pause-write bumps all land: the lock spans each one

Run: cd tests && ../.venv/bin/python config_store_test.py <path-to-core>
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


scratch = Path(tempfile.mkdtemp(prefix="lvi-cfgstore-"))
cfg_file = scratch / "config.yaml"
cfg_file.write_text("counter:\n  n: 0\n")
os.environ["LVI_CONFIG"] = str(cfg_file)

# E402 by design: config_store reads the LVI_CONFIG set above.
import config_store  # noqa: E402


# update() is a plain wrapper: fn's return value comes back to the caller.
value = asyncio.run(config_store.update(lambda: "the value"))
check("update: returns what fn returns", value == "the value", f"got {value!r}")

# fn runs off the caller's thread, and LOCK is held for the whole call:
# while fn is inside, another thread's non-blocking acquire must fail.
caller_thread = threading.get_ident()
fn_thread = {}
fn_started = threading.Event()
release_fn = threading.Event()


def _hold_lock():
    fn_thread["id"] = threading.get_ident()
    fn_started.set()
    release_fn.wait(5)


async def _while_fn_holds_lock():
    task = asyncio.get_running_loop().create_task(config_store.update(_hold_lock))
    for _ in range(5000):            # 1 ms polls, bounded at 5 s
        if fn_started.is_set():
            break
        await asyncio.sleep(0.001)
    locked_out = not config_store.LOCK.acquire(blocking=False)
    if not locked_out:               # never leave the probe holding the lock
        config_store.LOCK.release()
    release_fn.set()
    await task
    return locked_out


locked_out = asyncio.run(_while_fn_holds_lock())
check("update: fn runs on a worker thread, not the caller's",
      fn_thread.get("id") is not None and fn_thread["id"] != caller_thread,
      f"caller={caller_thread} fn={fn_thread.get('id')}")
check("update: another thread cannot take LOCK while fn runs", locked_out)

# fn's exception reaches the caller unchanged, and the file is untouched.
cfg_file.write_text("counter:\n  n: 3\n")
before = cfg_file.read_bytes()
boom_msg = "deliberately not found"


def _boom():
    raise FileNotFoundError(boom_msg)


caught = None
try:
    asyncio.run(config_store.update(_boom))
except FileNotFoundError as e:
    caught = e
check("update: fn's exception reaches the caller unchanged",
      type(caught) is FileNotFoundError and str(caught) == boom_msg, f"caught={caught!r}")
check("update: a fn that raises leaves config.yaml untouched",
      cfg_file.read_bytes() == before)

# The race this lane exists for: each bump's read and write share one lock,
# so no bump can read a value another bump is about to overwrite.
cfg_file.write_text("counter:\n  n: 0\n")


async def _bump_all():
    def bump():
        n = int((config_store.read_section("counter") or {}).get("n") or 0)
        time.sleep(0.005)             # the window an unlocked save would lose
        config_store.write_section("counter", {"n": n + 1})

    await asyncio.gather(*(config_store.update(bump) for _ in range(20)))


asyncio.run(_bump_all())
final = (config_store.read_section("counter") or {}).get("n")
check("update: 20 concurrent read-modify-writes all land", final == 20, f"counter={final}")

for f in scratch.iterdir():
    f.unlink()
scratch.rmdir()
if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
