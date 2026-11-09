"""Lumen Cloud endpoints: website pages, sign-up / login, organisations and teams, billing, payment webhooks, admin."""
import hashlib
import json
import os
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import company, public_url
from . import billing, control, mailer, plans, security, site, tenants
from .control import cx, iso, now, one, parse, rows

router = APIRouter()
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATIC = os.path.join(ROOT, "static")


# ------------------------------------------------------------------ helpers
def ctx(request):
    return getattr(request.state, "cloud", None) or {}


def need_user(request):
    c = ctx(request)
    if not c.get("user"):
        raise HTTPException(401, "Sign in first.")
    return c


def need_org(request, role="member"):
    c = need_user(request)
    if not c.get("org"):
        raise HTTPException(409, "Create an organisation first.")
    if not control.role_at_least(c.get("role"), role):
        raise HTTPException(403, f"Only an organisation {role} or owner can do this.")
    return c


def need_verified(c):
    if not c["user"].get("email_verified_at"):
        raise HTTPException(403, "Verify your email address first (we sent you a link; you can resend it in Settings).")


def _https(request):
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https" or public_url().startswith("https://")


def _login_response(request, user_id, org_id, payload):
    from .middleware import client_ip
    raw = security.create_session(user_id, org_id, client_ip(request), request.headers.get("user-agent"))
    r = JSONResponse(payload)
    r.set_cookie(security.SESSION_COOKIE, raw, max_age=security.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                 secure=_https(request), path="/")
    return r


def _safe_next(n, default="/app"):
    """Only a path on this site: no scheme or host, no '//' or backslash tricks, no control characters."""
    from urllib.parse import urlsplit
    if not isinstance(n, str) or not n.startswith("/") or n.startswith("//") or "\\" in n or any(ord(ch) < 32 for ch in n):
        return default
    u = urlsplit(n)
    return default if (u.scheme or u.netloc) else n


def _new_org(c, name, owner, trial=False, oid=None):
    """Rows for a new organisation. Create its database first with tenants.create(oid, ...) (outside the transaction)."""
    oid = oid or control.new_id("org")
    if not tenants.exists(oid):
        tenants.create(oid, "My first project", f"{owner['name']} <{owner['email']}>")
    trial_end = iso(now() + timedelta(days=plans.get("team").get("trial_days", 14))) if trial else None
    c.execute("INSERT INTO orgs(id, name, plan, trial_ends_at, created_at, owner_id) VALUES(?,?,?,?,?,?)",
              (oid, name.strip()[:80], "free", trial_end, iso(), owner["id"]))
    c.execute("INSERT INTO members VALUES(?,?,?,?)", (oid, owner["id"], "owner", iso()))
    control.audit(c, f"user:{owner['email']}", oid, "ORG_CREATED", {"name": name, "trial": bool(trial)})
    return oid


def _send_verification(u):
    t = security.create_token("verify", 72, user_id=u["id"], email=u["email"])
    mailer.send(u["email"], f"Confirm your email for {company()['brand']}",
                [f"Hello {u['name']},", "Please confirm this is your email address. The link works for 3 days."],
                ("Confirm my email", f"{public_url()}/verify?token={t}"))


def _usage_view(o):
    lim = plans.limits(o)
    with cx() as c:
        u = control.usage(c, o["id"])
    return {"month": u["month"], "traces": u["traces"], "llm_calls": u["llm_calls"], "limits": lim}


# ------------------------------------------------------------------ website pages
PAGES = {
    "/pricing": ("pricing", "Pricing", "Free, Team and Enterprise plans. Prices in rupees and dollars."),
    "/docs": ("docs", "Documentation", "Quickstart, SDK, REST API, evaluations, approvals and self-hosting."),
    "/security": ("security", "Security", "How Lumen Cloud keeps your data apart, encrypted, backed up and in India."),
    "/terms": ("terms", "Terms of service", ""), "/privacy": ("privacy", "Privacy policy", ""),
    "/refunds": ("refunds", "Cancellation and refund policy", ""), "/shipping": ("shipping", "Delivery policy", ""),
    "/contact": ("contact", "Contact", ""), "/about": ("about", "About", ""),
    "/login": ("login", "Log in", ""), "/signup": ("signup", "Start free", ""), "/forgot": ("forgot", "Reset your password", ""),
    "/reset": ("reset", "Choose a new password", ""),
}


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return site.page("index", "", "", ctx(request))


def _page_route(path, name, title, desc):
    def handler(request: Request):
        return site.page(name, title, desc, ctx(request))
    router.add_api_route(path, handler, methods=["GET"], response_class=HTMLResponse, name=f"page_{name}")


for _p, (_n, _t, _d) in PAGES.items():
    _page_route(_p, _n, _t, _d)


