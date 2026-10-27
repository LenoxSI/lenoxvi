#!/usr/bin/env python3
"""Sign firmware images with this bot's key (core/firmware_signing.py).

Run on the bot host, from the repo root, with the bot's Python:
    ~/venvs/lenoxvi/bin/python scripts/sign_firmware.py firmware/array/array_esp32.bin
deploy_firmware.sh runs it after copying a build in. Creates the key on first
use (secrets/firmware-signing-key.pem, or $LVI_STATE_DIR/secrets/). Prints the
key fingerprint; never the key.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import firmware_signing


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    rc = 0
    for name in argv:
        p = Path(name)
        if not p.is_file():
            print(f"missing: {p}", file=sys.stderr)
            rc = 1
            continue
        try:
            changed = firmware_signing.sign_file(p)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"FAILED {p}: {e}", file=sys.stderr)
            rc = 1
            continue
        print(f"{'signed' if changed else 'already signed'}: {p} (key {firmware_signing.fingerprint()[:16]}…)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
