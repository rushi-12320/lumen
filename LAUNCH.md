# Lumen Cloud: launch guide

Lumen is now a public product: a website where anyone can sign up, a free plan, a paid Team plan billed by
Razorpay (India) or Stripe (abroad), teams with roles, and a separate database for every customer, all running
on AWS in Mumbai with our own GPU for approved LLM jobs. The self-hosted edition keeps working as before.

## How a customer uses Lumen

| Stage | What the customer does | What Lumen does |
|---|---|---|
| 1. Discover | Lands on the website, reads pricing, security and docs, opens the **live demo** (`/demo`). It starts on **Try it**: they chat with a sample bot and watch every answer get traced and checked | Each demo visitor gets a private copy with sample data, reset after 2 hours without use |
| 2. Sign up | Name, work email, company, password, accepts terms. No card | Creates their organisation with its own database, a first project, default alert rules; sends a confirmation email |
| 3. First trace (~5 min) | "Get started" page: creates an API key, runs `pip install` from your site and adds two lines (OpenAI, Anthropic and Gemini calls are then traced by themselves); or sets two OpenTelemetry variables (any language); or one `curl` | Shows the key once, waits live, says "First trace received" |
| 4. Daily use | Watches the console: traces, cost, latency, quality checks, alerts, incidents | Redacts personal data, scores every answer, opens incidents, the ops agent proposes fixes |
| 5. Team | Invites colleagues as member, admin or owner | Invitation emails; only admins and owners approve fixes, LLM jobs, keys and billing |
| 6. Approvals | An admin approves a rollback or an LLM job (judge, summary) | Runs it on our GPU in Mumbai, records who approved it in the audit ledger |
| 7. Release gate | Their CI calls `POST /v1/experiments` before shipping a prompt | Pass/fail against the baseline |
| 8. Upgrade | Starts a 14-day Team trial, then pays in ₹ (UPI, cards, net banking) or $ | Razorpay or Stripe checkout; plan switches on the signed webhook |
| 9. Limits | Gets emails at 80% and 100% of monthly traces | At 100% new traces get `429` until next month or an upgrade; their app keeps working |
| 10. Leave | Cancels (active until the period ends), exports all data, or deletes the organisation | Moves to Free; export = their full SQLite database; deletion erases it at once |
| 11. Enterprise | Needs it inside their network | Same product self-hosted; SSO on request |

## Plans (a proposal: change them in `LUMEN_PLANS_FILE` and in Razorpay/Stripe)

| | Free | Team | Enterprise |
|---|---|---|---|
| Price | ₹0 | ₹6,999/month + GST, or $79/month | Custom |
| Projects | 1 | 10 | Custom |
| Traces a month | 50,000 | 1,000,000 | Custom |
| Data kept | 14 days | 90 days | Custom |
| Members | 3 | 15 | Custom |
| LLM calls on our GPU | 50 | 2,000 | Custom |
| Extras | | Ops agent, approvals, 14-day free trial, email support | Self-host or dedicated, SSO on request, SLA, DPA |

## What it costs to run (per month)

**Start: about ₹1,200 + GST** on one small server in Mumbai (AWS Lightsail 2 GB, $12), with free email (Resend) and a
domain. Everything works except LLM jobs, which need a GPU. Step by step: `deploy/single/GO-LIVE.md`.

**With the GPU (when customers need LLM jobs):** about **$485 (≈ ₹42,700)** with the GPU always on, or **about $257
(≈ ₹22,600)** with the GPU on 08:00–21:00 IST Monday to Saturday. The GPU is most of it. About 7 Team customers
cover the always-on setup. Breakdown and assumptions: `deploy/aws/DEPLOY-AWS.md`.

## What only you can do (I can't create accounts or pay for you)

**Company and tax**
- [ ] A registered company (Private Limited or LLP), a current account and PAN, so Razorpay and invoices are in its name
- [ ] GST registration and invoicing: ask a chartered accountant (B2B SaaS, exports to foreign customers usually
      zero-rated under a Letter of Undertaking)
- [ ] A trademark search for "Lumen": the name is common, so check for conflicts before you spend on the brand,
      and choose a domain you can keep (set `LUMEN_BRAND` if you rename it)

