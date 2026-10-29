# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — the app: service user, state folder, code at a
# release tag, the Python environment, and the speech models.

# Source of the code: --source, else the clone this script runs from.
# Upgrades fetch from that clone's origin (or from --source again).
resolve_source() {
  if [[ -z ${LVI_SOURCE:-} ]]; then
    LVI_SOURCE=$(git -C "$LVI_SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null) \
      || die "run this from a clone of the LenoxVI repo, or pass --source <git url or path>"
  fi
  if [[ -z ${LVI_TAG:-} ]]; then
    if [[ -d $LVI_SOURCE/.git || -d $LVI_SOURCE/refs ]]; then
      LVI_TAG=$(git -C "$LVI_SOURCE" tag -l 'v*' | newest_tag)
    else
      LVI_TAG=$(git ls-remote --tags --refs "$LVI_SOURCE" 'v*' | sed 's#.*refs/tags/##' | newest_tag)
    fi
    [[ -n $LVI_TAG ]] || die "no release tag (vX.Y.Z) found in $LVI_SOURCE — pass --tag"
  fi
  LVI_ORIGIN=$(git -C "$LVI_SOURCE" remote get-url origin 2>/dev/null || printf '%s' "$LVI_SOURCE")
}

ensure_user() {
  if id -u "$LVI_USER" >/dev/null 2>&1; then
    ok "service user: $LVI_USER (exists)"
  else
    run useradd --system --home-dir "$LVI_STATE_REAL" --no-create-home --shell /usr/sbin/nologin "$LVI_USER"
    ok "service user: $LVI_USER (created)"
  fi
  return 0
}

ensure_state_dir() {
  if [[ -d $LVI_STATE ]]; then
    ok "data folder: $LVI_STATE_REAL (kept)"
  else
    run install -d -m 0750 -o "$LVI_USER" -g "$LVI_USER" "$LVI_STATE"
    ok "data folder: $LVI_STATE_REAL"
  fi
}

install_code() {
  run install -d -m 0755 "$LVI_OPT"
  if [[ -d $LVI_APP/.git ]]; then
    ok "code: $(git -C "$LVI_APP" describe --tags 2>/dev/null || echo unknown) (kept — upgrades: lenoxvi upgrade)"
    trust_app_repo
    return 0
  fi
  local from=$LVI_SOURCE
  [[ -d $from ]] && from="file://$(cd "$from" && pwd)"   # --depth only works over a URL
  run git -c advice.detachedHead=false -c init.defaultBranch=main clone --quiet --depth 1 --branch "$LVI_TAG" "$from" "$LVI_APP"
  run git -C "$LVI_APP" remote set-url origin "$LVI_ORIGIN"
  trust_app_repo
  ok "code: $LVI_TAG"
}

# The code is root's; the bot (and `lenoxvi status` without sudo) read its
# version with git, which refuses a repo owned by someone else unless it's
# listed as safe. One system-wide entry for exactly this folder.
trust_app_repo() {
  git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$LVI_APP_REAL" \
    || run git config --system --add safe.directory "$LVI_APP_REAL"
}

# uv env shared by install and upgrade: Python and cache outside any home,
# bytecode compiled up front (the service can't write to /opt).
_uv() {
  run env UV_PYTHON_INSTALL_DIR="$LVI_OPT/python" UV_CACHE_DIR="$LVI_CACHE/uv" UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 "$LVI_BIN/uv" "$@"
}

install_venv() {
  if [[ ! -x $LVI_VENV/bin/python ]]; then
    if [[ $PY_OK == yes ]]; then
      _uv venv --quiet -p "$PY_BIN" "$LVI_VENV"
    else
      _uv python install --quiet 3.12
      _uv venv --quiet -p 3.12 --python-preference only-managed "$LVI_VENV"
    fi
  fi
  pip_sync
  ok "python packages: installed"
}

# pip_sync — install the app's requirements into the venv (a no-op when met).
pip_sync() {
  _uv pip install --quiet --python "$LVI_VENV/bin/python" -r "$LVI_APP/requirements.txt"
}

# Speech models up front, so the first start doesn't sit downloading: the
# Embedded pair, Kokoro (the voice) and Whisper base (speech-to-text). They
# land in the service user's home (= the data folder), where the bot looks.
_as_user() {
  run_logged runuser -u "$LVI_USER" -- env HOME="$LVI_STATE_REAL" LVI_STATE_DIR="$LVI_STATE_REAL" "$@"
}

fetch_models() {
  _as_user bash "$LVI_APP_REAL/scripts/fetch_kokoro_models.sh" || die "couldn't download the Kokoro voice model (see above)"
  ok "voice model (Kokoro): ready"
  _as_user "$LVI_VENV_REAL/bin/python" -c \
    "import faster_whisper as f; f.download_model('base')" \
    || warn "couldn't download the Whisper base speech model now — the bot will fetch it on first start"
  ok "speech model (Whisper base): ready"
}

LVI_APP_REAL=$LVI_OPT_REAL/app
LVI_VENV_REAL=$LVI_OPT_REAL/venv

install_app() {
  step "Installing LenoxVI $LVI_TAG"
  ensure_user
  ensure_state_dir
  install_code
  install_venv
  fetch_models
}