@router.get("/verify", response_class=HTMLResponse)
def verify(request: Request, token: str = ""):
    t = security.use_token("verify", token)
    if t:
        with cx(True) as c:
            c.execute("UPDATE users SET email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?", (iso(), t["user_id"]))
        msg, ok = "Your email address is confirmed. Thank you!", True
    else:
        msg, ok = "This link is invalid or has expired. Sign in and resend it from Settings.", False
    return site.page("message", "Email confirmation", "", ctx(request),
                     {"heading": "Email confirmed" if ok else "Link not valid", "message": msg,
                      "link": "/app", "link_text": "Open the console"})


@router.get("/invite", response_class=HTMLResponse)
def invite_page(request: Request, token: str = ""):
    t = security.use_token("invite", token, consume=False)
    if not t:
        return site.page("message", "Invitation", "", ctx(request),
                         {"heading": "Invitation not valid", "message": "This invitation is invalid, used or expired. "
                          "Ask your colleague to send a new one.", "link": "/", "link_text": "Home"})
    with cx() as c:
        o = control.org(c, t["org_id"])
    return site.page("invite", "Join your team", "", ctx(request),
                     {"org_name": o["name"] if o else "your team", "invite_email": t["email"], "invite_token": token})


@router.get("/healthz")
def healthz():
    with cx() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return f"User-agent: *\nDisallow: /app\nDisallow: /api\nDisallow: /admin\nSitemap: {public_url()}/sitemap.xml\n"


@router.get("/sitemap.xml")
def sitemap():
    urls = "".join(f"<url><loc>{public_url()}{p}</loc></url>" for p in ["/", "/pricing", "/docs", "/security", "/demo",
                                                                         "/about", "/contact", "/terms", "/privacy", "/refunds", "/shipping"])
    return HTMLResponse(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                        media_type="application/xml")


# ------------------------------------------------------------------ console pages
def _console_html(head, body):
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        doc = f.read()
    doc = doc.replace('<link rel="stylesheet" href="/static/styles.css">', '<link rel="stylesheet" href="/static/styles.css">' + head)
    return doc.replace('<script src="/static/app.js"></script>', body + '<script src="/static/app.js"></script>')


@router.get("/app", response_class=HTMLResponse)
def console(request: Request):
    c = need_user(request)
    if not c.get("org"):
        return RedirectResponse("/app/onboarding", 303)
    o = c["org"]
    boot = {"user": {"name": c["user"]["name"], "email": c["user"]["email"], "verified": bool(c["user"].get("email_verified_at"))},
            "org": {"id": o["id"], "name": o["name"]}, "role": c["role"], "orgs": c["orgs"], "admin": c.get("admin", False),
            "usage": _usage_view(o)}
    data = json.dumps(boot).replace("</", "<\\/")
    return HTMLResponse(_console_html('<link rel="stylesheet" href="/site/console.css">',
                                      f'<script>window.LUMEN_CLOUD={data};</script><script src="/site/console-cloud.js"></script>'))


@router.get("/demo", response_class=HTMLResponse)
def demo(request: Request):
    from . import DEMO
    if not DEMO:
        return site.page("message", "Demo", "", ctx(request), {"heading": "The live demo is off", "message":
                         "Sign up free to try Lumen on your own app.", "link": "/signup", "link_text": "Start free"})
    from .. import sandbox
    from .middleware import client_ip
    r = HTMLResponse(_console_html('<link rel="stylesheet" href="/site/console.css">', '<script src="/site/console-demo.js"></script>'),
                     headers={"Cache-Control": "no-store"})        # the page is what makes a visitor's copy
    if not sandbox.exists(request.cookies.get(sandbox.COOKIE)):
        if not security.limiter.allow(f"demo:{client_ip(request)}", 10, 3600):
            return site.page("message", "Demo", "", ctx(request), {"heading": "Too many demo copies", "message":
                             "This address opened many demo copies in the last hour. Try again later, or sign up free.",
                             "link": "/signup", "link_text": "Start free"}, status=429)
        r.set_cookie(sandbox.COOKIE, sandbox.create(), max_age=sandbox.TTL_S, httponly=True, samesite="lax", secure=_https(request))
    return r


@router.get("/app/onboarding", response_class=HTMLResponse)
def onboarding(request: Request):
    return site.page("onboarding", "Get started", "", ctx(request), app=True)


