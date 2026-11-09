"""Trace ingestion: validate -> detect injection / PII on raw text -> redact -> cost -> online evaluators -> store.

Raw (unredacted) text is never written to the database.
"""
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from . import evaluators, ledger
from .db import get_setting, jl, now_dt, now_iso, one, rows, tx
from .guardrails import authorize, get_policy
from .redact import injection_hits, redact

DEFAULT_PRICING = {  # USD per 1M tokens - example values, edit in Settings to match your contracts
    "fast-model": {"in": 0.15, "out": 0.60},
    "smart-model": {"in": 2.50, "out": 10.00},
    "local-llama3.1": {"in": 0.0, "out": 0.0},
}


def pricing(conn):
    return {**DEFAULT_PRICING, **(get_setting(conn, "pricing", {}) or {})}


def cost_of(prices, model, tin, tout):
    p = prices.get(model or "", {"in": 0.0, "out": 0.0})
    return round(((tin or 0) * p["in"] + (tout or 0) * p["out"]) / 1e6, 8)


# ------------------------------------------------------------------ API keys
def hash_key(k):
    return hashlib.sha256(k.encode()).hexdigest()


def create_key(conn, project_id, name, by):
    d = authorize(conn, f"human:{by}", "create_api_key", project_id, project_id, context={"admin": True})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    raw = "lmn_" + secrets.token_urlsafe(24)
    kid = "KEY-" + uuid.uuid4().hex[:8].upper()
    with tx(conn):
        conn.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,0)", (kid, project_id, name, hash_key(raw), raw[:8], now_iso(), by))
        ledger.append(conn, "API_KEY_CREATED", f"human:{by}", kid, {"project": project_id, "name": name, "prefix": raw[:8]})
    from .sandbox import register_key
    register_key(hash_key(raw))
    return {"ok": True, "id": kid, "key": raw, "note": "Shown once. Only a SHA-256 hash is stored."}


def project_for_key(conn, raw):
    if not raw:
        return None
    r = one(conn, "SELECT project_id FROM api_keys WHERE key_hash=? AND revoked=0", (hash_key(raw),))
    return r["project_id"] if r else None


# ------------------------------------------------------------------ traces
MAX_TEXT = 200_000        # characters kept per text field (input, output, span text); the rest is cut
MAX_JSON = 50_000         # metadata / tags / span attributes
MAX_SPANS = 1000          # spans kept per trace
FUTURE_SLACK = timedelta(minutes=10)


def _text(v):
    if v is None:
        return None
    t = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return t if len(t) <= MAX_TEXT else t[:MAX_TEXT] + f" ... [cut: {len(t) - MAX_TEXT:,} more characters]"


def _short(v, n=500):
    return None if v is None else str(v)[:n]


def _json(v, what):
    t = json.dumps(v, ensure_ascii=False, default=str)
    return t if len(t) <= MAX_JSON else json.dumps({"_dropped": f"{what} larger than {MAX_JSON:,} characters"})


