# Put Lumen live for about ₹1,200 a month

Result: `https://<your domain>` with the website, sign-up, the public demo (it opens on **Try it**), the console,
and the two-line SDK install for customers. One small server in Mumbai, automatic HTTPS, daily backups.

What is off on this setup: **LLM jobs** (LLM judge, summaries), because they need a GPU server. Everything else
works: traces, checks, alerts, the ops agent, approvals of rollbacks, datasets, the CI gate, teams, billing.
When customers need LLM jobs or more capacity, move to the AWS setup in `deploy/aws/` (about $485 a month).

## What it costs

Prices checked on the providers' pages on 9 Nov 2026, before GST; rupees at ₹93.86 per $ (RBI reference rate that day).

| | Choice | Per month |
|---|---|---|
| Server | AWS Lightsail, Mumbai: 2 GB RAM, 2 vCPUs, 60 GB SSD, 1.5 TB transfer, public IPv4 | $12 ≈ ₹1,126 |
| Static IP | Lightsail static IP, attached to the server | free while attached |
| Snapshots (recommended) | Lightsail automatic snapshots: the last 7 days, billed on storage used | $0.05 per GB-month |
| Email | Resend free plan: 3,000 emails a month, at most 100 a day | ₹0 |
| Domain | `.in` at Hostinger: ₹1 the first year, ₹899 a year after; or `.com` at Cloudflare, about $11.17 a year | about ₹75–90 |
| **Total** | | **about ₹1,200 + GST** |

Other options:
- **Smaller:** Lightsail 1 GB ($7 ≈ ₹657 a month) runs the demo and the first few customers; the installer adds 2 GB of swap.
- **DigitalOcean, Bangalore:** $6 (1 GB) or $12 (2 GB); weekly backups add 20%. It blocks email ports 25, 465 and 587, so use port 2587 with Resend or 2525 with Brevo.
- **Not recommended:**
  - Hetzner Singapore doubled its prices in June 2026 (€15.99 for 2 GB).
  - Oracle's Always Free needs a card, often has no capacity, and reclaims idle machines.
- **Free, demo only:** Render's free plan can host the public demo from `render.yaml`. It has no sign-up, sleeps after 15 minutes idle and takes about a minute to wake. It keeps no data and blocks email ports. Fine for a link in a pitch deck, not for customers.
- **Email alternatives:**
  - Brevo: free for 300 a day.
  - Zoho ZeptoMail: ₹150 per 10,000 emails in India.
  - Amazon SES: $0.10–0.16 per 1,000. It has no free tier any more, and new accounts start in a sandbox.

## 1. Create the server (10 minutes)

1. In the AWS console, open **Lightsail** and choose **Create instance**. Pick these settings:
   - Region: **Mumbai**
   - Linux/Unix, **OS Only**, **Ubuntu 24.04 LTS**
   - Plan: **$12** (2 GB)
   - Name: `lumen`
2. On the instance's **Networking** tab, choose **Attach static IP**. It is free while it is attached.
3. Still on **Networking**, check the IPv4 firewall: allow **SSH (22)**, **HTTP (80)** and **HTTPS (443)**. Add HTTPS if it is missing.
4. On the **Snapshots** tab, turn on **automatic snapshots**. They keep the last 7 days, outside the server.

## 2. Domain and DNS (5 minutes, then a wait)

At your registrar, add an **A record** from your domain to the static IP. For example, `app` → `13.233.x.x` gives
`app.yourcompany.in`. HTTPS starts by itself once the record works, usually within minutes.

## 3. Email (10 minutes)

Lumen sends account emails: confirm address, invitations, password reset, usage warnings.

1. Sign up at Resend and add the domain you will send from under **Domains**. Simplest is the domain Lumen runs
   on (for example `app.yourcompany.in`): the installer's suggested From address uses it.
2. Add the DNS records it shows (SPF and DKIM) at your registrar, and wait until Resend shows the domain as verified.
3. Create an API key with sending access.

The installer then asks for these:

| Setting | Value |
|---|---|
| SMTP host | `smtp.resend.com` |
| Port | `587` (Lightsail blocks only port 25) |
| User | `resend` |
| Password | the API key |
| From address | `Lumen <no-reply@app.yourcompany.in>` (an address at the domain you verified) |

