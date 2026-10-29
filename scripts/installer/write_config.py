"""Write the installer's keyless config.yaml from config.example.yaml.

    write_config.py <example> <dest> --host IP
        New file only: an existing <dest> is kept untouched. The new file is
        the example with the address filled in, the echo intelligence, no
        rooms and the wake gate on. Speech comes from the example as it is.
    write_config.py --update <dest> --host IP
        Change only the address in an existing file (lenoxvi reconfigure).
    write_config.py --get <dest>
        Print the address (clip_server.host_ip) from an existing file.

Writes to a temp file and renames, so an interrupted run never leaves half
a config. Comments in the file are kept (ruamel round-trip).
"""
import argparse
import os
import sys

from ruamel.yaml import YAML


def _yaml():
    y = YAML()
    y.preserve_quotes = True
    return y


def _save(y, cfg, dest):
    tmp = f"{dest}.tmp"
    with open(tmp, "w") as f:
        y.dump(cfg, f)
    if os.path.exists(dest):
        os.chmod(tmp, os.stat(dest).st_mode & 0o777)
    os.replace(tmp, dest)


def _apply(cfg, host=None):
    if host:
        cfg.setdefault("clip_server", {})["host_ip"] = host


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+")
    p.add_argument("--update", action="store_true")
    p.add_argument("--get", action="store_true")
    p.add_argument("--host")
    a = p.parse_args(argv)
    y = _yaml()

    if a.get:
        with open(a.paths[0]) as f:
            cfg = y.load(f)
        print((cfg.get("clip_server") or {}).get("host_ip") or "")
        return 0

    if a.update:
        dest = a.paths[0]
        with open(dest) as f:
            cfg = y.load(f)
        _apply(cfg, a.host)
        _save(y, cfg, dest)
        print(f"updated {dest}")
        return 0

    if len(a.paths) != 2 or not a.host:
        p.error("new config needs <example> <dest> --host IP")
    example, dest = a.paths
    if os.path.exists(dest):
        print(f"kept existing {dest}")
        return 0
    with open(example) as f:
        cfg = y.load(f)
    cfg["intelligence"]["kind"] = "stub"
    cfg["room_zones"] = {}
    cfg["wake"]["enabled"] = True
    _apply(cfg, a.host)
    _save(y, cfg, dest)
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
