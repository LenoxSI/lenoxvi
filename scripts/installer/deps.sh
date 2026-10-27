# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — dependencies. apt installs only what's missing (asked
# once); gum and uv are pinned downloads checked against their sha256.

APT_PACKAGES=(ca-certificates curl git openssl qrencode iproute2 whiptail)

GUM_VERSION=2.0.2
GUM_SHA256_x86_64=d842e06d93dbed90af48cb8dd10698db6f22e331fc40346bb37bbc753109edc2
GUM_SHA256_aarch64=8ebf8b54ec1e8c81f2bb58b59ff9b70998186a4d11375f0cf357b80e0ccfa1d5
UV_VERSION=0.13.6
UV_SHA256_x86_64=f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645
UV_SHA256_aarch64=229e194255c7958d86d56e8790d969a6f74736592133f0dfd0076f754c642c1d

GUM_URL_BASE=${GUM_URL_BASE:-https://github.com/charmbracelet/gum/releases/download}
UV_URL_BASE=${UV_URL_BASE:-https://github.com/astral-sh/uv/releases/download}

# apt_missing <pkg>... -> echoes the ones not installed
apt_missing() {
  local p
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || printf '%s\n' "$p"
  done
}

install_apt() {
  local missing
  mapfile -t missing < <(apt_missing "${APT_PACKAGES[@]}")
  if (( ${#missing[@]} == 0 )); then
    ok "system packages: all present"
    return 0
  fi
  ask_yes "Install these system packages with apt: ${missing[*]}?" y \
    || die "LenoxVI needs: ${missing[*]}"
  run_logged env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq \
    || die "apt-get update failed (see above)"
  run_logged env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq --no-install-recommends "${missing[@]}" \
    || die "apt-get install failed (see above)"
  ok "system packages: installed ${missing[*]}"
}

# fetch_pinned <url> <sha256> <dest> — download, check, then move into place.
fetch_pinned() {
  local url=$1 sum=$2 dest=$3 tmp
  if [[ $LVI_DRY_RUN == 1 ]]; then
    printf '+ fetch %s -> %s (sha256 %s)\n' "$url" "$dest" "${sum:0:12}"
    return 0
  fi
  tmp=$(mktemp "${dest}.XXXXXX")
  if ! curl -fsSL --retry 2 --connect-timeout 15 -o "$tmp" "$url"; then
    rm -f "$tmp"; return 1
  fi
  if [[ $(sha256sum "$tmp" | cut -d' ' -f1) != "$sum" ]]; then
    rm -f "$tmp"
    warn "checksum mismatch for $url — file discarded"
    return 1
  fi
  mv -f "$tmp" "$dest"
}

_triple_sum() { local v="$1_SHA256_$ARCH"; printf '%s' "${!v}"; }

# _install_tar <name> <url> <sha256> <member path in the tarball>
_install_tar() {
  local name=$1 url=$2 sum=$3 member=$4 tar
  run mkdir -p "$LVI_BIN"
  tar="$LVI_BIN/.$name.tar.gz"
  fetch_pinned "$url" "$sum" "$tar" || return 1
  run tar -xzf "$tar" -C "$LVI_BIN" --strip-components=1 "$member"
  run rm -f "$tar"
}

install_gum() {
  if [[ -x $LVI_BIN/gum ]] && "$LVI_BIN/gum" --version 2>/dev/null | grep -q "$GUM_VERSION"; then
    return 0
  fi
  local garch=$ARCH
  [[ $garch == aarch64 ]] && garch=arm64
  local base="gum_${GUM_VERSION}_Linux_${garch}"
  if _install_tar gum "$GUM_URL_BASE/v$GUM_VERSION/$base.tar.gz" "$(_triple_sum GUM)" "$base/gum"; then
    UI=
    pick_ui
  else
    note "couldn't download gum (the menu tool) — using simple prompts instead"
  fi
}

install_uv() {
  if [[ -x $LVI_BIN/uv ]] && "$LVI_BIN/uv" --version 2>/dev/null | grep -q "$UV_VERSION"; then
    ok "uv $UV_VERSION: present"
    return 0
  fi
  local base="uv-${ARCH}-unknown-linux-gnu"
  _install_tar uv "$UV_URL_BASE/$UV_VERSION/$base.tar.gz" "$(_triple_sum UV)" "$base/uv" \
    || die "couldn't download uv $UV_VERSION (needed to install Python packages). Check the internet connection and run this again."
  ok "uv $UV_VERSION: installed"
}

install_deps() {
  step "Installing what LenoxVI needs"
  install_apt
  install_gum
  install_uv
}
