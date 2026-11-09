"""Every request of Lumen Cloud passes here first.

  /api/*        console: the session cookie picks the user and organisation -> that organisation's database;
                roles, plan limits and the GPU quota are checked; the signed-in user's name signs every decision.
  /v1/*         SDK / CI: the X-Lumen-Key picks the organisation; monthly trace limit; usage metering.
  /demo/api/*   public demo: each visitor's private throw-away copy (LUMEN_DEMO=sandbox).
  /auth/*       sign-up, login ...: rate limited, same-origin only.
  /app, /admin  pages that need a signed-in user (else -> /login).
Everything else (website, docs, static files, payment webhooks) passes through.
"""
import json
import re
from urllib.parse import quote, urlparse

import anyio
from starlette.requests import Request

from .. import ingest, sandbox
from . import DEMO, admin_emails, env, public_url
from . import control, plans, security, tenants

WRITE = ("POST", "PUT", "PATCH", "DELETE")
ROLE_RULES = [  # (method, path, minimum role) for console actions
    ("POST", r"^/api/proposals/[^/]+/decision$", "admin"),
    ("POST", r"^/api/llm/requests/[^/]+/decision$", "admin"),
    ("POST", r"^/api/llm/test$", "admin"),
    ("PUT", r"^/api/policy$", "admin"),
    ("POST", r"^/api/policy/killswitch$", "admin"),
    ("PUT", r"^/api/pricing$", "admin"),
    ("POST", r"^/api/keys$", "admin"),
    ("POST", r"^/api/keys/[^/]+/revoke$", "admin"),
    ("POST", r"^/api/projects$", "admin"),
    ("POST", r"^/api/prompts/promote$", "admin"),
    ("POST", r"^/api/rules/[^/]+/toggle$", "admin"),      # switching alerting off is as serious as the kill switch
]
CLOUD_BLOCKED = [  # (method, path, why) for everyone on Lumen Cloud
    ("PUT", r"^/api/llm/config$", "The LLM endpoint is run by Lumen Cloud (our GPU server) and cannot be changed."),
    ("POST", r"^/api/sim/tick$", "Simulated traffic is only available in the demo."),
    ("GET", r"^/api/ledger/tamper-demo$", "The tamper demonstration is only available in the demo."),
    ("POST", r"^/api/demo/", "Not available in Lumen Cloud."),
]
CLOUD_ONLY = ("/api/me", "/api/org", "/api/billing", "/api/admin", "/api/orgs")
LOCKED_POLICY = {"llm_allow_non_local"}
AUTH_PER_MIN = int(env("LUMEN_AUTH_PER_MIN", "20"))   # sign-up / login / reset attempts per IP per minute
PLAY_PER_MIN = int(env("LUMEN_PLAYGROUND_PER_MIN", "20"))   # Try-it messages per person per minute
LIMITED = ("/v1/traces", "/v1/otlp/v1/traces")          # requests that add traces: refused once the monthly limit is reached


def client_ip(req):
    """uvicorn (--proxy-headers) already puts the address Caddy saw into req.client; never trust a header here."""
    return req.client.host if req.client else "?"


def same_origin(req):
    """The request comes from a page of this site: same scheme and host (an http:// page cannot write to https://)."""
    src = req.headers.get("origin") or req.headers.get("referer")
    if not src:
        return False
    u, pub = urlparse(src), urlparse(public_url())
    if pub.netloc and u.netloc.lower() == pub.netloc.lower():
        return u.scheme == pub.scheme
    return u.netloc.lower() == (req.headers.get("host") or "").lower() and u.scheme == req.url.scheme


def actor(user):
    return f"{user['name']} <{user['email']}>"


async def _json(send, status, detail, extra=None, headers=None):
    body = json.dumps({"detail": detail, **(extra or {})}).encode()
    hs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())] + (headers or [])
    await send({"type": "http.response.start", "status": status, "headers": hs})
    await send({"type": "http.response.body", "body": body})