Until this is set, emails are written to files on the server instead of being sent.

## 4. Install (one command, about 10 minutes)

On the instance page, choose **Connect using SSH** (it opens in the browser). Then get the code onto the server
in one of two ways.

**From your GitHub repository** (for a private repository, sign in with your username and a fine-grained
personal access token that can only read this repository):
```
git clone https://github.com/<you>/lumen.git && cd lumen
```

**Or upload `lumen.zip`** from your computer, in PowerShell. The key is under Lightsail > Account > SSH keys.
```
scp -i LightsailDefaultKey-ap-south-1.pem lumen.zip ubuntu@<static IP>:~
```
Then, on the server:
```
sudo apt-get install -y unzip && unzip lumen.zip && cd lumen
```

Then:
```
sudo bash deploy/single/install.sh
```

The installer asks for:
- your domain
- your email (that account gets the admin page)
- the SMTP settings

Then it:
1. adds swap and a firewall
2. installs Docker
3. writes `deploy/single/.env` with a new secret key (private: mode 600)
4. starts Lumen behind Caddy
5. schedules the daily backup

## 5. Check it works (5 minutes)

1. `https://<domain>/healthz` shows `{"ok": true}`.
2. `https://<domain>/demo` opens on **Try it**. Ask a sample question, switch the bot version, and open the trace.
3. **Sign up** with the admin email and confirm it. `https://<domain>/admin` now opens.
4. In **Get started**, create a key. On your laptop:
   ```
   pip install https://<domain>/sdk/lumen_llm_sdk-0.2.0-py3-none-any.whl
   ```
   Then add the two lines to any Python app that calls OpenAI, Anthropic or Gemini:
   ```python
   import lumen_sdk
   lumen_sdk.init(api_key="lmn_...", host="https://<domain>")
   ```
   The **Get started** page shows the exact `pip install` line, and switches to "First trace received" when the
   first trace arrives. To try it without a cloud API key, run `python sdk/example_ollama.py --key lmn_... --host https://<domain>`
   against a local Ollama.

## 6. Company details and payments

Edit the settings and apply them:
```
cd ~/lumen/deploy/single
sudo nano .env               # LUMEN_COMPANY_NAME, address, support email and phone, GSTIN, grievance officer, ...
sudo docker compose up -d    # applies the change
```

**Payments: Razorpay test mode first.** Follow `deploy/aws/DEPLOY-AWS.md`, step 7: create the plan, the API keys
and a webhook. The webhook URL is `https://<domain>/webhooks/razorpay`. Put the keys in `.env`, then run
`sudo docker compose up -d`.

Test the journey:
1. Sign up.
2. Choose **Upgrade** and pay with a test card.
3. Check that the plan shows Team.
4. Cancel.

Switch to live keys only after a lawyer has reviewed the policies and KYC is done (see `LAUNCH.md`).

## Operating it

| Task | How (in `~/lumen/deploy/single`) |
|---|---|
| Update | `cd ~/lumen && git pull` (or upload a new zip), then `cd deploy/single && sudo docker compose up -d --build` |
| Logs | `sudo docker compose logs -f lumen` |
| Restart | `sudo docker compose restart` |
| Backups | Daily at 03:17 to `/var/backups/lumen`, kept 14 days, plus Lightsail snapshots. Log: `/var/log/lumen-backup.log` |
| Restore | `sudo bash restore.sh /var/backups/lumen/lumen-YYYYMMDD-HHMM.tar.gz`, or restore a whole Lightsail snapshot to a new instance and move the static IP to it |
| Uptime | A free monitor (for example UptimeRobot) on `https://<domain>/healthz` |
| Demo limits | Each visitor's copy: 24 MB, 8 hours, at most 10 new copies per address per hour, 60 copies at once (`LUMEN_MAX_SANDBOXES`). Turn the demo off with `LUMEN_DEMO=""` |
| Security updates | Ubuntu installs them automatically; reboot now and then with `sudo reboot` (Lumen starts again by itself) |

**When to grow:**
1. First, a bigger Lightsail plan: take a snapshot, then create the larger instance from it.
2. Then the AWS setup with a GPU (`deploy/aws/DEPLOY-AWS.md`), when customers need LLM jobs or more capacity.
