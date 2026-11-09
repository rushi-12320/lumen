"""One SQLite database per organisation (data/tenants/<org id>.db): creation, routing, export, deletion, retention."""
import json
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import timedelta

from .. import db, ledger, simulator
from ..llm_gate import DEFAULT_LLM
from . import data_dir, env

_ID = re.compile(r"^org_[0-9a-f]{16}$")
PREFIX = "tenant:"          # marks a tenant in the request context (demo sandboxes never contain ':')


def tenants_dir():
    d = os.path.join(data_dir(), "tenants")
    os.makedirs(d, exist_ok=True)
    return d


def path(org_id):
    if not _ID.match(org_id or ""):
        raise ValueError("invalid organisation id")
    return os.path.join(tenants_dir(), f"{org_id}.db")


def org_for_path(p):
    """The organisation whose database file this is, or None (demo copies, the demo template)."""
    if not p:
        return None
    d, f = os.path.split(os.path.abspath(p))
    if d != os.path.abspath(tenants_dir()) or not f.endswith(".db"):
        return None
    return f[:-3] if _ID.match(f[:-3]) else None


def exists(org_id):
    return bool(_ID.match(org_id or "")) and os.path.exists(path(org_id))


def all_ids():
    return sorted(f[:-3] for f in os.listdir(tenants_dir()) if f.endswith(".db") and _ID.match(f[:-3]))


def activate(org_id):
    """Route this request (and everything it calls, threads started with its context included) to the org's database."""
    from .. import sandbox
    sandbox._current_db.set(path(org_id))
    sandbox._current_id.set(PREFIX + org_id)


def current_org():
    from .. import sandbox
    sid = sandbox.current_id() or ""
    return sid[len(PREFIX):] if sid.startswith(PREFIX) else None


def platform_llm():
    """The platform GPU (Ollama) every organisation uses; jobs still need a named person's approval."""
    url = env("LUMEN_PLATFORM_LLM_URL")
    return {"enabled": bool(url), "base_url": url or DEFAULT_LLM["base_url"],
            "model": env("LUMEN_PLATFORM_LLM_MODEL", "llama3.1:8b"), "timeout_s": 180, "max_output_tokens": 384}


def create(org_id, project_name, actor):
    """A fresh, empty workspace: audit ledger, the first project, sensible alert rules, the platform LLM."""
    c = db.connect(path(org_id))
    try:
        pid = "PRJ-" + secrets.token_hex(3).upper()
        with db.tx(c):
            ledger.append(c, "GENESIS", "system", "ledger", {"note": "Lumen audit ledger created"})
            c.execute("INSERT INTO projects VALUES(?,?,?,?)", (pid, project_name, db.now_iso(), "{}"))
            generic = [r for r in simulator.RULES if not r[5]]       # rules that need no route name
            for i, (name, metric, op, thr, win, flt, sev) in enumerate(generic):
                c.execute("INSERT INTO alert_rules VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                          (f"RULE-{i + 1:02d}", pid, name, metric, op, thr, win, json.dumps(flt), sev, db.now_iso(), "lumen"))
            ledger.append(c, "PROJECT_CREATED", f"human:{actor}", pid, {"name": project_name, "rules": len(generic)})
            db.set_setting(c, "llm", platform_llm())
    finally:
        c.close()
    return pid


def sync_llm_all():
    """Point every organisation at the current platform LLM (after LUMEN_PLATFORM_LLM_URL / _MODEL change)."""
    cfg = platform_llm()
    for oid in all_ids():
        c = db.connect(path(oid))
        try:
            if (db.get_setting(c, "llm", {}) or {}) != cfg:
                db.set_setting(c, "llm", cfg)
        finally:
            c.close()


def projects(org_id):
    c = db.connect(path(org_id))
    try:
        return db.rows(c, "SELECT id, name, created_at FROM projects ORDER BY created_at")
    finally:
        c.close()


def trace_count(org_id):
    c = db.connect(path(org_id))
    try:
        return db.one(c, "SELECT COUNT(*) n FROM traces")["n"]
    finally:
        c.close()


def export(org_id):
    """A consistent copy of the organisation's database, as a temporary file path (the caller deletes it)."""
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix=f"lumen-export-{org_id}-")
    os.close(fd)
    src, dst = sqlite3.connect(path(org_id)), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return tmp


def delete(org_id):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path(org_id) + suffix)
        except OSError:
            pass


def apply_retention(org_id, days):
    """Delete traces (with their spans and evaluations) older than the plan's retention. Returns traces deleted."""
    cut = (db.now_dt() - timedelta(days=days)).isoformat()
    c = db.connect(path(org_id))
    try:
        with db.tx(c):
            old = "SELECT id FROM traces WHERE ts < ?"
            c.execute(f"DELETE FROM spans WHERE trace_id IN ({old})", (cut,))
            c.execute(f"DELETE FROM evals WHERE trace_id IN ({old})", (cut,))
            n = c.execute("DELETE FROM traces WHERE ts < ?", (cut,)).rowcount
        return n
    finally:
        c.close()