@router.get("/app/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return site.page("settings", "Settings", "", ctx(request), app=True)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not need_user(request).get("admin"):
        raise HTTPException(404, "Not found")
    return site.page("admin", "Admin", "", ctx(request), app=True)


# ------------------------------------------------------------------ sign-up, login, passwords
class SignupIn(BaseModel):
    name: str
    email: str
    password: str
    org: str = ""
    accept: bool = False
    invite: str = ""
    plan: str = ""


@router.post("/auth/signup")
def signup(body: SignupIn, request: Request):
    name, email = security.clean_name(body.name), body.email.strip().lower()
    problem = security.name_problem(name, "Name") or (security.name_problem(body.org, "Team name") if body.org.strip() else None)
    if problem:
        raise HTTPException(400, problem)
    if not security.valid_email(email):
        raise HTTPException(400, "Please enter a valid email address.")
    problem = security.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    if not body.accept:
        raise HTTPException(400, "Please accept the terms of service and privacy policy.")
    inv = security.use_token("invite", body.invite, consume=False) if body.invite else None
    if body.invite and not inv:
        raise HTTPException(400, "This invitation is invalid or has expired.")
    with cx() as c:
        if control.user_by_email(c, email):
            raise HTTPException(409, "An account with this email already exists. Log in instead.")
    joining = bool(inv and inv["email"] == email)
    uid, pw_hash = control.new_id("usr"), security.hash_password(body.password)
    new_oid = None if joining else control.new_id("org")
    if new_oid:
        tenants.create(new_oid, "My first project", f"{name} <{email}>")
    try:
        with cx(True) as c:
            if control.user_by_email(c, email):
                raise HTTPException(409, "An account with this email already exists. Log in instead.")
            verified = iso() if joining else None      # the invite link proved the address
            c.execute("INSERT INTO users(id, email, name, pw_hash, email_verified_at, created_at) VALUES(?,?,?,?,?,?)",
                      (uid, email, name, pw_hash, verified, iso()))
            u = control.user(c, uid)
            control.audit(c, f"user:{email}", None, "USER_SIGNED_UP", {"invite": joining})
            if joining:            # still unused and not revoked at this moment
                if c.execute("UPDATE tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                             (iso(), inv["token_hash"], iso())).rowcount != 1:
                    raise HTTPException(400, "This invitation is invalid or has expired.")
                c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?,?)", (inv["org_id"], uid, inv["role"] or "member", iso()))
                oid, nxt = inv["org_id"], "/app"
            else:
                oid = _new_org(c, security.clean_name(body.org) or f"{name}'s team", u, trial=body.plan == "team", oid=new_oid)
                nxt = "/app/onboarding"
    except Exception:
        if new_oid:
            tenants.delete(new_oid)
        raise
    if not verified:
        _send_verification(u)
    return _login_response(request, uid, oid, {"ok": True, "next": nxt})


FAILS_PER_ADDRESS = 8      # wrong passwords for one email from one address before that address waits 15 minutes
FAILS_PER_ACCOUNT = 30     # from all addresses together before the account itself is locked for 15 minutes


class LoginIn(BaseModel):
    email: str
    password: str
    next: str = ""


@router.post("/auth/login")
def login(body: LoginIn, request: Request):
    from .middleware import client_ip
    email = body.email.strip().lower()
    pair = f"loginfail:{email}|{client_ip(request)}"
    if security.limiter.count(pair, 900) >= FAILS_PER_ADDRESS:
        raise HTTPException(429, "Too many wrong passwords. Try again in 15 minutes or reset your password.")
    with cx() as c:
        u = control.user_by_email(c, email)
    if u and parse(u.get("locked_until")) and parse(u["locked_until"]) > now():
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes or reset your password.")
    if not u or not security.verify_password(body.password, u["pw_hash"]):
        security.limiter.hit(pair)
        if u:
            t, until = iso(), iso(now() + timedelta(minutes=15))
            with cx(True) as c:          # one atomic step, so parallel guesses all count; an expired lock starts over
                c.execute("UPDATE users SET failed_logins=CASE WHEN locked_until<=? THEN 1 ELSE COALESCE(failed_logins,0)+1 END, "
                          "locked_until=CASE WHEN (CASE WHEN locked_until<=? THEN 1 ELSE COALESCE(failed_logins,0)+1 END)>=? THEN ? "
                          "WHEN locked_until<=? THEN NULL ELSE locked_until END WHERE id=?",
                          (t, t, FAILS_PER_ACCOUNT, until, t, u["id"]))
        raise HTTPException(401, "Email or password is wrong.")
    with cx() as c:
        orgs = control.orgs_of(c, u["id"])
    return _login_response(request, u["id"], orgs[0]["id"] if orgs else None,
                           {"ok": True, "next": _safe_next(body.next, "/app" if orgs else "/app/onboarding")})


@router.post("/auth/logout")
def logout(request: Request):
    raw = request.cookies.get(security.SESSION_COOKIE)
    if raw:
        security.end_session(raw)
    r = JSONResponse({"ok": True, "next": "/"})
    r.delete_cookie(security.SESSION_COOKIE, path="/")
    return r


class EmailIn(BaseModel):
    email: str


@router.post("/auth/forgot")
def forgot(body: EmailIn):
    with cx() as c:
        u = control.user_by_email(c, body.email)
    if u and security.limiter.allow(f"forgot:{u['id']}", 3, 3600):
        t = security.create_token("reset", 1, user_id=u["id"], email=u["email"])
        mailer.send(u["email"], "Reset your password", [f"Hello {u['name']},", "Someone (hopefully you) asked to reset "
                    "your password. The link works for one hour. If it was not you, ignore this email."],
                    ("Choose a new password", f"{public_url()}/reset?token={t}"))
    return {"ok": True, "note": "If an account exists for this email, we sent a reset link."}


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/auth/reset")
def reset(body: ResetIn, request: Request):
    problem = security.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    t = security.use_token("reset", body.token)
    if not t:
        raise HTTPException(400, "This reset link is invalid or has expired. Ask for a new one.")
    pw_hash = security.hash_password(body.password)
    with cx(True) as c:
        c.execute("UPDATE users SET pw_hash=?, failed_logins=0, locked_until=NULL, email_verified_at=COALESCE(email_verified_at, ?) "
                  "WHERE id=?", (pw_hash, iso(), t["user_id"]))
        orgs = control.orgs_of(c, t["user_id"])
        control.audit(c, f"user:{t['email']}", None, "PASSWORD_RESET")
    security.end_all_sessions(t["user_id"])
    return _login_response(request, t["user_id"], orgs[0]["id"] if orgs else None, {"ok": True, "next": "/app"})


@router.post("/auth/resend-verification")
def resend(request: Request):
    c = need_user(request)
    if c["user"].get("email_verified_at"):
        return {"ok": True, "note": "Your email is already confirmed."}
    if not security.limiter.allow(f"verify:{c['user']['id']}", 3, 3600):
        raise HTTPException(429, "We already sent a few links. Check your spam folder or try again in an hour.")
    _send_verification(c["user"])
    return {"ok": True, "note": f"We sent a new link to {c['user']['email']}."}


class OrgPick(BaseModel):
    org_id: str


@router.post("/auth/switch-org")
def switch_org(body: OrgPick, request: Request):
    c = need_user(request)
    if not any(o["id"] == body.org_id for o in c["orgs"]):
        raise HTTPException(404, "Organisation not found.")
    with cx(True) as db_:
        db_.execute("UPDATE sessions SET org_id=? WHERE token_hash=?", (body.org_id, c["session"]["token_hash"]))
    return {"ok": True, "next": "/app"}


class TokenIn(BaseModel):
    token: str


@router.post("/auth/accept-invite")
def accept_invite(body: TokenIn, request: Request):
    c = need_user(request)
    t = security.use_token("invite", body.token, consume=False)
    if not t:
        raise HTTPException(400, "This invitation is invalid or has expired.")
    if t["email"] != c["user"]["email"].lower():
        raise HTTPException(403, f"This invitation was sent to {t['email']}. Log in with that address.")
    with cx(True) as db_:
        db_.execute("INSERT OR IGNORE INTO members VALUES(?,?,?,?)", (t["org_id"], c["user"]["id"], t["role"] or "member", iso()))
        db_.execute("UPDATE tokens SET used_at=? WHERE token_hash=?", (iso(), t["token_hash"]))
        db_.execute("UPDATE users SET email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?", (iso(), c["user"]["id"]))
        db_.execute("UPDATE sessions SET org_id=? WHERE token_hash=?", (t["org_id"], c["session"]["token_hash"]))
        control.audit(db_, f"user:{c['user']['email']}", t["org_id"], "INVITE_ACCEPTED", {"role": t["role"]})
    return {"ok": True, "next": "/app"}


# ------------------------------------------------------------------ account
@router.get("/api/me")
def me(request: Request):
    c = need_user(request)
    u, o = c["user"], c.get("org")
    out = {"user": {"id": u["id"], "name": u["name"], "email": u["email"], "verified": bool(u.get("email_verified_at"))},
           "orgs": c["orgs"], "role": c.get("role"), "admin": c.get("admin", False), "org": None}
    if o:
        out["org"] = {"id": o["id"], "name": o["name"], "created_at": o["created_at"], "trial_ends_at": o.get("trial_ends_at"),
                      "suspended": bool(o.get("suspended"))}
        out["usage"] = _usage_view(o)
        out["projects"] = tenants.projects(o["id"]) if tenants.exists(o["id"]) else []
        out["traces_total"] = tenants.trace_count(o["id"]) if tenants.exists(o["id"]) else 0
    return out


class NameIn(BaseModel):
    name: str


@router.put("/api/me")
def update_me(body: NameIn, request: Request):
    c = need_user(request)
    name = security.clean_name(body.name)
    problem = security.name_problem(name, "Name")
    if problem:
        raise HTTPException(400, problem)
    with cx(True) as db_:
        db_.execute("UPDATE users SET name=? WHERE id=?", (name, c["user"]["id"]))
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/api/me/password")
def change_password(body: PasswordIn, request: Request):
    c = need_user(request)
    if not security.limiter.allow(f"pw:{c['user']['id']}", 10, 3600):
        raise HTTPException(429, "Too many attempts. Try again later.")
    if not security.verify_password(body.current, c["user"]["pw_hash"]):
        raise HTTPException(400, "Your current password is wrong.")
    problem = security.password_problem(body.new)
    if problem:
        raise HTTPException(400, problem)
    pw_hash = security.hash_password(body.new)
    with cx(True) as db_:
        db_.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, c["user"]["id"]))
        control.audit(db_, f"user:{c['user']['email']}", None, "PASSWORD_CHANGED")
    security.end_all_sessions(c["user"]["id"], keep_raw=c["token"])
    return {"ok": True, "note": "Password changed. Other devices were signed out."}


