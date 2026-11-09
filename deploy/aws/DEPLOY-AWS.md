# Put Lumen Cloud live on AWS Mumbai

Result: your public website `https://<your domain>` where anyone can sign up, use Lumen and pay (Razorpay in
India, Stripe abroad), running on AWS in Mumbai with a GPU for approved LLM jobs, backups and alarms.

```
browsers, SDKs ──HTTPS──> app server (t3.medium): Caddy ─> Lumen Cloud ─> data/control.db + data/tenants/<org>.db
                                   │                         │
                                   │ every 15 min            └─ private network ─> GPU server (g4dn.xlarge): Ollama
                                   └─> S3 (encrypted): release zip + backups        (no public port)
Razorpay / Stripe ──webhooks──> /webhooks/razorpay, /webhooks/stripe        Amazon SES ──> your users' inboxes
```

Time: about half a day of your time, plus waiting for AWS GPU quota, SES approval and Razorpay KYC (hours to days).

## What it costs per month (AWS Mumbai, on-demand, Nov 2026 prices)

| Item | Always-on GPU | GPU business hours |
|---|---|---|
| App server t3.medium ($0.0448/h) | $32.70 | $32.70 |
| GPU server g4dn.xlarge ($0.579/h) | $422.70 | about $196 (08:00–21:00 IST, Mon–Sat) |
| Disks: 60 GB + 120 GB gp3 ($0.0912/GB) | $16.40 | $16.40 |
| 2 public IPv4 addresses ($0.005/h each) | $7.30 | $7.30 |
| S3 backups, Secrets Manager, SES, alarms | about $2–5 | about $2–5 |
| **Total** | **about $485 (≈ ₹42,700)** | **about $257 (≈ ₹22,600)** |

