#!/bin/bash
# deploy_firmware.sh — build the reSpeaker node firmware and deliver it over
# OTA. Run on the VM (esp-idf env required). After this script, the node
# updates itself — no USB cable, no basement trip.
#
# Flow:
#   1. Build <track>_esp32.bin (pipecat URL baked in; NO WiFi credentials —
#      nodes read them from NVS, set via the dashboard WiFi flow)
#   2. Bump firmware/<track>/version.json (semver: pass "major"/"minor"/"patch",
#      default patch; or --version X.Y.Z to set explicitly)
#   3. Copy bin + version.json to the lenoxvi repo on the appliance host
#   4. Restart the bot (serves /firmware/* via core/firmware_server.py) —
#      only drops the node's live session; files are picked up per request
#   5. Poke the node (future: an HTTP poke endpoint); today the node picks
#      it up on its next boot/reconnect
#
# Usage (--track is REQUIRED — there is no default line):
#   ./scripts/deploy_firmware.sh --track array                  # patch bump
#   ./scripts/deploy_firmware.sh --track flex minor             # 1.0.1 -> 1.1.0
#   ./scripts/deploy_firmware.sh --track array --version 1.2.0
# Track law: one name per line, everywhere. Both lines build from the
# firmware repo's `main` branch (2026-10-16 branch fold), chosen by -DBOARD:
#   array  XVF3800 mic-array board  -DBOARD=array  tags array-vX.Y.Z
#          build/array/array_esp32.bin -> firmware/array/ (/firmware/array/*;
#          the legacy /firmware/version URL also serves this track)
#   flex   reSpeaker Flex           -DBOARD=flex   tags flex-vX.Y.Z
#          build/flex/flex_esp32.bin   -> firmware/flex/  (/firmware/flex/*)
# NEVER stage one line's build on the other's track: that node would
# cross-flash itself and boot the wrong hardware config.
set -euo pipefail

FW_REPO="${FW_REPO:-$HOME/projects/lenoxvi-firmware}"
LVI_LOCAL="${LVI_LOCAL:-$HOME/projects/lenoxvi}"
LVI_HOST="${LVI_HOST:-lenoxvi}"

if [ "${1:-}" != "--track" ] || [ -z "${2:-}" ]; then
  echo "FATAL: --track array|flex is required (no default line — a wrong guess cross-flashes a node)"
  exit 1
fi
TRACK="$2"; shift 2
case "$TRACK" in
  array|flex) LVI_REMOTE_DIR="projects/lenoxvi/firmware/$TRACK" ;;
  *) echo "FATAL: unknown track '$TRACK' (array|flex)"; exit 1 ;;
esac

# ── 0. Preflight: the checkout must be the firmware repo's `main` ──────────
# Since the 2026-10-16 branch fold one source tree builds both lines; the
# line is the -DBOARD passed to the build below (= $TRACK), never whatever
# happens to be checked out. A different branch (an old per-line branch, a
# feature branch) is refused: it could carry the other line's code. Backstops
# after the build: the image's own identity must match $TRACK (below), and a
# 1.2.9+ node refuses an image named for the other line.
FW_BRANCH="main"
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
# (0003 = the data-channel handshake). The fetch script verifies
# pin + patch stack and refuses a hand-edited tree.
if [ -f "$FW_REPO/scripts/fetch_libpeer.sh" ]; then
  bash "$FW_REPO/scripts/fetch_libpeer.sh" || { echo "FATAL: deps/libpeer check failed"; exit 1; }
fi
echo "==> firmware source: $FW_BRANCH @ $(git -C "$FW_REPO" rev-parse --short HEAD)"
# Test hook: tests/test_deploy_guard.sh runs the preflight matrix with no
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
# running a cred-free image and online has proven its NVS. A BRAND-NEW board with
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
# Fresh config from sdkconfig.defaults on EVERY build, one per line, inside
# its build dir. The repo-root sdkconfig is generated once and then wins over
# sdkconfig.defaults (an explicit "is not set" line survives a defaults
# change) — a watchdog-enable change in defaults would have shipped with watchdogs still OFF.
# The image must equal the committed defaults, nothing else.
SDKCFG="$FW_REPO/build/$TRACK/sdkconfig"
mkdir -p "$FW_REPO/build/$TRACK"
rm -f "$SDKCFG"
WIFI_SSID="$WIFI_SSID" WIFI_PASSWORD="$WIFI_PASSWORD" \
PIPECAT_SMALLWEBRTC_URL="$PIPECAT_SMALLWEBRTC_URL" PIPECAT_ROOM="$PIPECAT_ROOM" \
idf.py -B "$FW_REPO/build/$TRACK" -DBOARD="$TRACK" -DSDKCONFIG="$SDKCFG" build
# (One build dir per line: a cached BOARD can never leak from one line's
# build into the other's.)

BIN="$FW_REPO/build/$TRACK/${TRACK}_esp32.bin"
[ -f "$BIN" ] || { echo "FATAL: $BIN missing after build"; exit 1; }
# Prove it, don't trust it: every compile unit must define the WiFi creds as
# EMPTY. (A stale build/ or a CMake change could still bake them.) Reads only
# the build's own compile_commands.json; never prints a value.
python3 - "$FW_REPO/build/$TRACK/compile_commands.json" <<'PYEOF' || { echo "FATAL: built image carries WiFi credentials — not publishing"; exit 1; }
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
# The .bin EMBEDS its own version
# (esp_app_desc_t, project(VERSION) at build time) — the binary is the
# truth, this script's counter is not. We read the bin's version and
# GATE on it: refuse to ship a build whose embedded version disagrees
# with the manifest we're about to write. Kills the mismatch class
# (manifest/binary mismatch = the node re-flashes the same fw every
# boot) and the botched-bake class (forgot to bump CMakeLists — the
# script used to silently ship the old version under a new number).
BIN_VER=$(python3 "$LVI_LOCAL/scripts/app_bin_version.py" "$BIN")
echo "==> binary self-reports version: ${BIN_VER:-<none>}"
# Board identity: the image's compiled-in OTA URL names its own
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
  "filename": "${TRACK}_esp32.bin",
  "md5": "$BIN_MD5",
  "built": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
echo "==> version $CUR -> $NEW (md5 $BIN_MD5)"

# ── Version gate: the bin's embedded version MUST equal the manifest's ─────
# A mismatch means CMakeLists wasn't bumped (botched bake) or the build
# picked up a stale artifact. Shipping it re-flashes the same firmware
# every boot or mislabels a deploy. Either way: STOP.
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
# The manifest names only ${TRACK}_esp32.bin — drop any other .bin left in the
# track dir (e.g. a pre-rename node_esp32.bin) so it holds exactly what's served.
ssh "$LVI_HOST" "find ~/$LVI_REMOTE_DIR -maxdepth 1 -name '*.bin' ! -name '${TRACK}_esp32.bin' -delete"

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
    Expected: "firmware_server: serving $TRACK/${TRACK}_esp32.bin" then the node
    reboots into $NEW.
EOF