async def _redirect(send, url):
    await send({"type": "http.response.start", "status": 303, "headers": [(b"location", url.encode()), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


def _sdk_org(key):
    """(organisation, traces used this month) for an SDK key, or None. Runs in a worker thread."""
    oid = control.org_for_key(ingest.hash_key(key))
    if not (oid and tenants.exists(oid)):
        return None
    with control.cx() as c:
        o = control.org(c, oid)
        return (o, control.usage(c, oid)["traces"]) if o else None


def _meter(o, added, lim):
    u = control.add_usage(o["id"], traces=added)
    _usage_warnings(o, u["traces"], lim)


def _traces_used(oid):
    with control.cx() as c:
        return control.usage(c, oid)["traces"]


def _added(state):
    """Traces a successful ingest response added: the X-Lumen-Added header (OpenTelemetry), else the JSON body."""
    h = state["headers"].get(b"x-lumen-added")
    try:
        return int(h) if h is not None else int(json.loads(state["body"] or b"{}").get("added", 0))
    except (ValueError, TypeError, AttributeError):
        return 0


async def _read_body(receive):
    chunks, more = [], True
    while more:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunks.append(msg.get("body", b""))
        more = msg.get("more_body", False)
        if sum(map(len, chunks)) > 8 * 1024 * 1024:
            break
    return b"".join(chunks)


def _replay(body, receive):
    done = False

    async def again():
        nonlocal done
        if not done:
            done = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return again


def _capture(send, keep):
    state = {"status": 0, "body": b"", "headers": {}}

    async def wrapped(msg):
        if msg["type"] == "http.response.start":
            state["status"] = msg["status"]
            state["headers"] = {k.lower(): v for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body" and keep and len(state["body"]) < 2_000_000:
            state["body"] += msg.get("body", b"")
        await send(msg)
    return wrapped, state


def _secure_headers(send, https):
    extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin"),
             (b"x-frame-options", b"DENY"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
    if https:
        extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))

    async def wrapped(msg):
        if msg["type"] == "http.response.start":
            msg = {**msg, "headers": list(msg.get("headers", [])) + extra}
        await send(msg)
    return wrapped


def attach(scope, raw, s, u):
    """Session details for the routes: request.state.cloud."""
    ctx = {"token": raw, "session": s, "user": u, "org": None, "role": None, "orgs": []}
    if u:
        with control.cx() as c:
            ctx["orgs"] = control.orgs_of(c, u["id"])
            oid = s.get("org_id") if s else None
            if not any(o["id"] == oid for o in ctx["orgs"]):
                oid = ctx["orgs"][0]["id"] if ctx["orgs"] else None
                if s:
                    c.execute("UPDATE sessions SET org_id=? WHERE token_hash=?", (oid, s["token_hash"]))
            if oid:
                ctx["org"] = control.org(c, oid)
                ctx["role"] = (control.membership(c, oid, u["id"]) or {}).get("role")
        ctx["admin"] = bool(u.get("email_verified_at")) and (u["email"] or "").lower() in admin_emails()
    scope.setdefault("state", {})["cloud"] = ctx
    return ctx


class CloudMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        req = Request(scope)
        https = (req.headers.get("x-forwarded-proto") or scope.get("scheme")) == "https"
        send = _secure_headers(send, https)
        path, method = scope["path"], scope["method"]
        if path.startswith("/demo/api/"):
            return await self._demo(scope, receive, send, req)
        if path.startswith("/v1/"):
            return await self._sdk(scope, receive, send, req)
        raw = req.cookies.get(security.SESSION_COOKIE)
        s, u = await anyio.to_thread.run_sync(security.session, raw) if raw else (None, None)
        if path.startswith("/api/"):
            return await self._console(scope, receive, send, req, raw, s, u)
        if path.startswith("/auth/"):
            if method in WRITE and not same_origin(req):
                return await _json(send, 403, "Cross-site request blocked.")
            if method in WRITE and not security.limiter.allow(f"auth:{client_ip(req)}", AUTH_PER_MIN, 60):
                return await _json(send, 429, "Too many attempts. Wait a minute and try again.")
        if path in ("/app", "/admin") or path.startswith("/app/"):
            if not u:
                return await _redirect(send, f"/login?next={quote(path)}")
        await anyio.to_thread.run_sync(attach, scope, raw, s, u)
        return await self.app(scope, receive, send)

    # ------------------------------------------------------------------ console API
    async def _console(self, scope, receive, send, req, raw, s, u):
        path, method = scope["path"], scope["method"]
        if not u:
            return await _json(send, 401, "Sign in first.", {"login": "/login"})
        if method in WRITE and not same_origin(req):
            return await _json(send, 403, "Cross-site request blocked.")
        ctx = await anyio.to_thread.run_sync(attach, scope, raw, s, u)
        o, role = ctx["org"], ctx["role"]
        if path.startswith(CLOUD_ONLY):
            if method in WRITE:
                if not security.limiter.allow(f"user:{u['id']}", 60, 60):
                    return await _json(send, 429, "Too many changes per minute. Wait a moment.")
                if o and o.get("suspended") and path.startswith("/api/org/invites") and not ctx.get("admin"):
                    return await _json(send, 403, "This organisation is suspended. Contact support.")
            if o and tenants.exists(o["id"]):
                tenants.activate(o["id"])
            return await self.app(scope, receive, send)
        if not o or not tenants.exists(o["id"]):
            return await _json(send, 409, "Create an organisation first.", {"onboarding": "/app/onboarding"})
        if o.get("suspended") and not ctx.get("admin"):
            return await _json(send, 403, "This organisation is suspended. Contact support.")
        tenants.activate(o["id"])
        for m, rx, why in CLOUD_BLOCKED:
            if method == m and re.search(rx, path):
                return await _json(send, 403, why)
        for m, rx, need in ROLE_RULES:
            if method == m and re.search(rx, path) and not control.role_at_least(role, need):
                return await _json(send, 403, f"Only an organisation {need} or owner can do this. Ask one in Settings > Team.")
        if method in WRITE and not security.limiter.allow(f"org:{o['id']}", 600, 60):
            return await _json(send, 429, "Too many changes per minute. Wait a moment.")
        lim = plans.limits(o)
        play = method == "POST" and path == "/api/playground/chat"      # Try-it traces count like any other trace
        if play:
            if not security.limiter.allow(f"play:{u['id']}", PLAY_PER_MIN, 60):
                return await _json(send, 429, "Too many messages per minute. Wait a moment.")
            if await anyio.to_thread.run_sync(_traces_used, o["id"]) >= lim["traces_per_month"]:
                return await _json(send, 429, f"Monthly trace limit reached ({lim['traces_per_month']:,} on the {lim['name']} plan).",
                                   {"upgrade": "/app/settings#billing"})
        if method == "POST" and path == "/api/projects" and len(await anyio.to_thread.run_sync(tenants.projects, o["id"])) >= lim["projects"]:
            return await _json(send, 402, f"The {lim['name']} plan includes {lim['projects']} project(s). "
                                          "Upgrade in Settings > Billing for more.", {"upgrade": "/app/settings#billing"})
        llm_calls = 0
        if method in WRITE:
            # every console write must be a JSON object: it is read here (limits, quota) and signed with the user
            if (req.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                return await _json(send, 415, "Send JSON (Content-Type: application/json).")
            body = await _read_body(receive)
            try:
                obj = json.loads(body or b"{}")
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                return await _json(send, 400, "The request body must be a JSON object.")
            if path == "/api/policy" and set((obj.get("changes") or {})) & LOCKED_POLICY:
                return await _json(send, 403, "The data-residency policy is fixed in Lumen Cloud.")
            if re.match(r"^/api/llm/requests/[^/]+/decision$", path) and obj.get("decision") != "reject":
                if not u.get("email_verified_at"):
                    return await _json(send, 403, "Verify your email address before running LLM jobs (see the link we sent).")
                llm_calls = await anyio.to_thread.run_sync(self._llm_calls, path.split("/")[4])
                ok, used = await anyio.to_thread.run_sync(control.reserve_llm, o["id"], llm_calls, lim["llm_calls_per_month"])
                if not ok:
                    left = max(0, lim["llm_calls_per_month"] - used)
                    return await _json(send, 402, f"This job needs {llm_calls} LLM calls; {left} of "
                                                  f"{lim['llm_calls_per_month']:,} are left this month on the {lim['name']} plan.",
                                       {"upgrade": "/app/settings#billing"})
            obj["by"] = actor(u)                     # decisions are signed by the signed-in person
            receive = _replay(json.dumps(obj).encode(), receive)
        wrapped, state = _capture(send, keep=bool(llm_calls) or play)
        try:
            await self.app(scope, receive, wrapped)
            if play and 200 <= state["status"] < 300 and _added(state):
                await anyio.to_thread.run_sync(_meter, o, _added(state), lim)
        finally:
            if llm_calls:                    # reserved above: give the calls back unless the job was really approved
                try:
                    approved = 200 <= state["status"] < 300 and bool(json.loads(state["body"] or b"{}").get("ok"))
                except ValueError:
                    approved = False
                if not approved:
                    await anyio.to_thread.run_sync(lambda: control.add_usage(o["id"], llm_calls=-llm_calls))

    @staticmethod
    def _llm_calls(rid):
        from .. import db
        c = db.connect()
        try:
            r = db.one(c, "SELECT n_calls FROM llm_requests WHERE id=?", (rid,))
            return int(r["n_calls"]) if r else 0
        finally:
            c.close()

    # ------------------------------------------------------------------ SDK / CI
    async def _sdk(self, scope, receive, send, req):
        key = req.headers.get("x-lumen-key")
        found = await anyio.to_thread.run_sync(_sdk_org, key) if key else None
        if found:
            o, used = found
            oid = o["id"]
            if o.get("suspended"):
                return await _json(send, 403, "This organisation is suspended. Contact support.")
            tenants.activate(oid)
            if not (scope["method"] == "POST" and scope["path"] in LIMITED):
                return await self.app(scope, receive, send)
            lim = plans.limits(o)
            if used >= lim["traces_per_month"]:
                return await _json(send, 429, f"Monthly trace limit reached ({lim['traces_per_month']:,} on the {lim['name']} plan). "
                                              f"Upgrade at {public_url()}/app/settings#billing", {"upgrade": True})
            if scope["path"] != "/v1/traces":         # OpenTelemetry: counted when stored (lumen/otlp.py, server._meter_otlp)
                return await self.app(scope, receive, send)
            wrapped, state = _capture(send, keep=True)
            await self.app(scope, receive, wrapped)
            if 200 <= state["status"] < 300:
                added = _added(state)
                if added:
                    await anyio.to_thread.run_sync(_meter, o, added, lim)
            return
        # the public demo has no SDK keys in Lumen Cloud: sending traces needs a (free) account
        return await _json(send, 401, "invalid or missing X-Lumen-Key")

    # ------------------------------------------------------------------ public demo
    async def _demo(self, scope, receive, send, req):
        if not DEMO:
            return await _json(send, 404, "The public demo is not enabled.")
        path, method = scope["path"][len("/demo"):], scope["method"]
        scope = {**scope, "path": path, "raw_path": path.encode()}
        sid = req.cookies.get(sandbox.COOKIE)
        if not sandbox.exists(sid):      # copies are made only by opening the /demo page (rate limited per address)
            return await _json(send, 409, "Your demo copy has expired. Reload the demo page.", {"reload": "/demo"})
        sandbox.activate(sid)
        sandbox.touch(sid)
        if method in WRITE:
            if not same_origin(req):
                return await _json(send, 403, "Cross-site request blocked.")
            if method == "POST" and path == "/api/keys":
                return await _json(send, 403, "API keys come with your own account. Sign up free to send traces from your app.")
            msg = sandbox.blocked(method, path)
            if msg:
                return await _json(send, 403, msg)
            if sandbox.full(sid):
                return await _json(send, 413, "This demo copy is full. Reload the demo for a fresh copy, or sign up free.")
            if not sandbox.allow_write(sid):
                return await _json(send, 429, "Too many changes per minute in the demo - wait a moment.")
        return await self.app(scope, receive, send)


def _usage_warnings(org, used, lim):
    cap = lim["traces_per_month"]
    for share, tag in ((1.0, "traces100"), (0.8, "traces80")):
        if used >= share * cap:
            if control.mark_notified(org["id"], tag):
                from .billing import owners
                from .mailer import send
                with control.cx() as c:
                    to = owners(c, org["id"])
                what = "reached" if share >= 1 else "used 80% of"
                for r in to:
                    send(r["email"], f"Lumen: you have {what} this month's traces",
                         [f"Hello {r['name']},", f"{org['name']} has {what} its {cap:,} traces for this month on the "
                          f"{lim['name']} plan ({used:,} so far)." + (" New traces are refused until the month ends or you upgrade."
                                                                      if share >= 1 else "")],
                         ("Upgrade or view usage", f"{public_url()}/app/settings#billing"))
            break
