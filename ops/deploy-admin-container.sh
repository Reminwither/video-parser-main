#!/usr/bin/env bash
# Build while the current admin stays up. Restore its exact image on failure.
set -Eeuo pipefail

start_container() {
  docker run -d --name video-parser-admin --restart unless-stopped \
    -p 127.0.0.1:7861:7861 --env-file /opt/video-parser/.env.admin \
    -e AUTH_DB_PATH=/opt/video-parser/data/auth.db \
    -e ASSETS_DIR=/opt/video-parser/downloads -e LOGS_DIR=/opt/video-parser/logs \
    -e SESSION_COOKIE_SECURE=1 -v /opt/video-parser:/opt/video-parser:ro "$1"
}

wait_for_health() {
  for attempt in 1 2 3 4 5 6 7 8; do
    if curl -fsS --max-time 10 http://127.0.0.1:7861/health >/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 1
}

expect_status() {
  local actual
  actual=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$1")
  if [ "$actual" != "$2" ]; then
    echo "Admin endpoint returned $actual (expected $2): $1" >&2
    return 1
  fi
}

rollback() {
  trap - ERR
  echo "Admin release failed; restoring previous container image" >&2
  docker rm -f video-parser-admin >/dev/null 2>&1 || true
  if [ -n "$previous_image" ]; then
    if start_container "$previous_image" && wait_for_health; then
      echo "Previous admin image restored and healthy" >&2
    else
      echo "Admin rollback failed; immediate operator attention required" >&2
    fi
  else
    echo "No previous admin image exists for this first deployment" >&2
  fi
  exit 1
}

echo "Building candidate admin image"
docker build -t video-parser-admin:candidate /opt/video-parser-admin/admin
previous_image=$(docker inspect --format '{{.Image}}' video-parser-admin 2>/dev/null || true)
trap rollback ERR

docker rm -f video-parser-admin >/dev/null 2>&1 || true
start_container video-parser-admin:candidate
wait_for_health
expect_status http://127.0.0.1:7861/ 302
expect_status http://127.0.0.1:7861/login 200
public_url=${ADMIN_PUBLIC_URL:-https://110.40.138.167/admin}
expect_status "$public_url/health" 200
expect_status "$public_url/" 302
expect_status "$public_url/login" 200
docker tag video-parser-admin:candidate video-parser-admin:latest
trap - ERR
echo "Admin release healthy locally and over public HTTPS"
