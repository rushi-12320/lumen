"""Lumen Cloud: the public, multi-customer service (sign-up, teams, plans, billing) around the Lumen console.

Turn on with LUMEN_MODE=cloud. Off by default: Lumen stays the single-team, self-hosted tool.

* Every organisation (a customer's workspace) gets its OWN SQLite database file: data/tenants/<org id>.db.
  The console and the SDK API work unchanged inside it; nothing can query across customers.
* data/control.db holds the accounts: users, organisations, members, sessions, invites, usage, billing.
* lumen/cloud/middleware.py routes every request: the session cookie (console) or the X-Lumen-Key (SDK)
  picks the organisation, plan limits are checked, and the signed-in user's name signs every decision.
* LUMEN_DEMO=sandbox adds the public demo at /demo (each visitor a private throw-away copy).

All settings come from environment variables (see deploy/aws/lumen.env.example).
"""
import os
import secrets

ENABLED = os.environ.get("LUMEN_MODE", "").strip().lower() == "cloud"
DEMO = os.environ.get("LUMEN_DEMO", "").strip().lower() == "sandbox"


def _data_dir():
    from ..db import DB_PATH
    return os.environ.get("LUMEN_DATA_DIR") or os.path.dirname(os.path.abspath(DB_PATH))


def data_dir():
    d = _data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def public_url():
    return env("LUMEN_PUBLIC_URL", "http://127.0.0.1:8100").rstrip("/")


def secret_key():
    """Pepper for token hashes. LUMEN_SECRET_KEY in production; else one generated once and kept in the data dir."""
    k = env("LUMEN_SECRET_KEY")
    if k:
        return k.encode()
    p = os.path.join(data_dir(), "secret.key")
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(48))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    with open(p, encoding="utf-8") as f:
        return f.read().strip().encode()


def admin_emails():
    return {e.strip().lower() for e in env("LUMEN_ADMIN_EMAILS").split(",") if e.strip()}


def company():
    """Legal and contact details shown on the website and in emails (placeholders until set)."""
    return {
        "name": env("LUMEN_COMPANY_NAME", "[Company legal name]"),
        "brand": env("LUMEN_BRAND", "Lumen"),
        "address": env("LUMEN_COMPANY_ADDRESS", "[Registered office address]"),
        "email": env("LUMEN_SUPPORT_EMAIL", "[support email]"),
        "phone": env("LUMEN_SUPPORT_PHONE", "[support phone]"),
        "gstin": env("LUMEN_GSTIN", "[GSTIN]"),
        "grievance": env("LUMEN_GRIEVANCE_OFFICER", "[Grievance officer name, email]"),
        "jurisdiction": env("LUMEN_JURISDICTION", "[City], India"),
        "updated": env("LUMEN_POLICIES_UPDATED", "[date]"),
    }