MAX_ORGS = 10


class OrgIn(BaseModel):
    name: str


@router.post("/api/orgs")
def create_org(body: OrgIn, request: Request):
    c = need_user(request)
    if len(c["orgs"]) >= MAX_ORGS:
        raise HTTPException(400, f"You can belong to at most {MAX_ORGS} organisations.")
    name = security.clean_name(body.name) or "New team"
    problem = security.name_problem(name, "Team name")
    if problem:
        raise HTTPException(400, problem)
    oid = control.new_id("org")
    tenants.create(oid, "My first project", f"{c['user']['name']} <{c['user']['email']}>")
    try:
        with cx(True) as db_:
            if one(db_, "SELECT COUNT(*) n FROM members WHERE user_id=?", (c["user"]["id"],))["n"] >= MAX_ORGS:
                raise HTTPException(400, f"You can belong to at most {MAX_ORGS} organisations.")
            _new_org(db_, name, c["user"], oid=oid)
            if c.get("session"):
                db_.execute("UPDATE sessions SET org_id=? WHERE token_hash=?", (oid, c["session"]["token_hash"]))
    except Exception:
        tenants.delete(oid)
        raise
    return {"ok": True, "id": oid, "next": "/app/onboarding"}


# ------------------------------------------------------------------ organisation and team
@router.get("/api/org/members")
def members(request: Request):
    c = need_org(request)
    with cx() as db_:
        ms = control.members_of(db_, c["org"]["id"])
        invites = rows(db_, "SELECT email, role, created_at, expires_at FROM tokens WHERE kind='invite' AND org_id=? AND used_at IS NULL "
                            "AND expires_at>? ORDER BY created_at DESC", (c["org"]["id"], iso()))
    return {"members": [{**m, "verified": bool(m.pop("email_verified_at"))} for m in ms], "invites": invites,
            "limit": plans.limits(c["org"])["members"], "me": c["user"]["id"], "my_role": c["role"]}


