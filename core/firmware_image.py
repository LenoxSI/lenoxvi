"""Which OTA track an ESP32 app image belongs to — read from the image itself.

Two firmware lines, one track name each, used everywhere (server dirs,
upload API, deploy script, git branches, tags):

    array — XVF3800 mic-array board   (-DBOARD=array, tags array-v*)
    flex  — reSpeaker Flex             (-DBOARD=flex,  tags flex-v*)
    (both lines build from the firmware repo's single `main` branch)

A wrong pick cross-flashes the other node (AGENTS.md cross-flash law; the
array node has no way to boot Flex firmware), so an image must PROVE its
line. Two independent witnesses:

1. Project name (esp_app_desc_t.project_name, from CMake `project()`):
   `array_esp32` / `flex_esp32` since firmware 1.2.9. Older images of BOTH
   lines say `node_esp32` and can't be told apart by name.

2. OTA URL format (ota.cpp FW_URL_FMT, compiled in per line) — the track an
   image will fetch its own updates from is the track it belongs on:

       flex           "%.*s/firmware/flex/%s"
       array >=1.2.9  "%.*s/firmware/array/%s"
       array <=1.2.8  "%.*s/firmware/%s"        (legacy URL, still served)

Rule: exactly one URL marker is required. A named image (1.2.9+) must ALSO
name the same line; a `node_esp32` image is judged by its marker alone (the
2026-10-15 guard; verified 2026-10-16 against the served array 1.2.8 and
flex 1.2.8 images). Anything else is None — never guessed onto a track.
"""

TRACKS = ("array", "flex")

TRACK_LABEL = {"array": "array (XVF3800 mic-array board)", "flex": "reSpeaker Flex"}

# esp_app_desc_t rides right after the 24-byte image header + 8-byte first
# segment header: magic at byte 32, version[32] at 48, project_name[32] at 80.
_DESC_MAGIC = b"\x32\x54\xcd\xab"
_PROJECT_TRACK = {b"array_esp32": "array", b"flex_esp32": "flex"}
_LEGACY_PROJECT = b"node_esp32"

_MARKERS = (
    (b"%.*s/firmware/flex/%s", "flex"),
    (b"%.*s/firmware/array/%s", "array"),
    (b"%.*s/firmware/%s", "array"),     # array <=1.2.8 — legacy URL
)


def project_name(data: bytes) -> bytes | None:
    """The image's esp_app_desc_t project_name, or None if not an app image."""
    if data[32:36] != _DESC_MAGIC:
        return None
    return data[80:112].split(b"\x00")[0]


def image_track(data: bytes) -> str | None:
    """'array' or 'flex' when the image proves its line; None otherwise."""
    found = [track for mark, track in _MARKERS if mark in data]
    if len(found) != 1:
        return None                      # no marker, or more than one
    marker_track = found[0]
    name = project_name(data)
    if name in _PROJECT_TRACK:
        # Named image (1.2.9+): name and URL must agree.
        return marker_track if _PROJECT_TRACK[name] == marker_track else None
    if name == _LEGACY_PROJECT:
        return marker_track              # pre-1.2.9: the URL marker decides
    return None                          # unknown project — not ours
