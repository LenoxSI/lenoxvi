#!/bin/bash
# test_deploy_guard.sh — prove deploy_firmware.sh's preflight (branch ↔ track,
# clean tree, libpeer stack) refuses every cross-flash shape and passes the
# right ones. Runs against a THROWAWAY clone of the firmware repo with
# DEPLOY_PREFLIGHT_ONLY=1: no credentials, no build, no publish, and the real
# checkout is never modified.
#
#   ./scripts/test_deploy_guard.sh        # exits non-zero on any mismatch
set -uo pipefail

LVI_LOCAL="${LVI_LOCAL:-$HOME/projects/lenoxvi}"
FW_REAL="${FW_REAL:-$HOME/projects/lenoxvi-firmware}"
DEPLOY="$LVI_LOCAL/scripts/deploy_firmware.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/deploy-guard.XXXXXX")
trap 'rm -rf "$T"' EXIT

git clone -q --no-hardlinks "$FW_REAL" "$T/fw"
git -C "$T/fw" checkout -q array-xvf3800
git -C "$T/fw" checkout -q flex-xvf3800
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

git -C "$T/fw" checkout -q array-xvf3800
run PASS   "array track, checkout on array-xvf3800"           array
run REFUSE "flex track, checkout on array-xvf3800 (cross-flash)" flex

git -C "$T/fw" checkout -q flex-xvf3800
run PASS   "flex track, checkout on flex-xvf3800"               flex
run REFUSE "array track, checkout on flex-xvf3800 (cross-flash)" array

# Old track name and missing --track: refused, never defaulted to a line.
git -C "$T/fw" checkout -q array-xvf3800
run REFUSE "retired track name 'node' (renamed array)"           node
out=$(FW_REPO="$T/fw" DEPLOY_PREFLIGHT_ONLY=1 bash "$DEPLOY" 2>&1); rc=$?
if [ $rc -ne 0 ] && grep -q -- "--track array|flex is required" <<<"$out"; then
  printf '[PASS] %-58s -> %s\n' "no --track at all (no default line)" REFUSE
else
  printf '[FAIL] %-58s -> rc=%s (expected REFUSE)\n' "no --track at all (no default line)" "$rc"; fail=1
fi
git -C "$T/fw" checkout -q flex-xvf3800

git -C "$T/fw" checkout -q --detach HEAD
run REFUSE "flex track, detached HEAD"                          flex
git -C "$T/fw" checkout -q flex-xvf3800

echo "// test edit" >> "$T/fw/src/ota.cpp"
run REFUSE "flex track, uncommitted change to a tracked file"   flex
git -C "$T/fw" checkout -q -- src/ota.cpp

touch "$T/fw/untracked-scratch.txt"
run PASS   "flex track, untracked file only (build env, allowed)" flex
rm -f "$T/fw/untracked-scratch.txt"

echo "/* hand edit */" >> "$T/fw/deps/libpeer/src/sctp.c"
run REFUSE "flex track, hand-edited deps/libpeer"               flex

# The real checkout, read-only (the fetch script is idempotent on a verified
# tree): whatever branch it is on, exactly one track may pass.
REAL_BRANCH=$(git -C "$FW_REAL" symbolic-ref --quiet --short HEAD 2>/dev/null || echo detached)
case "$REAL_BRANCH" in
  array-xvf3800) run PASS "REAL checkout ($REAL_BRANCH): array track" array "$FW_REAL"
                run REFUSE "REAL checkout ($REAL_BRANCH): flex track" flex "$FW_REAL" ;;
  flex-xvf3800) run REFUSE "REAL checkout ($REAL_BRANCH): array track" array "$FW_REAL"
                run PASS "REAL checkout ($REAL_BRANCH): flex track" flex "$FW_REAL" ;;
  *)            run REFUSE "REAL checkout ($REAL_BRANCH): array track" array "$FW_REAL"
                run REFUSE "REAL checkout ($REAL_BRANCH): flex track" flex "$FW_REAL" ;;
esac

[ $fail -eq 0 ] && echo "ALL GUARD CASES PASSED" || echo "GUARD TEST FAILED"
exit $fail