INVITES_PER_ORG, INVITES_PER_USER, INVITES_PER_RECIPIENT = 20, 30, 3     # per 24 hours


class InviteIn(BaseModel):
    email: str
    role: str = "member"


@router.post("/api/org/invites")
def invite(body: InviteIn, request: Request):
    c = need_org(request, "admin")
    need_verified(c)
    email, role = body.email.strip().lower(), body.role if body.role in control.ROLES else "member"
    if role == "owner" and c["role"] != "owner":
        raise HTTPException(403, "Only an owner can invite another owner.")
    if not security.valid_email(email):
        raise HTTPException(400, "Please enter a valid email address.")
    o = c["org"]
    with cx(True) as db_:
        if one(db_, "SELECT 1 x FROM members m JOIN users u ON u.id=m.user_id WHERE m.org_id=? AND u.email=?", (o["id"], email)):
            raise HTTPException(409, "This person is already in the team.")
        n = len(control.members_of(db_, o["id"])) + one(db_, "SELECT COUNT(*) n FROM tokens WHERE kind='invite' AND org_id=? "
                                                            "AND used_at IS NULL AND expires_at>?", (o["id"], iso()))["n"]
        lim = plans.limits(o)
        if n >= lim["members"]:
            raise HTTPException(402, f"The {lim['name']} plan includes {lim['members']} members. Upgrade in Settings > Billing.")
        day = iso(now() - timedelta(days=1))
        sent_org = one(db_, "SELECT COUNT(*) n FROM tokens WHERE kind='invite' AND org_id=? AND created_at>?", (o["id"], day))["n"]
        sent_me = one(db_, "SELECT COUNT(*) n FROM audit WHERE action='MEMBER_INVITED' AND ts>? AND actor=?",
                      (day, f"user:{c['user']['email']}"))["n"]          # the audit trail outlives deleted organisations
        if sent_org >= INVITES_PER_ORG or sent_me >= INVITES_PER_USER:
            raise HTTPException(429, "You sent many invitations in the last 24 hours. Try again tomorrow.")
        to_them = one(db_, "SELECT COUNT(*) n FROM audit WHERE action='MEMBER_INVITED' AND ts>? AND instr(detail, ?)>0",
                      (day, json.dumps({"email": email})[1:-1]))["n"]
        if to_them >= INVITES_PER_RECIPIENT:
            raise HTTPException(429, "This person was invited several times today. They can use the latest invitation.")
        db_.execute("UPDATE tokens SET used_at=? WHERE kind='invite' AND org_id=? AND email=? AND used_at IS NULL", (iso(), o["id"], email))
        control.audit(db_, f"user:{c['user']['email']}", o["id"], "MEMBER_INVITED", {"email": email, "role": role})
    t = security.create_token("invite", 24 * 7, org_id=o["id"], email=email, role=role)
    brand = company()["brand"]
    mailer.send(email, f"You are invited to a team on {brand}",
                [f"You have been invited to a team on {brand}, a service that shows how AI apps behave in production.",
                 f"Team: {o['name']}", f"Invited by: {c['user']['name']} <{c['user']['email']}>", f"Role: {role}",
                 "The invitation works for 7 days. If you were not expecting it, ignore this email."],
                ("Accept the invitation", f"{public_url()}/invite?token={t}"))
    return {"ok": True, "note": f"Invitation sent to {email}."}


