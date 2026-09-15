#!/bin/bash
# One-shot self-signed cert for the lenoxvi web client (HTTPS = secure context
# = browsers allow microphone access on the LAN IP; plain HTTP is hard-blocked).
# Regenerate after expiry (825 days). Not a secret; safe to keep on the box.
set -e
cd "$(dirname "$0")/.."
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/key.pem -out certs/cert.pem -days 825 \
  -subj "/CN=appliance-host" \
  -addext "subjectAltName=IP:203.0.113.185,DNS:appliance-host" 2>/dev/null
chmod 600 certs/key.pem
echo CERT-OK $(openssl x509 -in certs/cert.pem -noout -enddate)