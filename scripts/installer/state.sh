# shellcheck shell=bash disable=SC2034,SC2153
# LenoxVI installer — the two questions and the keyless state:
# config.yaml, .env (dashboard token only), the TLS cert. Never overwrites
# a config or .env that's already there.

# detect_ip -> the IPv4 of the interface that holds the default route; no
# route = empty output and status 0, so ask_questions' "pass --host" fires.
detect_ip() {
  { ip -4 route get 1.1.1.1 2>/dev/null || true; } | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

all_ips() { ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1] " (" $2 ")"}'; }

ask_questions() {
  step "Two questions"
  [[ -n ${UI:-} ]] || pick_ui
  local guess cur_ip=''
  # Already installed: today's address is the default (a re-run with --yes
  # must not move it). Only the first field counts.
  if [[ -r $LVI_STATE/config.yaml && -x $LVI_VENV/bin/python ]]; then
    read -r cur_ip _ < <("$LVI_VENV/bin/python" "$LVI_APP/scripts/installer/write_config.py" --get "$LVI_STATE/config.yaml" 2>/dev/null || true) || true
  fi
  guess=${cur_ip:-$(detect_ip)}
  if [[ -z ${HOST_IP:-} ]]; then
    if [[ -z $guess && ( $UI == none || $UI == notty ) ]]; then
      die "couldn't work out this machine's network address — pass --host <its LAN IP>"
    fi
    local others
    others=$(all_ips | tr '\n' ' ')
    [[ -n $others ]] && note "addresses on this machine: $others"
    HOST_IP=$(ask_input "This machine's LAN address (nodes and phones reach the dashboard here)" "$guess")
  fi
  [[ $HOST_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "\"$HOST_IP\" isn't an IPv4 address"
  ok "address: $HOST_IP"
  # No speech question: Embedded is fixed and CPU-only; LocalAI and Cloud are
  # picked later on the dashboard.
  ok "speech: Embedded (Whisper base + Kokoro)"

  if [[ -z ${MAKE_TOKEN:-} ]]; then
    if [[ -f $LVI_STATE/.env ]]; then
      MAKE_TOKEN=no
    elif ask_yes "Protect the dashboard's settings with a token? (recommended)" y; then
      MAKE_TOKEN=yes
    else
      MAKE_TOKEN=no
    fi
  fi
  return 0
}

write_state() {
  step "Writing settings"
  write_config_file
  write_env
  write_cert
}

write_config_file() {
  if [[ -f $LVI_STATE/config.yaml ]]; then
    ok "config: kept your $LVI_STATE_REAL/config.yaml"
  else
    run_quiet runuser -u "$LVI_USER" -- "$LVI_VENV_REAL/bin/python" "$LVI_APP_REAL/scripts/installer/write_config.py" \
      "$LVI_APP_REAL/config.example.yaml" "$LVI_STATE_REAL/config.yaml" --host "$HOST_IP"
    ok "config: written (no keys needed — the voice is local, the intelligence answers with an echo until you pick one)"
  fi
}

write_env() {
  DASH_TOKEN=
  if [[ -f $LVI_STATE/.env ]]; then
    ok "keys file: kept your $LVI_STATE_REAL/.env"
  else
    local body='# Keys go in on the dashboard (System · Keys); it writes them here.'$'\n'
    if [[ $MAKE_TOKEN == yes ]]; then
      DASH_TOKEN=$(openssl rand -hex 32)
      body+="LVI_DASHBOARD_TOKEN=$DASH_TOKEN"$'\n'
    fi
    printf '%s' "$body" | write_file "$LVI_STATE/.env" 0600 "$LVI_USER:$LVI_USER"
    ok "keys file: created"
  fi
}

write_cert() {
  if [[ -f $LVI_STATE/certs/cert.pem ]]; then
    ok "https certificate: kept"
  else
    run_quiet runuser -u "$LVI_USER" -- env LVI_STATE_DIR="$LVI_STATE_REAL" bash "$LVI_APP_REAL/scripts/make-cert.sh" "$HOST_IP"
    ok "https certificate: made for $HOST_IP"
  fi
}
