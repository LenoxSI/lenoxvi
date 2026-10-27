#!/usr/bin/env bash
# LenoxVI installer banner + theme. Source this from install.sh.
# Brand: open = Mist, holler = Lime, secondary text = Muted.

# ── Colour support: truecolor > 256 > none (respects NO_COLOR) ────────────
if [[ -n "${NO_COLOR:-}" || ! -t 1 ]]; then
  LIME="" MIST="" MUTED="" RESET=""
elif [[ "${COLORTERM:-}" =~ ^(truecolor|24bit)$ ]]; then
  LIME=$'\e[1;38;2;182;242;58m'    # #B6F23A
  MIST=$'\e[1;38;2;238;243;234m'   # #EEF3EA
  MUTED=$'\e[38;2;151;162;144m'    # #97A290
  RESET=$'\e[0m'
else
  LIME=$'\e[1;38;5;155m' MIST=$'\e[1;38;5;255m' MUTED=$'\e[38;5;247m' RESET=$'\e[0m'
fi

# ── Gum theme (hex is auto-downgraded by gum on limited terminals) ────────
export GUM_INPUT_CURSOR_FOREGROUND="#B6F23A"
export GUM_INPUT_PROMPT_FOREGROUND="#B6F23A"
export GUM_INPUT_PLACEHOLDER_FOREGROUND="#97A290"
export GUM_CHOOSE_CURSOR_FOREGROUND="#B6F23A"
export GUM_CHOOSE_SELECTED_FOREGROUND="#B6F23A"
export GUM_CHOOSE_HEADER_FOREGROUND="#EEF3EA"
export GUM_CONFIRM_SELECTED_BACKGROUND="#B6F23A"
export GUM_CONFIRM_SELECTED_FOREGROUND="#0F120E"
export GUM_CONFIRM_PROMPT_FOREGROUND="#EEF3EA"
export GUM_SPIN_SPINNER_FOREGROUND="#B6F23A"

# ── whiptail fallback (newt only knows named colours) ─────────────────────
export NEWT_COLORS='root=,black window=,black border=green,black title=brightgreen,black
textbox=white,black button=black,brightgreen actbutton=black,green
entry=white,black checkbox=white,black actcheckbox=black,brightgreen'

banner() {
  local cols; cols=${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}
  echo
  if (( cols >= 86 )); then
    # ANSI Shadow — 84 cols
    printf '%s%s%s\n'  "$MIST" ' ██████╗ ██████╗ ███████╗███╗   ██╗' "$LIME██╗  ██╗ ██████╗ ██╗     ██╗     ███████╗██████╗ $RESET"
    printf '%s%s%s\n'  "$MIST" '██╔═══██╗██╔══██╗██╔════╝████╗  ██║' "$LIME██║  ██║██╔═══██╗██║     ██║     ██╔════╝██╔══██╗$RESET"
    printf '%s%s%s\n'  "$MIST" '██║   ██║██████╔╝█████╗  ██╔██╗ ██║' "$LIME███████║██║   ██║██║     ██║     █████╗  ██████╔╝$RESET"
    printf '%s%s%s\n'  "$MIST" '██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║' "$LIME██╔══██║██║   ██║██║     ██║     ██╔══╝  ██╔══██╗$RESET"
    printf '%s%s%s\n'  "$MIST" '╚██████╔╝██║     ███████╗██║ ╚████║' "$LIME██║  ██║╚██████╔╝███████╗███████╗███████╗██║  ██║$RESET"
    printf '%s%s%s\n'  "$MIST" ' ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝' "$LIME╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝$RESET"
  elif (( cols >= 44 )); then
    # Small — 42 cols, safe on SSH/serial consoles
    printf '%s%s%s%s\n' "$MIST" '                    ' "$LIME" " _        _ _         $RESET"
    printf '%s%s%s%s\n' "$MIST" ' ___ _ __  ___ _ _  ' "$LIME" "| |_  ___| | |___ _ _ $RESET"
    printf '%s%s%s%s\n' "$MIST" "/ _ \\ '_ \\/ -_) ' \\ " "$LIME" "| ' \\/ _ \\ | / -_) '_|$RESET"
    printf '%s%s%s%s\n' "$MIST" '\___/ .__/\___|_||_|' "$LIME" "|_||_\\___/_|_\\___|_|  $RESET"
    printf '%s%s%s\n'   "$MIST" '    |_|' "$RESET"
  else
    printf '%sopen%sholler%s\n' "$MIST" "$LIME" "$RESET"
  fi
  if (( cols >= 44 )); then
    printf '%s  open-source voice for AI agents · installer v%s%s\n\n' "$MUTED" "${LVI_VERSION:-dev}" "$RESET"
  else
    printf '%sinstaller v%s%s\n\n' "$MUTED" "${LVI_VERSION:-dev}" "$RESET"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then banner; fi
