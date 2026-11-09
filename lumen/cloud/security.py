"""Passwords (scrypt), session and one-time tokens (only keyed hashes stored), and a small in-memory rate limiter."""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

from . import secret_key
from .control import cx, expires_in, iso, now, one, parse

SESSION_COOKIE = "lumen_session"
SESSION_DAYS = 30
_N, _R, _P = 2 ** 14, 8, 1
EMAIL_RX = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,24}$")


# ------------------------------------------------------------------ passwords
def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(pw, stored):
    try:
        alg, n, r, p, salt, h = (stored or "").split("$")
        if alg != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(base64.b64decode(h)))
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def password_problem(pw):
    if len(pw or "") < 10:
        return "Use at least 10 characters."
    if len(pw) > 200:
        return "Use at most 200 characters."
    if pw.lower() in ("password12", "1234567890", "qwertyuiop", "lumenlumen"):
        return "That password is too common."
    return None


def valid_email(email):
    return bool(EMAIL_RX.match((email or "").strip()))


# ------------------------------------------------------------------ tokens
def new_token():
    return secrets.token_urlsafe(32)


def token_hash(t):
    return hmac.new(secret_key(), (t or "").encode(), hashlib.sha256).hexdigest()


def create_token(kind, hours, user_id=None, org_id=None, email=None, role=None):
    raw = new_token()
    with cx(True) as c:
        c.execute("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?,NULL)",
                  (token_hash(raw), kind, user_id, org_id, (email or "").lower() or None, role, iso(), expires_in(hours=hours)))
    return raw


def use_token(kind, raw, consume=True):
    """The token's row if it is valid, unused and of this kind; marks it used when consume=True."""
    th = token_hash(raw)
    with cx(True) as c:
        t = one(c, "SELECT * FROM tokens WHERE token_hash=? AND kind=?", (th, kind))
        if not t or t["used_at"] or (parse(t["expires_at"]) and parse(t["expires_at"]) < now()):
            return None
        if consume:
            c.execute("UPDATE tokens SET used_at=? WHERE token_hash=?", (iso(), th))
    return t


# ------------------------------------------------------------------ sessions
def create_session(user_id, org_id, ip=None, ua=None):
    raw = new_token()
    with cx(True) as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                  (token_hash(raw), user_id, org_id, iso(), expires_in(days=SESSION_DAYS), iso(), ip, (ua or "")[:200]))
        c.execute("UPDATE users SET last_login_at=?, failed_logins=0, locked_until=NULL WHERE id=?", (iso(), user_id))
    return raw


def session(raw):
    """(session, user) for a valid session token, refreshing last_seen at most every 5 minutes."""
    if not raw:
        return None, None
    th = token_hash(raw)
    with cx() as c:
        s = one(c, "SELECT * FROM sessions WHERE token_hash=?", (th,))
        if not s:
            return None, None
        if parse(s["expires_at"]) and parse(s["expires_at"]) < now():
            c.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
            return None, None
        u = one(c, "SELECT * FROM users WHERE id=?", (s["user_id"],))
        if u and (not parse(s["last_seen"]) or (now() - parse(s["last_seen"])).total_seconds() > 300):
            c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (iso(), th))
    return (s, u) if u else (None, None)


def end_session(raw):
    with cx(True) as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(raw),))


def end_all_sessions(user_id, keep_raw=None):
    keep = token_hash(keep_raw) if keep_raw else ""
    with cx(True) as c:
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, keep))


# ------------------------------------------------------------------ rate limits (one process)
class Limiter:
    def __init__(self):
        self._hits, self._lock = {}, threading.Lock()

    def allow(self, key, limit, per_s):
        t = time.time()
        with self._lock:
            q = [x for x in self._hits.get(key, []) if t - x < per_s]
            ok = len(q) < limit
            if ok:
                q.append(t)
            self._hits[key] = q
            if len(self._hits) > 20000:
                for k in [k for k, v in self._hits.items() if not v or t - v[-1] > 3600]:
                    self._hits.pop(k, None)
            return ok

    def count(self, key, per_s):
        """How many times `key` was recorded in the last `per_s` seconds (without recording)."""
        t = time.time()
        with self._lock:
            q = [x for x in self._hits.get(key, []) if t - x < per_s]
            self._hits[key] = q
            return len(q)

    def hit(self, key):
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())


limiter = Limiter()

_NAME_EXTRA = set(" .,'’&()-")


def clean_name(text):
    return " ".join((text or "").split())


def name_problem(text, what="Name", n=60):
    """Names appear in emails to other people, so they stay plain: letters (any language), numbers, a little
    punctuation, no web addresses."""
    import unicodedata
    t = clean_name(text)
    if not t:
        return f"Please enter a {what.lower()}."
    if len(t) > n:
        return f"{what} can be at most {n} characters."
    if any(not (ch.isalnum() or ch in _NAME_EXTRA or unicodedata.category(ch) in ("Mn", "Mc")) for ch in t):
        return f"{what} can use letters, numbers, spaces and . , ' & ( ) -"
    if "www." in t.lower():
        return f"{what} cannot contain a web address."
    return None