Plus a domain (about ₹1,000 a year) and payment fees (Razorpay about 2% + GST in India, international cards up to
3% + GST; Stripe's fees if you get an account). Data out to the internet: the first 100 GB a month are free.
At ₹6,999 a month, about 7 Team customers cover the always-on setup. Rupees at about ₹88 per $; check current prices.

## 1. Accounts you create (in your company's name)

1. **AWS account** (aws.amazon.com). Turn on MFA for the root user, create an admin user for daily work, and set a
   budget alert (Billing > Budgets) at, say, $550 a month.
2. **GPU quota:** new accounts may not start GPUs. In Mumbai: Service Quotas > Amazon EC2 > "Running On-Demand
   G and VT instances" > request **8** vCPUs. Approval can take a day.
3. **Domain:** buy one (any registrar). You will add an A record in step 5 and SES records in step 3.
4. **Razorpay** (razorpay.com): sign up as a business, finish KYC, and enable **Subscriptions**. For foreign
   customers also request **International payments**. Their review checks your website's pricing, terms, privacy,
   cancellation & refund, delivery (shipping) and contact pages: Lumen has all of them.
5. **Stripe** (optional): Stripe is invite-only for Indian businesses. Request an invite from stripe.com; until you
   have it, foreign customers can pay through Razorpay (set `RAZORPAY_PLAN_TEAM_USD`).

## 2. Company details for the website

You need: legal company name, registered address, support email and phone, GSTIN, a grievance officer (name and
email, for India's privacy law) and the city for disputes. They go into the settings secret in step 6.
**Have a lawyer review the terms, privacy, refund and delivery pages** (they are drafts) before you take money.

## 3. Email (Amazon SES)

SES console (Mumbai) > Identities > Create identity > Domain > your domain; add the DKIM CNAME records it shows
at your registrar. Then "Request production access" (explain: account emails for a B2B SaaS; volume under
1,000 a day). Until approved, SES only sends to addresses you verified yourself.

## 4. Create the stack

CloudFormation console (region **Asia Pacific (Mumbai)**) > Create stack > Upload `deploy/aws/lumen-cloud.yaml`.

| Parameter | Value |
|---|---|
| DomainName | e.g. `app.yourdomain.com` |
| AdminEmail | your email (alarms, certificate notices, access to `/admin`) |
| EmailFrom | `Lumen <no-reply@yourdomain.com>` |
| VpcId / SubnetId | the default VPC and any of its subnets |
| EnableGpu / GpuSchedule | `true` / `AlwaysOn` (or `BusinessHours` to roughly halve the GPU cost) |

Tick "I acknowledge that AWS CloudFormation might create IAM resources" and create. It takes about 10 minutes.
Confirm the "AWS Notification - Subscription Confirmation" email so alarms reach you.

## 5. DNS

At your registrar: an **A record** from your domain to the stack's `AppPublicIP` output. The HTTPS certificate is
fetched automatically a few minutes after the record works.

## 6. Settings, then the first release

1. Secrets Manager > the secret from the `SettingsSecret` output > Retrieve secret value > Edit: fill in the
   company details (step 2). Leave the payment fields empty for now. Do not change `LUMEN_SECRET_KEY`.
2. On your computer, in the Lumen folder: `python tools/make_release.py` → `dist/lumen.zip`.
3. S3 console > the `BackupBucket` > create folder `release` > upload `lumen.zip` into it.
   The server deploys it by itself within about 5 minutes (the first build takes longest).
4. Open `https://<domain>/healthz` → `{"ok": true}`, then `https://<domain>` → the website. Sign up with your
   AdminEmail: you get `/admin`.

Every later update: build a new zip and upload it to the same place. The server backs up, deploys, checks
`/healthz` and rolls back by itself if the new version does not start.

## 7. Payments

Use **test mode first**, then repeat with live keys.

**Razorpay**
1. Subscriptions > Plans > Create: monthly, every 1 month, ₹6,999 (add a USD plan too if international is enabled).
2. Settings > API keys > Generate. Settings > Webhooks > Add: URL = `RazorpayWebhookUrl` output, a secret you
   choose, events: `subscription.activated`, `subscription.charged`, `subscription.pending`,
   `subscription.halted`, `subscription.cancelled`, `subscription.completed`, `subscription.paused`,
   `subscription.resumed`.
3. Put `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_PLAN_TEAM_INR` in the secret.

**Stripe** (when you have an account)
1. Product "Lumen Team" with a monthly price of $79 → `STRIPE_PRICE_TEAM_USD`.
2. Developers > Webhooks > Add endpoint: `StripeWebhookUrl` output, events `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`,
   `invoice.payment_failed` → `STRIPE_WEBHOOK_SECRET`. Secret key → `STRIPE_SECRET_KEY`.
3. Settings > Billing > Customer portal: turn it on (lets customers update cards and download invoices).

Apply the settings: EC2 > `lumen-app` > Connect > Session Manager, then `sudo lumen-deploy --force`.

**Test the whole journey:** sign up → confirm email → Settings > Billing > Upgrade (Razorpay test card or UPI
`success@razorpay`) → the plan shows Team → cancel. Then switch the secret to live keys and deploy again.

## 8. Go live checklist

- [ ] Lawyer reviewed the policies → set `LUMEN_POLICIES_FINAL` to `1` and the date in `LUMEN_POLICIES_UPDATED`
- [ ] SES production access approved; a sign-up email arrives in a Gmail inbox (not spam)
- [ ] Live payment keys; one real ₹ payment and refund tested
- [ ] A free uptime monitor (for example UptimeRobot) on `https://<domain>/healthz`
- [ ] AWS budget alert set; MFA on

## Operating it

| Task | How |
|---|---|
| Shell on the server | EC2 > lumen-app > Connect > Session Manager (no SSH port is open) |
| Logs | `sudo docker compose -p lumen logs -f lumen` (in `/opt/lumen/current/deploy/aws`) |
| Deploy log | `sudo journalctl -u lumen-deploy -n 100` |
| Restart | `cd /opt/lumen/current/deploy/aws && sudo docker compose --env-file /opt/lumen/stack.env -p lumen restart` |
| Backups | every 15 min to `s3://<bucket>/backups/frequent/` (kept 2 days) and daily to `backups/daily/` (kept 35 days) |
| Restore | download a backup `.tar.gz`, stop Lumen (`docker compose ... stop lumen`), extract it into `/opt/lumen/data`, `chown -R 10001:10001 /opt/lumen/data`, start Lumen |
| Bigger server | CloudFormation > Update stack > AppInstanceType (a few minutes of downtime) |
| Customers, plans, MRR | `https://<domain>/admin` (only emails in AdminEmail, once that address is confirmed). Granting a plan to a paying customer stops their Razorpay/Stripe subscription from renewing |
| Public demo limits | each visitor's copy: up to 24 MB and 8 hours, at most 10 new copies per address per hour, no API keys. Change with `LUMEN_SANDBOX_MAX_MB`, `LUMEN_SANDBOX_MAX_AGE_MIN`, `LUMEN_DEMO_NEW_PER_HOUR`; turn the demo off with `LUMEN_DEMO=` |
| A CDN or load balancer in front of Caddy | set Caddy's `trusted_proxies` to it; otherwise every visitor shares the CDN's address in rate limits and login protection |
| Prices and limits | `LUMEN_PLANS_FILE` (see `lumen.env.example`) and the matching Razorpay/Stripe plan |

**When to grow:** one app server handles hundreds of small organisations. Measured on 2 vCPUs, it took in
about 800 traces a second; a project's 24-hour dashboard stays fast up to about 50,000 traces a day. Past that, move to a
bigger instance first, then to Postgres with pre-computed roll-ups (planned).
