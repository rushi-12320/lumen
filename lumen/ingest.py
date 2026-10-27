"""Trace ingestion: validate -> detect injection / PII on raw text -> redact -> cost -> online evaluators -> store.

Raw (unredacted) text is never written to the database.
"""
import hashlib
import json
import secrets
import uuid

from . import evaluators, ledger
from .db import get_setting, now_iso, one, tx
from .guardrails import authorize, get_policy
from .redact import injection_hits, redact, redact_value

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
def _text(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def normalize(t):
    spans = t.get("spans") or []
    if not spans and (t.get("input") is not None or t.get("output") is not None):
        spans = [{"type": "llm", "name": t.get("name", "llm"), "model": t.get("model"), "input": t.get("input"),
                  "output": t.get("output"), "tokens_in": t.get("tokens_in"), "tokens_out": t.get("tokens_out"),
                  "latency_ms": t.get("latency_ms"), "status": t.get("status", "ok"), "error": t.get("error")}]
    for k, s in enumerate(spans):
        s.setdefault("id", f"s{k + 1}")
        s.setdefault("type", "llm")
        s.setdefault("name", s["type"])
        s.setdefault("start_ms", 0)
        s.setdefault("status", "error" if s.get("error") else "ok")
    return spans


def ingest_traces(conn, project_id, traces, actor="sdk", ts_default=None):
    pol, prices = get_policy(conn), pricing(conn)
    added, skipped, redactions = 0, 0, 0
    with tx(conn):
        for t in traces:
            tid = str(t.get("id") or t.get("trace_id") or uuid.uuid4().hex)
            if conn.execute("SELECT 1 FROM traces WHERE id=?", (tid,)).fetchone():
                skipped += 1
                continue
            spans = normalize(t)
            md = t.get("metadata") or {}
            red_tags, tag_counts = redact_value(t.get("tags") or [], pol["redact_pii"])
            red_md, md_counts = redact_value(md, pol["redact_pii"])
            red_user_id, user_counts = redact_value(t.get("user_id"), pol["redact_pii"])
            red_session_id, session_counts = redact_value(t.get("session_id"), pol["redact_pii"])
            llm = [s for s in spans if s["type"] == "llm"]
            main = llm[-1] if llm else (spans[-1] if spans else {})
            raw_name = t.get("name") or main.get("name") or "trace"
            trace_name, name_counts = redact(raw_name, pol["redact_pii"])
            trace_model, model_counts = redact(main.get("model"), pol["redact_pii"])
            trace_prompt_name, prompt_name_counts = redact(
                t.get("prompt_name") or md.get("prompt_name"), pol["redact_pii"])
            trace_prompt_version, prompt_version_counts = redact(
                _text(t.get("prompt_version") or md.get("prompt_version")), pol["redact_pii"])
            raw_in = _text(t.get("input") if t.get("input") is not None else (llm[0].get("input") if llm else None))
            raw_out = _text(t.get("output") if t.get("output") is not None else main.get("output"))
            context = "\n".join(_text(s.get("output")) or "" for s in spans if s["type"] == "retrieval") or None
            inj = injection_hits(raw_in)
            _, out_pii = redact(raw_out, True)
            red_in, c1 = redact(raw_in, pol["redact_pii"])
            red_out, c2 = redact(raw_out, pol["redact_pii"])
            red_ctx, ctx_counts = redact(context, pol["redact_pii"])
            n_red = (sum(c1.values()) + sum(c2.values()) + sum(ctx_counts.values())
                     + sum(tag_counts.values()) + sum(md_counts.values())
                     + sum(user_counts.values()) + sum(session_counts.values())
                     + sum(name_counts.values()) + sum(model_counts.values())
                     + sum(prompt_name_counts.values()) + sum(prompt_version_counts.values()))
            tin = sum(int(s.get("tokens_in") or 0) for s in llm)
            tout = sum(int(s.get("tokens_out") or 0) for s in llm)
            cost = round(sum(cost_of(prices, s.get("model"), s.get("tokens_in"), s.get("tokens_out")) for s in llm), 8)
            lat = t.get("latency_ms")
            if lat is None:
                lat = max([(s.get("start_ms") or 0) + (s.get("latency_ms") or 0) for s in spans] or [0])
            status = "error" if t.get("status") == "error" or any(s["status"] == "error" for s in spans) else "ok"
            res = evaluators.run(list(evaluators.ONLINE), raw_out if status == "ok" else (raw_out or ""),
                                 inp=raw_in, context=context, pii_counts=out_pii, latency_ms=lat, slo_ms=pol["slo_ms"],
                                 expects_json=bool(md.get("expects_json")))
            if status == "error":
                res.pop("not_empty", None)
            quality_fail = sorted(k for k, v in res.items() if not v["passed"] and k not in evaluators.SECURITY_SIGNALS)
            safe_spans = []
            for s in spans:
                si, input_counts = redact(_text(s.get("input")), pol["redact_pii"])
                so, output_counts = redact(_text(s.get("output")), pol["redact_pii"])
                safe_attrs, attrs_counts = redact_value(s.get("attrs") or {}, pol["redact_pii"])
                safe_error, error_counts = redact(_text(s.get("error")), pol["redact_pii"])
                span_name, span_name_counts = redact(s["name"], pol["redact_pii"])
                span_model, span_model_counts = redact(s.get("model"), pol["redact_pii"])
                if _text(s.get("input")) != raw_in:
                    n_red += sum(input_counts.values())
                if _text(s.get("output")) != raw_out:
                    n_red += sum(output_counts.values())
                n_red += sum(attrs_counts.values()) + sum(error_counts.values())
                if s["name"] != raw_name:
                    n_red += sum(span_name_counts.values())
                if s is not main:
                    n_red += sum(span_model_counts.values())
                safe_spans.append((str(s["id"]), tid, project_id, s.get("parent_id"), s["type"], span_name,
                                   span_model, si, so, s.get("tokens_in"), s.get("tokens_out"), s.get("latency_ms"),
                                   cost_of(prices, s.get("model"), s.get("tokens_in"), s.get("tokens_out"))
                                   if s["type"] == "llm" else 0, s["status"], safe_error, s.get("start_ms"),
                                   json.dumps(safe_attrs, default=str)))
            row = {"id": tid, "project_id": project_id, "name": trace_name,
                   "ts": t.get("ts") or t.get("timestamp") or ts_default or now_iso(), "user_id": red_user_id,
                   "session_id": red_session_id, "tags": json.dumps(red_tags), "metadata": json.dumps(red_md, default=str),
                   "prompt_name": trace_prompt_name,
                   "prompt_version": trace_prompt_version,
                   "model": trace_model, "status": status, "latency_ms": round(float(lat), 1), "cost": cost,
                   "tokens_in": tin, "tokens_out": tout, "input": red_in, "output": red_out, "redactions": n_red,
                   "injection": int(bool(inj)), "eval_passed": int(not quality_fail and status == "ok"),
                   "eval_failed": json.dumps(quality_fail)}
            conn.execute("INSERT INTO traces VALUES(:id,:project_id,:name,:ts,:user_id,:session_id,:tags,:metadata,:prompt_name,"
                         ":prompt_version,:model,:status,:latency_ms,:cost,:tokens_in,:tokens_out,:input,:output,:redactions,"
                         ":injection,:eval_passed,:eval_failed)", row)
            conn.executemany("INSERT INTO spans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", safe_spans)
            conn.executemany("INSERT INTO evals VALUES(?,?,?,?,?,?)",
                             [(tid, project_id, k, v["score"], int(v["passed"]), v["detail"]) for k, v in res.items()])
            added += 1
            redactions += n_red
        ledger.append(conn, "TRACES_INGESTED", actor, f"project:{project_id}",
                      {"added": added, "skipped": skipped, "pii_redacted": redactions})
    return {"added": added, "skipped_duplicates": skipped, "pii_redacted": redactions}
