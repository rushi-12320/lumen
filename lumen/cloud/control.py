"""The control database (data/control.db): users, organisations, members, sessions, one-time tokens, the SDK key
index, monthly usage, billing state and an audit trail of account actions. Customer data never lives here."""
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT UNIQUE COLLATE NOCASE, name TEXT, pw_hash TEXT,
  email_verified_at TEXT, created_at TEXT, last_login_at TEXT, failed_logins INTEGER DEFAULT 0, locked_until TEXT);
CREATE TABLE IF NOT EXISTS orgs(id TEXT PRIMARY KEY, name TEXT, plan TEXT DEFAULT 'free', trial_ends_at TEXT,
  created_at TEXT, owner_id TEXT, suspended INTEGER DEFAULT 0, billing_provider TEXT, billing_customer TEXT,
  billing_subscription TEXT, billing_status TEXT, billing_currency TEXT, current_period_end TEXT,
  cancel_at_period_end INTEGER DEFAULT 0, notes TEXT);
CREATE TABLE IF NOT EXISTS members(org_id TEXT, user_id TEXT, role TEXT, created_at TEXT, PRIMARY KEY(org_id, user_id));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT, org_id TEXT, created_at TEXT,
  expires_at TEXT, last_seen TEXT, ip TEXT, ua TEXT);
CREATE TABLE IF NOT EXISTS tokens(token_hash TEXT PRIMARY KEY, kind TEXT, user_id TEXT, org_id TEXT, email TEXT,
  role TEXT, created_at TEXT, expires_at TEXT, used_at TEXT);
CREATE TABLE IF NOT EXISTS sdk_keys(key_hash TEXT PRIMARY KEY, org_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS usage(org_id TEXT, month TEXT, traces INTEGER DEFAULT 0, llm_calls INTEGER DEFAULT 0,
  notified TEXT DEFAULT '', PRIMARY KEY(org_id, month));
CREATE TABLE IF NOT EXISTS webhook_events(provider TEXT, event_id TEXT, received_at TEXT, PRIMARY KEY(provider, event_id));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, org_id TEXT, action TEXT,
  detail TEXT);
CREATE INDEX IF NOT EXISTS ix_members_user ON members(user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_orgs_sub ON orgs(billing_subscription);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit(action, ts);
"""

ROLES = ("member", "admin", "owner")      # each includes the rights of the ones before it
_lock = threading.RLock()


def path():
    return os.environ.get("LUMEN_CONTROL_DB") or os.path.join(data_dir(), "control.db")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt=None):
    return (dt or now()).isoformat()


def month(dt=None):
    return (dt or now()).strftime("%Y-%m")


def parse(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _connect():
    c = sqlite3.connect(path(), timeout=15, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


_ready = set()
MIGRATIONS = [("orgs", "pending_subscription", "TEXT"),       # columns added after the first release
              ("orgs", "billing_event_at", "INTEGER")]


def _init(c, p):
    with _lock:
        if p in _ready:
            return
        c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        _ready.add(p)


@contextmanager
def cx(write=False):
    """A connection to the control database. Reads run in parallel (WAL); write=True wraps the block in one
    transaction, one writer at a time. Keep slow work (password hashing, creating databases) outside write blocks."""
    p = path()
    c = _connect()
    try:
        _init(c, p)
        if write:
            with _lock:
                c.execute("BEGIN IMMEDIATE")
                try:
                    yield c
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
        else:
            yield c
    finally:
        c.close()


def one(c, sql, args=()):
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(8)}"


def audit(c, actor, org_id, action, detail=None):
    c.execute("INSERT INTO audit(ts, actor, org_id, action, detail) VALUES(?,?,?,?,?)",
              (iso(), actor, org_id, action, json.dumps(detail or {}, default=str)))


# ------------------------------------------------------------------ users, orgs, members
def user_by_email(c, email):
    return one(c, "SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))


def user(c, uid):
    return one(c, "SELECT * FROM users WHERE id=?", (uid,))


def org(c, oid):
    return one(c, "SELECT * FROM orgs WHERE id=?", (oid,))


def membership(c, oid, uid):
    return one(c, "SELECT * FROM members WHERE org_id=? AND user_id=?", (oid, uid))


def orgs_of(c, uid):
    return rows(c, "SELECT o.id, o.name, o.plan, m.role FROM members m JOIN orgs o ON o.id=m.org_id "
                   "WHERE m.user_id=? ORDER BY m.created_at", (uid,))


def members_of(c, oid):
    return rows(c, "SELECT u.id, u.name, u.email, u.email_verified_at, m.role, m.created_at FROM members m "
                   "JOIN users u ON u.id=m.user_id WHERE m.org_id=? ORDER BY m.created_at", (oid,))


def role_at_least(role, need):
    return ROLES.index(role) >= ROLES.index(need) if role in ROLES and need in ROLES else False


# ------------------------------------------------------------------ SDK keys -> organisation
def register_key(key_hash, org_id):
    with cx(True) as c:
        c.execute("INSERT OR REPLACE INTO sdk_keys VALUES(?,?,?)", (key_hash, org_id, iso()))


def org_for_key(key_hash):
    with cx() as c:
        r = one(c, "SELECT org_id FROM sdk_keys WHERE key_hash=?", (key_hash,))
    return r["org_id"] if r else None


# ------------------------------------------------------------------ usage
def usage(c, oid, m=None):
    m = m or month()
    r = one(c, "SELECT * FROM usage WHERE org_id=? AND month=?", (oid, m))
    return r or {"org_id": oid, "month": m, "traces": 0, "llm_calls": 0, "notified": ""}


def add_usage(oid, traces=0, llm_calls=0):
    m = month()
    with cx(True) as c:
        c.execute("INSERT INTO usage(org_id, month, traces, llm_calls) VALUES(?,?,?,?) ON CONFLICT(org_id, month) "
                  "DO UPDATE SET traces=traces+excluded.traces, llm_calls=llm_calls+excluded.llm_calls",
                  (oid, m, int(traces), int(llm_calls)))
        return usage(c, oid, m)


def reserve_llm(oid, calls, cap):
    """Add `calls` to this month's LLM usage only if it stays within `cap`, in one step (parallel approvals cannot
    both pass the check). Returns (ok, used before). Give the calls back with add_usage(oid, llm_calls=-calls)."""
    m = month()
    with cx(True) as c:
        used = usage(c, oid, m)["llm_calls"]
        if used + calls > cap:
            return False, used
        c.execute("INSERT INTO usage(org_id, month, llm_calls) VALUES(?,?,?) ON CONFLICT(org_id, month) "
                  "DO UPDATE SET llm_calls=llm_calls+excluded.llm_calls", (oid, m, int(calls)))
        return True, used


def mark_notified(oid, tag):
    """True the first time `tag` is marked for this org this month (so each warning email goes once)."""
    m = month()
    with cx(True) as c:
        u = usage(c, oid, m)
        tags = set(filter(None, (u.get("notified") or "").split(",")))
        if tag in tags:
            return False
        tags.add(tag)
        c.execute("INSERT INTO usage(org_id, month, notified) VALUES(?,?,?) ON CONFLICT(org_id, month) "
                  "DO UPDATE SET notified=excluded.notified", (oid, m, ",".join(sorted(tags))))
        return True


def expires_in(**kw):
    return iso(now() + timedelta(**kw))