**Legal**
- [ ] A lawyer reviews `/terms`, `/privacy`, `/refunds`, `/shipping` (drafts based on India's DPDP Act)
- [ ] Name a grievance officer; write a short incident-response plan (who does what if data leaks; DPDP requires
      telling the Data Protection Board and affected people)
- [ ] A Data Processing Agreement template for business customers who ask for one

**Accounts** (step by step in `deploy/aws/DEPLOY-AWS.md`)
- [ ] AWS: account with MFA and a budget alert, GPU quota (8 vCPUs of G instances in Mumbai), SES domain + production access
- [ ] Domain and DNS
- [ ] Razorpay: KYC, Subscriptions, International payments, plan, API keys, webhook
- [ ] Stripe: request an invite (invite-only for Indian businesses); until then foreign cards go through Razorpay

## Launch order

1. **Week 1:** accounts; put Lumen on one server (`deploy/single/GO-LIVE.md`, about an hour); test the whole
   customer journey in payment test mode. Share `/demo` with prospects: it starts on Try it.
2. **Week 2:** legal review done; SES approved; live payment keys; one real payment and refund. Invite 5–10
   friendly teams (design partners) to the Free plan and watch where they get stuck in `/admin`.
3. **Week 3 onwards:** public launch: LinkedIn posts, a technical blog post, Product Hunt and Hacker News, the GitHub
   repository for the self-hosted edition. Track sign-ups, first traces within a day, trials, paid conversions.

## Built and tested vs not yet

**Built and tested (42 automated tests, including 18 end-to-end Lumen Cloud flows, plus the real pages in a browser):**
Try it (sample bot in the demo and the console), the two-line Python setup (pip wheel served by Lumen; OpenAI, Anthropic
and Gemini calls traced by themselves, checked against each provider's real client library), OpenTelemetry intake
(protobuf and JSON, checked with the real OpenTelemetry exporter), the one-server install, website (home, pricing in ₹ and $,
docs, security, legal pages, contact, about), sign-up / login / email confirmation / password reset / lockout,
organisations with a separate database each, invitations and roles, onboarding to first trace, console with
account bar, plans and limits, monthly usage and warning emails, retention clean-up, GPU job quota, Razorpay and
Stripe checkout with verified webhooks, trial, cancel, data export, organisation deletion, admin page, public demo,
AWS stack (validated with cfn-lint, 0 findings), self-deploying release with backup and automatic rollback (tested
with stand-ins for AWS).

**Security reviews:** two independent reviews of accounts, tenant isolation, billing and the public demo found
13 issues, then 9 more. A third review of the new customer-facing code (Try it, the SDK, OpenTelemetry intake, the
one-server scripts) found 6 more and 6 smaller ones. All are fixed and each has a regression test. Among them:
- **Accounts and access:** console writes must be same-origin JSON; admins need a confirmed email; redirects stay on
  the site; wrong passwords are counted per email and address, so a stranger cannot lock someone out.
- **Billing:** one-time payment proofs; replay-proof, in-order webhooks; no second subscription while one can still
  charge; plans set by hand are never changed by payment events.
- **Usage and the public demo:** GPU quota reserved atomically; the public demo is bounded (per-address limit, size and
  age caps, no API keys); each organisation's database has its own write lock, so one customer cannot stall the
  others.
- **OpenTelemetry intake:** traces are counted when they are stored, so plan limits cannot be dodged; memory and
  work per request are bounded (traces, spans, values, nesting, decompressed size); attributes and error messages are
  redacted like prompts and answers; malformed input gets a 400, never a crash.

**Not yet (next, in this order):** two-factor login for users; Google sign-in; GSTIN captured at checkout on
invoices; a public status page; SSO/SAML for Enterprise; Postgres with pre-computed roll-ups when one project
passes ~50,000 traces a day. It has not yet run on a real AWS account or taken a real payment: do that in test mode first.

## Where things are

| Path | What |
|---|---|
| `lumen/cloud/` | Accounts, organisations, routing, plans, billing, email, website rendering |
| `site/pages/`, `site/assets/` | Website, legal pages, onboarding, settings, admin; console add-ons |
| `deploy/single/` | One-server install (about ₹1,200 a month): `install.sh`, Docker Compose, Caddy, backups, `GO-LIVE.md` |
| `deploy/aws/` | CloudFormation stack, Docker Compose, Caddy, settings reference, step-by-step guide |
| `lumen/playground.py`, `lumen/otlp.py`, `sdk/` | Try it; OpenTelemetry intake; the Python SDK (served as a wheel at `/sdk/`) |
| `tools/make_release.py` | Builds `dist/lumen.zip`, the file you upload to deploy |
| `tests/cloud_flows.py` | End-to-end tests of the cloud product |
