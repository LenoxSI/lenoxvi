#!/bin/bash
# One-shot self-signed cert for the lenoxvi web client (HTTPS = secure context
# = browsers allow microphone access on the LAN IP; plain HTTP is hard-blocked).
# Regenerate after expiry (825 days). Not a secret; safe to keep on the box.
#
# Usage: make-cert.sh <bot-host-LAN-IP>
#   bash scripts/make-cert.sh <bot-host-LAN-IP>
# The IP goes into the cert's subjectAltName — it must be the IP browsers
# will actually load the client from, so it can't be baked in here.
set -e
IP="${1:?usage: make-cert.sh <bot-host-LAN-IP>}"
cd "$(dirname "$0")/.."
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/key.pem -out certs/cert.pem -days 825 \
  -subj "/CN=appliance-host" \
  -addext "subjectAltName=IP:${IP},DNS:appliance-host" 2>/dev/null
chmod 600 certs/key.pem
echo CERT-OK $(openssl x509 -in certs/cert.pem -noout -enddate)