def _num(v, cast=float):
    """A number from client data, or None (bad values never fail the whole batch)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = cast(float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return x if x == x and abs(x) < 1e15 else None      # no NaN or absurd values


def trace_time(v):
    """The trace's time as UTC ISO text (the same form as now_iso()). Accepts ISO 8601 or unix seconds/milliseconds.
    Anything unreadable, or more than 10 minutes in the future (which would escape the retention clean-up), is now."""
    now, dt = now_dt(), None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            dt = datetime.fromtimestamp(float(v) / (1000.0 if abs(v) > 1e11 else 1.0), timezone.utc)
        except (OverflowError, OSError, ValueError):
            dt = None
    elif isinstance(v, str) and v.strip():
        raw = v.strip()
        raw = raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw
        try:
            dt = datetime.fromisoformat(raw)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
    if dt is None or dt > now + FUTURE_SLACK:
        return now.isoformat()
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _scrub(v, on=True, depth=0):
    """Redact every string inside attributes, metadata and tags (keys too) before they are stored."""
    if not on:
        return v
    if isinstance(v, str):
        return redact(v, True)[0]
    if depth > 20:
        return None
    if isinstance(v, dict):
        return {(redact(k, True)[0] if isinstance(k, str) else k): _scrub(x, on, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x, on, depth + 1) for x in v]
    return v


def normalize(t):
    spans = t.get("spans") if isinstance(t.get("spans"), list) else []
    spans = [dict(s) for s in spans if isinstance(s, dict)][:MAX_SPANS]
    if not spans and (t.get("input") is not None or t.get("output") is not None):
        spans = [{"type": "llm", "name": t.get("name", "llm"), "model": t.get("model"), "input": t.get("input"),
                  "output": t.get("output"), "tokens_in": t.get("tokens_in"), "tokens_out": t.get("tokens_out"),
                  "latency_ms": t.get("latency_ms"), "status": t.get("status", "ok"), "error": t.get("error")}]
    seen = set()
    for k, s in enumerate(spans):
        s["id"] = _short(s.get("id") or f"s{k + 1}", 200)
        if s["id"] in seen:                      # a repeated span id would fail the whole batch: keep both spans
            s["id"] = f"{s['id'][:180]}~{k + 1}"
        seen.add(s["id"])
        s["type"] = _short(s.get("type") or "llm", 50)
        s["name"] = _short(s.get("name") or s["type"], 500)
        s["model"] = _short(s.get("model"), 200)
        s["parent_id"] = _short(s.get("parent_id"), 200)
        s["error"] = _text(s.get("error"))
        for f in ("tokens_in", "tokens_out"):
            s[f] = _num(s.get(f), int)
        s["latency_ms"] = _num(s.get("latency_ms"))
        s["start_ms"] = _num(s.get("start_ms")) or 0
        s["status"] = _short(s.get("status"), 20) or ("error" if s.get("error") else "ok")
    return spans


def ingest_traces(conn, project_id, traces, actor="sdk", ts_default=None):
    pol, prices = get_policy(conn), pricing(conn)
    added, skipped, redactions = 0, 0, 0
    with tx(conn):
        for t in traces:
            if not isinstance(t, dict):
                skipped += 1
                continue
            tid = _short(t.get("id") or t.get("trace_id") or uuid.uuid4().hex, 200)
            if conn.execute("SELECT 1 FROM traces WHERE id=?", (tid,)).fetchone():
                skipped += 1
                continue
            spans = normalize(t)
            md = t.get("metadata") if isinstance(t.get("metadata"), dict) else {}
            llm = [s for s in spans if s["type"] == "llm"]
            main = llm[-1] if llm else (spans[-1] if spans else {})
            raw_in = _text(t.get("input") if t.get("input") is not None else (llm[0].get("input") if llm else None))
            raw_out = _text(t.get("output") if t.get("output") is not None else main.get("output"))
            context = _text("\n".join(_text(s.get("output")) or "" for s in spans if s["type"] == "retrieval")) or None
            inj = injection_hits(raw_in)
            _, out_pii = redact(raw_out, True)
            red_in, c1 = redact(raw_in, pol["redact_pii"])
            red_out, c2 = redact(raw_out, pol["redact_pii"])
            red_ctx, _ = redact(context, pol["redact_pii"])
            n_red = sum(c1.values()) + sum(c2.values())
            tin = sum(int(s.get("tokens_in") or 0) for s in llm)
            tout = sum(int(s.get("tokens_out") or 0) for s in llm)
            cost = round(sum(cost_of(prices, s.get("model"), s.get("tokens_in"), s.get("tokens_out")) for s in llm), 8)
            lat = _num(t.get("latency_ms"))
            if lat is None:
                lat = max([(s.get("start_ms") or 0) + (s.get("latency_ms") or 0) for s in spans] or [0])
            status = "error" if t.get("status") == "error" or any(s["status"] == "error" for s in spans) else "ok"
            res = evaluators.run(list(evaluators.ONLINE), raw_out if status == "ok" else (raw_out or ""),
                                 inp=raw_in, context=context, pii_counts=out_pii, latency_ms=lat, slo_ms=pol["slo_ms"],
                                 expects_json=bool(md.get("expects_json")))
            if status == "error":
                res.pop("not_empty", None)
            quality_fail = sorted(k for k, v in res.items() if not v["passed"] and k not in evaluators.SECURITY_SIGNALS)
            row = {"id": tid, "project_id": project_id, "name": _short(t.get("name") or main.get("name") or "trace"),
                   "ts": trace_time(t.get("ts") or t.get("timestamp") or ts_default), "user_id": _short(t.get("user_id")),
                   "session_id": _short(t.get("session_id")), "tags": _json(_scrub(t.get("tags") or [], pol["redact_pii"]), "tags"),
                   "metadata": _json(_scrub(md, pol["redact_pii"]), "metadata"),
                   "prompt_name": _short(t.get("prompt_name") or md.get("prompt_name"), 200),
                   "prompt_version": _short(t.get("prompt_version") or md.get("prompt_version") or "", 50) or None,
                   "model": main.get("model"), "status": status, "latency_ms": round(float(lat), 1), "cost": cost,
                   "tokens_in": tin, "tokens_out": tout, "input": red_in, "output": red_out, "redactions": n_red,
                   "injection": int(bool(inj)), "eval_passed": int(not quality_fail and status == "ok"),
                   "eval_failed": json.dumps(quality_fail)}
            conn.execute("INSERT INTO traces VALUES(:id,:project_id,:name,:ts,:user_id,:session_id,:tags,:metadata,:prompt_name,"
                         ":prompt_version,:model,:status,:latency_ms,:cost,:tokens_in,:tokens_out,:input,:output,:redactions,"
                         ":injection,:eval_passed,:eval_failed)", row)
            for s in spans:
                si, _ = redact(_text(s.get("input")), pol["redact_pii"])
                so, _ = redact(_text(s.get("output")), pol["redact_pii"])
                conn.execute("INSERT INTO spans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (str(s["id"]), tid, project_id, s.get("parent_id"), s["type"], s["name"], s.get("model"), si, so,
                              s.get("tokens_in"), s.get("tokens_out"), s.get("latency_ms"),
                              cost_of(prices, s.get("model"), s.get("tokens_in"), s.get("tokens_out")) if s["type"] == "llm" else 0,
                              s["status"], redact(s.get("error"), pol["redact_pii"])[0], s.get("start_ms"),
                              _json(_scrub(s.get("attrs") or {}, pol["redact_pii"]), "attributes")))
            conn.executemany("INSERT INTO evals VALUES(?,?,?,?,?,?)",
                             [(tid, project_id, k, v["score"], int(v["passed"]), v["detail"]) for k, v in res.items()])
            added += 1
            redactions += n_red
        ledger.append(conn, "TRACES_INGESTED", actor, f"project:{project_id}",
                      {"added": added, "skipped": skipped, "pii_redacted": redactions})
    return {"added": added, "skipped_duplicates": skipped, "pii_redacted": redactions}


def append_spans(conn, project_id, trace_id, spans):
    """Spans that arrive after their trace was stored (OpenTelemetry batches): redacted, added, totals updated.
    The trace's evaluator results are not recomputed."""
    pol, prices = get_policy(conn), pricing(conn)
    spans = normalize({"spans": spans})
    tin = tout = added = 0
    cost, failed = 0.0, False
    with tx(conn):
        for s in spans:
            si, _ = redact(_text(s.get("input")), pol["redact_pii"])
            so, _ = redact(_text(s.get("output")), pol["redact_pii"])
            c = cost_of(prices, s.get("model"), s.get("tokens_in"), s.get("tokens_out")) if s["type"] == "llm" else 0
            cur = conn.execute("INSERT OR IGNORE INTO spans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (str(s["id"]), trace_id, project_id, s.get("parent_id"), s["type"], s["name"], s.get("model"), si, so,
                                s.get("tokens_in"), s.get("tokens_out"), s.get("latency_ms"), c, s["status"],
                                redact(s.get("error"), pol["redact_pii"])[0], s.get("start_ms"),
                                _json(_scrub(s.get("attrs") or {}, pol["redact_pii"]), "attributes")))
            if cur.rowcount == 1:
                added += 1
                failed = failed or s["status"] == "error"
                if s["type"] == "llm":
                    tin, tout, cost = tin + (s.get("tokens_in") or 0), tout + (s.get("tokens_out") or 0), cost + c
        if tin or tout or cost:
            conn.execute("UPDATE traces SET tokens_in=COALESCE(tokens_in,0)+?, tokens_out=COALESCE(tokens_out,0)+?, "
                         "cost=ROUND(COALESCE(cost,0)+?, 8) WHERE id=? AND project_id=?", (tin, tout, cost, trace_id, project_id))
        if failed:
            conn.execute("UPDATE traces SET status='error', eval_passed=0 WHERE id=? AND project_id=?", (trace_id, project_id))
    return added


