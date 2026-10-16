#!/bin/bash
# deploy_firmware.sh — build the reSpeaker node firmware and deliver it over
# OTA. Run on the VM (esp-idf env required). After this script, the node
# updates itself — no USB cable, no basement trip.
#
# Flow:
#   1. Build node_esp32.bin (pipecat URL baked in; NO WiFi credentials —
#      nodes read them from NVS, set via the dashboard WiFi flow)
#   2. Bump firmware/node/version.json (semver: pass "major"/"minor"/"patch",
#      default patch; or --version X.Y.Z to set explicitly)
#   3. Copy bin + version.json to the lenoxvi repo on the appliance host
#   4. Restart the bot (serves /firmware/* via core/firmware_server.py) —
#      only drops the node's live session; files are picked up per request
#   5. Poke the node (future: an HTTP poke endpoint); today the node picks
#      it up on its next boot/reconnect
#
# Usage:
#   ./scripts/deploy_firmware.sh              # patch bump, array track
#   ./scripts/deploy_firmware.sh minor        # 1.0.1 -> 1.1.0
#   ./scripts/deploy_firmware.sh --version 1.2.0
#   ./scripts/deploy_firmware.sh --track flex # flex track (any bump args
#                                             # work the same; binary is
#                                             # built with XVF_OTA_TRACK=flex
#                                             # and ships to firmware/flex/)
# Track law (design note/034a): the array node (XVF3800 mic-array board) is
# track "node" — legacy URLs /firmware/version + /firmware/<bin>, manifest
# in firmware/node/. The flex node (reSpeaker Flex) is track "flex" —
# /firmware/flex/*, manifest in firmware/flex/. NEVER stage a flex build
# into firmware/node/ or vice versa: the other node would cross-flash
# itself and boot the wrong hardware config.
set -euo pipefail

FW_REPO="${FW_REPO:-$HOME/projects/lenoxvi-firmware}"
LVI_LOCAL="${LVI_LOCAL:-$HOME/projects/lenoxvi}"
LVI_HOST="${LVI_HOST:-lenoxvi}"

TRACK="node"
if [ "${1:-}" = "--track" ]; then
  TRACK="$2"; shift 2
fi
case "$TRACK" in
  node) LVI_REMOTE_DIR="projects/lenoxvi/firmware/node"; OTA_TRACK_ENV="" ;;
  flex) LVI_REMOTE_DIR="projects/lenoxvi/firmware/flex"; OTA_TRACK_ENV="flex" ;;
  *) echo "FATAL: unknown track '$TRACK' (node|flex)"; exit 1 ;;
esac

# ── 0. Preflight: the checkout must BE this track's firmware line ──────────
# The script builds whatever $FW_REPO has checked out. Both lines live in one
# repo (array = array-xvf3800, flex = flex-xvf3800), so the wrong checkout publishes
# the OTHER board's firmware to this track — the node accepts it as an
# upgrade and cross-flashes itself (2026-10-15: the checkout sat on
# flex-xvf3800 while the default track is node). Refuse instead of guessing.
case "$TRACK" in
  node) FW_BRANCH="array-xvf3800" ;;
  flex) FW_BRANCH="flex-xvf3800" ;;
esac
FW_CUR=$(git -C "$FW_REPO" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "(detached HEAD)")
if [ "$FW_CUR" != "$FW_BRANCH" ]; then
  echo "FATAL: track '$TRACK' ships branch '$FW_BRANCH', but $FW_REPO is on '$FW_CUR'."
  echo "       Building it would publish the other board's firmware to the '$TRACK'"
  echo "       track and the node would cross-flash itself. Switch first:"
  echo "         git -C \"$FW_REPO\" checkout $FW_BRANCH"
  exit 1
fi
# Tracked changes = an image no commit can reproduce. (Untracked/ignored files
# — sdkconfig, deps/, components/ — are the build environment, not source.)
if [ -n "$(git -C "$FW_REPO" status --porcelain --untracked-files=no)" ]; then
  echo "FATAL: $FW_REPO has uncommitted changes to tracked files — commit or stash first:"
  git -C "$FW_REPO" status --short --untracked-files=no | head -10
  exit 1
fi
# libpeer is gitignored and shared by both lines; its patches are load-bearing
# (0003 = the data-channel handshake, design note). The fetch script verifies
# pin + patch stack and refuses a hand-edited tree.
if [ -f "$FW_REPO/scripts/fetch_libpeer.sh" ]; then
  bash "$FW_REPO/scripts/fetch_libpeer.sh" || { echo "FATAL: deps/libpeer check failed"; exit 1; }
fi
echo "==> firmware source: $FW_BRANCH @ $(git -C "$FW_REPO" rev-parse --short HEAD)"
# Test hook: scripts/test_deploy_guard.sh runs the preflight matrix with no
# credentials, no build and no publish.
if [ "${DEPLOY_PREFLIGHT_ONLY:-}" = "1" ]; then
  echo "==> preflight OK (DEPLOY_PREFLIGHT_ONLY) — stopping before the build"
  exit 0
fi

