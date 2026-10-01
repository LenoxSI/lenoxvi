#!/bin/bash
# lenoxvi bot restart (run on the appliance host).
# Now a thin wrapper over the systemd user unit lenoxvi-bot.service
# (2026-09-20): the unit owns startup, crash-restart, boot survival, and
# journald logging. This keeps the same UX (bash scripts/restart.sh).
# Manual usage notes:
#   status:  systemctl --user status lenoxvi-bot
#   logs:    journalctl --user -u lenoxvi-bot -f
systemctl --user restart lenoxvi-bot.service
echo "lenoxvi-bot restarted (systemd; logs: journalctl --user -u lenoxvi-bot)"