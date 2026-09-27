# Production access and storage

Use HTTPS through a reverse proxy for public access. A domain with Caddy-managed certificates is simplest. If the server has a stable public IP and no domain, use the IP certificate procedure in `ops/certbot/README.md`; it requires reliable automatic renewal. Keep the application ports bound to loopback so browsers cannot bypass TLS.

## HTTPS proxy

1. For a domain, point its DNS `A` record at the server. For an IP certificate, use the server's stable public IPv4 address and follow `ops/certbot/README.md`. Allow inbound TCP 80 and 443.
2. Merge the matching site blocks from `ops/caddy/Caddyfile.example` into the active Caddy configuration, preserving unrelated sites. For a domain, Caddy can obtain and renew certificates automatically; for an IP, Certbot issues and renews the short-lived certificate and its deploy hook reloads Caddy.
3. Bind the app container to `127.0.0.1:7860:7860` and the optional admin container to `127.0.0.1:7861:7861`.
4. Set `SESSION_COOKIE_SECURE=1` and `ALLOW_REGISTER=0` in the app environment, then restart the app after the proxy is serving HTTPS.
5. Remove public firewall rules for 7860 and 7861. Keep only the ports required by the proxy and administration access.

The current live deployment record describes a direct HTTP setup. It has not been changed by editing this repository; apply the proxy and firewall steps during a deployment window.

## Persistence and cached videos

- Mount `/app/data` to persistent storage. It contains `auth.db` and user accounts.
- Back up the data directory before replacing containers. Restrict backup permissions because it contains account hashes and session records.
- `MAX_VIDEO_DOWNLOAD_MB` defaults to 500 MB per cached video.
- `VIDEO_RETENTION_DAYS=0` leaves cached videos untouched. Set a positive value, such as `30`, to delete server-cached `.mp4` files older than that many days at startup.
- Keep `downloads`, `cache`, `logs`, `static/videos`, and `data` on a volume with monitoring and enough free space.

## Registration and cross-origin clients

- Self-registration is off by default. Set `ALLOW_REGISTER=1` only when open registration is intended.
- Same-origin UI/API requests need no CORS configuration. For a separate frontend, set `CORS_ALLOW_ORIGINS` to its exact origin or comma-separated origins; do not use `*` with authenticated requests.
- Set database credentials through `DB_HOST`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME`. Do not keep database passwords in source files.
- Rotate any credential that was previously committed to Git; deleting it from the current file does not remove it from repository history.

## Operational checks

- Start the combined service with `python app.py`; it initializes session authentication when `REQUIRE_AUTH=1` or `APP_PASS` is set. Do not expose the standalone development API entry point.
- Check `/health` locally and through the public HTTPS address after restart.
- Back up `/app/data` before deployment and verify it remains present after container recreation.
- The deployment workflows stop before replacing a container unless the HTTPS health check succeeds. Configure the main Caddy site first; the optional admin workflow requires its HTTPS `:8443` route before it can deploy.
- Monitor disk usage and application logs; cached media and analysis caches may grow independently.
