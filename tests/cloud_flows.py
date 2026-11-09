"""Lumen Cloud end to end (run by tests/test_cloud.py in its own process, because LUMEN_MODE is read at import).

Covers: website pages, sign-up / verification / login / reset, tenant isolation, roles, SDK keys and usage,
plan limits, the GPU quota, Razorpay + Stripe checkout and signed webhooks, the public demo, admin, export,
deletion and retention."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

D = tempfile.mkdtemp(prefix="lumen-cloud-")
os.environ.update(
    LUMEN_MODE="cloud", LUMEN_DEMO="sandbox", LUMEN_DB=f"{D}/lumen.db", LUMEN_DATA_DIR=D, LUMEN_MAIL_SYNC="1",
    LUMEN_PUBLIC_URL="http://testserver", LUMEN_ADMIN_EMAILS="admin@example.com", LUMEN_AUTH_PER_MIN="1000",
    LUMEN_COMPANY_NAME="Test Labs Pvt Ltd", LUMEN_SUPPORT_EMAIL="help@example.com",
    RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="rz_secret", RAZORPAY_WEBHOOK_SECRET="rz_hook", RAZORPAY_PLAN_TEAM_INR="plan_inr",
    STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_test", STRIPE_PRICE_TEAM_USD="price_usd",
    LUMEN_PLATFORM_LLM_URL="http://10.255.255.1:11434", LUMEN_PLATFORM_LLM_MODEL="llama3.1:8b")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import threading  # noqa: E402

from lumen import db, llm_gate, otlp, sandbox  # noqa: E402
from lumen.app import app  # noqa: E402
from lumen.cloud import billing, control, mailer, routes, server, tenants  # noqa: E402

O = {"Origin": "http://testserver"}


def new_client(ip="testclient"):
    c = TestClient(app, base_url="http://testserver", client=(ip, 50000))
    c.__enter__()
    return c


def signup(c, name, email, org="", **kw):
    r = c.post("/auth/signup", json={"name": name, "email": email, "password": "correct horse 42", "org": org, "accept": True, **kw}, headers=O)
    assert r.status_code == 200, r.text
    return r.json()


def link(email, path):
    subject, body = mailer.latest(email)
    m = re.search(r"http://testserver(" + re.escape(path) + r"\?token=[\w\-]+)", body or "")
    assert m, (subject, body)
    return m.group(1)


@pytest.fixture(scope="module")
def alice():
    c = new_client()
    out = signup(c, "Alice Rao", "alice@example.com", "Acme AI")
    assert out["next"] == "/app/onboarding"
    return c


def test_public_pages_render_without_placeholders_left():
    c = new_client()
    for p in ["/", "/pricing", "/docs", "/security", "/terms", "/privacy", "/refunds", "/shipping", "/contact", "/about",
              "/login", "/signup", "/forgot", "/reset", "/robots.txt", "/sitemap.xml", "/healthz"]:
        r = c.get(p)
        assert r.status_code == 200, p
        assert "{{" not in r.text, p
    home = c.get("/").text
    assert "₹6,999" in home and "$79" in home and "Start free" in home
    assert "Test Labs Pvt Ltd" in c.get("/terms").text and "Draft." in c.get("/terms").text
    assert c.get("/app", follow_redirects=False).headers["location"].startswith("/login")
    assert c.get("/sdk/lumen_sdk.py").text.startswith('"""Lumen Python SDK')
    docs = c.get("/docs").text                              # the website's documentation, not FastAPI's API explorer
    assert "Python SDK" in docs and "swagger" not in docs.lower() and "/sdk/lumen_llm_sdk-" in docs
    assert c.get("/openapi.json").status_code == 404


def test_cross_site_and_bad_input_are_refused():
    c = new_client()
    assert c.post("/auth/login", json={"email": "x@example.com", "password": "y"}).status_code == 403          # no Origin
    assert c.post("/auth/login", json={"email": "x@example.com", "password": "y"}, headers={"Origin": "http://evil.example"}).status_code == 403
    r = c.post("/auth/signup", json={"name": "A", "email": "a@example.com", "password": "short", "accept": True}, headers=O)
    assert r.status_code == 400 and "10 characters" in r.json()["detail"]
    r = c.post("/auth/signup", json={"name": "A", "email": "a@example.com", "password": "long enough pw", "accept": False}, headers=O)
    assert r.status_code == 400
    assert c.get("/api/projects").status_code == 401
    assert c.post("/auth/login", json={"email": "x@example.com", "password": "y"}, headers={"Origin": "https://testserver"}).status_code == 403
    r = c.post("/auth/signup", json={"name": "Security Team: call +1-555-0100", "email": "n@example.com", "password": "long enough pw",
                                     "accept": True}, headers=O)
    assert r.status_code == 400 and "letters" in r.json()["detail"]
    r = c.post("/auth/signup", json={"name": "Priya", "email": "n@example.com", "password": "long enough pw", "org": "Verify at http://evil",
                                     "accept": True}, headers=O)
    assert r.status_code == 400
    for bad in ["//evil.example", "/\\evil.example", "/\tevil", "https://evil.example"]:     # no open redirects after login
        signup_c = new_client()
        signup(signup_c, "R", f"r{abs(hash(bad))}@example.com")
        r = signup_c.post("/auth/login", json={"email": f"r{abs(hash(bad))}@example.com", "password": "correct horse 42", "next": bad}, headers=O)
        assert r.status_code == 200 and r.json()["next"] == "/app", (bad, r.json())


