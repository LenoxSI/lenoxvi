"""Which OTA track an ESP32 app image belongs to — read from the image itself.

Both firmware lines (array = track "node", reSpeaker Flex = track "flex")
build the same project name (`node_esp32`) and, since 2026-10-15, the same
version numbers for the same hardening level. So neither the filename, the
project name nor the version can tell a human — or the upload route — which
board an image is for. A wrong pick cross-flashes the other node (AGENTS.md
cross-flash law; the array node has no way to boot Flex firmware).

The image DOES know where it will fetch its own updates from. ota.cpp builds
the update URL from a format string that is compiled in per line:

    flex  (-DXVF_OTA_TRACK="flex"):  "%.*s/firmware/flex/%s"
    array (no XVF_OTA_TRACK):        "%.*s/firmware/%s"

That is exactly the property that matters — the track an image will OTA from
is the track it belongs on. Present in every image since flex 1.0.0
introduced per-model tracks (verified 2026-10-15 against array 1.1.3, 1.2.7,
1.2.8 and flex 1.2.6, 1.2.7: exactly one marker each, never both).
"""

FLEX_MARK = b"%.*s/firmware/flex/%s"
NODE_MARK = b"%.*s/firmware/%s"


def image_track(data: bytes) -> str | None:
    """'flex' or 'node' for an image carrying exactly one line's marker;
    None when it carries neither (not an lenoxvi app, or a pre-track build)
    or both (can't be trusted either way)."""
    flex = FLEX_MARK in data
    node = NODE_MARK in data
    if flex and not node:
        return "flex"
    if node and not flex:
        return "node"
    return None


TRACK_LABEL = {"node": "array (XVF3800 mic-array board)", "flex": "reSpeaker Flex"}
