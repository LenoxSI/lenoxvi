#!/bin/bash
# Sync the appliance host's checkout with this repo (run from the dev box).
#
# House rule: no GitHub credentials on hosts — the host can't pull.
# So we ship the repo as a git bundle over SSH instead. After this,
# GitHub == VM == host, byte for byte.
#
# SAFETY: the host reset is --hard. The host is a PURE DEPLOY TARGET:
# never edit files on the appliance host directly — edit here, commit, push,
# then run this. (Untracked+gitignored files like .env, logs/, clips/
# are NOT touched by reset --hard.)
#
# Usage: bash scripts/sync_host.sh
set -euo pipefail

HOST=lenoxvi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE=/tmp/lenoxvi.bundle

git -C "$DIR" bundle create "$BUNDLE" main --tags
scp -q "$BUNDLE" "$HOST:/tmp/lenoxvi.bundle"
# Gitignored runtime files travel alongside the bundle — the host's
# config.yaml/AGENTS.md/CLAUDE.md are NOT in git anymore (2026-09-20).
#
# CONFIG AUTHORITY (amended 2026-09-30, design note follow-up): the host's
# config.yaml is the LIVE one — the dashboard edits it at runtime. The
# VM copy is a bootstrap/editing convenience ONLY. So: PULL host ->
# VM first, then ship everything EXCEPT config.yaml. Never push config.
# (The old push-every-sync clobbered dashboard-written room_zones
# twice on 2026-09-29/30 before this fix.)
scp -q "$HOST:~/projects/lenoxvi/config.yaml" "$DIR/config.yaml"
scp -q "$DIR/AGENTS.md" "$DIR/CLAUDE.md" "$HOST:/tmp/" 2>/dev/null || true
ssh -o BatchMode=yes "$HOST" 'cd ~/projects/lenoxvi && \
  git fetch /tmp/lenoxvi.bundle +refs/heads/main:refs/remotes/origin/main "+refs/tags/*:refs/tags/*" && \
  git reset --hard refs/remotes/origin/main && \
  mv /tmp/AGENTS.md /tmp/CLAUDE.md . 2>/dev/null; \
  echo "--- host now at:" && git log --oneline -1 && \
  git status --short | head -5 && echo "--- status clean if nothing listed above"'
rm -f "$BUNDLE"