def test_signup_creates_an_isolated_workspace_and_verification(alice):
    me = alice.get("/api/me").json()
    assert me["role"] == "owner" and me["org"]["name"] == "Acme AI" and not me["user"]["verified"]
    assert len(me["projects"]) == 1 and me["usage"]["limits"]["plan"] == "free"
    ps = alice.get("/api/projects").json()
    assert [p["id"] for p in ps] == [me["projects"][0]["id"]]          # not the demo's projects
    llm = alice.get("/api/llm/config").json() if alice.get("/api/llm/config").status_code == 200 else None
    assert alice.get(link("alice@example.com", "/verify")).status_code == 200
    assert alice.get("/api/me").json()["user"]["verified"]
    page = alice.get("/app").text
    assert "LUMEN_CLOUD" in page and "console-cloud.js" in page and "alice@example.com" in page
    assert llm is None or llm.get("base_url", "").startswith("http://10.255.255.1")


def test_keys_traces_usage_and_signed_decisions(alice):
    pid = alice.get("/api/me").json()["projects"][0]["id"]
    r = alice.post("/api/keys", json={"project": pid, "name": "ci", "by": "Mallory"}, headers=O).json()
    assert r["ok"] and r["key"].startswith("lmn_")
    key = r["key"]
    led = alice.get("/api/ledger").json()
    rows = led if isinstance(led, list) else led.get("entries", led.get("items", []))
    assert any("alice@example.com" in json.dumps(e) for e in rows) and not any("Mallory" in json.dumps(e) for e in rows)
    sdk = new_client()
    t = sdk.post("/v1/traces", json={"traces": [{"name": "hello", "input": "Hi, mail me at bob@corp.com", "output": "Hello!"},
                                                {"name": "hello", "input": "Hi", "output": "Hello again"}]}, headers={"X-Lumen-Key": key})
    assert t.status_code == 200 and t.json()["added"] == 2
    assert sdk.post("/v1/traces", json={"traces": [{"name": "x"}]}, headers={"X-Lumen-Key": "lmn_nope"}).status_code == 401
    # console writes must be JSON objects: a plain HTML form (which skips the browser's cross-site checks) is refused
    form = {**O, "Content-Type": "application/x-www-form-urlencoded"}
    assert alice.post("/api/keys", content=b"project=x&name=y", headers=form).status_code == 415
    assert alice.post("/api/keys", content=b"[]", headers={**O, "Content-Type": "application/json"}).status_code == 400
    me = alice.get("/api/me").json()
    assert me["usage"]["traces"] == 2 and me["traces_total"] == 2
    tr = alice.get(f"/api/traces?project={pid}").json()
    assert tr["total"] == 2 and all("bob@corp.com" not in (i["input"] or "") for i in tr["items"])   # PII redacted
    pytest.KEY, pytest.PID = key, pid


def test_customers_cannot_see_each_other(alice):
    bob = new_client()
    signup(bob, "Bob Iyer", "bob@example.com", "Bobco")
    bp = bob.get("/api/projects").json()
    assert pytest.PID not in [p["id"] for p in bp]
    assert bob.get(f"/api/traces?project={pytest.PID}").json()["total"] == 0
    assert bob.get(f"/api/overview?project={pytest.PID}").status_code == 404
    assert pytest.PID not in json.dumps(bob.get("/api/events/recent").json())