# WiFi credentials are NEVER baked into an OTA image (2026-10-15): these
# images are served over plain HTTP to the whole LAN (/firmware/...), so a
# baked password is readable by anyone who can fetch the .bin. Nodes read
# WiFi from NVS (tknode: wifi_ssid/wifi_pass — dashboard WiFi flow); the
# baked value is only a fallback when NVS is empty. Every node already
# running a cred-free image and online has proven its NVS (both did on
# 2026-10-15: room-b + room-a on 1.2.8). A BRAND-NEW board with
# empty NVS needs one USB bench flash with credentials first — that is a
# bench build, never this script.
if [ -n "${WIFI_SSID:-}${WIFI_PASSWORD:-}" ]; then
  echo "NOTE: WIFI_SSID/WIFI_PASSWORD are set in your environment — ignored;"
  echo "      OTA images never carry WiFi credentials (nodes use NVS)."
fi
WIFI_SSID=""
WIFI_PASSWORD=""
PIPECAT_SMALLWEBRTC_URL="${PIPECAT_SMALLWEBRTC_URL:?must be set (e.g. https://<bot-host-LAN-IP>:7860/api/offer)}"
PIPECAT_ROOM="${PIPECAT_ROOM:-lenoxvi}"

# ── 1. Build ────────────────────────────────────────────────────────────────
echo "==> building firmware (track: $TRACK)"
cd "$FW_REPO"
export IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env/idf5.3_py3.12_env"
source "$HOME/esp-idf/export.sh" >/dev/null
# Data-channel message logging is debug-only and prints set_wifi payloads
# (the WiFi password) to serial — never in a deployed image. unset (not an
# empty assignment: the old firmware CMake treated set-but-empty as ON, so
# this line used to ENABLE it on every script build, 2026-10-13). Also
# protects against a value inherited from the caller's shell.
unset LOG_DATACHANNEL_MESSAGES
WIFI_SSID="$WIFI_SSID" WIFI_PASSWORD="$WIFI_PASSWORD" \
PIPECAT_SMALLWEBRTC_URL="$PIPECAT_SMALLWEBRTC_URL" PIPECAT_ROOM="$PIPECAT_ROOM" \
XVF_OTA_TRACK="$OTA_TRACK_ENV" idf.py build

BIN="$FW_REPO/build/node_esp32.bin"
[ -f "$BIN" ] || { echo "FATAL: $BIN missing after build"; exit 1; }
# Prove it, don't trust it: every compile unit must define the WiFi creds as
# EMPTY. (A stale build/ or a CMake change could still bake them.) Reads only
# the build's own compile_commands.json; never prints a value.
python3 - "$FW_REPO/build/compile_commands.json" <<'PYEOF' || { echo "FATAL: built image carries WiFi credentials — not publishing"; exit 1; }
import json, re, sys
cmds = [c.get("command", "") for c in json.load(open(sys.argv[1]))]
bad = 0
seen = 0
for c in cmds:
    for key in ("WIFI_SSID", "WIFI_PASSWORD"):
        for m in re.finditer(r"-D" + key + r"=(\S+)", c):
            seen += 1
            if m.group(1).replace("\\", "").replace('"', ""):
                bad += 1
if seen == 0 or bad:
    sys.exit(1)
print(f"==> WiFi credentials: empty in all {seen} defines (NVS supplies them)")
PYEOF
BIN_MD5=$(md5sum "$BIN" | cut -d' ' -f1)

# ── 2. Version bump ────────────────────────────────────────────────────────
# M9 (design note, fixed 2026-10-08): the .bin EMBEDS its own version
# (esp_app_desc_t, project(VERSION) at build time) — the binary is the
# truth, this script's counter is not. We read the bin's version and
# GATE on it: refuse to ship a build whose embedded version disagrees
# with the manifest we're about to write. Kills the note-016 class
# (manifest/binary mismatch = the node re-flashes the same fw every
# boot) and the botched-bake class (forgot to bump CMakeLists — the
# script used to silently ship the old version under a new number).
BIN_VER=$(python3 "$LVI_LOCAL/scripts/app_bin_version.py" "$BIN")
echo "==> binary self-reports version: ${BIN_VER:-<none>}"
# Board identity (design note): the image's compiled-in OTA URL names its own
# track. Backstop to the branch guard — never publish an image whose own
# track isn't the one we're about to stage it on.
BIN_TRACK=$(python3 "$LVI_LOCAL/scripts/app_bin_version.py" --track "$BIN")
if [ "$BIN_TRACK" != "$TRACK" ]; then
  echo "FATAL: the built image belongs to track '${BIN_TRACK:-<unknown>}', not '$TRACK' — not publishing (cross-flash)."
  exit 1
fi
echo "==> binary self-reports track: $BIN_TRACK"
VFILE="$LVI_LOCAL/firmware/$TRACK/version.json"
mkdir -p "$LVI_LOCAL/firmware/$TRACK"
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

# ── M9 gate: the bin's embedded version MUST equal the manifest's ──────────
# A mismatch means CMakeLists wasn't bumped (botched bake) or the build
# picked up a stale artifact. Shipping it re-flashes the same firmware
# every boot (design note) or mislabels a deploy. Either way: STOP.
if [ -z "$BIN_VER" ]; then
  echo "FATAL: no embedded version found in $BIN — not an lenoxvi app image?"; exit 1
fi
if [ "$BIN_VER" != "$NEW" ]; then
  echo "FATAL: binary says $BIN_VER, manifest says $NEW."
  echo "  Bump project(VERSION) in $FW_REPO/CMakeLists.txt to $NEW and rebuild"
  echo "  (or pass --version $BIN_VER to ship what was actually built)."
  exit 1
fi

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