@router.post("/api/org/invites/revoke")
def revoke_invite(body: EmailIn, request: Request):
    c = need_org(request, "admin")
    with cx(True) as db_:
        n = db_.execute("UPDATE tokens SET used_at=? WHERE kind='invite' AND org_id=? AND email=? AND used_at IS NULL",
                        (iso(), c["org"]["id"], body.email.strip().lower())).rowcount
    return {"ok": True, "revoked": n}


class RoleIn(BaseModel):
    role: str


def _owners(db_, oid):
    return [m for m in control.members_of(db_, oid) if m["role"] == "owner"]


@router.put("/api/org/members/{uid}")
def set_role(uid: str, body: RoleIn, request: Request):
    c = need_org(request, "admin")
    if body.role not in control.ROLES:
        raise HTTPException(400, "Role must be member, admin or owner.")
    with cx(True) as db_:
        m = control.membership(db_, c["org"]["id"], uid)
        if not m:
            raise HTTPException(404, "Member not found.")
        if "owner" in (body.role, m["role"]) and c["role"] != "owner":
            raise HTTPException(403, "Only an owner can change who is an owner.")
        if m["role"] == "owner" and body.role != "owner" and len(_owners(db_, c["org"]["id"])) <= 1:
            raise HTTPException(400, "The organisation needs at least one owner.")
        db_.execute("UPDATE members SET role=? WHERE org_id=? AND user_id=?", (body.role, c["org"]["id"], uid))
        control.audit(db_, f"user:{c['user']['email']}", c["org"]["id"], "ROLE_CHANGED", {"user": uid, "role": body.role})
    return {"ok": True}


@router.delete("/api/org/members/{uid}")
def remove_member(uid: str, request: Request):
    c = need_org(request)
    self_leave = uid == c["user"]["id"]
    if not self_leave and not control.role_at_least(c["role"], "admin"):
        raise HTTPException(403, "Only an admin or owner can remove members.")
    with cx(True) as db_:
        m = control.membership(db_, c["org"]["id"], uid)
        if not m:
            raise HTTPException(404, "Member not found.")
        if m["role"] == "owner" and c["role"] != "owner":
            raise HTTPException(403, "Only an owner can remove an owner.")
        if m["role"] == "owner" and len(_owners(db_, c["org"]["id"])) <= 1:
            raise HTTPException(400, "The last owner cannot leave. Make someone else owner, or delete the organisation.")
        db_.execute("DELETE FROM members WHERE org_id=? AND user_id=?", (c["org"]["id"], uid))
        db_.execute("UPDATE sessions SET org_id=NULL WHERE user_id=? AND org_id=?", (uid, c["org"]["id"]))
        control.audit(db_, f"user:{c['user']['email']}", c["org"]["id"], "MEMBER_REMOVED", {"user": uid})
    return {"ok": True, "next": "/app" if self_leave else None}


@router.put("/api/org")
def rename_org(body: OrgIn, request: Request):
    c = need_org(request, "admin")
    problem = security.name_problem(body.name, "Team name")
    if problem:
        raise HTTPException(400, problem)
    with cx(True) as db_:
        db_.execute("UPDATE orgs SET name=? WHERE id=?", (security.clean_name(body.name), c["org"]["id"]))
    return {"ok": True}