def test_roles_invites_and_limits(alice):
    r = alice.post("/api/org/invites", json={"email": "carol@example.com", "role": "member"}, headers=O)
    assert r.status_code == 200, r.text
    inv = link("carol@example.com", "/invite")
    carol = new_client()
    assert "Join Acme AI" in carol.get(inv).text
    token = inv.split("token=")[1]
    signup(carol, "Carol D", "carol@example.com", invite=token)
    me = carol.get("/api/me").json()
    assert me["role"] == "member" and me["org"]["name"] == "Acme AI" and me["user"]["verified"]
    assert carol.post("/api/keys", json={"project": pytest.PID, "name": "x"}, headers=O).status_code == 403
    assert carol.post("/api/projects", json={"name": "x"}, headers=O).status_code == 403
    assert carol.get(f"/api/traces?project={pytest.PID}").json()["total"] == 2
    # the free plan: one project, three members (alice, carol + one invite)
    assert alice.post("/api/projects", json={"name": "Second"}, headers=O).status_code == 402
    assert alice.post("/api/org/invites", json={"email": "dan@example.com"}, headers=O).status_code == 200
    assert alice.post("/api/org/invites", json={"email": "erin@example.com"}, headers=O).status_code == 402
    # cloud-wide blocks
    assert alice.put("/api/llm/config", json={"changes": {"base_url": "http://evil:1"}, "by": "x"}, headers=O).status_code == 403
    assert alice.post("/api/sim/tick", json={"project": pytest.PID, "n": 5}, headers=O).status_code == 403
    assert alice.put("/api/policy", json={"changes": {"llm_allow_non_local": True}, "by": "x"}, headers=O).status_code == 403


def test_invitation_limits():
    gus = new_client()
    signup(gus, "Gus P", "gus@example.com", "Gusco")
    gus.get(link("gus@example.com", "/verify"))
    codes = [gus.post("/api/org/invites", json={"email": "target@example.com"}, headers=O).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]                        # the same person, at most 3 a day
    body = mailer.latest("target@example.com")[1]
    assert body.index("You have been invited") < body.index("Gusco")       # our words first, the team's name after
    old = routes.INVITES_PER_USER
    routes.INVITES_PER_USER = 4
    try:
        gus.request("DELETE", "/api/org", json={"confirm": "Gusco"}, headers=O)       # deleting the team does not reset it
        gus.post("/api/orgs", json={"name": "Gusco Two"}, headers=O)
        r = gus.post("/api/org/invites", json={"email": "someone@example.com"}, headers=O)
        assert r.status_code == 200, r.text
        assert gus.post("/api/org/invites", json={"email": "other@example.com"}, headers=O).status_code == 429
    finally:
        routes.INVITES_PER_USER = old


def test_monthly_trace_limit_and_warning_emails(alice):
    oid = alice.get("/api/me").json()["org"]["id"]
    with control.cx(True) as c:
        c.execute("UPDATE usage SET traces=40000 WHERE org_id=?", (oid,))
    sdk = new_client()
    r = sdk.post("/v1/traces", json={"traces": [{"name": "a", "input": "q", "output": "a"}]}, headers={"X-Lumen-Key": pytest.KEY})
    assert r.status_code == 200
    assert "80%" in mailer.latest("alice@example.com")[0]
    with control.cx(True) as c:
        c.execute("UPDATE usage SET traces=50000 WHERE org_id=?", (oid,))
    r = sdk.post("/v1/traces", json={"traces": [{"name": "a"}]}, headers={"X-Lumen-Key": pytest.KEY})
    assert r.status_code == 429 and "limit" in r.json()["detail"]
    with control.cx(True) as c:
        c.execute("UPDATE usage SET traces=3 WHERE org_id=?", (oid,))


