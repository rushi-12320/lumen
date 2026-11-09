"""Billing with Razorpay (INR; also USD when your Razorpay account has international payments) and Stripe (USD).

Customers pay on the provider's own checkout: Lumen never sees card or bank details, it stores only the
provider's customer / subscription ids and the subscription status. Every webhook is verified with its signing
secret and applied once; events older than the last one applied to an organisation are ignored, because providers
may deliver them late or out of order."""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import company, env, public_url
from .control import audit, cx, iso, one, rows

RZP = "https://api.razorpay.com/v1"
STRIPE = "https://api.stripe.com/v1"
CURRENCIES = ("INR", "USD")


class BillingError(Exception):
    pass


def _http(method, url, headers, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode() or "{}")
        except ValueError:
            d = {}
        err = d.get("error") or {}
        raise BillingError(err.get("description") or err.get("message") or f"payment provider error (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise BillingError(f"cannot reach the payment provider: {e.reason}")


# ------------------------------------------------------------------ configuration
def razorpay_ready():
    return bool(env("RAZORPAY_KEY_ID") and env("RAZORPAY_KEY_SECRET"))


def stripe_ready():
    return bool(env("STRIPE_SECRET_KEY"))


def provider_for(currency):
    if currency == "INR" and razorpay_ready() and env("RAZORPAY_PLAN_TEAM_INR"):
        return "razorpay"
    if currency == "USD":
        if stripe_ready() and env("STRIPE_PRICE_TEAM_USD"):
            return "stripe"
        if razorpay_ready() and env("RAZORPAY_PLAN_TEAM_USD"):
            return "razorpay"
    return None


def available():
    return {cur: provider_for(cur) for cur in CURRENCIES}


def _unix_iso(v):
    try:
        return datetime.fromtimestamp(int(v), timezone.utc).replace(microsecond=0).isoformat() if v else None
    except (TypeError, ValueError, OSError):
        return None


def _set(c, org_id, actor, action, **fields):
    if fields:
        c.execute(f"UPDATE orgs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), org_id))
    audit(c, actor, org_id, action, fields)


def _org_by_sub(c, sub_id):
    return one(c, "SELECT * FROM orgs WHERE billing_subscription=?", (sub_id,)) if sub_id else None


ENDED = ("", "cancelled", "halted", "completed", "paused", "pending", "expired")
LIVE = ("active", "past_due", "cancelling")


def live_subscription(o):
    """True while a provider subscription can still charge this organisation."""
    return bool(o and o.get("billing_subscription")) and (o.get("billing_status") or "") in LIVE


def _granted(o):
    """A plan an admin set up by hand: payment events never change it."""
    return (o.get("billing_status") or "") == "granted"


def _stop_renewal(provider, sub_id):
    try:
        if provider == "razorpay":
            _rzp("POST", f"/subscriptions/{sub_id}/cancel", {"cancel_at_cycle_end": 1})
        elif provider == "stripe":
            _stripe("POST", f"/subscriptions/{sub_id}", {"cancel_at_period_end": True})
        return True
    except BillingError:
        return False


def _extra(o, sub_id):
    """Why a newly paid subscription is not the organisation's plan: 'granted' (an admin set the plan by hand), or
    'duplicate' (another subscription is still live, e.g. a checkout finished in a second tab). None: it is the plan."""
    if _granted(o):
        return "granted"
    return "duplicate" if live_subscription(o) and o.get("billing_subscription") != sub_id else None


def _extra_fields(o, provider, sub_id, why):
    # a granted organisation remembers the subscription (so its later events are recognised and ignored);
    # a duplicate never replaces the live subscription
    f = {"pending_subscription": None}
    if why == "granted":
        f.update(billing_provider=provider, billing_subscription=sub_id, cancel_at_period_end=1)
    return f


def _extra_subscription(provider, sub_id, why):
    """Keep the organisation's plan, stop the extra subscription renewing, and tell the owners."""
    stopped = _stop_renewal(provider, sub_id)
    have = "a plan arranged with us" if why == "granted" else "an active Team subscription"
    return ("About your payment", [f"We received a payment for the Team plan, but your organisation already has {have}, "
            "so your plan does not change.",
            "We stopped the extra subscription from renewing. Contact us for a refund of this payment." if stopped else
            "Please contact us so we can cancel the extra subscription and refund this payment."])


def _sub_org(c, sub_id, org_hint):
    """The organisation an event is about: the one whose current (or just-started) subscription it is. The org id
    in the provider's metadata is only used when that organisation has no live subscription of its own, so an
    event about an old subscription can never change the plan of a newer one."""
    o = _org_by_sub(c, sub_id) or (one(c, "SELECT * FROM orgs WHERE pending_subscription=?", (sub_id,)) if sub_id else None)
    if o:
        return o
    o = one(c, "SELECT * FROM orgs WHERE id=?", (org_hint,)) if org_hint else None
    return o if o and (not o.get("billing_subscription") or (o.get("billing_status") or "") in ENDED) else None


def _in_order(c, o, ts):
    """False for an event created before the last one applied to this organisation (a late retry)."""
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts and (o.get("billing_event_at") or 0) > ts:
        return False
    if ts:
        c.execute("UPDATE orgs SET billing_event_at=? WHERE id=?", (ts, o["id"]))
    return True


def _first_time(c, provider, event_id):
    if not event_id:
        return True
    if one(c, "SELECT 1 x FROM webhook_events WHERE provider=? AND event_id=?", (provider, event_id)):
        return False
    c.execute("INSERT INTO webhook_events VALUES(?,?,?)", (provider, event_id, iso()))
    return True


def owners(c, org_id):
    return rows(c, "SELECT u.email, u.name FROM members m JOIN users u ON u.id=m.user_id "
                   "WHERE m.org_id=? AND m.role IN ('owner','admin')", (org_id,))


# ------------------------------------------------------------------ Razorpay
def _rzp(method, path, body=None):
    auth = base64.b64encode(f"{env('RAZORPAY_KEY_ID')}:{env('RAZORPAY_KEY_SECRET')}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    return _http(method, RZP + path, {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}, data)


def razorpay_checkout(org, user, currency):
    sub = _rzp("POST", "/subscriptions", {
        "plan_id": env(f"RAZORPAY_PLAN_TEAM_{currency}"), "total_count": 120, "quantity": 1, "customer_notify": 1,
        "notes": {"org_id": org["id"], "org_name": org["name"][:200]}})
    with cx(True) as c:       # the plan changes only when this payment is confirmed
        _set(c, org["id"], f"user:{user['email']}", "BILLING_CHECKOUT_STARTED", pending_subscription=sub["id"],
             billing_currency=currency)
    return {"provider": "razorpay", "key_id": env("RAZORPAY_KEY_ID"), "subscription_id": sub["id"],
            "name": company()["brand"], "description": "Team plan, monthly",
            "prefill": {"name": user["name"], "email": user["email"]}}


def razorpay_signature_ok(payment_id, subscription_id, signature):
    """Checkout success: HMAC-SHA256(payment_id + '|' + subscription_id) with the API key secret."""
    if not (env("RAZORPAY_KEY_SECRET") and payment_id and subscription_id and signature):
        return False
    exp = hmac.new(env("RAZORPAY_KEY_SECRET").encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, signature)


def razorpay_confirm(org_id, payment_id, subscription_id, signature, actor):
    """The browser's proof of payment after Razorpay Checkout. It only completes the checkout this organisation
    started (pending_subscription), and each payment counts once, so an old proof cannot bring back an ended plan."""
    if not razorpay_signature_ok(payment_id, subscription_id, signature):
        raise BillingError("payment signature does not match")
    with cx(True) as c:
        o = one(c, "SELECT * FROM orgs WHERE id=?", (org_id,))
        if not o:
            raise BillingError("organisation not found")
        if o.get("pending_subscription") != subscription_id:
            if o.get("billing_subscription") == subscription_id and o.get("billing_status") in ("active", "cancelling"):
                return {"ok": True, "plan": o.get("plan") or "team"}       # the webhook got here first
            raise BillingError("this payment does not belong to a checkout in progress")
        if not _first_time(c, "rzp_payment", payment_id):
            raise BillingError("this payment was already used")
        why = _extra(o, subscription_id)
        if why:
            _set(c, org_id, actor, "BILLING_EXTRA_SUBSCRIPTION", **_extra_fields(o, "razorpay", subscription_id, why))
            to = owners(c, org_id)
        else:
            _set(c, org_id, actor, "BILLING_ACTIVATED", plan="team", billing_status="active", billing_provider="razorpay",
                 billing_subscription=subscription_id, pending_subscription=None, cancel_at_period_end=0)
            return {"ok": True, "plan": "team"}
    _notify(to, _extra_subscription("razorpay", subscription_id, why))
    return {"ok": True, "plan": o.get("plan"), "note": "Your organisation already has a plan, so nothing changed; we emailed you about this payment."}


def razorpay_webhook_ok(raw, signature):
    """Webhook: X-Razorpay-Signature = hex HMAC-SHA256 of the RAW body with the webhook secret."""
    secret = env("RAZORPAY_WEBHOOK_SECRET")
    if not (secret and signature):
        return False
    return hmac.compare_digest(hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), signature)


MAX_EVENT_AGE_S = 3 * 86400      # Razorpay retries failed deliveries for 24 hours


def razorpay_event(evt, body_hash):
    """`body_hash` = SHA-256 of the signed raw body. Razorpay's event-id header is not covered by the signature,
    so it cannot be trusted to spot a replayed event; the body (which carries the event's created_at) can."""
    kind = evt.get("event") or ""
    sub = ((evt.get("payload") or {}).get("subscription") or {}).get("entity") or {}
    if not kind.startswith("subscription."):
        return {"ignored": kind}
    created = evt.get("created_at")
    if isinstance(created, (int, float)) and created < time.time() - MAX_EVENT_AGE_S:
        return {"ignored": "event too old"}
    activates = kind in ("subscription.activated", "subscription.charged", "subscription.resumed")
    notify = stop = None
    with cx(True) as c:
        if not _first_time(c, "razorpay", body_hash):
            return {"duplicate": True}
        o = _sub_org(c, sub.get("id"), (sub.get("notes") or {}).get("org_id"))
        if not o:
            return {"ignored": "not the organisation's current subscription"}
        if sub.get("id") != o.get("billing_subscription") and not activates:
            return {"ignored": "subscription never became active"}     # e.g., an unpaid checkout was cancelled
        if not _in_order(c, o, created):
            return {"ignored": "older than the last event applied"}
        end = _unix_iso(sub.get("current_end"))
        why = _extra(o, sub.get("id")) if activates else ("granted" if _granted(o) else None)
        if why:
            if not (activates and sub.get("id") != o.get("billing_subscription")):
                audit(c, "razorpay", o["id"], "BILLING_EVENT_IGNORED", {"event": kind, "why": "granted plan"})
                return {"ignored": "the organisation has a granted plan"}
            _set(c, o["id"], "razorpay", "BILLING_EXTRA_SUBSCRIPTION", **_extra_fields(o, "razorpay", sub.get("id"), why))
            stop = ("razorpay", sub.get("id"), why)
        elif activates:
            cancelling = bool(o.get("cancel_at_period_end") and o.get("billing_subscription") == sub.get("id"))
            _set(c, o["id"], "razorpay", "BILLING_ACTIVE", plan="team", billing_provider="razorpay",
                 billing_status="cancelling" if cancelling else "active", cancel_at_period_end=int(cancelling),
                 billing_subscription=sub.get("id"), pending_subscription=None, current_period_end=end or o.get("current_period_end"))
            notify = ("Your Lumen Team plan is active", ["Thank you - your payment went through and the Team plan is active."]) \
                if kind == "subscription.activated" else None
        elif kind == "subscription.pending":
            _set(c, o["id"], "razorpay", "BILLING_PAST_DUE", billing_status="past_due")
            notify = ("Payment failed - action needed", ["We could not charge your Team plan. Razorpay will retry; "
                      "please check your payment method. You keep the Team plan for 7 days after the period ends."])
        elif kind in ("subscription.halted", "subscription.cancelled", "subscription.completed", "subscription.paused"):
            _set(c, o["id"], "razorpay", "BILLING_ENDED", plan="free", billing_status=kind.split(".")[1],
                 cancel_at_period_end=0, current_period_end=end)
            notify = ("Your Team plan has ended", ["Your organisation is on the Free plan now. Your data is kept; "
                      "anything older than the Free plan's retention will be deleted. Upgrade any time."])
        else:
            return {"ignored": kind}
        to = owners(c, o["id"])
    _notify(to, _extra_subscription(*stop) if stop else notify)
    return {"ok": True, "org": o["id"], "event": kind}


# ------------------------------------------------------------------ Stripe
def _form(d, prefix=""):
    out = []
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                out += _form(x, f"{key}[{i}]") if isinstance(x, dict) else [(f"{key}[{i}]", str(x))]
        elif v is not None:
            out.append((key, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


def _stripe(method, path, body=None):
    data = urllib.parse.urlencode(_form(body)).encode() if body is not None else None
    return _http(method, STRIPE + path, {"Authorization": f"Bearer {env('STRIPE_SECRET_KEY')}",
                                         "Content-Type": "application/x-www-form-urlencoded"}, data)


def stripe_checkout(org, user, currency):
    body = {"mode": "subscription", "line_items": [{"price": env(f"STRIPE_PRICE_TEAM_{currency}"), "quantity": 1}],
            "success_url": f"{public_url()}/app/settings?billing=success#billing",
            "cancel_url": f"{public_url()}/app/settings?billing=cancelled#billing",
            "client_reference_id": org["id"], "allow_promotion_codes": True,
            "metadata": {"org_id": org["id"]}, "subscription_data": {"metadata": {"org_id": org["id"]}}}
    if org.get("billing_provider") == "stripe" and org.get("billing_customer"):
        body["customer"] = org["billing_customer"]
    else:
        body["customer_email"] = user["email"]
    s = _stripe("POST", "/checkout/sessions", body)
    with cx(True) as c:
        _set(c, org["id"], f"user:{user['email']}", "BILLING_CHECKOUT_STARTED", billing_currency=currency)
    return {"provider": "stripe", "url": s["url"]}


def stripe_webhook_ok(raw, header, tolerance=300):
    """Stripe-Signature: t=<unix>,v1=<hex HMAC-SHA256 of '<t>.<raw body>' with the endpoint secret>."""
    secret = env("STRIPE_WEBHOOK_SECRET")
    if not (secret and header):
        return False
    parts = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs(time.time() - t) > tolerance:
        return False
    exp = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(exp, v) for v in parts.get("v1", []))


def _period_end(sub):
    if sub.get("current_period_end"):
        return _unix_iso(sub["current_period_end"])
    items = ((sub.get("items") or {}).get("data") or [])
    return _unix_iso(items[0].get("current_period_end")) if items else None


def stripe_event(evt):
    kind, obj = evt.get("type") or "", ((evt.get("data") or {}).get("object") or {})
    notify = stop = None
    ended = ("Your Team plan has ended", ["Your organisation is on the Free plan now. Your data is kept; "
             "anything older than the Free plan's retention will be deleted. Upgrade any time."])
    with cx(True) as c:
        if not _first_time(c, "stripe", evt.get("id")):
            return {"duplicate": True}
        if kind == "checkout.session.completed":
            oid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("org_id")
            o = one(c, "SELECT * FROM orgs WHERE id=?", (oid,))
            if not o or obj.get("mode") != "subscription":
                return {"ignored": "unknown organisation"}
            if not _in_order(c, o, evt.get("created")):
                return {"ignored": "older than the last event applied"}
            why = _extra(o, obj.get("subscription"))
            if why:
                _set(c, o["id"], "stripe", "BILLING_EXTRA_SUBSCRIPTION", **_extra_fields(o, "stripe", obj.get("subscription"), why))
                stop = ("stripe", obj.get("subscription"), why)
            else:
                _set(c, o["id"], "stripe", "BILLING_ACTIVE", plan="team", billing_status="active", billing_provider="stripe",
                     billing_customer=obj.get("customer"), billing_subscription=obj.get("subscription"), cancel_at_period_end=0)
                notify = ("Your Lumen Team plan is active", ["Thank you - your payment went through and the Team plan is active."])
        elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            o = _sub_org(c, obj.get("id"), (obj.get("metadata") or {}).get("org_id"))
            if not o:
                return {"ignored": "not the organisation's current subscription"}
            st, end = obj.get("status"), _period_end(obj)
            activating = not kind.endswith("deleted") and st in ("active", "trialing")
            if obj.get("id") != o.get("billing_subscription") and not activating:
                return {"ignored": "not the organisation's current subscription"}    # e.g., an abandoned checkout expired
            if not _in_order(c, o, evt.get("created")):
                return {"ignored": "older than the last event applied"}
            why = _extra(o, obj.get("id")) if activating else ("granted" if _granted(o) else None)
            if why:
                if not (activating and obj.get("id") != o.get("billing_subscription")):
                    audit(c, "stripe", o["id"], "BILLING_EVENT_IGNORED", {"event": kind, "why": "granted plan"})
                    return {"ignored": "the organisation has a granted plan"}
                _set(c, o["id"], "stripe", "BILLING_EXTRA_SUBSCRIPTION", **_extra_fields(o, "stripe", obj.get("id"), why))
                stop = ("stripe", obj.get("id"), why)
            elif kind.endswith("deleted") or st in ("canceled", "incomplete_expired"):
                _set(c, o["id"], "stripe", "BILLING_ENDED", plan="free", billing_status="cancelled", cancel_at_period_end=0, current_period_end=end)
                notify = ended
            elif st in ("past_due", "unpaid"):
                _set(c, o["id"], "stripe", "BILLING_PAST_DUE", billing_status="past_due", current_period_end=end)
            elif activating:
                cancelling = bool(obj.get("cancel_at_period_end"))
                _set(c, o["id"], "stripe", "BILLING_ACTIVE", plan="team", billing_status="cancelling" if cancelling else "active",
                     billing_provider="stripe", billing_subscription=obj.get("id"), billing_customer=obj.get("customer"),
                     cancel_at_period_end=int(cancelling), current_period_end=end)
            else:
                return {"ignored": st}
        elif kind == "invoice.payment_failed":
            o = _org_by_sub(c, obj.get("subscription"))
            if not o or _granted(o) or not _in_order(c, o, evt.get("created")):
                return {"ignored": "not the organisation's current subscription, a granted plan, or an old event"}
            _set(c, o["id"], "stripe", "BILLING_PAYMENT_FAILED", billing_status="past_due")
            notify = ("Payment failed - action needed", ["We could not charge your Team plan. Please update your card "
                      "under Settings > Billing. You keep the Team plan for 7 days after the period ends."])
        else:
            return {"ignored": kind}
        to = owners(c, o["id"])
    _notify(to, _extra_subscription(*stop) if stop else notify)
    return {"ok": True, "org": o["id"], "event": kind}


# ------------------------------------------------------------------ both
def checkout(org, user, currency):
    p = provider_for(currency)
    if not p:
        raise BillingError(f"Paying in {currency} is not set up yet. Contact {company()['email']}.")
    return razorpay_checkout(org, user, currency) if p == "razorpay" else stripe_checkout(org, user, currency)


def cancel(org, actor):
    sub, p = org.get("billing_subscription"), org.get("billing_provider")
    if not sub or org.get("billing_status") not in ("active", "past_due"):
        raise BillingError("there is no active subscription to cancel")
    if p == "razorpay":
        _rzp("POST", f"/subscriptions/{sub}/cancel", {"cancel_at_cycle_end": 1})
    elif p == "stripe":
        _stripe("POST", f"/subscriptions/{sub}", {"cancel_at_period_end": True})
    with cx(True) as c:
        _set(c, org["id"], actor, "BILLING_CANCEL_REQUESTED", billing_status="cancelling", cancel_at_period_end=1)
    return {"ok": True, "note": "The Team plan stays active until the end of the paid period."}


def manage_url(org):
    """Where the customer updates the card / sees invoices: Stripe's portal, or Razorpay's subscription page."""
    sub, p = org.get("billing_subscription"), org.get("billing_provider")
    if p == "stripe" and org.get("billing_customer"):
        return _stripe("POST", "/billing_portal/sessions", {"customer": org["billing_customer"],
                                                            "return_url": f"{public_url()}/app/settings#billing"})["url"]
    if p == "razorpay" and sub:
        return _rzp("GET", f"/subscriptions/{sub}").get("short_url")
    return None


def _notify(to, msg):
    if not (to and msg):
        return
    from .mailer import send
    for r in to:
        send(r["email"], msg[0], [f"Hello {r['name']},"] + msg[1], ("Open billing", f"{public_url()}/app/settings#billing"))
