# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — the system service, the dashboard restart grant, the
# lenoxvi command, and the health check.

# render_unit <host ip> -> the unit text on stdout
render_unit() {
  local ip=$1 line
  while IFS= read -r line || [[ -n $line ]]; do
    printf '%s\n' "${line//@HOST_IP@/$ip}"
  done <"$LVI_UNIT_TEMPLATE"
}

# install_unit <ip> — writes the unit only when it changed.
# Sets UNIT_CHANGED=yes|no.
install_unit() {
  local new
  new=$(render_unit "$1")
  UNIT_CHANGED=no
  if [[ -f $LVI_UNIT ]] && [[ $(cat "$LVI_UNIT") == "$new" ]]; then
    return 0
  fi
  UNIT_CHANGED=yes
  printf '%s\n' "$new" | write_file "$LVI_UNIT" 0644 -
  run systemctl daemon-reload
}

install_cli() {
  local target=$LVI_APP_REAL/scripts/installer/lenoxvi
  if [[ -L $LVI_CLI && $(readlink "$LVI_CLI") == "$target" ]]; then return 0; fi
  run ln -sfn "$target" "$LVI_CLI"
}

# render_restart_helper -> the helper body on stdout. The dashboard's
# restart button runs it as root via sudo: it only schedules (the restart
# fires 2 s later from a detached transient unit), so the dashboard's
# HTTP response completes first.
render_restart_helper() {
  cat <<'EOF'
#!/usr/bin/env bash
# schedule-restart — written by the installer, run by the dashboard's
# restart button via sudo. It only schedules: the restart fires 2 s later
# from a detached transient unit, after the dashboard's response has
# completed.
exec systemd-run --unit="lenoxvi-dash-restart-$(date +%s%3N)" -- bash -c 'sleep 2 && systemctl restart lenoxvi.service'
EOF
}

# install_restart_grant <ip> — the restart button's privileged path: a
# root-owned helper plus a sudoers drop-in granting the service user
# exactly that helper (nothing else). Idempotent: nothing is written when
# both files already match.
install_restart_grant() {
  local user new_helper new_grant tmp
  user=$(render_unit "$1" | sed -n 's/^User=//p' | head -1)
  [[ -n $user ]] || die "the unit template has no User= — cannot grant the dashboard restart"
  [[ -d $LVI_LIB ]] || run install -d -m 0755 "$LVI_LIB"
  new_helper=$(render_restart_helper)
  if [[ ! -f $LVI_RESTART_HELPER || $(cat "$LVI_RESTART_HELPER") != "$new_helper" ]]; then
    printf '%s\n' "$new_helper" | write_file "$LVI_RESTART_HELPER" 0755 root:root
  fi
  new_grant=$(printf '%s ALL=(root) NOPASSWD: %s\n' "$user" "$LVI_RESTART_HELPER_REAL")
  if [[ -f $LVI_SUDOERS && $(cat "$LVI_SUDOERS") == "$new_grant" ]]; then return 0; fi
  # sudo parses the drop-in the moment it lands, so the exact bytes pass
  # visudo first and a refusal aborts, leaving nothing behind. Dry-run
  # prints the check against the drop-in's path; the real run stages the
  # same bytes (visudo takes a file).
  if [[ $LVI_DRY_RUN == 1 ]]; then
    run visudo -cf "$LVI_SUDOERS"
  else
    tmp=$(mktemp)
    printf '%s\n' "$new_grant" >"$tmp"
    if ! visudo -cf "$tmp"; then
      rm -f "$tmp"
      die "the dashboard restart grant failed visudo's check — nothing installed"
    fi
    rm -f "$tmp"
  fi
  printf '%s\n' "$new_grant" | write_file "$LVI_SUDOERS" 0440 root:root
}

install_service() {
  step "Starting the service"
  install_unit "$HOST_IP"
  install_restart_grant "$HOST_IP"
  install_cli
  if systemctl is-active --quiet lenoxvi 2>/dev/null; then
    if [[ $UNIT_CHANGED == yes ]]; then
      run systemctl restart lenoxvi
      ok "service: restarted (settings changed)"
    else
      ok "service: running (nothing changed)"
    fi
  else
    run systemctl enable --now --quiet lenoxvi
    ok "service: enabled and started"
  fi
}

# wait_healthy <ip> [seconds] — the dashboard answers (any HTTP status).
# 0 = up; 2 = still starting after the wait (slow machine loading models);
# 1 = the service stopped (crashed).
wait_healthy() {
  local ip=$1 secs=${2:-300} i code
  [[ $LVI_DRY_RUN == 1 ]] && { printf '+ wait for https://%s:7860/dashboard\n' "$ip"; return 0; }
  for ((i = 0; i < secs; i += 3)); do
    code=$(curl -sk -o /dev/null -m 3 -w '%{http_code}' "https://$ip:7860/dashboard" || true)
    [[ $code =~ ^[1-5][0-9][0-9]$ && $code != 000 ]] && return 0
    systemctl is-active --quiet lenoxvi || break
    (( i > 0 && i % 60 == 0 )) && ok "still starting (loading the speech models)…"
    sleep 3
  done
  if systemctl is-active --quiet lenoxvi; then
    warn "still starting after ${secs} s — slow machines can take a few more minutes. Check with: lenoxvi status"
    return 2
  fi
  warn "the service stopped. Last log lines:"
  journalctl -u lenoxvi -n 20 --no-pager >&2 || true
  printf '  Full log: journalctl -u lenoxvi\n' >&2
  return 1
}

handoff() {
  local url="https://$HOST_IP:7860/dashboard"
  step "LenoxVI is running"
  printf '\n  %sOpen the dashboard:%s %s%s%s\n' "$MIST" "$RESET" "$LIME" "$url" "$RESET"
  printf '  Your browser warns about the certificate once (this machine made it): choose Advanced, then continue.\n'
  if [[ -n ${DASH_TOKEN:-} ]]; then
    printf '\n  Dashboard token (shown once, also in %s/.env):\n  %s\n' "$LVI_STATE_REAL" "$DASH_TOKEN"
  fi
  printf '\n  %sNext: open the dashboard — it walks you through setup.%s\n' "$MUTED" "$RESET"
  printf '  Manage it: lenoxvi status | logs | restart | reconfigure | upgrade | uninstall\n\n'
}