def test_playground_and_opentelemetry_count_as_traces(alice):
    me = alice.get("/api/me").json()
    oid, before = me["org"]["id"], me["usage"]["traces"]
    ask = {"project": pytest.PID, "message": "How do I reset my password?", "variant": "leaky"}
    r = alice.post("/api/playground/chat", json=ask, headers=O)
    assert r.status_code == 200 and r.json()["added"] == 1 and "[EMAIL]" in r.json()["stored"]["output"]
    assert alice.get("/api/me").json()["usage"]["traces"] == before + 1
    assert next(p for p in alice.get("/api/projects").json() if p["id"] == pytest.PID)["playground"] == 1
    now = time.time_ns()
    otel = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]}, "scopeSpans": [{"spans": [
        {"traceId": "ab" * 16, "spanId": "cd" * 8, "name": "chat", "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 10 ** 8),
         "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                        {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}}]}]}]}]}
    sdk, h = new_client(), {"X-Lumen-Key": pytest.KEY, "Content-Type": "application/json"}
    r = sdk.post("/v1/otlp/v1/traces", content=json.dumps(otel), headers=h)
    assert r.status_code == 200 and r.headers["x-lumen-added"] == "1"
    assert alice.get("/api/me").json()["usage"]["traces"] == before + 2
    held = json.loads(json.dumps(otel))                 # a span whose root has not arrived: held, counted when stored
    held["resourceSpans"][0]["scopeSpans"][0]["spans"][0].update(traceId="cd" * 16, parentSpanId="ef" * 8)
    assert sdk.post("/v1/otlp/v1/traces", content=json.dumps(held), headers=h).headers["x-lumen-held"] == "1"
    assert alice.get("/api/me").json()["usage"]["traces"] == before + 2
    assert otlp.flush_stale(force=True) == 1
    assert alice.get("/api/me").json()["usage"]["traces"] == before + 3
    with control.cx(True) as c:
        c.execute("UPDATE usage SET traces=50000 WHERE org_id=?", (oid,))
    assert alice.post("/api/playground/chat", json=ask, headers=O).status_code == 429
    assert sdk.post("/v1/otlp/v1/traces", content=json.dumps(otel), headers=h).status_code == 429
    with control.cx(True) as c:
        c.execute("UPDATE usage SET traces=? WHERE org_id=?", (before + 3, oid))
    v = new_client("198.51.100.7")                          # a demo visitor: the playground works in their copy
    assert v.get("/demo").status_code == 200
    r = v.post("/demo/api/playground/chat", json={"project": "PRJ-SUPPORT", "message": "What is the API rate limit?"}, headers=O)
    assert r.status_code == 200 and r.json()["passed"]
    assert v.get("/demo/api/meta").json()["sdk"]["wheel"].startswith("/sdk/lumen_llm_sdk-")
    assert new_client().get("/sdk/").status_code == 200


def test_llm_jobs_need_approval_and_respect_the_quota(alice):
    oid = alice.get("/api/me").json()["org"]["id"]
    c = db.connect(tenants.path(oid))
    big = llm_gate.create_request(c, pytest.PID, "judge", "EXP-1", [f"prompt {i}" for i in range(60)], "alice")
    small = llm_gate.create_request(c, pytest.PID, "judge", "EXP-2", ["prompt a", "prompt b"], "alice")
    c.close()
    assert big["ok"] and small["ok"]
    r = alice.post(f"/api/llm/requests/{big['id']}/decision", json={"decision": "approve"}, headers=O)
    assert r.status_code == 402 and "left this month" in r.json()["detail"]
    r = alice.post(f"/api/llm/requests/{small['id']}/decision", json={"decision": "approve"}, headers=O)
    assert r.status_code == 200 and r.json().get("ok"), r.text
    assert alice.get("/api/me").json()["usage"]["llm_calls"] == 2
    again = alice.post(f"/api/llm/requests/{small['id']}/decision", json={"decision": "approve"}, headers=O)
    assert again.status_code == 400 and alice.get("/api/me").json()["usage"]["llm_calls"] == 2    # decided once, counted once
    results = []
    ts = [threading.Thread(target=lambda: results.append(control.reserve_llm("org_race", 40, 50)[0])) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with control.cx() as c:
        assert results.count(True) == 1 and c.execute("SELECT llm_calls FROM usage WHERE org_id='org_race'").fetchone()[0] == 40


def _rz_sig(body):
    return hmac.new(b"rz_hook", body, hashlib.sha256).hexdigest()


def _stripe_sig(body, t=None):
    t = t or int(time.time())
    return f"t={t},v1=" + hmac.new(b"whsec_test", f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def test_razorpay_checkout_verify_and_webhooks(alice, monkeypatch):
    b = alice.get("/api/billing").json()
    assert b["available"] == {"INR": "razorpay", "USD": "stripe"} and b["plan"] == "free"
    calls = []
    monkeypatch.setattr(billing, "_http", lambda m, u, h, d=None, timeout=25: calls.append((m, u, d)) or {"id": "sub_TEST1", "short_url": "https://rzp.io/x"})
    r = alice.post("/api/billing/checkout", json={"currency": "INR"}, headers=O)
    assert r.status_code == 200 and r.json()["subscription_id"] == "sub_TEST1", r.text
    assert json.loads(calls[0][2])["plan_id"] == "plan_inr"
    assert alice.get("/api/billing").json()["plan"] == "free"            # nothing changes until the payment is confirmed
    bad = alice.post("/api/billing/razorpay/verify", json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_TEST1",
                                                           "razorpay_signature": "0" * 64}, headers=O)
    assert bad.status_code == 400
    sig = hmac.new(b"rz_secret", b"pay_1|sub_TEST1", hashlib.sha256).hexdigest()
    ok = alice.post("/api/billing/razorpay/verify", json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_TEST1",
                                                          "razorpay_signature": sig}, headers=O)
    assert ok.status_code == 200 and alice.get("/api/billing").json()["plan"] == "team"
    assert alice.post("/api/projects", json={"name": "Second"}, headers=O).status_code == 200    # Team: 10 projects
    now = int(time.time())
    evt = json.dumps({"event": "subscription.charged", "created_at": now - 100,
                      "payload": {"subscription": {"entity": {"id": "sub_TEST1", "current_end": now + 30 * 86400}}}}).encode()
    hook = new_client()
    assert hook.post("/webhooks/razorpay", content=evt, headers={"X-Razorpay-Signature": "bad"}).status_code == 400
    r = hook.post("/webhooks/razorpay", content=evt, headers={"X-Razorpay-Signature": _rz_sig(evt), "X-Razorpay-Event-Id": "evt_1"})
    assert r.status_code == 200 and r.json()["ok"]
    assert hook.post("/webhooks/razorpay", content=evt, headers={"X-Razorpay-Signature": _rz_sig(evt), "X-Razorpay-Event-Id": "evt_1"}).json()["duplicate"]
    # the event-id header is not signed: a replay with a fresh header is still a duplicate
    assert hook.post("/webhooks/razorpay", content=evt, headers={"X-Razorpay-Signature": _rz_sig(evt), "X-Razorpay-Event-Id": "evt_x"}).json()["duplicate"]
    assert alice.get("/api/billing").json()["period_end"]
    end = json.dumps({"event": "subscription.cancelled", "created_at": now - 50, "payload": {"subscription": {"entity": {"id": "sub_TEST1"}}}}).encode()
    hook.post("/webhooks/razorpay", content=end, headers={"X-Razorpay-Signature": _rz_sig(end), "X-Razorpay-Event-Id": "evt_2"})
    assert alice.get("/api/billing").json()["plan"] == "free"
    assert "ended" in mailer.latest("alice@example.com")[0]
    # a late retry of an earlier charge, and a days-old event, cannot bring the plan back
    late = json.dumps({"event": "subscription.charged", "created_at": now - 80,
                       "payload": {"subscription": {"entity": {"id": "sub_TEST1", "current_end": now + 29 * 86400}}}}).encode()
    assert "older" in hook.post("/webhooks/razorpay", content=late, headers={"X-Razorpay-Signature": _rz_sig(late)}).json()["ignored"]
    old = json.dumps({"event": "subscription.activated", "created_at": now - 5 * 86400,
                      "payload": {"subscription": {"entity": {"id": "sub_TEST1"}}}}).encode()
    assert hook.post("/webhooks/razorpay", content=old, headers={"X-Razorpay-Signature": _rz_sig(old)}).json()["ignored"] == "event too old"
    # the browser's old proof of payment cannot be replayed to re-activate the ended plan
    again = alice.post("/api/billing/razorpay/verify", json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_TEST1",
                                                             "razorpay_signature": sig}, headers=O)
    assert again.status_code == 400 and alice.get("/api/billing").json()["plan"] == "free"


def test_razorpay_webhook_before_browser_confirm(monkeypatch):
    fay = new_client()
    signup(fay, "Fay M", "fay@example.com", "Fayco")
    fay.get(link("fay@example.com", "/verify"))
    oid = fay.get("/api/me").json()["org"]["id"]
    monkeypatch.setattr(billing, "_http", lambda m, u, h, d=None, timeout=25: {"id": "sub_FAY"})
    assert fay.post("/api/billing/checkout", json={"currency": "INR"}, headers=O).status_code == 200
    act = json.dumps({"event": "subscription.activated", "created_at": int(time.time()),
                      "payload": {"subscription": {"entity": {"id": "sub_FAY", "notes": {"org_id": oid}}}}}).encode()
    assert new_client().post("/webhooks/razorpay", content=act, headers={"X-Razorpay-Signature": _rz_sig(act)}).json()["ok"]
    assert fay.get("/api/billing").json()["plan"] == "team"
    sig = hmac.new(b"rz_secret", b"pay_F|sub_FAY", hashlib.sha256).hexdigest()
    r = fay.post("/api/billing/razorpay/verify", json={"razorpay_payment_id": "pay_F", "razorpay_subscription_id": "sub_FAY",
                                                       "razorpay_signature": sig}, headers=O)
    assert r.status_code == 200 and r.json()["plan"] == "team"
    # a paid organisation cannot start a second checkout
    assert fay.post("/api/billing/checkout", json={"currency": "INR"}, headers=O).status_code == 400


def test_stripe_checkout_and_webhooks(monkeypatch):
    dave = new_client()
    signup(dave, "Dave K", "dave@example.com", "Dave Inc")
    dave.get(link("dave@example.com", "/verify"))
    monkeypatch.setattr(billing, "_http", lambda m, u, h, d=None, timeout=25: {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})
    r = dave.post("/api/billing/checkout", json={"currency": "USD"}, headers=O)
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    oid = dave.get("/api/me").json()["org"]["id"]
    hook = new_client()
    done = json.dumps({"id": "evt_s1", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "client_reference_id": oid, "customer": "cus_1", "subscription": "sub_s1"}}}).encode()
    assert hook.post("/webhooks/stripe", content=done, headers={"Stripe-Signature": _stripe_sig(done, int(time.time()) - 3600)}).status_code == 400
    assert hook.post("/webhooks/stripe", content=done, headers={"Stripe-Signature": _stripe_sig(done)}).status_code == 200
    b = dave.get("/api/billing").json()
    assert b["plan"] == "team" and b["provider"] == "stripe"
    upd = json.dumps({"id": "evt_s2", "type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_s1", "customer": "cus_1", "status": "active", "cancel_at_period_end": True,
        "items": {"data": [{"current_period_end": int(time.time()) + 86400 * 20}]}}}}).encode()
    hook.post("/webhooks/stripe", content=upd, headers={"Stripe-Signature": _stripe_sig(upd)})
    b = dave.get("/api/billing").json()
    assert b["status"] == "cancelling" and b["plan"] == "team" and b["period_end"]
    gone = json.dumps({"id": "evt_s3", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_s1", "status": "canceled"}}}).encode()
    hook.post("/webhooks/stripe", content=gone, headers={"Stripe-Signature": _stripe_sig(gone)})
    assert dave.get("/api/billing").json()["plan"] == "free"


def test_no_double_billing_and_granted_plans_hold(monkeypatch):
    hal = new_client()
    signup(hal, "Hal V", "hal@example.com", "Halco")
    hal.get(link("hal@example.com", "/verify"))
    oid = hal.get("/api/me").json()["org"]["id"]
    long_ago = control.iso(control.now() - timedelta(days=20))
    with control.cx(True) as c:      # a Team plan whose payment failed weeks ago; Stripe is still retrying
        c.execute("UPDATE orgs SET plan='team', billing_status='past_due', billing_provider='stripe', billing_subscription='sub_old', "
                  "billing_customer='cus_h', current_period_end=? WHERE id=?", (long_ago, oid))
    b = hal.get("/api/billing").json()
    assert b["plan"] == "free" and b["live"] is True
    r = hal.post("/api/billing/checkout", json={"currency": "USD"}, headers=O)
    assert r.status_code == 400 and "Update card" in r.json()["detail"]
    # an admin grants Enterprise: the self-serve subscription is told to stop renewing
    calls = []
    monkeypatch.setattr(billing, "_http", lambda m, u, h, d=None, timeout=25: calls.append((m, u, d)) or {})
    import lumen.cloud.middleware as mw
    monkeypatch.setattr(mw, "admin_emails", lambda: ["ops2@example.com"])
    ops = new_client()
    signup(ops, "Ops Two", "ops2@example.com", "Ops2")
    ops.get(link("ops2@example.com", "/verify"))
    assert ops.post(f"/api/admin/orgs/{oid}", json={"plan": "enterprise"}, headers=O).status_code == 200
    assert any("/subscriptions/sub_old" in u for _, u, _ in calls)
    assert hal.get("/api/billing").json()["plan"] == "enterprise"
    hook = new_client()
    stray = json.dumps({"id": "evt_g1", "type": "customer.subscription.updated", "created": int(time.time()), "data": {"object": {
        "id": "sub_stray", "status": "incomplete_expired", "metadata": {"org_id": oid}}}}).encode()
    hook.post("/webhooks/stripe", content=stray, headers={"Stripe-Signature": _stripe_sig(stray)})
    renew = json.dumps({"id": "evt_g2", "type": "customer.subscription.updated", "created": int(time.time()), "data": {"object": {
        "id": "sub_old", "status": "active", "customer": "cus_h"}}}).encode()
    hook.post("/webhooks/stripe", content=renew, headers={"Stripe-Signature": _stripe_sig(renew)})
    late = json.dumps({"id": "evt_g3", "type": "checkout.session.completed", "created": int(time.time()), "data": {"object": {
        "mode": "subscription", "client_reference_id": oid, "customer": "cus_h", "subscription": "sub_late"}}}).encode()
    calls.clear()
    assert hook.post("/webhooks/stripe", content=late, headers={"Stripe-Signature": _stripe_sig(late)}).json()["ok"]
    assert hal.get("/api/billing").json()["plan"] == "enterprise"            # none of these changed the grant
    assert any("/subscriptions/sub_late" in u for _, u, _ in calls)           # the late payment will not renew
    assert "About your payment" in mailer.latest("hal@example.com")[0]


def test_a_second_subscription_never_replaces_the_live_one(monkeypatch):
    ivy = new_client()
    signup(ivy, "Ivy N", "ivy@example.com", "Ivyco")
    ivy.get(link("ivy@example.com", "/verify"))
    oid = ivy.get("/api/me").json()["org"]["id"]
    calls = []
    monkeypatch.setattr(billing, "_http", lambda m, u, h, d=None, timeout=25: calls.append((m, u, d)) or {"id": "sub_IVY1"})
    assert ivy.post("/api/billing/checkout", json={"currency": "INR"}, headers=O).status_code == 200
    sig = hmac.new(b"rz_secret", b"pay_I1|sub_IVY1", hashlib.sha256).hexdigest()
    assert ivy.post("/api/billing/razorpay/verify", json={"razorpay_payment_id": "pay_I1", "razorpay_subscription_id": "sub_IVY1",
                                                          "razorpay_signature": sig}, headers=O).json()["plan"] == "team"
    with control.cx(True) as c:          # a second checkout that was opened in another tab before the first was paid
        c.execute("UPDATE orgs SET pending_subscription='sub_IVY2' WHERE id=?", (oid,))
    calls.clear()
    act = json.dumps({"event": "subscription.activated", "created_at": int(time.time()),
                      "payload": {"subscription": {"entity": {"id": "sub_IVY2", "notes": {"org_id": oid}}}}}).encode()
    assert new_client().post("/webhooks/razorpay", content=act, headers={"X-Razorpay-Signature": _rz_sig(act)}).json()["ok"]
    with control.cx() as c:
        row = c.execute("SELECT plan, billing_status, billing_subscription FROM orgs WHERE id=?", (oid,)).fetchone()
    assert tuple(row) == ("team", "active", "sub_IVY1")                     # still billed once, on the first subscription
    assert any("/subscriptions/sub_IVY2/cancel" in u for _, u, _ in calls)  # the extra one will not renew
    assert "About your payment" in mailer.latest("ivy@example.com")[0]


def test_trial_password_reset_and_lockout():
    erin = new_client()
    signup(erin, "Erin", "erin2@example.com", plan="team")
    assert erin.get("/api/billing").json()["why"] == "trial"
    assert erin.post("/api/org/trial", headers=O).status_code == 400            # one trial only
    anon = new_client()
    anon.post("/auth/forgot", json={"email": "erin2@example.com"}, headers=O)
    tok = link("erin2@example.com", "/reset").split("token=")[1]
    assert anon.post("/auth/reset", json={"token": tok, "password": "brand new secret"}, headers=O).status_code == 200
    assert anon.post("/auth/reset", json={"token": tok, "password": "another secret 1"}, headers=O).status_code == 400   # used
    assert erin.get("/api/me").status_code == 401                                # old sessions ended
    x = new_client("203.0.113.9")
    for _ in range(8):
        assert x.post("/auth/login", json={"email": "erin2@example.com", "password": "wrong"}, headers=O).status_code == 401
    assert x.post("/auth/login", json={"email": "erin2@example.com", "password": "brand new secret"}, headers=O).status_code == 429
    # someone guessing from one address does not lock the real person out from theirs
    me = new_client("198.51.100.7")
    r = me.post("/auth/login", json={"email": "erin2@example.com", "password": "brand new secret"}, headers={**O, "X-Forwarded-For": "6.6.6.6"})
    assert r.status_code == 200
    with control.cx() as c:
        ips = {r[0] for r in c.execute("SELECT ip FROM sessions").fetchall()}
    assert "198.51.100.7" in ips and not any("6.6.6.6" in (ip or "") for ip in ips)     # the address uvicorn trusts, not a header
    # many wrong passwords from many addresses lock the account; after the lock ends, one mistake does not re-lock it
    with control.cx(True) as c:
        c.execute("UPDATE users SET failed_logins=30, locked_until=? WHERE email='erin2@example.com'",
                  (control.iso(control.now() - timedelta(minutes=1)),))
    assert new_client("192.0.2.1").post("/auth/login", json={"email": "erin2@example.com", "password": "wrong"}, headers=O).status_code == 401
    with control.cx() as c:
        assert tuple(c.execute("SELECT failed_logins, locked_until FROM users WHERE email='erin2@example.com'").fetchone()) == (1, None)
    assert new_client("192.0.2.2").post("/auth/login", json={"email": "erin2@example.com", "password": "brand new secret"}, headers=O).status_code == 200


def test_public_demo_is_separate():
    v = new_client()
    r = v.get("/demo/api/projects")                      # no copy yet: the API never creates one
    assert r.status_code == 409 and r.json()["reload"] == "/demo"
    page = v.get("/demo")
    assert page.status_code == 200 and "console-demo.js" in page.text
    assert "lumen_sandbox" in page.headers.get("set-cookie", "") and "httponly" in page.headers["set-cookie"].lower()
    ps = v.get("/demo/api/projects")
    assert ps.status_code == 200 and "PRJ-SUPPORT" in [p["id"] for p in ps.json()]
    assert v.get("/demo/api/demo").json()["demo"] is True
    assert v.post("/demo/api/llm/requests/X/decision", json={"decision": "approve", "by": "v"}, headers=O).status_code == 403
    assert "set-cookie" not in v.get("/demo").headers          # an existing copy is reused
    r = v.post("/demo/api/keys", json={"project": "PRJ-SUPPORT", "name": "x", "by": "v"}, headers=O)
    assert r.status_code == 403 and "Sign up" in r.json()["detail"]                 # no anonymous ingestion keys
    r = v.post("/demo/api/sim/tick", json={"project": "PRJ-SUPPORT", "n": 500}, headers=O)
    assert r.status_code == 200 and r.json()["added"] <= 50
    old_cap = sandbox.MAX_BYTES
    sandbox.MAX_BYTES = 1024
    try:
        assert v.post("/demo/api/sim/tick", json={"project": "PRJ-SUPPORT", "n": 1}, headers=O).status_code == 413
    finally:
        sandbox.MAX_BYTES = old_cap
    codes = []
    for _ in range(11):
        v.cookies.clear()
        codes.append(v.get("/demo").status_code)
    assert codes.count(429) >= 1 and codes[0] == 200          # at most 10 new copies per address per hour


def test_admin_export_delete_and_retention(alice):
    adm = new_client()
    signup(adm, "Admin", "admin@example.com", "Ops")
    assert adm.get("/admin").status_code == 404 and adm.get("/api/admin/orgs").status_code == 404   # not verified yet
    adm.get(link("admin@example.com", "/verify"))
    assert adm.get("/admin").status_code == 200
    data = adm.get("/api/admin/orgs").json()
    assert data["totals"]["orgs"] >= 4 and any(o["name"] == "Acme AI" for o in data["orgs"])
    assert alice.get("/api/admin/orgs").status_code == 404
    acme = next(o for o in data["orgs"] if o["name"] == "Acme AI")
    adm.post(f"/api/admin/orgs/{acme['id']}", json={"plan": "enterprise"}, headers=O)
    assert alice.get("/api/billing").json()["plan"] == "enterprise"
    r = alice.post("/api/billing/checkout", json={"currency": "INR"}, headers=O)
    assert r.status_code == 400 and "Enterprise" in r.json()["detail"]      # a granted plan is never replaced by a checkout
    exp = alice.get("/api/org/export")
    assert exp.status_code == 200 and exp.content.startswith(b"SQLite format 3")
    oid = alice.get("/api/me").json()["org"]["id"]
    c = sqlite3.connect(tenants.path(oid))
    c.execute("INSERT INTO traces(id, project_id, name, ts, status) VALUES('old1', ?, 'x', '2020-01-01T00:00:00+00:00', 'ok')", (pytest.PID,))
    c.commit()
    c.close()
    assert server.maintenance()["deleted"] >= 1
    sdk = new_client()
    r = sdk.post("/v1/traces", json={"traces": [
        {"id": "future1", "name": "f", "input": "q", "output": "a", "ts": "2099-01-01T00:00:00Z"},
        {"id": "huge1", "name": "h", "input": "q", "output": "a", "ts": 10 ** 400},
        {"id": "odd1", "name": "o", "input": "q", "output": "a", "ts": "not a time", "latency_ms": "slow", "tokens_in": "x",
         "metadata": "not a dict", "spans": [{"type": "llm", "input": "q", "output": "x" * 300_000}, "junk"]}]},
        headers={"X-Lumen-Key": pytest.KEY})
    assert r.status_code == 200 and r.json()["added"] == 3, r.text
    c = sqlite3.connect(tenants.path(oid))
    ts = dict(c.execute("SELECT id, ts FROM traces WHERE id IN ('future1', 'odd1')").fetchall())
    longest = c.execute("SELECT MAX(LENGTH(output)) FROM spans WHERE trace_id='odd1'").fetchone()[0]
    c.close()
    soon = time.time() + 600
    assert all(datetime.fromisoformat(v).timestamp() <= soon for v in ts.values()), ts      # cannot dodge retention
    assert longest < 201_000
    assert alice.request("DELETE", "/api/org", json={"confirm": "wrong"}, headers=O).status_code == 400
    r = alice.request("DELETE", "/api/org", json={"confirm": "Acme AI"}, headers=O)
    assert r.status_code == 200 and not tenants.exists(oid)
    sdk = new_client()
    assert sdk.post("/v1/traces", json={"traces": [{"name": "a"}]}, headers={"X-Lumen-Key": pytest.KEY}).status_code == 401