MARKS = re.compile(r"\[(EMAIL|API_KEY|AWS_KEY|CARD|PHONE|IP)\]")


def reevaluate(conn, project_id, trace_id, name=None, input=None, output=None, latency_ms=None, fill_input=None, fill_output=None,
               ts=None):
    """Update a stored trace from data that arrived late (an OpenTelemetry root span or LLM answer) and score it again.
    input / output replace the stored text; fill_input / fill_output are used only where it is empty. Works on the
    stored, already-redacted text: personal data shows there as [EMAIL] etc. and still counts as a PII leak."""
    t = one(conn, "SELECT * FROM traces WHERE id=? AND project_id=?", (trace_id, project_id))
    if not t:
        return False
    pol = get_policy(conn)
    new = {}
    for field, value, only_if_empty in (("input", input, False), ("output", output, False),
                                        ("input", fill_input, True), ("output", fill_output, True)):
        if value is not None and value != "" and not (only_if_empty and (new.get(field) or t[field])):
            new[field] = redact(_text(value), pol["redact_pii"])[0]
    if name:
        new["name"] = _short(name)
    if latency_ms is not None and _num(latency_ms) is not None:
        new["latency_ms"] = round(float(latency_ms), 1)
    if ts:
        new["ts"] = trace_time(ts)
    row = {**t, **new}
    spans = rows(conn, "SELECT type, output, status FROM spans WHERE trace_id=? AND project_id=?", (trace_id, project_id))
    context = _text("\n".join(s["output"] or "" for s in spans if s["type"] == "retrieval")) or None
    out, inp = row["output"] or "", row["input"]
    pii = {}
    for m in MARKS.finditer(out):
        pii[m.group(1)] = pii.get(m.group(1), 0) + 1
    md = jl(row["metadata"], {}) or {}
    status = "error" if row["status"] == "error" or any(s["status"] == "error" for s in spans) else "ok"
    res = evaluators.run(list(evaluators.ONLINE), out, inp=inp, context=context, pii_counts=pii, latency_ms=row["latency_ms"],
                         slo_ms=pol["slo_ms"], expects_json=bool(isinstance(md, dict) and md.get("expects_json")))
    if status == "error":
        res.pop("not_empty", None)
    quality_fail = sorted(k for k, v in res.items() if not v["passed"] and k not in evaluators.SECURITY_SIGNALS)
    marks = len(MARKS.findall(inp or "")) + len(MARKS.findall(out))
    with tx(conn):
        conn.execute("UPDATE traces SET name=?, ts=?, input=?, output=?, latency_ms=?, status=?, injection=?, "
                     "redactions=MAX(COALESCE(redactions,0),?), eval_passed=?, eval_failed=? WHERE id=? AND project_id=?",
                     (row["name"], row["ts"], inp, row["output"], row["latency_ms"], status, int(bool(injection_hits(inp))), marks,
                      int(not quality_fail and status == "ok"), json.dumps(quality_fail), trace_id, project_id))
        conn.execute("DELETE FROM evals WHERE trace_id=? AND project_id=?", (trace_id, project_id))
        conn.executemany("INSERT INTO evals VALUES(?,?,?,?,?,?)",
                         [(trace_id, project_id, k, v["score"], int(v["passed"]), v["detail"]) for k, v in res.items()])
    return True
