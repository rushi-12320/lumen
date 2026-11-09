"""Plans and their limits. Defaults below; override any of it with a JSON file at LUMEN_PLANS_FILE.

Prices are a starting proposal (Indian prices exclude 18% GST). Change them here or in the JSON file, and create
the matching plans in Razorpay / Stripe (their ids go in RAZORPAY_PLAN_TEAM_INR, RAZORPAY_PLAN_TEAM_USD,
STRIPE_PRICE_TEAM_USD)."""
import copy
import json
import os
from datetime import timedelta

from .control import now, parse

DEFAULT_PLANS = {
    "free": {
        "name": "Free", "tagline": "For one developer trying Lumen on a real app",
        "price": {"INR": 0, "USD": 0}, "projects": 1, "traces_per_month": 50_000, "retention_days": 14,
        "members": 3, "llm_calls_per_month": 50,
        "features": ["Tracing with cost and latency", "PII redaction before storage", "Online evaluators and alerts",
                     "Datasets, experiments and CI release gate", "Prompt registry", "Hash-chained audit ledger",
                     "50 approved LLM calls a month on our GPU"],
    },
    "team": {
        "name": "Team", "tagline": "For a team running an LLM app in production", "trial_days": 14,
        "price": {"INR": 6999, "USD": 79}, "projects": 10, "traces_per_month": 1_000_000, "retention_days": 90,
        "members": 15, "llm_calls_per_month": 2_000,
        "features": ["Everything in Free", "Ops agent with human approvals", "2,000 approved LLM calls a month",
                     "10 projects, 15 members", "90-day retention", "Email support, next business day"],
    },
    "enterprise": {
        "name": "Enterprise", "tagline": "For regulated companies and private deployments", "contact": True,
        "price": None, "projects": 1000, "traces_per_month": 50_000_000, "retention_days": 365, "members": 1000,
        "llm_calls_per_month": 50_000,
        "features": ["Everything in Team", "Self-hosted or dedicated cloud in India", "SSO (SAML / OIDC) on request",
                     "Custom retention and limits", "Security review support and DPA", "Support SLA"],
    },
}
ORDER = ("free", "team", "enterprise")


def plans():
    p = copy.deepcopy(DEFAULT_PLANS)
    f = os.environ.get("LUMEN_PLANS_FILE")
    if f and os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                p[k] = {**p.get(k, {}), **v}
    return p


def get(name):
    ps = plans()
    return ps.get(name) or ps["free"]


def effective(org):
    """(plan name, why) the organisation is on right now: a paid or granted plan, an active trial, else Free."""
    if not org:
        return "free", "no organisation"
    status = org.get("billing_status") or ""
    if org.get("plan") in ("team", "enterprise"):
        if status in ("", "active", "granted", "cancelling"):
            return org["plan"], (status or "granted")
        end = parse(org.get("current_period_end"))
        if status == "past_due" and end and end + timedelta(days=7) > now():
            return org["plan"], "payment overdue (7-day grace)"
    trial = parse(org.get("trial_ends_at"))
    if trial and trial > now():
        return "team", "trial"
    return "free", "free plan"


def limits(org):
    name, why = effective(org)
    p = get(name)
    return {"plan": name, "why": why, "name": p["name"], "projects": p["projects"], "members": p["members"],
            "traces_per_month": p["traces_per_month"], "retention_days": p["retention_days"],
            "llm_calls_per_month": p["llm_calls_per_month"]}
