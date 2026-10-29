# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — shared helpers. Sourced by install.sh and the
# lenoxvi command; never run on its own.
#
# Every command that changes the system goes through `run`, so --dry-run
# (LVI_DRY_RUN=1) prints the exact command list instead of doing anything.
# LVI_PREFIX roots every path in a scratch folder (tests); the service unit
# still names the real paths.

LVI_DRY_RUN=${LVI_DRY_RUN:-0}
LVI_YES=${LVI_YES:-0}
LVI_PREFIX=${LVI_PREFIX:-}

# Real paths (what the unit and the config name) ...
LVI_OPT_REAL=/opt/lenoxvi
LVI_STATE_REAL=/var/lib/lenoxvi
LVI_USER=${LVI_USER:-lenoxvi}   # tests run as themselves
# ... and where this run reads and writes them.
LVI_OPT=$LVI_PREFIX$LVI_OPT_REAL
LVI_APP=$LVI_OPT/app
LVI_VENV=$LVI_OPT/venv
LVI_BIN=$LVI_OPT/bin
LVI_STATE=$LVI_PREFIX$LVI_STATE_REAL
LVI_UNIT=$LVI_PREFIX/etc/systemd/system/lenoxvi.service
LVI_CLI=$LVI_PREFIX/usr/local/bin/lenoxvi
LVI_CACHE=$LVI_PREFIX/var/cache/lenoxvi
LVI_LOG=$LVI_PREFIX/var/log/lenoxvi-install.log

: "${LIME:=}" "${MIST:=}" "${MUTED:=}" "${RESET:=}"

run() {
  if [[ $LVI_DRY_RUN == 1 ]]; then
    local out='+'
    local a
    for a in "$@"; do
      if [[ $a =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then out+=" $a"; else out+=" $(printf '%q' "$a")"; fi
    done
    printf '%s\n' "$out"
  else
    "$@"
  fi
}

# run_logged — run with its output appended to the install log; on failure
# the last lines are shown, so a broken step still explains itself.
run_logged() {
  if [[ $LVI_DRY_RUN == 1 ]]; then run "$@"; return 0; fi
  printf '\n$ %s\n' "$*" >>"$LVI_LOG"
  if ! "$@" >>"$LVI_LOG" 2>&1; then
    printf '  last lines of %s:\n' "$LVI_LOG" >&2
    tail -n 15 "$LVI_LOG" | sed 's/^/    /' >&2
    return 1
  fi
}

# run_quiet — run with its output hidden (dry-run still prints the command).
run_quiet() {
  if [[ $LVI_DRY_RUN == 1 ]]; then run "$@"; else "$@" >/dev/null; fi
}

# write_file <dest> <mode> <owner> — content on stdin. Temp file + rename, so a
# Ctrl-C never leaves half a file. Dry-run prints one line instead.
write_file() {
  local dest=$1 mode=$2 owner=$3
  if [[ $LVI_DRY_RUN == 1 ]]; then
    cat >/dev/null
    printf '+ write %s (%s %s)\n' "$dest" "$mode" "$owner"
    return 0
  fi
  local tmp
  tmp=$(mktemp "${dest}.XXXXXX")
  cat >"$tmp"
  chmod "$mode" "$tmp"
  [[ $owner == - ]] || chown "$owner" "$tmp"
  mv -f "$tmp" "$dest"
}

step() { printf '\n%s▸ %s%s\n' "$LIME" "$1" "$RESET"; }
ok()   { printf '  %s\n' "$1"; }
note() { printf '  %snote:%s %s\n' "$MUTED" "$RESET" "$1"; }
warn() { printf '  warning: %s\n' "$1" >&2; }
die()  { printf '\n  error: %s\n' "$1" >&2; exit 1; }

_has_tty() { [[ -r /dev/tty && -w /dev/tty ]] && { : </dev/tty; } 2>/dev/null; }

# UI: gum (themed), whiptail, plain read; "none" = answer with defaults.
pick_ui() {
  if [[ $LVI_YES == 1 ]]; then UI=none
  elif ! _has_tty; then UI=notty
  elif command -v gum >/dev/null 2>&1 || [[ -x $LVI_BIN/gum ]]; then UI=gum
  elif command -v whiptail >/dev/null 2>&1; then UI=whiptail
  else UI=plain
  fi
}

_gum() { if command -v gum >/dev/null 2>&1; then gum "$@"; else "$LVI_BIN/gum" "$@"; fi; }

_no_answer() { die "no terminal to ask \"$1\" — re-run with --yes (and --host) to use the defaults"; }

# ask_yes "<question>" y|n -> exit status 0 = yes
ask_yes() {
  local q=$1 def=$2 ans
  [[ -n ${UI:-} ]] || pick_ui
  case $UI in
    none) [[ $def == y ]] ;;
    notty) _no_answer "$q" ;;
    gum) if [[ $def == y ]]; then _gum confirm "$q" </dev/tty; else _gum confirm --default=false "$q" </dev/tty; fi ;;
    whiptail) if [[ $def == y ]]; then whiptail --yesno "$q" 10 70 </dev/tty >/dev/tty; else whiptail --defaultno --yesno "$q" 10 70 </dev/tty >/dev/tty; fi ;;
    *) local hint='[Y/n]'; [[ $def == y ]] || hint='[y/N]'
       read -r -p "  $q $hint " ans </dev/tty
       ans=${ans:-$def}; [[ $ans == [Yy]* ]] ;;
  esac
}

# ask_input "<question>" "<default>" -> echoes the answer
ask_input() {
  local q=$1 def=$2 ans
  [[ -n ${UI:-} ]] || pick_ui
  case $UI in
    none) printf '%s\n' "$def" ;;
    notty) _no_answer "$q" ;;
    gum) _gum input --header "$q" --value "$def" </dev/tty ;;
    whiptail) whiptail --inputbox "$q" 10 70 "$def" 3>&1 1>&2 2>&3 </dev/tty ;;
    *) read -r -p "  $q [$def] " ans </dev/tty; printf '%s\n' "${ans:-$def}" ;;
  esac
}

# Newest release tag (vX.Y.Z, no pre-release suffix) from a tag list on stdin.
newest_tag() { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1; }
