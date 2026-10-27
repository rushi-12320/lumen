"""Public demo mode: every visitor gets a private, working copy of the demo.

Turn on with the environment variable LUMEN_DEMO=sandbox (off by default: normal single-tenant Lumen).

* The main database (LUMEN_DB) is the seeded TEMPLATE. Nobody writes to it in demo mode.
* A visitor's first request copies the template to sandboxes/<id>.db and sets a cookie. From then on every
  request of that browser reads and writes only that copy: approve the rollback, run the ops agent, create
  keys, send traces with the SDK, change rules. Other visitors never see it.
* SDK / CI calls (/v1/*) have no cookie: they are routed by their X-Lumen-Key to the sandbox that created the key.
* Sandboxes expire after LUMEN_SANDBOX_TTL_MIN minutes without use (default 120) and at most
  LUMEN_MAX_SANDBOXES exist (default 200, oldest removed first).
* Disabled in demo mode, because a public server must not call arbitrary addresses: changing the LLM endpoint or the
  data-residency policy, and running LLM jobs. Writes are rate limited per visitor.
"""
import contextvars
import json
import os
import re
import secrets
import sqlite3
import threading
import time

ENABLED = os.environ.get("LUMEN_DEMO", "").lower() == "sandbox"
TTL_S = int(os.environ.get("LUMEN_SANDBOX_TTL_MIN", "120")) * 60
MAX_SANDBOXES = int(os.environ.get("LUMEN_MAX_SANDBOXES", "200"))
WRITES_PER_MIN = int(os.environ.get("LUMEN_DEMO_WRITES_PER_MIN", "60"))
COOKIE = "lumen_sandbox"
_ID_RX = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
_lock = threading.RLock()
_current_db = contextvars.ContextVar("lumen_db", default=None)
_current_id = contextvars.ContextVar("lumen_sandbox_id", default=None)
_writes = {}

BLOCKED = [  # (method, path regex, message)
    ("PUT", r"^/api/llm/config$", "Changing the LLM endpoint is disabled in the hosted demo."),
    ("POST", r"^/api/llm/", "LLM jobs are disabled in the hosted demo (it has no local model). Run Lumen yourself with Ollama to try them."),
]
BLOCKED_POLICY_KEYS = {"llm_allow_non_local"}


def _dir():
    from .db import DB_PATH
    d = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "sandboxes")
    os.makedirs(d, exist_ok=True)
    return d


def _path(sid):
    return os.path.join(_dir(), f"{sid}.db")


def _index_path():
    return os.path.join(_dir(), "keys.json")


def current_db():
    return _current_db.get()


def current_id():
    return _current_id.get()


def activate(sid):
    """Route this request (and anything it calls) to sandbox `sid`."""
    _current_id.set(sid)
    _current_db.set(_path(sid))


def _cleanup():
    now = time.time()
    files = []
    for f in os.listdir(_dir()):
        if f.endswith(".db"):
            p = os.path.join(_dir(), f)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    files.sort()
    expired = [p for m, p in files if now - m > TTL_S]
    extra = [p for _, p in files[: max(0, len(files) - MAX_SANDBOXES + 1)]]
    gone = set()
    for p in set(expired + extra):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(p + suffix)
            except OSError:
                pass
        gone.add(os.path.basename(p)[:-3])
    if gone:
        idx = _load_index()
        _save_index({h: s for h, s in idx.items() if s not in gone})


def create():
    """New sandbox = a consistent copy of the seeded template database."""
    from .db import DB_PATH, connect
    with _lock:
        _cleanup()
        sid = secrets.token_urlsafe(16)
        src = connect(DB_PATH)
        dst = sqlite3.connect(_path(sid))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    return sid


def exists(sid):
    return bool(sid) and bool(_ID_RX.match(sid)) and os.path.exists(_path(sid))


def touch(sid):
    try:
        os.utime(_path(sid), None)
    except OSError:
        pass


def reset(sid):
    with _lock:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_path(sid) + suffix)
            except OSError:
                pass


# ------------------------------------------------------------------ API keys -> sandbox (for /v1 SDK calls)
def _load_index():
    try:
        with open(_index_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_index(idx):
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, _index_path())


def register_key(key_hash):
    """Called when a key is created: remember which sandbox it belongs to."""
    sid = current_id()
    if not (ENABLED and sid):
        return
    with _lock:
        idx = _load_index()
        idx[key_hash] = sid
        _save_index(idx)


def sandbox_for_key(raw):
    if not raw:
        return None
    from .ingest import hash_key
    sid = _load_index().get(hash_key(raw))
    return sid if exists(sid) else None


# ------------------------------------------------------------------ limits
def allow_write(visitor):
    now = time.time()
    with _lock:
        q = [t for t in _writes.get(visitor, []) if now - t < 60]
        if len(q) >= WRITES_PER_MIN:
            _writes[visitor] = q
            return False
        q.append(now)
        _writes[visitor] = q
        if len(_writes) > 5000:  # forget idle visitors
            for k in [k for k, v in _writes.items() if not v or now - v[-1] > 120]:
                _writes.pop(k, None)
        return True


def blocked(method, path):
    for m, rx, msg in BLOCKED:
        if method == m and re.search(rx, path):
            return msg
    return None


def info(sid):
    left = None
    if sid and os.path.exists(_path(sid)):
        left = max(0, int(TTL_S - (time.time() - os.path.getmtime(_path(sid)))))
    return {"demo": ENABLED, "sandbox": bool(sid), "ttl_minutes": TTL_S // 60, "expires_in_s": left,
            "note": "This is your own private copy of the demo. Changes are visible only to you and reset after "
                    f"{TTL_S // 60} minutes without use."}
