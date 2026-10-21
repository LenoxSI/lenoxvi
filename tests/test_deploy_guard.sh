#!/bin/bash
# test_deploy_guard.sh — prove deploy_firmware.sh's preflight (branch ↔ track,
# clean tree, libpeer stack) refuses every cross-flash shape and passes the
# right ones. Runs against a THROWAWAY clone of the firmware repo with
# DEPLOY_PREFLIGHT_ONLY=1: no credentials, no build, no publish, and the real
# checkout is never modified.
#
#   ./tests/test_deploy_guard.sh          # exits non-zero on any mismatch
set -uo pipefail

LVI_LOCAL="${LVI_LOCAL:-$HOME/projects/lenoxvi}"
FW_REAL="${FW_REAL:-$HOME/projects/lenoxvi-firmware}"
DEPLOY="$LVI_LOCAL/scripts/deploy_firmware.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/deploy-guard.XXXXXX")
trap 'rm -rf "$T"' EXIT

git clone -q --no-hardlinks "$FW_REAL" "$T/fw"
git -C "$T/fw" checkout -q main
git -C "$T/fw" branch -q some-other-branch   # stands in for an old per-line / feature branch
# libpeer is gitignored: give the clone a copy of the real (verified) tree.
mkdir -p "$T/fw/deps" && cp -a "$FW_REAL/deps/libpeer" "$T/fw/deps/libpeer"

fail=0
run() {  # run <expect PASS|REFUSE> <label> <track> [repo]
  local expect="$1" label="$2" track="$3" repo="${4:-$T/fw}" out rc got
  out=$(FW_REPO="$repo" DEPLOY_PREFLIGHT_ONLY=1 bash "$DEPLOY" --track "$track" 2>&1); rc=$?
  if [ $rc -eq 0 ] && grep -q "preflight OK" <<<"$out"; then got=PASS; else got=REFUSE; fi
  if [ "$got" = "$expect" ]; then
    printf '[PASS] %-58s -> %s\n' "$label" "$got"
  else
    printf '[FAIL] %-58s -> %s (expected %s)\n' "$label" "$got" "$expect"
    sed 's/^/         /' <<<"$out" | tail -4
    fail=1
  fi
}

# Branch fold (2026-10-16): both lines build from `main`; the line is -DBOARD.
git -C "$T/fw" checkout -q main
run PASS   "array track, checkout on main"                        array
run PASS   "flex track, checkout on main"                         flex
run PASS   "waveshare track, checkout on main"                    waveshare

git -C "$T/fw" checkout -q some-other-branch
run REFUSE "array track, checkout on another branch"              array
run REFUSE "flex track, checkout on another branch"               flex
run REFUSE "waveshare track, checkout on another branch"          waveshare
git -C "$T/fw" checkout -q main

# Old track name and missing --track: refused, never defaulted to a line.
run REFUSE "retired track name 'node' (renamed array)"           node
run REFUSE "unknown track name 'wave' (no fuzzy match)"         wave
out=$(FW_REPO="$T/fw" DEPLOY_PREFLIGHT_ONLY=1 bash "$DEPLOY" 2>&1); rc=$?
if [ $rc -ne 0 ] && grep -q -- "--track array|flex|waveshare is required" <<<"$out"; then
  printf '[PASS] %-58s -> %s\n' "no --track at all (no default line)" REFUSE
else
  printf '[FAIL] %-58s -> rc=%s (expected REFUSE)\n' "no --track at all (no default line)" "$rc"; fail=1
fi

git -C "$T/fw" checkout -q --detach HEAD
run REFUSE "flex track, detached HEAD"                          flex
git -C "$T/fw" checkout -q main

echo "// test edit" >> "$T/fw/src/ota.cpp"
run REFUSE "flex track, uncommitted change to a tracked file"   flex
git -C "$T/fw" checkout -q -- src/ota.cpp

touch "$T/fw/untracked-scratch.txt"
run PASS   "flex track, untracked file only (build env, allowed)" flex
rm -f "$T/fw/untracked-scratch.txt"

echo "/* hand edit */" >> "$T/fw/deps/libpeer/src/sctp.c"
run REFUSE "flex track, hand-edited deps/libpeer"               flex

# The real checkout, read-only: on main both tracks pass; anywhere else neither.
# (Skipped while it has uncommitted tracked changes — that's the dirty-tree
# refusal, already covered above.)
REAL_BRANCH=$(git -C "$FW_REAL" symbolic-ref --quiet --short HEAD 2>/dev/null || echo detached)
if [ -n "$(git -C "$FW_REAL" status --porcelain --untracked-files=no)" ]; then
  echo "[SKIP] REAL checkout has uncommitted changes — not exercised"
elif [ "$REAL_BRANCH" = "main" ]; then
  run PASS "REAL checkout (main): array track" array "$FW_REAL"
  run PASS "REAL checkout (main): flex track"  flex  "$FW_REAL"
  run PASS "REAL checkout (main): waveshare track" waveshare "$FW_REAL"
else
  run REFUSE "REAL checkout ($REAL_BRANCH): array track" array "$FW_REAL"
  run REFUSE "REAL checkout ($REAL_BRANCH): flex track"  flex  "$FW_REAL"
  run REFUSE "REAL checkout ($REAL_BRANCH): waveshare track" waveshare "$FW_REAL"
fi

# Static: the script never restarts the bot. firmware_server reads the track dir
# per request, and a restart drops every room; nodes pick up a staged image on
# their own next boot (restart just that node from the dashboard).
if grep -vE '^[[:space:]]*#' "$DEPLOY" | grep -qE 'restart\.sh|systemctl[^#]*restart'; then
  echo "[FAIL] static: deploy_firmware.sh restarts the bot"; fail=1
else
  echo "[PASS] static: deploy_firmware.sh never restarts the bot"
fi

[ $fail -eq 0 ] && echo "ALL GUARD CASES PASSED" || echo "GUARD TEST FAILED"
exit $fail
