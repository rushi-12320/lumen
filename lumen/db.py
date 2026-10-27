import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("LUMEN_DB", os.path.join(os.path.dirname(__file__), "..", "data", "lumen.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, created_at TEXT, settings TEXT);
CREATE TABLE IF NOT EXISTS api_keys(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, key_hash TEXT UNIQUE, prefix TEXT,
  created_at TEXT, created_by TEXT, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS traces(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, ts TEXT, user_id TEXT, session_id TEXT,
  tags TEXT, metadata TEXT, prompt_name TEXT, prompt_version TEXT, model TEXT, status TEXT, latency_ms REAL, cost REAL,
  tokens_in INTEGER, tokens_out INTEGER, input TEXT, output TEXT, redactions INTEGER, injection INTEGER,
  eval_passed INTEGER, eval_failed TEXT);
CREATE INDEX IF NOT EXISTS ix_traces_pts ON traces(project_id, ts);
CREATE TABLE IF NOT EXISTS spans(id TEXT, trace_id TEXT, project_id TEXT, parent_id TEXT, type TEXT, name TEXT, model TEXT,
  input TEXT, output TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms REAL, cost REAL, status TEXT, error TEXT,
  start_ms REAL, attrs TEXT, PRIMARY KEY(trace_id, id));
CREATE TABLE IF NOT EXISTS evals(trace_id TEXT, project_id TEXT, evaluator TEXT, score REAL, passed INTEGER, detail TEXT,
  PRIMARY KEY(trace_id, evaluator));
CREATE INDEX IF NOT EXISTS ix_evals_p ON evals(project_id, evaluator);
CREATE TABLE IF NOT EXISTS alert_rules(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, metric TEXT, op TEXT, threshold REAL,
  window_min INTEGER, filter TEXT, severity TEXT, enabled INTEGER DEFAULT 1, created_at TEXT, created_by TEXT);
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, project_id TEXT, rule_id TEXT, created_at TEXT, severity TEXT,
  title TEXT, detail TEXT, evidence TEXT, status TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, description TEXT, created_at TEXT, created_by TEXT);
CREATE TABLE IF NOT EXISTS dataset_items(id TEXT PRIMARY KEY, dataset_id TEXT, input TEXT, expected TEXT, context TEXT,
  source_trace TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, project_id TEXT, dataset_id TEXT, name TEXT, prompt_name TEXT,
  prompt_version TEXT, model TEXT, evaluators TEXT, created_at TEXT, created_by TEXT, status TEXT, summary TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS experiment_results(experiment_id TEXT, item_id TEXT, output TEXT, latency_ms REAL, cost REAL,
  scores TEXT, passed INTEGER, PRIMARY KEY(experiment_id, item_id));
CREATE TABLE IF NOT EXISTS prompts(project_id TEXT, name TEXT, version INTEGER, template TEXT, model TEXT, params TEXT,
  created_at TEXT, created_by TEXT, note TEXT, PRIMARY KEY(project_id, name, version));
CREATE TABLE IF NOT EXISTS prompt_labels(project_id TEXT, name TEXT, label TEXT, version INTEGER, updated_at TEXT, updated_by TEXT,
  PRIMARY KEY(project_id, name, label));
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, project_id TEXT, created_at TEXT, kind TEXT, title TEXT, payload TEXT,
  evidence TEXT, critic TEXT, status TEXT, decided_by TEXT, decided_at TEXT, note TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS llm_requests(id TEXT PRIMARY KEY, project_id TEXT, created_at TEXT, created_by TEXT, purpose TEXT,
  target TEXT, model TEXT, endpoint TEXT, n_calls INTEGER, est_tokens INTEGER, preview TEXT, payload TEXT, status TEXT,
  decided_by TEXT, decided_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS agent_runs(id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, steps TEXT, input_hash TEXT, output_hash TEXT, summary TEXT, params TEXT, replay_of TEXT);
CREATE TABLE IF NOT EXISTS ledger(seq INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, actor TEXT, target TEXT, payload TEXT,
  prev_hash TEXT, entry_hash TEXT);
CREATE TABLE IF NOT EXISTS guard_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, project_id TEXT, actor TEXT, action TEXT,
  target TEXT, decision TEXT, reasons TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def now_dt():
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso():
    return now_dt().isoformat()


def iso_minus(ts, **kw):
    return (datetime.fromisoformat(ts) - timedelta(**kw)).isoformat()


def connect(path=None):
    from .sandbox import current_db  # demo mode: this request's private copy
    path = path or current_db() or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn):
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def jl(v, default=None):
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def get_setting(conn, key, default=None):
    r = one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