@router.get("/api/org/export")
def export(request: Request):
    from starlette.background import BackgroundTask
    c = need_org(request, "admin")
    p = tenants.export(c["org"]["id"])
    with cx(True) as db_:
        control.audit(db_, f"user:{c['user']['email']}", c["org"]["id"], "DATA_EXPORTED")
    return FileResponse(p, media_type="application/vnd.sqlite3", filename=f"lumen-{c['org']['id']}-{iso()[:10]}.sqlite",
                        background=BackgroundTask(os.remove, p))


class ConfirmIn(BaseModel):
    confirm: str


@router.delete("/api/org")
def delete_org(body: ConfirmIn, request: Request):
    c = need_org(request, "owner")
    o = c["org"]
    if body.confirm.strip() != o["name"]:
        raise HTTPException(400, "Type the organisation's exact name to confirm.")
    if o.get("billing_status") in ("active", "past_due"):
        try:
            billing.cancel(o, f"user:{c['user']['email']}")
        except billing.BillingError as e:
            raise HTTPException(400, f"Cancel the subscription first: {e}")
    elif o.get("billing_subscription") and o.get("billing_status") in ("cancelling", "granted"):
        billing._stop_renewal(o.get("billing_provider"), o["billing_subscription"])    # make sure nothing renews
    tenants.delete(o["id"])
    with cx(True) as db_:
        for t in ("members", "sdk_keys", "usage", "tokens"):
            db_.execute(f"DELETE FROM {t} WHERE org_id=?", (o["id"],))
        db_.execute("UPDATE sessions SET org_id=NULL WHERE org_id=?", (o["id"],))
        db_.execute("DELETE FROM orgs WHERE id=?", (o["id"],))
        control.audit(db_, f"user:{c['user']['email']}", o["id"], "ORG_DELETED", {"name": o["name"]})
    return {"ok": True, "next": "/app" if len(c["orgs"]) > 1 else "/app/onboarding"}


@router.post("/api/org/trial")
def start_trial(request: Request):
    c = need_org(request, "admin")
    o = c["org"]
    if o.get("trial_ends_at") or o.get("billing_subscription"):
        raise HTTPException(400, "This organisation has already had a trial.")
    if plans.effective(o)[0] != "free":
        raise HTTPException(400, "You are already on a paid plan.")
    end = iso(now() + timedelta(days=plans.get("team").get("trial_days", 14)))
    with cx(True) as db_:
        db_.execute("UPDATE orgs SET trial_ends_at=? WHERE id=?", (end, o["id"]))
        control.audit(db_, f"user:{c['user']['email']}", o["id"], "TRIAL_STARTED", {"ends": end})
    return {"ok": True, "trial_ends_at": end}


# ------------------------------------------------------------------ billing
@router.get("/api/billing")
def billing_info(request: Request):
    c = need_org(request)
    o = c["org"]
    name, why = plans.effective(o)
    return {"plan": name, "why": why, "status": o.get("billing_status"), "provider": o.get("billing_provider"),
            "currency": o.get("billing_currency"), "period_end": o.get("current_period_end"),
            "cancel_at_period_end": bool(o.get("cancel_at_period_end")), "trial_ends_at": o.get("trial_ends_at"),
            "trial_used": bool(o.get("trial_ends_at") or o.get("billing_subscription")), "live": billing.live_subscription(o),
            "usage": _usage_view(o), "plans": {k: plans.get(k) for k in plans.ORDER}, "available": billing.available(),
            "can_manage": control.role_at_least(c["role"], "admin"), "verified": bool(c["user"].get("email_verified_at"))}


class CheckoutIn(BaseModel):
    currency: str = "INR"


@router.post("/api/billing/checkout")
def start_checkout(body: CheckoutIn, request: Request):
    c = need_org(request, "admin")
    need_verified(c)
    plan, why = plans.effective(c["org"])
    if plan != "free" and why != "trial":
        raise HTTPException(400, f"You already have the {plans.get(plan)['name']} plan.")
    if billing.live_subscription(c["org"]):          # e.g., a failed payment the provider is still retrying
        raise HTTPException(400, "Your subscription is still open with the payment provider. Use 'Update card / invoices' "
                                 "to fix the payment instead of starting a second subscription.")
    try:
        return billing.checkout(c["org"], c["user"], body.currency.upper())
    except billing.BillingError as e:
        raise HTTPException(400, str(e))


