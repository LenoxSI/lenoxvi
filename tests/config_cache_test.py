"""Config cache + WiiM buffer fixes from the 10-24 review (host venv).

- config._load: a write that clears the cache while a reader is mid-parse
  must not leave the reader's PRE-write copy cached (review B #1)
- WiiMOutput collects reply audio in a bytearray, not a list of ints (A #6)

Run: cd tests && ~/venvs/lenoxvi/bin/python config_cache_test.py <path-to-core>
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

CORE = sys.argv[1] if len(sys.argv) > 1 else "../core"
sys.path.insert(0, CORE)
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


scratch = Path(tempfile.mkdtemp(prefix="lvi-cfgcache-"))
cfg_file = scratch / "config.yaml"
cfg_file.write_text("wake:\n  name: Before\n")
os.environ["LVI_CONFIG"] = str(cfg_file)

import config
import yaml

config.CONFIG_PATH = str(cfg_file)
config._load.cache_clear()
check("config: first load reads the file", config.cfg("wake", "name") == "Before")
check("config: _load.cache_clear still exists (config_store and tests call it)",
      callable(getattr(config._load, "cache_clear", None)))
boot = config.boot_config()

# The race: a reader parses the OLD file slowly; meanwhile a write replaces
# the file and clears the cache. The reader's stale result must not stick.
real_safe_load = yaml.safe_load
parsing = threading.Event()
release = threading.Event()


def slow_safe_load(text):
    data = real_safe_load(text)
    if "Before" in str(text):
        parsing.set()
        release.wait(5)
    return data


config._load.cache_clear()
yaml.safe_load = slow_safe_load
result = {}
t = threading.Thread(target=lambda: result.setdefault("r", config._load()))
t.start()
parsing.wait(5)
cfg_file.write_text("wake:\n  name: After\n")   # the dashboard write lands mid-parse
config._load.cache_clear()
release.set()
t.join(5)
yaml.safe_load = real_safe_load
check("config: the reader caught mid-parse still gets its own (old) result",
      result.get("r", {}).get("wake", {}).get("name") == "Before", str(result))
check("config: ...but the cache does not keep it: the next read sees the write",
      config.cfg("wake", "name") == "After", str(config.cfg("wake", "name")))
check("config: boot_config never refreshes", config.boot_config() is boot and boot["wake"]["name"] == "Before")
cfg_file.write_text("wake: [unclosed\n")
config._load.cache_clear()
try:
    config._load()
    raised = False
except RuntimeError as e:
    raised = "parse error" in str(e)
check("config: a broken file is still a loud RuntimeError", raised)
cfg_file.unlink()
config._load.cache_clear()
check("config: a missing file still reads as {}", config._load() == {})

# WiiM: bytearray buffer
src = Path(CORE, "wiim_output.py").read_text()
check("wiim pin: reply audio collects in a bytearray (not one list element per byte)",
      "self._pcm: bytearray = bytearray()" in src and "self._pcm: list[bytes] = []" not in src)

for f in scratch.iterdir():
    f.unlink()
scratch.rmdir()
if FAILS:
    print(f"\n{len(FAILS)} FAIL(S): {FAILS}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
