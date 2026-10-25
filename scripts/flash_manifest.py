#!/usr/bin/env python3
"""Build one firmware line's FULL flash set from an ESP-IDF build dir.

Usage: flash_manifest.py <build_dir> <out_dir>

Copies the bootloader, partition table and OTA data next to the app under
fixed served names and writes flash.json for the dashboard's Node setup page.
Offsets come from the build's own flasher_args.json, never from this file.
Refuses anything unexpected: a blank board flashed from a wrong set is a
dead node.
"""
import json
import shutil
import sys
from pathlib import Path

SERVED = {"bootloader": "bootloader.bin", "partition-table": "partition-table.bin",
          "ota-data": "ota_data_initial.bin"}
BOOT_OFFSETS = {0x0: "bootloader", 0x8000: "partition-table", 0x10000: "ota-data"}
APP_OFFSET = 0x20000
SETTINGS_REGION = (0x9000, 0x10000)  # nvs + phy_init: never part of a flash set


def build(build_dir: Path, out_dir: Path) -> dict:
    try:
        fa = json.loads((build_dir / "flasher_args.json").read_text())
        files = {int(k, 16): v for k, v in fa["flash_files"].items()}
        fs = fa["flash_settings"]
        flash = {"mode": fs["flash_mode"], "freq": fs["flash_freq"], "size": fs["flash_size"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"FATAL: can't read {build_dir}/flasher_args.json ({e})")
    if set(files) != set(BOOT_OFFSETS) | {APP_OFFSET}:
        raise SystemExit(f"FATAL: unexpected flash layout {sorted(hex(o) for o in files)}")
    parts = []
    lo, hi = SETTINGS_REGION
    for off, name in sorted(BOOT_OFFSETS.items()):
        src = build_dir / files[off]
        if not src.is_file():
            raise SystemExit(f"FATAL: missing {src}")
        if off < hi and off + src.stat().st_size > lo:
            raise SystemExit(f"FATAL: {name} would write the settings region")
        parts.append({"name": name, "file": SERVED[name], "offset": off})
    out_dir.mkdir(parents=True, exist_ok=True)
    for p, off in zip(parts, sorted(BOOT_OFFSETS)):
        shutil.copyfile(build_dir / files[off], out_dir / p["file"])
    manifest = {"flash": flash, "parts": parts, "app_offset": APP_OFFSET}
    (out_dir / "flash.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: flash_manifest.py <build_dir> <out_dir>")
    m = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"flash set: {', '.join(p['file'] for p in m['parts'])} + flash.json")
