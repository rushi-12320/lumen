"""Aggregations over traces: KPIs, time series, breakdowns and single metric values used by alert rules."""
import math
from datetime import datetime, timedelta

from .db import jl, rows

METRICS = {
    "requests": "Requests", "error_rate": "Error rate (%)", "p95_latency_ms": "p95 latency (ms)", "p50_latency_ms": "p50 latency (ms)",
    "cost_total": "Cost (USD)", "cost_per_request": "Cost per request (USD)", "eval_pass_rate": "Eval pass rate (%)",
    "groundedness_avg": "Avg groundedness", "pii_leaks": "PII leaks in output", "injection_attempts": "Injection attempts",
    "tokens_out_avg": "Avg output tokens",
}
DIMENSIONS = ["name", "model", "prompt_version", "user_segment"]


def pctl(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load(conn, project_id, since=None, until=None, filters=None):
    sql = "SELECT * FROM traces WHERE project_id=?"
    args = [project_id]
    if since:
        sql += " AND ts>?"
        args.append(since)
    if until:
        sql += " AND ts<=?"
        args.append(until)
    for k, v in (filters or {}).items():
        if k in ("name", "model", "prompt_version", "status") and v:
            sql += f" AND {k}=?"
            args.append(v)
    ts = rows(conn, sql + " ORDER BY ts, id", args)
    if ts:
        ev = rows(conn, "SELECT trace_id, evaluator, score, passed FROM evals WHERE project_id=? AND trace_id IN "
                        "(SELECT id FROM traces WHERE project_id=?" + (" AND ts>?" if since else "") + (" AND ts<=?" if until else "") + ")",
                  [project_id, project_id] + [x for x in (since, until) if x])
        by = {}
        for e in ev:
            by.setdefault(e["trace_id"], {})[e["evaluator"]] = e
        for t in ts:
            t["_evals"] = by.get(t["id"], {})
            md = jl(t["metadata"], {}) or {}
            t["user_segment"] = md.get("segment") or md.get("plan") or "default"
    return ts


def value(ts, metric):
    n = len(ts)
    if metric == "requests":
        return n
    if not n:
        return None
    if metric == "error_rate":
        return round(100 * sum(t["status"] == "error" for t in ts) / n, 3)
    if metric in ("p95_latency_ms", "p50_latency_ms"):
        return round(pctl([t["latency_ms"] for t in ts], 0.95 if metric.startswith("p95") else 0.5), 1)
    if metric == "cost_total":
        return round(sum(t["cost"] or 0 for t in ts), 6)
    if metric == "cost_per_request":
        return round(sum(t["cost"] or 0 for t in ts) / n, 8)
    if metric == "eval_pass_rate":
        ok = [t for t in ts if t["status"] == "ok"]
        return round(100 * sum(t["eval_passed"] for t in ok) / len(ok), 3) if ok else None
    if metric == "groundedness_avg":
        g = [t["_evals"]["groundedness"]["score"] for t in ts if "groundedness" in t.get("_evals", {})]
        return round(sum(g) / len(g), 4) if g else None
    if metric == "pii_leaks":
        return sum(1 for t in ts if t.get("_evals", {}).get("pii_leak", {}).get("passed") == 0)
    if metric == "injection_attempts":
        return sum(t["injection"] for t in ts)
    if metric == "tokens_out_avg":
        return round(sum(t["tokens_out"] or 0 for t in ts) / n, 2)
    raise ValueError(f"unknown metric {metric}")


def timeseries(ts, since, until, bucket_min=60):
    t0, t1 = datetime.fromisoformat(since), datetime.fromisoformat(until)
    n = max(1, math.ceil((t1 - t0).total_seconds() / 60 / bucket_min))
    buckets = [[] for _ in range(n)]
    for t in ts:
        k = int((datetime.fromisoformat(t["ts"]) - t0).total_seconds() // (bucket_min * 60))
        if 0 <= k < n:
            buckets[k].append(t)
    out = []
    for k, b in enumerate(buckets):
        out.append({"ts": (t0 + timedelta(minutes=bucket_min * (k + 1))).isoformat(),
                    **{m: value(b, m) for m in ("requests", "error_rate", "p95_latency_ms", "cost_total", "eval_pass_rate",
                                                "groundedness_avg", "injection_attempts", "pii_leaks")}})
    return out


def breakdown(ts, dim):
    groups = {}
    for t in ts:
        groups.setdefault(t.get(dim) or "–", []).append(t)
    return sorted([{"value": k, "requests": len(g), "error_rate": value(g, "error_rate"), "p95_latency_ms": value(g, "p95_latency_ms"),
                    "cost_total": value(g, "cost_total"), "eval_pass_rate": value(g, "eval_pass_rate"),
                    "groundedness_avg": value(g, "groundedness_avg")} for k, g in groups.items()], key=lambda r: -r["requests"])


def summary(ts):
    return {m: value(ts, m) for m in METRICS}


def eval_rates(ts):
    agg = {}
    for t in ts:
        for k, e in t.get("_evals", {}).items():
            a = agg.setdefault(k, [0, 0])
            a[0] += 1
            a[1] += e["passed"]
    return {k: {"n": n, "pass_rate": round(100 * p / n, 2)} for k, (n, p) in sorted(agg.items())}