class RazorpayIn(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


@router.post("/api/billing/razorpay/verify")
def razorpay_verify(body: RazorpayIn, request: Request):
    c = need_org(request, "admin")
    try:
        return billing.razorpay_confirm(c["org"]["id"], body.razorpay_payment_id, body.razorpay_subscription_id,
                                        body.razorpay_signature, f"user:{c['user']['email']}")
    except billing.BillingError as e:
        raise HTTPException(400, str(e))


@router.post("/api/billing/cancel")
def cancel_plan(request: Request):
    c = need_org(request, "admin")
    try:
        return billing.cancel(c["org"], f"user:{c['user']['email']}")
    except billing.BillingError as e:
        raise HTTPException(400, str(e))


@router.get("/api/billing/manage")
def manage(request: Request):
    c = need_org(request, "admin")
    try:
        return {"url": billing.manage_url(c["org"])}
    except billing.BillingError as e:
        raise HTTPException(400, str(e))


@router.post("/webhooks/razorpay")
async def razorpay_hook(request: Request):
    raw = await request.body()
    if not billing.razorpay_webhook_ok(raw, request.headers.get("x-razorpay-signature")):
        raise HTTPException(400, "bad signature")
    return await run_in_threadpool(billing.razorpay_event, json.loads(raw), hashlib.sha256(raw).hexdigest())


@router.post("/webhooks/stripe")
async def stripe_hook(request: Request):
    raw = await request.body()
    if not billing.stripe_webhook_ok(raw, request.headers.get("stripe-signature")):
        raise HTTPException(400, "bad signature")
    return await run_in_threadpool(billing.stripe_event, json.loads(raw))


# ------------------------------------------------------------------ admin (LUMEN_ADMIN_EMAILS)
def need_admin(request):
    c = need_user(request)
    if not c.get("admin"):
        raise HTTPException(404, "Not found")
    return c


@router.get("/api/admin/orgs")
def admin_orgs(request: Request):
    need_admin(request)
    with cx() as db_:
        os_ = rows(db_, "SELECT o.*, u.email owner_email, (SELECT COUNT(*) FROM members m WHERE m.org_id=o.id) members "
                        "FROM orgs o LEFT JOIN users u ON u.id=o.owner_id ORDER BY o.created_at DESC")
        us = {r["org_id"]: r for r in rows(db_, "SELECT * FROM usage WHERE month=?", (control.month(),))}
        totals = {"users": one(db_, "SELECT COUNT(*) n FROM users")["n"], "orgs": len(os_)}
    out, mrr = [], {"INR": 0, "USD": 0}
    for o in os_:
        name, why = plans.effective(o)
        if o.get("billing_status") in ("active", "cancelling") and o.get("plan") == "team" and o.get("billing_currency") in mrr:
            mrr[o["billing_currency"]] += (plans.get("team")["price"] or {}).get(o["billing_currency"], 0)
        out.append({"id": o["id"], "name": o["name"], "owner": o["owner_email"], "members": o["members"], "plan": name, "why": why,
                    "billing_status": o.get("billing_status"), "provider": o.get("billing_provider"), "created_at": o["created_at"],
                    "suspended": bool(o.get("suspended")), "traces": (us.get(o["id"]) or {}).get("traces", 0),
                    "llm_calls": (us.get(o["id"]) or {}).get("llm_calls", 0), "notes": o.get("notes")})
    totals["paying"] = sum(1 for o in out if o["why"] in ("active", "cancelling"))
    totals["trials"] = sum(1 for o in out if o["why"] == "trial")
    return {"orgs": out, "totals": totals, "mrr": mrr}


class AdminOrgIn(BaseModel):
    plan: Optional[str] = None
    trial_days: Optional[int] = None
    suspended: Optional[bool] = None
    note: Optional[str] = None


@router.post("/api/admin/orgs/{oid}")
def admin_update(oid: str, body: AdminOrgIn, request: Request):
    c = need_admin(request)
    with cx() as db_:
        o = control.org(db_, oid)
    if not o:
        raise HTTPException(404, "Organisation not found.")
    f = {}
    if body.plan in plans.ORDER:
        if billing.live_subscription(o) and o.get("billing_status") != "cancelling":
            try:              # a plan set by hand replaces self-serve billing: the subscription must not renew
                billing.cancel(o, f"admin:{c['user']['email']}")
            except billing.BillingError as e:
                raise HTTPException(502, f"Could not stop the customer's {o.get('billing_provider')} subscription ({e}). Nothing was changed.")
        f.update(plan=body.plan, billing_status="granted" if body.plan != "free" else None)
    if body.trial_days is not None:
        f["trial_ends_at"] = iso(now() + timedelta(days=max(0, min(body.trial_days, 365))))
    if body.suspended is not None:
        f["suspended"] = int(body.suspended)
    if body.note is not None:
        f["notes"] = body.note[:500]
    with cx(True) as db_:
        if not control.org(db_, oid):
            raise HTTPException(404, "Organisation not found.")
        if f:
            db_.execute(f"UPDATE orgs SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), oid))
        control.audit(db_, f"admin:{c['user']['email']}", oid, "ADMIN_UPDATE", f)
    return {"ok": True}
