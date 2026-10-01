#!/usr/bin/env bash
# fetch_xmos_fw.sh — download the XMOS XVF3800 I2S-master firmware image
# (the ONLY image the mic nodes will run; see README "Mic node hardware").
#
# WHY THIS IS A DOWNLOAD AND NOT A COMMITTED FILE (M15, Opus review):
# the image is released by the reSpeaker project with NO LICENSE file —
# redistributing it in this repo is not permitted. You fetch it from
# upstream, same as a trained wake-word model.
#
# Source (measured, design note): reSpeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY
# → xmos_firmwares/i2s/application_xvf3800_i2s_master_v1.0.8_48k.bin
# md5 a78df62809b03a7bec72bfa139a17521, 888,832 bytes.
#
# NEVER flash the slave image (16k) — boot lottery, 4 lost days (design note).
set -euo pipefail

DEST="${1:-firmware/xvf3800/application_xvf3800_i2s_master_v1.0.8_48k.bin}"
EXPECT_MD5="a78df62809b03a7bec72bfa139a17521"
BASE="https://github.com/reSpeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/raw/main/xmos_firmwares/i2s"

mkdir -p "$(dirname "$DEST")"
echo "fetch: $BASE/$(basename "$DEST")"
curl -fsSL "$BASE/$(basename "$DEST")" -o "$DEST"

GOT=$(md5sum "$DEST" | cut -d' ' -f1)
if [ "$GOT" != "$EXPECT_MD5" ]; then
    echo "md5 MISMATCH: got $GOT, expected $EXPECT_MD5" >&2
    echo "refusing to keep the file — delete it and retry" >&2
    rm -f "$DEST"
    exit 1
fi
echo "ok: $DEST (md5 verified)"
echo "flash per README 'Mic node hardware' (XMOS port, DFU, hold Mute)."