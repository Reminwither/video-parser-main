#!/usr/bin/env bash
set -euo pipefail

: "${RENEWED_LINEAGE:?Certbot must set RENEWED_LINEAGE before running this deploy hook}"

install -d -o root -g caddy -m 0750 /etc/caddy/certs
install -o root -g caddy -m 0640 "$RENEWED_LINEAGE/fullchain.pem" /etc/caddy/certs/video-parser-fullchain.pem
install -o root -g caddy -m 0640 "$RENEWED_LINEAGE/privkey.pem" /etc/caddy/certs/video-parser-key.pem

/usr/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
# The configuration text is unchanged after renewing a file-backed certificate.
# Force provisioning so Caddy replaces the certificate held in memory.
/usr/bin/caddy reload --force --config /etc/caddy/Caddyfile --adapter caddyfile

expected=$(openssl x509 -in /etc/caddy/certs/video-parser-fullchain.pem -noout -fingerprint -sha256)
served=$(timeout 15 openssl s_client -connect 127.0.0.1:443 \
  -servername "${VIDEO_PARSER_CERT_HOST:-110.40.138.167}" </dev/null 2>/dev/null |
  openssl x509 -noout -fingerprint -sha256)
if [ "$served" != "$expected" ]; then
  echo "Caddy is not serving the renewed certificate after reload" >&2
  exit 1
fi
echo "Caddy is serving the renewed certificate"
