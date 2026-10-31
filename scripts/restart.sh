#!/bin/bash
# LenoxVI bot restart (run on the appliance host).
# Now a thin wrapper over the systemd user unit lenoxvi-bot.service:
# the unit owns startup, crash-restart, boot survival, and
# journald logging. This keeps the same UX (bash scripts/restart.sh).
# Manual usage notes:
#   status:  systemctl --user status lenoxvi-bot
#   logs:    journalctl --user -u lenoxvi-bot -f
set -euo pipefail

HINT="status: systemctl --user status lenoxvi-bot; logs: journalctl --user -u lenoxvi-bot -f"
if ! systemctl --user restart lenoxvi-bot.service; then
  echo "lenoxvi-bot restart FAILED ($HINT)" >&2
  exit 1
fi
# A restart call can succeed for a unit that then fails to come up, and a
# Type=simple unit reports active as soon as the process starts: only a unit
# that stays active for STABLE polls in a row counts as restarted.
STABLE=5
up=0
for _ in $(seq 1 15); do
  if systemctl --user is-active --quiet lenoxvi-bot.service; then
    up=$((up + 1))
    if [ "$up" -ge "$STABLE" ]; then
      echo "lenoxvi-bot restarted (systemd; logs: journalctl --user -u lenoxvi-bot)"
      exit 0
    fi
  elif [ "$up" -gt 0 ]; then
    echo "lenoxvi-bot started, then stopped ($HINT)" >&2
    exit 1
  fi
  sleep 1
done
echo "lenoxvi-bot is not active after restart ($HINT)" >&2
exit 1
