#!/bin/bash
# deploy_firmware.sh — build the reSpeaker node firmware and deliver it over
# OTA. Run on the VM (esp-idf env required). After this script, the node
# updates itself — no USB cable, no basement trip.
#
# Flow:
#   1. Build node_esp32.bin (WIFI creds + pipecat URL baked in)
#   2. Bump firmware/node/version.json (semver: pass "major"/"minor"/"patch",
#      default patch; or --version X.Y.Z to set explicitly)
#   3. Copy bin + version.json to lenoxvi repo on appliance-host
#   4. Restart the bot (serves /firmware/* via core/firmware_server.py)
#   5. Poke the node: send "check-ota" over the WebRTC data channel via the
#      bot's debug hook — or wait for the node's boot-time check on next
#      natural reboot
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

WIFI_SSID="${WIFI_SSID:-MyWiFiNetwork}"
WIFI_PASSWORD="${WIFI_PASSWORD:?must be set}"
PIPECAT_SMALLWEBRTC_URL="${PIPECAT_SMALLWEBRTC_URL:-https://203.0.113.185:7860/api/offer}"
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

# ── 3. Ship to appliance-host ──────────────────────────────────────────────────
echo "==> copying firmware + manifest to $LVI_HOST"
scp -q "$BIN" "$VFILE" "$LVI_HOST:$LVI_REMOTE_DIR/"

# ── 4. Restart bot (picks up new /firmware/* files) ────────────────────────
echo "==> restarting bot on $LVI_HOST"
ssh "$LVI_HOST" 'bash ~/projects/lenoxvi/scripts/restart.sh'

# ── 5. Poke the node ───────────────────────────────────────────────────────
# The node checks at every boot; for a no-touch update, send check-ota over
# the data channel. Simplest reliable poke today: none needed if the node
# reboots naturally; otherwise use the bot's console (future: an HTTP poke
# endpoint). We log what to watch for:
cat <<EOF

==> DONE. Firmware $NEW staged on $LVI_HOST.
    The node will pick it up on its next boot, or send it the
    {"type": "check-ota"} data-channel message to update in place.
    Watch (appliance-host): tail -f ~/esp32-flash/serial via serial_watch.py
    Expected: "OTA: Update available -> $NEW" then reboot into new build.
EOF