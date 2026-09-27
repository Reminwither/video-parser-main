#!/usr/bin/env bash
set -euo pipefail

: "${RENEWED_LINEAGE:?Certbot must set RENEWED_LINEAGE before running this deploy hook}"

install -d -o root -g caddy -m 0750 /etc/caddy/certs
install -o root -g caddy -m 0640 "$RENEWED_LINEAGE/fullchain.pem" /etc/caddy/certs/video-parser-fullchain.pem
install -o root -g caddy -m 0640 "$RENEWED_LINEAGE/privkey.pem" /etc/caddy/certs/video-parser-key.pem

/usr/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
/usr/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
