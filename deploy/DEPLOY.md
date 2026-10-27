# Put Lumen live on the internet

Result: Lumen at `https://lumen.yourdomain.com`, with HTTPS, a password on the web UI, and your apps
sending traces with a project key. Time needed: about 1 hour. Cost: a small cloud server (about $10–25 per month).

```
your apps (SDK, CI)  --HTTPS + X-Lumen-Key-->  Caddy (HTTPS, password on UI)  -->  Lumen  -->  SQLite on a volume
people (browser)     --HTTPS + password----->                                          (optional) Ollama
```

## 1. Get a server and a domain

- **Server:** any Linux VM with 2 vCPU, 4 GB RAM, 40 GB disk, Ubuntu 22.04/24.04. Pick a region near your users
  (for India: Mumbai or Hyderabad on AWS, Azure, Google Cloud, DigitalOcean or Hetzner).
  Add 8 GB+ RAM (or a GPU) only if you will run Ollama on the same server.
- **Domain:** buy one (or use a subdomain you own) and create a DNS **A record**:
  `lumen.yourdomain.com -> <server public IP>`.
- **Firewall:** open only ports 22 (SSH), 80 and 443.

```
sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
```

## 2. Install Docker on the server

```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER     # log out and back in
```

## 3. Copy Lumen to the server

```
scp lumen.zip user@<server-ip>:~      # from your computer
ssh user@<server-ip>
sudo apt install -y unzip && unzip lumen.zip && sudo mv lumen /opt/lumen
cd /opt/lumen/deploy
```
(Or `git clone` your private repository into `/opt/lumen`.)

## 4. Configure

```
cp .env.example .env
docker run --rm caddy:2.8 caddy hash-password --plaintext 'choose-a-strong-password'
nano .env
```
Fill in `DOMAIN`, `ACME_EMAIL`, `LUMEN_USER`, and paste the hash into `LUMEN_PASSWORD_HASH`,
**replacing every `$` with `$$`** (Docker Compose needs that).

## 5. Start

```
docker compose up -d --build
docker compose ps            # both services "running"
docker compose logs -f caddy # wait for "certificate obtained successfully"
```
Open `https://lumen.yourdomain.com`, log in, and type your name in **Signed in as**.

## 6. Connect a real app

1. In Lumen, create a project and a key (Settings & Keys). Copy the `lmn_...` key once; only its hash is stored.
2. In your app:
   ```python
   from lumen_sdk import Lumen
   lumen = Lumen(host="https://lumen.yourdomain.com", api_key="lmn_...")
   ```
   Put the key in your app's secret store, not in code.
3. Release gate in CI: `python tools/lumen_gate.py gate --host https://lumen.yourdomain.com --key $LUMEN_KEY ...`

`/v1/*` (SDK, prompts, datasets, CI) is protected by the project key and has no password, so apps and CI
work without the login. Everything else needs the password.

## 7. Optional: local LLM

```
docker compose --profile llm up -d
docker compose exec ollama ollama pull llama3.1
```
In Lumen → Settings, set the LLM endpoint to `http://ollama:11434`. It is a private address, so the
data-residency policy allows it. Every LLM job still waits on **Approvals**.

## 8. Keep it running

| Task | Command / how |
|---|---|
| Daily backup (keeps 14 days) | `crontab -e` → `0 2 * * * /opt/lumen/deploy/backup.sh` (copy `backups/` off the server too) |
| Update to a new version | copy the new code, then `docker compose up -d --build` |
| Logs | `docker compose logs -f lumen` |
| Uptime alert | a free monitor (UptimeRobot, Better Stack) on `https://lumen.yourdomain.com/v1/prompts/x` expecting 401 |
| Restore a backup | `docker compose stop lumen`, copy the file to the volume as `/data/lumen.db`, start again |
| Start fresh (removes demo data) | `docker compose down`, `docker volume rm deploy_lumen-data`, `docker compose up -d` |

## Before real customers use it

This setup is right for a **live demo, a pilot, or one internal team**. One shared password is not
per-person login, and names in "Signed in as" are still self-declared. Before several customers or
teams share an instance, add: SSO (OIDC) with per-user roles, Postgres instead of SQLite, separate
instances (or projects with access control) per customer, and a security review. For regulated
customers, the better model is that **each customer runs this same kit inside their own network**.

The first start loads the demo project ("Support Copilot") with synthetic traffic. Keep it for a public
demo; for a real team, use "My new project" or create your own.

## Public demo on Render (working, not read-only)

Set `LUMEN_DEMO=sandbox`. Every visitor then gets a **private copy** of the demo (cookie-based): they can approve
the rollback, run the ops agent, add traffic, create a key and send traces with the SDK. Nobody sees anyone else's
changes, and the shared demo can't be broken. Copies reset after `LUMEN_SANDBOX_TTL_MIN` minutes without use.
Changing the LLM endpoint, the data-residency policy and running LLM jobs stay disabled, so the public server never
calls an address a visitor chooses. Writes are rate limited per visitor.

On Render: use `render.yaml` (New -> Blueprint), or on the existing service set Start Command to
`uvicorn lumen.app:app --host 0.0.0.0 --port $PORT` and add the environment variable `LUMEN_DEMO=sandbox`.
Remove any read-only switch from an earlier deployment. Render's free plan sleeps after 15 minutes idle
(first visit then takes ~50 s) and its disk is reset on each deploy or restart, which is fine for a demo.
