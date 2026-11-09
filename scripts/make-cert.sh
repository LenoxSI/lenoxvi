#!/bin/bash
# HTTPS cert for the LenoxVI web client (HTTPS = secure context = browsers
# allow microphone access on the LAN IP; plain HTTP is hard-blocked). A
# long-lived bot CA (nodes will pin it in a later release — back
# secrets/tls-ca-key.pem up with the firmware key) issues a fresh serving
# cert every run: the CA is made once and never regenerated, only the
# disposable leaf is.
#
# Usage: make-cert.sh <bot-host-LAN-IP>
#   bash scripts/make-cert.sh <bot-host-LAN-IP>
# The IP goes into the cert's subjectAltName — it must be the IP browsers
# will actually load the client from, so it can't be baked in here.
# Writes under $LVI_STATE_DIR when it is set (where the bot looks), else to
# certs/ and secrets/ in the repo.
set -e
IP="${1:?usage: make-cert.sh <bot-host-LAN-IP>}"
cd "${LVI_STATE_DIR:-$(dirname "$0")/..}"
CA_KEY=secrets/tls-ca-key.pem
CA_CERT=certs/ca.pem
mkdir -p certs

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A half CA pair means the CA was damaged, not replaced on purpose: making a
# new one would cut off every node still pinned to the old CA, so refuse.
if [[ -f $CA_KEY && -f $CA_CERT ]]; then
  :
elif [[ -f $CA_KEY || -f $CA_CERT ]]; then
  echo "error: only one of the CA pair exists: $CA_KEY and $CA_CERT." >&2
  echo "A new CA would cut off nodes pinned to the old one; restore the" >&2
  echo "missing file or move both away to start over." >&2
  exit 1
else
  mkdir -p -m 700 secrets
  (umask 077; openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CA_KEY" -out "$CA_CERT" -days 7300 \
    -subj "/CN=lenoxvi CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null)
  chmod 644 "$CA_CERT"
fi

(umask 077; openssl req -new -newkey rsa:2048 -nodes \
  -keyout certs/key.pem -out "$TMP/leaf.csr" -subj "/CN=lenoxvi" 2>/dev/null)
cat > "$TMP/leaf.ext" <<EOF
subjectAltName=IP:${IP},DNS:lenoxvi
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
authorityKeyIdentifier=keyid
EOF
openssl x509 -req -in "$TMP/leaf.csr" \
  -CA "$CA_CERT" -CAkey "$CA_KEY" \
  -set_serial "0x$(openssl rand -hex 16)" \
  -days 825 -extfile "$TMP/leaf.ext" -out certs/cert.pem 2>/dev/null
chmod 600 "$CA_KEY" certs/key.pem
echo CERT-OK $(openssl x509 -in certs/cert.pem -noout -enddate)
echo CA-OK $(openssl x509 -in "$CA_CERT" -noout -fingerprint -sha256)
