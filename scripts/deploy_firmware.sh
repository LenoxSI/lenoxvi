#!/bin/bash
# deploy_firmware.sh — build the reSpeaker node firmware and deliver it over
# OTA. Run on the VM (esp-idf env required). After this script, the node
# updates itself — no USB cable, no basement trip.
#
# Flow:
#   1. Build node_esp32.bin (WIFI creds + pipecat URL baked in)
#   2. Bump firmware/node/version.json (semver: pass "major"/"minor"/"patch",
#      default patch; or --version X.Y.Z to set explicitly)
#   3. Copy bin + version.json to the lenoxvi repo on the appliance host
#   4. Restart the bot (serves /firmware/* via core/firmware_server.py) —
#      only drops the node's live session; files are picked up per request
#   5. Poke the node (future: an HTTP poke endpoint); today the node picks
#      it up on its next boot/reconnect
#
# Usage:
#   ./scripts/deploy_firmware.sh              # patch bump
#   ./scripts/deploy_firmware.sh minor        # 1.0.1 -> 1.1.0
#   ./scripts/deploy_firmware.sh --version 1.2.0
set -euo pipefail

FW_REPO="${FW_REPO:-$HOME/projects/lenoxvi-firmware}"
LVI_LOCAL="${LVI_LOCAL:-$HOME/projects/lenoxvi}"
LVI_HOST="${LVI_HOST:-lenoxvi}"
LVI_REMOTE_DIR="projects/lenoxvi/firmware/node"

WIFI_SSID="${WIFI_SSID:?must be set (your WiFi network name)}"
WIFI_PASSWORD="${WIFI_PASSWORD:?must be set}"
PIPECAT_SMALLWEBRTC_URL="${PIPECAT_SMALLWEBRTC_URL:?must be set (e.g. https://<bot-host-LAN-IP>:7860/api/offer)}"
PIPECAT_ROOM="${PIPECAT_ROOM:-bedroom}"

# ── 1. Build ────────────────────────────────────────────────────────────────
echo "==> building firmware"
cd "$FW_REPO"
export IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env/idf5.3_py3.12_env"
source "$HOME/esp-idf/export.sh" >/dev/null
LOG_DATACHANNEL_MESSAGES= WIFI_SSID="$WIFI_SSID" WIFI_PASSWORD="$WIFI_PASSWORD" \
PIPECAT_SMALLWEBRTC_URL="$PIPECAT_SMALLWEBRTC_URL" PIPECAT_ROOM="$PIPECAT_ROOM" \
idf.py build

BIN="$FW_REPO/build/node_esp32.bin"
[ -f "$BIN" ] || { echo "FATAL: $BIN missing after build"; exit 1; }
BIN_MD5=$(md5sum "$BIN" | cut -d' ' -f1)

# ── 2. Version bump ────────────────────────────────────────────────────────
VFILE="$LVI_LOCAL/firmware/node/version.json"
mkdir -p "$LVI_LOCAL/firmware/node"
if [ -f "$VFILE" ]; then
  CUR=$(python3 -c "import json;print(json.load(open('$VFILE'))['version'])")
else
  CUR="0.0.0"
fi

if [ "${1:-}" = "--version" ]; then
  NEW="$2"; shift 2
else
  BUMP="${1:-patch}"
  NEW=$(python3 - "$CUR" "$BUMP" <<'EOF'
import sys
cur, bump = sys.argv[1], sys.argv[2]
a, b, c = (int(x) for x in cur.split("."))
if bump == "major": a, b, c = a + 1, 0, 0
elif bump == "minor": a, b, c = a, b + 1, 0
else: c += 1
print(f"{a}.{b}.{c}")
EOF
)
fi

cat > "$VFILE" <<EOF
{
  "version": "$NEW",
  "filename": "node_esp32.bin",
  "md5": "$BIN_MD5",
  "built": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
echo "==> version $CUR -> $NEW (md5 $BIN_MD5)"

# ── 3. Ship to the appliance host ──────────────────────────────────────────────────
echo "==> copying firmware + manifest to $LVI_HOST"
scp -q "$BIN" "$VFILE" "$LVI_HOST:$LVI_REMOTE_DIR/"

# ── 4. Restart bot (drops the node's session; files read per request) ──────
echo "==> restarting bot on $LVI_HOST"
ssh "$LVI_HOST" 'bash ~/projects/lenoxvi/scripts/restart.sh'

# ── 5. Poke the node ───────────────────────────────────────────────────────
# The node checks at every boot; in-place updates (an HTTP poke
# endpoint) are future work — nothing sends a data-channel message
# today. We log what to watch for:
cat <<EOF

==> DONE. Firmware $NEW staged on $LVI_HOST.
    The node will pick it up on its next boot (in-place updates are
    future work — nothing sends a data-channel message today).
    OTA is the only flash path (USB is retired). Watch (appliance host):
      journalctl --user -u lenoxvi-bot -f | grep firmware_server
    Expected: "firmware_server: serving node_esp32.bin" then the node
    reboots into $NEW.
EOF