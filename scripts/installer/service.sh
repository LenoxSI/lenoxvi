# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — the system service, the lenoxvi command, and the
# health check.

# render_unit <host ip> <gpu|cpu> -> the unit text on stdout
render_unit() {
  local ip=$1 profile=$2 ld=''
  if [[ $profile == gpu ]]; then
    local py site lib dirs=()
    py=$(_venv_pyver)
    site="$LVI_VENV_REAL/lib/python$py/site-packages/nvidia"
    for lib in cublas cudnn cuda_nvrtc; do dirs+=("$site/$lib/lib"); done
    ld="Environment=LD_LIBRARY_PATH=$(IFS=:; echo "${dirs[*]}")"
  fi
  local line
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line == '@GPU_LD_LIBRARY_PATH@' ]]; then
      [[ -n $ld ]] && printf '%s\n' "$ld"
      continue
    fi
    printf '%s\n' "${line//@HOST_IP@/$ip}"
  done <"$LVI_UNIT_TEMPLATE"
}

# Python X.Y of the venv (3.12 when the venv isn't there yet: dry-run).
_venv_pyver() {
  if [[ -x $LVI_VENV/bin/python ]]; then
    "$LVI_VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])'
  elif [[ $PY_OK == yes ]]; then
    "$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])'
  else
    echo 3.12
  fi
}

# install_unit <ip> <profile> — writes the unit only when it changed.
# Sets UNIT_CHANGED=yes|no.
install_unit() {
  local new
  new=$(render_unit "$1" "$2")
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

install_service() {
  step "Starting the service"
  install_unit "$HOST_IP" "$STT_PROFILE"
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
  if command -v qrencode >/dev/null 2>&1 && [[ -t 1 ]]; then
    qrencode -t ANSIUTF8 -m 2 "$url" | sed 's/^/  /'
  fi
  printf '  Your browser warns about the certificate once (this machine made it): choose Advanced, then continue.\n'
  if [[ -n ${DASH_TOKEN:-} ]]; then
    printf '\n  Dashboard token (shown once, also in %s/.env):\n  %s\n' "$LVI_STATE_REAL" "$DASH_TOKEN"
  fi
  printf '\n  %sNext, on the dashboard: pick a voice and a brain, then add your rooms.%s\n' "$MUTED" "$RESET"
  printf '  Manage it: lenoxvi status | logs | restart | reconfigure | upgrade | uninstall\n\n'
}
