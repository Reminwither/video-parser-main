# HTTPS without buying a domain

Let's Encrypt now issues publicly trusted certificates for public IP addresses. IP certificates use the `shortlived` profile and expire after about 160 hours, so automated renewal is required. Certbot 5.4 or newer supports the IP `webroot` flow; Certbot issues and renews the certificate, while the deploy hook in this directory copies it for Caddy and reloads Caddy after renewal.

## 1. Set up the HTTP-01 challenge route

Use the server's stable public IPv4 address. Open inbound TCP 80 and 443 in the Tencent Cloud firewall/security group. Keep the app on 7860 and the admin service on 7861 bound to localhost. Caddy serves the admin panel at `https://110.40.138.167/admin/`.

Create the challenge directory and install the deploy hook:

```bash
sudo install -d -o root -g caddy -m 0750 /var/www/certbot
sudo install -o root -g root -m 0750 ops/certbot/install-ip-cert.sh /usr/local/bin/video-parser-cert-deploy
```

Merge `ops/caddy/Caddyfile.bootstrap.example` into the existing `/etc/caddy/Caddyfile`. It preserves the current public app port while Caddy serves the ACME challenge and proxies HTTP traffic. Keep existing unrelated site blocks.

The current deployment IP is `110.40.138.167`. If the IP changes later, update the example before installing it.

Validate and reload Caddy, then confirm a test file under `/var/www/certbot/.well-known/acme-challenge/` is reachable at `http://110.40.138.167/.well-known/acme-challenge/<filename>`.

## 2. Issue the IP certificate

Certbot 5.8 is installed in `/opt/certbot-venv` on the current server. On a new Ubuntu 24.04 server, install Certbot 5.4 or newer in an isolated environment:

```bash
sudo apt-get install -y python3-venv
sudo python3 -m venv /opt/certbot-venv
sudo /opt/certbot-venv/bin/pip install 'certbot>=5.4,<6'
sudo ln -sf /opt/certbot-venv/bin/certbot /usr/local/bin/certbot
```

Then issue the production certificate once:

```bash
sudo certbot certonly --non-interactive --agree-tos \
  --register-unsafely-without-email \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/certbot \
  --ip-address 110.40.138.167 \
  --deploy-hook /usr/local/bin/video-parser-cert-deploy
```

The initial deploy hook copies the certificate and reloads the current Caddy configuration. The certificate files are stored under `/etc/letsencrypt/live/110.40.138.167/` and copied to `/etc/caddy/certs/` with permissions readable by Caddy.

## 3. Switch Caddy to HTTPS

After the first certificate exists, install the final blocks from `ops/caddy/Caddyfile.example` into the Caddyfile. Validate and reload Caddy. Keep the HTTP ACME challenge handler on port 80 so renewal continues working. The admin panel shares the existing HTTPS port under `/admin/`.

Set these values in the server's `/opt/video-parser/.env`:

```dotenv
DOMAIN=https://110.40.138.167
REQUIRE_AUTH=1
ALLOW_REGISTER=0
SESSION_COOKIE_SECURE=1
```

This server uses a pip-installed Certbot, so install and enable the included twice-daily systemd timer (`video-parser-certbot-renew.timer`). Confirm it is active and that the deploy hook is recorded in the renewal config. Run `certbot renew --dry-run` once after setup. The normal renewal must run at least daily because the IP certificate lifetime is only about six days.

The deploy hook uses `caddy reload --force`: the Caddyfile itself stays unchanged
when a file-backed certificate renews, so an ordinary reload can leave the old
certificate in memory. The hook then compares the certificate served on local
port 443 with the installed certificate and exits unsuccessfully if they differ.
For a different server address, set `VIDEO_PARSER_CERT_HOST` in the renewal
service environment. Verify the certificate from outside the server as well;
the public monitor checks CA trust, the IP identity, and a 24-hour expiry margin.
See [Caddy's reload documentation](https://caddyserver.com/docs/command-line#caddy-reload).

## 4. Close direct application ports

After `https://110.40.138.167/health` works and the login cookie is marked `Secure`, remove public firewall rules for 7860 and 7861. Keep 80 and 443 open. The application deployment workflow checks the HTTPS endpoint before it replaces the running container.

If the Tencent public IP changes, issue a new certificate for the new IP and update the Caddyfile and `DOMAIN` value.
