#!/usr/bin/env python3
"""flash manifest: the full flash set the dashboard's Node setup page flashes.

Builds fake ESP-IDF build dirs (the real flasher_args.json shape) and checks
scripts/flash_manifest.py publishes exactly the three boot parts + flash.json,
refuses every unexpected layout, and that deploy_firmware.sh ships the set and
keeps it through its cleanup. Run from the repo root or tests/.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
FAILS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -- {detail}"))
    if not cond:
        FAILS.append(name)


STD = {"0x0": "bootloader/bootloader.bin", "0x8000": "partition_table/partition-table.bin",
       "0x10000": "ota_data_initial.bin", "0x20000": "waveshare_esp32.bin"}
SIZES = {"bootloader/bootloader.bin": 20000, "partition_table/partition-table.bin": 3072,
         "ota_data_initial.bin": 8192, "waveshare_esp32.bin": 4096}


def build_dir(files=None, sizes=None):
    d = Path(tempfile.mkdtemp())
    files = files or STD
    for rel in files.values():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xA5" * {**SIZES, **(sizes or {})}.get(rel, 4096))
    (d / "flasher_args.json").write_text(json.dumps({
        "flash_settings": {"flash_mode": "dio", "flash_size": "8MB", "flash_freq": "80m"},
        "flash_files": files}))
    return d


import flash_manifest

out = Path(tempfile.mkdtemp())
try:
    m = flash_manifest.build(build_dir(), out)
    check("flash manifest: three boot parts at the build's offsets",
          [(p["name"], p["offset"]) for p in m["parts"]]
          == [("bootloader", 0), ("partition-table", 0x8000), ("ota-data", 0x10000)], m)
    check("flash manifest: app offset + flash settings copied",
          m["app_offset"] == 0x20000 and m["flash"] == {"mode": "dio", "freq": "80m", "size": "8MB"}, m)
    check("flash manifest: boot files + flash.json written under their served names",
          all((out / n).is_file() for n in ("bootloader.bin", "partition-table.bin", "ota_data_initial.bin", "flash.json"))
          and json.loads((out / "flash.json").read_text()) == m
          and (out / "bootloader.bin").stat().st_size == 20000, sorted(q.name for q in out.iterdir()))
except SystemExit as e:
    check("flash manifest: a good build dir is accepted", False, str(e))


def refused(d):
    """True only for a real refusal ('FATAL: ...'), never a stub's blanket exit."""
    try:
        flash_manifest.build(d, Path(tempfile.mkdtemp()))
        return False
    except SystemExit as e:
        return str(e).startswith("FATAL")


check("flash manifest: an extra part (anything in the settings region) is refused",
      refused(build_dir({**STD, "0x9000": "nvs.bin"})))
check("flash manifest: a moved app offset is refused",
      refused(build_dir({("0x30000" if k == "0x20000" else k): v for k, v in STD.items()})))
_d = build_dir()
(_d / "ota_data_initial.bin").unlink()
check("flash manifest: a missing boot file is refused", refused(_d))
check("flash manifest: a bootloader that runs into the settings region is refused",
      refused(build_dir(sizes={"bootloader/bootloader.bin": 0x9000 + 1})))
_d = build_dir()
(_d / "flasher_args.json").write_text("{not json")
check("flash manifest: an unreadable flasher_args.json is refused", refused(_d))
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "flash_manifest.py"), "/nonexistent", str(out)],
                   capture_output=True, text=True, check=False)
check("flash manifest: the CLI exits nonzero with a FATAL line on a bad build dir",
      r.returncode != 0 and "FATAL" in (r.stdout + r.stderr), (r.returncode, r.stdout + r.stderr))

src = (ROOT / "scripts" / "deploy_firmware.sh").read_text()
check("flash manifest: deploy builds the set BEFORE writing version.json",
      0 < src.find("flash_manifest.py") < src.find('cat > "$VFILE"'))
check("flash manifest: deploy ships the three boot files + flash.json",
      all(f'"$FSTAGE"/{n}' in src for n in ("bootloader.bin", "partition-table.bin", "ota_data_initial.bin", "flash.json")))
check("flash manifest: deploy's cleanup spares the boot files",
      all(f"! -name '{n}'" in src for n in ("bootloader.bin", "partition-table.bin", "ota_data_initial.bin")))

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
