#!/usr/bin/env bash
# Lumen Cloud on one fresh Ubuntu server (22.04 or 24.04), in one command:
#   sudo bash deploy/single/install.sh
# It adds swap and a firewall, installs Docker, asks for your domain, email and (optionally) SMTP settings,
# writes .env with a new secret key, starts Lumen behind Caddy (automatic HTTPS) and schedules daily backups.
# Run it again at any time: it keeps your .env and only restarts what changed.
# Without questions:  sudo DOMAIN=app.example.com EMAIL=you@example.com bash deploy/single/install.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HERE=$(pwd)
case "$HERE" in
  *%*|*\'*|*" "*) echo "Move the Lumen folder to a path without spaces, quotes or % signs (for example /home/ubuntu/lumen)"; exit 1;;
esac
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "Please run with sudo: sudo bash $0"; exit 1; }
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }

say "1/6  Swap (keeps a 1-2 GB server stable)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "2 GB swap added"
else
  echo "swap already on"
fi

say "2/6  Firewall: SSH, HTTP and HTTPS only"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
  ufw --force enable >/dev/null && echo "ufw on"
fi

say "3/6  Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

say "4/6  Settings (.env)"
if [ ! -f .env ]; then
  DOMAIN=${DOMAIN:-}; EMAIL=${EMAIL:-}
  while [ -z "$DOMAIN" ]; do read -rp "Domain for Lumen, e.g. app.yourcompany.in: " DOMAIN; done
  while [ -z "$EMAIL" ]; do read -rp "Your email (HTTPS notices; this address gets the admin page): " EMAIL; done
  SMTP_HOST=${SMTP_HOST:-}; SMTP_PORT=${SMTP_PORT:-587}; SMTP_USER=${SMTP_USER:-}; SMTP_PASSWORD=${SMTP_PASSWORD:-}; EMAIL_FROM=${EMAIL_FROM:-}
  if [ -z "$SMTP_HOST" ] && [ -t 0 ]; then
    echo "Email for sign-up confirmations and password resets (press Enter to skip; add it to .env later):"
    read -rp "  SMTP host (e.g. smtp.resend.com): " SMTP_HOST
    if [ -n "$SMTP_HOST" ]; then
      read -rp "  SMTP port [587]: " p; SMTP_PORT=${p:-587}
      read -rp "  SMTP user: " SMTP_USER
      read -rsp "  SMTP password or API key: " SMTP_PASSWORD; echo
      echo "  The From address must be at a domain you verified with this email provider."
      read -rp "  From address [Lumen <no-reply@${DOMAIN}>]: " EMAIL_FROM
    fi
  fi
  EMAIL_FROM=${EMAIL_FROM:-"Lumen <no-reply@${DOMAIN}>"}
  cp env.example .env && chmod 600 .env
  DOMAIN="$DOMAIN" EMAIL="$EMAIL" SMTP_HOST="$SMTP_HOST" SMTP_PORT="$SMTP_PORT" SMTP_USER="$SMTP_USER" \
  SMTP_PASSWORD="$SMTP_PASSWORD" EMAIL_FROM="$EMAIL_FROM" python3 - .env <<'PY'
import os, re, secrets, sys
path = sys.argv[1]
def q(v):                       # Docker Compose: single quotes are taken literally
    if "'" not in v:
        return "'" + v + "'"
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'
vals = {"DOMAIN": os.environ["DOMAIN"].strip().lower(), "ACME_EMAIL": os.environ["EMAIL"].strip(),
        "LUMEN_ADMIN_EMAILS": os.environ["EMAIL"].strip().lower(), "LUMEN_SECRET_KEY": secrets.token_urlsafe(48),
        "LUMEN_SMTP_HOST": os.environ["SMTP_HOST"].strip(), "LUMEN_SMTP_PORT": os.environ["SMTP_PORT"].strip() or "587",
        "LUMEN_SMTP_USER": os.environ["SMTP_USER"], "LUMEN_SMTP_PASSWORD": os.environ["SMTP_PASSWORD"],
        "LUMEN_EMAIL_FROM": os.environ["EMAIL_FROM"]}
text = open(path, encoding="utf-8").read()
for k, v in vals.items():
    text = re.sub(rf"^{k}=.*$", lambda m: f"{k}={q(v)}", text, count=1, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PY
  echo ".env written (private, mode 600). Company details and payment keys can be added to it later."
else
  echo ".env already exists: keeping it"
fi
DOMAIN=$(grep -E '^DOMAIN=' .env | head -1 | cut -d= -f2- | tr -d "\"'")

say "5/6  Starting Lumen (the first build takes a few minutes)"
IP=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
DNS=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
if [ -n "$IP" ] && [ "$IP" != "$DNS" ]; then
  echo "Note: $DOMAIN points to ${DNS:-nothing yet}, but this server is $IP."
  echo "      Add a DNS A record $DOMAIN -> $IP. HTTPS starts by itself a few minutes after the record works."
fi
docker compose up -d --build
for _ in $(seq 1 60); do
  if docker compose exec -T lumen python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=3)" >/dev/null 2>&1; then
    echo "Lumen is running"; break
  fi
  sleep 3
done

say "6/6  Daily backups (03:17, kept 14 days in /var/backups/lumen)"
chmod 755 "$HERE/backup.sh" "$HERE/restore.sh"
echo "17 3 * * * root '$HERE/backup.sh' >> /var/log/lumen-backup.log 2>&1" > /etc/cron.d/lumen-backup
chmod 644 /etc/cron.d/lumen-backup
echo "scheduled"

say "Done"
echo "  Website and sign-up:  https://$DOMAIN"
echo "  Public demo:          https://$DOMAIN/demo   (start with Try it)"
echo "  Health check:         https://$DOMAIN/healthz"
echo "Sign up with $(grep -E '^LUMEN_ADMIN_EMAILS=' .env | cut -d= -f2- | tr -d "\"'") and confirm the email: that account gets /admin."
echo "Logs: cd $HERE && sudo docker compose logs -f lumen"
