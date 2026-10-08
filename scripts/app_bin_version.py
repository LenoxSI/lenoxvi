"""Parse esp_app_desc_t's version straight out of an ESP32 app .bin.

The descriptor (esp_app_desc.h, IDF 5.x) rides the app's first DROM
segment:
  +0  magic 0xABCD5432 (as written: bytes 32 54 CD AB)
  +4  secure_version (uint32)
  +8  reserved[2] (uint32 x2, IDF 5.x)
  +16 version[32]     <- the semver from project(... VERSION x.y.z)
  +48 project_name[32]

Verified live 2026-10-08 against a built flex image (reports 1.1.0 from
project(node_esp32 VERSION 1.1.0)); esptool image_info v1 does NOT print
the app version, so we parse it ourselves.

M9 (design note): deploy_firmware.sh uses this so the SHIPPED manifest can
never disagree with the binary it ships — the bin is the truth, the
script's counter is not.
"""
import struct

MAGIC = bytes([0x32, 0x54, 0xCD, 0xAB])


def app_version(bin_path: str) -> tuple[str, str] | None:
    data = open(bin_path, "rb").read()
    off = data.find(MAGIC)
    if off < 0:
        return None
    version = data[off + 16: off + 48].split(b"\x00")[0].decode(errors="replace")
    project = data[off + 48: off + 80].split(b"\x00")[0].decode(errors="replace")
    return version, project


if __name__ == "__main__":
    import sys
    r = app_version(sys.argv[1])
    print(r[0] if r else "")