# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — preflight. Reads only; changes nothing.
# Sets: OS_ID OS_VERSION OS_NAME ARCH PY_BIN PY_OK GPU GPU_NAME RAM_MB DISK_MB EXISTING

LVI_FORCE=${LVI_FORCE:-0}

# Supported: Ubuntu 22.04/24.04/26.04, Debian 12/13 (Raspberry Pi OS 64-bit reports debian).
os_supported() {
  case "$1:$2" in
    ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) return 0 ;;
    *) return 1 ;;
  esac
}

# _osr <file> <KEY> -> the value, quotes stripped (read, never sourced)
_osr() { sed -n "s/^$2=//p" "$1" | head -1 | tr -d '"'"'"; }

check_os() {
  local f=$LVI_PREFIX/etc/os-release
  [[ -r $f ]] || die "can't read /etc/os-release — unknown system"
  OS_ID=$(_osr "$f" ID)
  OS_VERSION=$(_osr "$f" VERSION_ID)
  OS_NAME=$(_osr "$f" PRETTY_NAME)
  [[ -n $OS_NAME ]] || OS_NAME="$OS_ID $OS_VERSION"
  ARCH=$(uname -m)
  case $ARCH in
    x86_64|aarch64) ;;
    armv6l|armv7l|armhf) die "this is a 32-bit system ($ARCH). Install the 64-bit Raspberry Pi OS (or another 64-bit OS) and run this again." ;;
    *) die "unsupported CPU architecture: $ARCH (x86_64 or aarch64 only)" ;;
  esac
  if [[ $OS_ID == raspbian ]]; then
    die "this is 32-bit Raspberry Pi OS. Install the 64-bit Raspberry Pi OS and run this again."
  fi
  if os_supported "$OS_ID" "$OS_VERSION"; then
    ok "system: $OS_NAME ($ARCH)"
  elif [[ $LVI_FORCE == 1 ]]; then
    warn "$OS_NAME is not tested — continuing because of --force"
  else
    die "$OS_NAME is not supported (Ubuntu 22.04/24.04/26.04, Debian 12/13, Raspberry Pi OS 64-bit). --force tries anyway."
  fi
}

# Python 3.12+ from the system, else uv fetches one later.
check_python() {
  PY_BIN=$(command -v python3 || true)
  PY_OK=no
  if [[ -n $PY_BIN ]] && "$PY_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY_OK=yes
    ok "python: $("$PY_BIN" -c 'import platform; print(platform.python_version())')"
  else
    ok "python: system one is older than 3.12 — a private 3.12 will be installed for LenoxVI"
  fi
}

check_gpu() {
  GPU=no
  GPU_NAME=
  if command -v nvidia-smi >/dev/null 2>&1 && GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1) && [[ -n $GPU_NAME ]]; then
    GPU=yes
    ok "gpu: $GPU_NAME"
  elif command -v lspci >/dev/null 2>&1 && lspci 2>/dev/null | grep -qi 'nvidia'; then
    note "an NVIDIA card is present but its driver isn't working, so speech-to-text runs on the CPU. After installing the driver, run: lenoxvi reconfigure"
  else
    ok "gpu: none (speech-to-text runs on the CPU)"
  fi
}

check_resources() {
  RAM_MB=$(free -m | awk '/^Mem:/ {print $2}')
  local dir=$LVI_PREFIX/opt
  [[ -d $dir ]] || dir=$LVI_PREFIX/
  DISK_MB=$(df -Pm "$dir" | awk 'NR==2 {print $4}')
  ok "memory: ${RAM_MB} MB, free disk: $((DISK_MB / 1024)) GB"
  (( RAM_MB >= 1800 )) || warn "under 2 GB of memory — LenoxVI needs about 1.2 GB on its own"
  (( DISK_MB >= 4000 )) || die "not enough free disk: $((DISK_MB / 1024)) GB (needs about 4 GB, 8 GB with a GPU)"
}

# Ports 7860 (dashboard) and 8765 (speaker clips). Ours when the service runs.
check_ports() {
  if [[ $EXISTING == yes ]] && systemctl is-active --quiet lenoxvi 2>/dev/null; then
    ok "ports: in use by LenoxVI itself"
    return 0
  fi
  local busy
  busy=$(ss -ltnpH 2>/dev/null | awk '$4 ~ /:(7860|8765)$/ {print $4 " " $6}')
  if [[ -n $busy ]]; then
    die "port 7860 or 8765 is already in use:
$busy
Stop that program (an older LenoxVI started by hand?) and run this again."
  fi
  ok "ports: 7860 and 8765 are free"
}

check_existing() {
  EXISTING=no
  if [[ -d $LVI_APP/.git ]]; then
    EXISTING=yes
    ok "already installed ($(git -C "$LVI_APP" describe --tags 2>/dev/null || echo unknown)) — this run only fills gaps; your config and keys are kept. Upgrades: lenoxvi upgrade"
  fi
}

preflight() {
  step "Checking this machine"
  check_os
  check_existing
  check_python
  check_gpu
  check_resources
  check_ports
}
