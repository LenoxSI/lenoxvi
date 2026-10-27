#!/usr/bin/env bash
# LenoxVI installer: a fresh Ubuntu / Debian / Raspberry Pi OS (64-bit) box
# to a running bot behind a system service, then the dashboard does the rest.
#
#   sudo bash scripts/install.sh            # from a clone; asks three questions
#   sudo bash scripts/install.sh --yes --host 192.0.2.10   # no questions
#   bash scripts/install.sh --dry-run --yes --host 192.0.2.10  # print, change nothing
#
# Installs: code in /opt/lenoxvi (read-only to the bot), data in
# /var/lib/lenoxvi (config, keys, certs, models), service lenoxvi,
# command lenoxvi. Never asks for keys, never installs drivers, never
# reboots, never overwrites an existing config or keys file.
set -euo pipefail

LVI_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/install.sh [options]

  --yes             use the default answer for every question
  --host IP         this machine's LAN address (default: detected)
  --stt gpu|cpu     where speech-to-text runs (default: GPU when one works)
  --token yes|no    protect dashboard settings with a token (default: yes)
  --tag vX.Y.Z      release to install (default: the newest one)
  --source URL|DIR  where to get the code (default: this clone)
  --dry-run         print every command instead of running it
  --force           try an untested system anyway
  -h, --help        this help
EOF
}

while (( $# )); do
  case $1 in
    --yes|-y) LVI_YES=1 ;;
    --dry-run) LVI_DRY_RUN=1 ;;
    --force) LVI_FORCE=1 ;;
    --host) HOST_IP=${2:?--host needs an address}; shift ;;
    --stt) STT_PROFILE=${2:?--stt needs gpu or cpu}; shift ;;
    --token) MAKE_TOKEN=${2:?--token needs yes or no}; shift ;;
    --tag) LVI_TAG=${2:?--tag needs a tag}; shift ;;
    --source) LVI_SOURCE=${2:?--source needs a URL or folder}; shift ;;
    --prefix) LVI_PREFIX=${2:?}; shift ;;   # tests only: root every path here
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
export LVI_YES LVI_DRY_RUN LVI_FORCE LVI_PREFIX

# shellcheck source=installer/banner.sh
source "$LVI_SCRIPT_DIR/installer/banner.sh"
# shellcheck source=installer/lib.sh
source "$LVI_SCRIPT_DIR/installer/lib.sh"
# shellcheck source=installer/preflight.sh
source "$LVI_SCRIPT_DIR/installer/preflight.sh"
# shellcheck source=installer/deps.sh
source "$LVI_SCRIPT_DIR/installer/deps.sh"
# shellcheck source=installer/app.sh
source "$LVI_SCRIPT_DIR/installer/app.sh"
# shellcheck source=installer/state.sh
source "$LVI_SCRIPT_DIR/installer/state.sh"
# shellcheck source=installer/service.sh
source "$LVI_SCRIPT_DIR/installer/service.sh"

case ${MAKE_TOKEN:-} in ''|yes|no) ;; *) die "--token must be yes or no" ;; esac

if [[ $LVI_DRY_RUN != 1 && $EUID -ne 0 ]]; then
  die "run it with sudo: sudo bash scripts/install.sh   (or --dry-run to only print the steps)"
fi

resolve_source
LVI_VERSION=${LVI_TAG#v}
banner
[[ $LVI_DRY_RUN == 1 ]] && note "dry run: nothing below is executed"

pick_ui
preflight
install_deps
ask_questions
install_app
write_state
LVI_UNIT_TEMPLATE=$LVI_APP/deploy/lenoxvi.service.in
[[ -f $LVI_UNIT_TEMPLATE ]] || LVI_UNIT_TEMPLATE=$LVI_SCRIPT_DIR/../deploy/lenoxvi.service.in
install_service
rc=0
wait_healthy "$HOST_IP" 300 || rc=$?
(( rc == 1 )) && die "LenoxVI didn't come up — see the log above. Re-running this script is safe."
handoff
