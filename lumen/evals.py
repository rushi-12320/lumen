"""Datasets, experiments, statistical comparison and the release gate."""
import json
import math
import random
import re
import uuid

from . import evaluators, events, ledger, llm_gate
from .db import jl, now_iso, one, rows, tx
from .guardrails import authorize, get_policy
from .redact import redact

JUDGE_PROMPT = """You are a strict evaluator. Rate the ANSWER to the QUESTION on a 1-5 scale for correctness and
faithfulness to the CONTEXT (5 = fully correct and supported, 1 = wrong or invented).
Reply with JSON only: {{"score": <1-5>, "reason": "<one sentence>"}}

QUESTION: {input}
CONTEXT: {context}
EXPECTED: {expected}
ANSWER: {output}"""


# ------------------------------------------------------------------ datasets
def create_dataset(conn, project_id, name, description, by):
    did = "DS-" + uuid.uuid4().hex[:8].upper()
    with tx(conn):
        conn.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?)", (did, project_id, name, description, now_iso(), by))
        ledger.append(conn, "DATASET_CREATED", f"human:{by}", did, {"project": project_id, "name": name})
    return did


def add_items(conn, dataset_id, items, by, approved=True, source="manual"):
    ds = one(conn, "SELECT * FROM datasets WHERE id=?", (dataset_id,))
    if not ds:
        raise ValueError("dataset not found")
    actor = by if ":" in by else f"human:{by}"
    d = authorize(conn, actor, "add_dataset_items", dataset_id, ds["project_id"], context={"approved": approved})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    ids = []
    with tx(conn):
        for it in items:
            iid = "IT-" + uuid.uuid4().hex[:10].upper()
            inp = redact(str(it.get("input", "")), True)[0]
            exp = redact(str(it["expected"]), True)[0] if it.get("expected") is not None else None
            ctx = redact(str(it["context"]), True)[0] if it.get("context") else None
            conn.execute("INSERT INTO dataset_items VALUES(?,?,?,?,?,?,?)",
                         (iid, dataset_id, inp, exp, ctx, it.get("source_trace"), now_iso()))
            ids.append(iid)
        ledger.append(conn, "DATASET_ITEMS_ADDED", actor, dataset_id, {"count": len(ids), "source": source})
    return {"ok": True, "added": len(ids), "ids": ids}


def items_from_traces(conn, project_id, trace_ids):
    out = []
    for tid in trace_ids:
        t = one(conn, "SELECT * FROM traces WHERE id=? AND project_id=?", (tid, project_id))
        if not t:
            continue
        ctx = "\n".join(s["output"] or "" for s in rows(conn, "SELECT output FROM spans WHERE trace_id=? AND type='retrieval'", (tid,)))
        out.append({"input": t["input"], "expected": None, "context": ctx or None, "source_trace": tid})
    return out


def parse_items(text):
    """JSONL or CSV (columns input, expected, context)."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("{"):
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    import csv
    import io
    return [dict(r) for r in csv.DictReader(io.StringIO(text))]


# ------------------------------------------------------------------ experiments
def _score_item(item, out, names, slo_ms=4000, latency_ms=None):
    _, pii = redact(out, True)
    return evaluators.run([n for n in names if n != "llm_judge"], out, inp=item["input"], context=item["context"],
                          expected=item["expected"], pii_counts=pii, latency_ms=latency_ms, slo_ms=slo_ms)


def _summarize(conn, exp_id):
    res = rows(conn, "SELECT * FROM experiment_results WHERE experiment_id=?", (exp_id,))
    names, agg = set(), {}
    for r in res:
        for k, v in (jl(r["scores"], {}) or {}).items():
            names.add(k)
            a = agg.setdefault(k, {"n": 0, "sum": 0.0, "pass": 0})
            a["n"] += 1
            a["sum"] += v["score"]
            a["pass"] += int(v["passed"])
    n = len(res)
    return {"items": n, "pass_rate": round(sum(r["passed"] for r in res) / n, 4) if n else None,
            "evaluators": {k: {"n": a["n"], "mean": round(a["sum"] / a["n"], 4), "pass_rate": round(a["pass"] / a["n"], 4)}
                           for k, a in sorted(agg.items())},
            "cost_total": round(sum(r["cost"] or 0 for r in res), 6),
            "latency_avg_ms": round(sum(r["latency_ms"] or 0 for r in res) / n, 1) if n else None}


def _recompute(conn, exp_id):
    exp = one(conn, "SELECT * FROM experiments WHERE id=?", (exp_id,))
    names = jl(exp["evaluators"], [])
    with tx(conn):
        for r in rows(conn, "SELECT * FROM experiment_results WHERE experiment_id=?", (exp_id,)):
            sc = jl(r["scores"], {}) or {}
            conn.execute("UPDATE experiment_results SET passed=? WHERE experiment_id=? AND item_id=?",
                         (int(all(v["passed"] for k, v in sc.items() if k not in evaluators.SECURITY_SIGNALS)), exp_id, r["item_id"]))
        s = _summarize(conn, exp_id)
        old = jl(exp["summary"], {}) or {}
        s["llm_judge"] = old.get("llm_judge")
        conn.execute("UPDATE experiments SET summary=? WHERE id=?", (json.dumps(s), exp_id))
    return s


def create_experiment(conn, project_id, dataset_id, name, prompt_name, prompt_version, model, names, outputs, by, source="upload"):
    """outputs: {item_id: {"output", "latency_ms", "cost"}} for items of the dataset."""
    items = {i["id"]: i for i in rows(conn, "SELECT * FROM dataset_items WHERE dataset_id=?", (dataset_id,))}
    if not items:
        raise ValueError("dataset is empty")
    if not any(i in items for i in outputs):
        raise ValueError("no outputs match items of this dataset")
    names = [n for n in names if n in evaluators.OFFLINE or n == "llm_judge"] or ["not_empty"]
    pol = get_policy(conn)
    eid = "EXP-" + uuid.uuid4().hex[:8].upper()
    with tx(conn):
        conn.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, project_id, dataset_id, name, prompt_name, str(prompt_version) if prompt_version is not None else None,
                      model, json.dumps(names), now_iso(), by, "running", "{}", source))
        for iid, o in outputs.items():
            if iid not in items:
                continue
            out = redact(str(o.get("output") or ""), True)[0]
            sc = _score_item(items[iid], str(o.get("output") or ""), names, pol["slo_ms"], o.get("latency_ms"))
            conn.execute("INSERT INTO experiment_results VALUES(?,?,?,?,?,?,?)",
                         (eid, iid, out, o.get("latency_ms"), o.get("cost") or 0, json.dumps(sc),
                          int(all(v["passed"] for k, v in sc.items() if k not in evaluators.SECURITY_SIGNALS))))
        ledger.append(conn, "EXPERIMENT_CREATED", f"human:{by}" if ":" not in by else by, eid,
                      {"dataset": dataset_id, "prompt": prompt_name, "version": prompt_version, "items": len(outputs), "source": source})
    s = _recompute(conn, eid)
    judge = None
    if "llm_judge" in names:
        judge = request_judge(conn, eid, by)
        s["llm_judge"] = {"status": "awaiting approval", "request": judge.get("id")} if judge.get("ok") else \
            {"status": "not requested", "reason": "; ".join(judge.get("reasons", []))}
    with tx(conn):
        conn.execute("UPDATE experiments SET status='completed', summary=? WHERE id=?", (json.dumps(s), eid))
    events.publish("experiment", id=eid, name=name)
    return {"id": eid, "summary": s, "llm_request": judge}


def request_judge(conn, eid, by):
    exp = one(conn, "SELECT * FROM experiments WHERE id=?", (eid,))
    res = rows(conn, "SELECT r.item_id, r.output, i.input, i.expected, i.context FROM experiment_results r "
                     "JOIN dataset_items i ON i.id=r.item_id WHERE r.experiment_id=? ORDER BY r.item_id", (eid,))
    prompts = [JUDGE_PROMPT.format(input=r["input"], context=r["context"] or "-", expected=r["expected"] or "-", output=r["output"])
               for r in res]
    return llm_gate.create_request(conn, exp["project_id"], "llm_judge", eid, prompts, by,
                                   meta={"experiment": eid, "item_ids": [r["item_id"] for r in res]})


def parse_judge(text):
    m = re.search(r'"score"\s*:\s*([1-5](?:\.\d+)?)', text or "") or re.search(r"\b([1-5])\b", text or "")
    return float(m.group(1)) if m else None


@llm_gate.on_complete("llm_judge")
def _judge_done(conn, req, results):
    meta = jl(req["payload"], {})["meta"]
    eid, ids = meta["experiment"], meta["item_ids"]
    scored = 0
    with tx(conn):
        for iid, r in zip(ids, results):
            s = parse_judge(r.get("text"))
            if s is None:
                continue
            row = one(conn, "SELECT scores FROM experiment_results WHERE experiment_id=? AND item_id=?", (eid, iid))
            sc = jl(row["scores"], {}) or {}
            sc["llm_judge"] = {"score": round((s - 1) / 4, 3), "passed": s >= 3,
                               "detail": f"judge {s:g}/5: {(r.get('text') or '')[:160]}"}
            conn.execute("UPDATE experiment_results SET scores=? WHERE experiment_id=? AND item_id=?", (json.dumps(sc), eid, iid))
            scored += 1
    s = _recompute(conn, eid)
    s["llm_judge"] = {"status": "scored", "request": req["id"], "scored": scored, "of": len(ids)}
    with tx(conn):
        conn.execute("UPDATE experiments SET summary=? WHERE id=?", (json.dumps(s), eid))
    return {"experiment": eid, "scored": scored}


def request_local_run(conn, project_id, dataset_id, prompt_name, prompt_version, template, names, by):
    items = rows(conn, "SELECT * FROM dataset_items WHERE dataset_id=? ORDER BY id", (dataset_id,))
    prompts = [template.replace("{input}", i["input"] or "").replace("{context}", i["context"] or "") for i in items]
    return llm_gate.create_request(conn, project_id, "experiment_run", f"{dataset_id}:{prompt_name}:v{prompt_version}", prompts, by,
                                   meta={"dataset": dataset_id, "prompt_name": prompt_name, "prompt_version": prompt_version,
                                         "item_ids": [i["id"] for i in items], "evaluators": names, "by": by})


@llm_gate.on_complete("experiment_run")
def _run_done(conn, req, results):
    meta = jl(req["payload"], {})["meta"]
    outs = {iid: {"output": r.get("text", ""), "latency_ms": None, "cost": 0} for iid, r in zip(meta["item_ids"], results)
            if "error" not in r}
    res = create_experiment(conn, req["project_id"], meta["dataset"], f"{meta['prompt_name']} v{meta['prompt_version']} (local {req['model']})",
                            meta["prompt_name"], meta["prompt_version"], req["model"],
                            [n for n in meta["evaluators"] if n != "llm_judge"], outs, meta["by"], source=f"local-llm:{req['id']}")
    return {"experiment": res["id"]}


# ------------------------------------------------------------------ comparison
def _sign_test_p(wins, losses):
    n = wins + losses
    if not n:
        return 1.0
    k = min(wins, losses)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * p), 4)


def _bootstrap_ci(deltas, seed=7, reps=1000):
    if not deltas:
        return None
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(rng.choice(deltas) for _ in range(n)) / n for _ in range(reps))
    return [round(means[int(0.025 * reps)], 4), round(means[int(0.975 * reps) - 1], 4)]


def compare(conn, a_id, b_id):
    A = {r["item_id"]: r for r in rows(conn, "SELECT * FROM experiment_results WHERE experiment_id=?", (a_id,))}
    B = {r["item_id"]: r for r in rows(conn, "SELECT * FROM experiment_results WHERE experiment_id=?", (b_id,))}
    common = sorted(set(A) & set(B))
    ea, eb = one(conn, "SELECT * FROM experiments WHERE id=?", (a_id,)), one(conn, "SELECT * FROM experiments WHERE id=?", (b_id,))
    if not ea or not eb:
        raise ValueError("experiment not found")
    items = {i["id"]: i for i in rows(conn, "SELECT * FROM dataset_items WHERE dataset_id=?", (eb["dataset_id"],))}
    per_eval = {}
    names = sorted(set().union(*[set(jl(A[i]["scores"], {})) & set(jl(B[i]["scores"], {})) for i in common]) if common else set())
    for name in names:
        d, w, l = [], 0, 0
        sa = sb = 0.0
        for i in common:
            x, y = jl(A[i]["scores"], {}).get(name), jl(B[i]["scores"], {}).get(name)
            if not x or not y:
                continue
            sa += x["score"]
            sb += y["score"]
            d.append(y["score"] - x["score"])
            w += y["score"] > x["score"] + 1e-9
            l += y["score"] < x["score"] - 1e-9
        n = len(d)
        if not n:
            continue
        p = _sign_test_p(w, l)
        per_eval[name] = {"n": n, "mean_a": round(sa / n, 4), "mean_b": round(sb / n, 4), "delta": round((sb - sa) / n, 4),
                          "wins": w, "losses": l, "ties": n - w - l, "sign_test_p": p, "ci95_delta": _bootstrap_ci(d),
                          "significant": p < 0.05}
    regressions = []
    for i in common:
        if A[i]["passed"] and not B[i]["passed"]:
            failed = [k for k, v in jl(B[i]["scores"], {}).items() if not v["passed"]]
            regressions.append({"item_id": i, "input": (items.get(i) or {}).get("input"), "failed": failed,
                                "output_a": A[i]["output"], "output_b": B[i]["output"]})
    improvements = sum(1 for i in common if not A[i]["passed"] and B[i]["passed"])
    sa_, sb_ = jl(ea["summary"], {}), jl(eb["summary"], {})
    return {"a": {"id": a_id, "name": ea["name"], **{k: sa_.get(k) for k in ("pass_rate", "cost_total", "latency_avg_ms")}},
            "b": {"id": b_id, "name": eb["name"], **{k: sb_.get(k) for k in ("pass_rate", "cost_total", "latency_avg_ms")}},
            "common_items": len(common), "evaluators": per_eval, "regressions": regressions[:30], "regression_count": len(regressions),
            "improvement_count": improvements}


# ------------------------------------------------------------------ release gate
def default_baseline(conn, exp):
    from .prompts import label_version
    prod = label_version(conn, exp["project_id"], exp["prompt_name"], "production") if exp["prompt_name"] else None
    cands = rows(conn, "SELECT id, prompt_version FROM experiments WHERE dataset_id=? AND id<>? AND status='completed' "
                       "ORDER BY created_at DESC", (exp["dataset_id"], exp["id"]))
    for c in cands:
        if prod is not None and c["prompt_version"] == str(prod):
            return c["id"]
    return None


def gate(conn, exp_id, baseline_id=None, actor="system"):
    pol = get_policy(conn)
    exp = one(conn, "SELECT * FROM experiments WHERE id=?", (exp_id,))
    if not exp:
        raise ValueError("experiment not found")
    s = jl(exp["summary"], {}) or {}
    baseline_id = baseline_id or default_baseline(conn, exp)
    checks = []

    def chk(name, ok, detail, hard=True):
        checks.append({"check": name, "result": "pass" if ok else ("fail" if hard else "warn"), "detail": detail})

    pr = s.get("pass_rate") or 0
    chk("Pass rate", pr >= pol["gate_min_pass_rate"], f"{pr:.1%} (min {pol['gate_min_pass_rate']:.0%})")
    pii = s.get("evaluators", {}).get("pii_leak")
    if pol["gate_block_on_pii"] and pii:
        fails = round(pii["n"] * (1 - pii["pass_rate"]))
        chk("No PII leaks", fails == 0, f"{fails} item(s) leak PII")
    cmp = None
    if baseline_id:
        cmp = compare(conn, baseline_id, exp_id)
        for name, e in cmp["evaluators"].items():
            if name in evaluators.SECURITY_SIGNALS:
                continue
            drop = -100 * e["delta"]
            chk(f"No regression: {name}", drop <= pol["gate_max_regression_pct"],
                f"mean {e['mean_a']:.3f} → {e['mean_b']:.3f} ({-drop:+.1f} pts, sign-test p={e['sign_test_p']})")
        ca, cb = cmp["a"]["cost_total"] or 0, cmp["b"]["cost_total"] or 0
        if ca > 0:
            inc = 100 * (cb - ca) / ca
            chk("Cost increase", inc <= pol["gate_max_cost_increase_pct"], f"{inc:+.1f}% (max +{pol['gate_max_cost_increase_pct']}%)")
    else:
        chk("Baseline available", False, "no production baseline experiment on this dataset - absolute checks only", hard=False)
    lj = s.get("llm_judge")
    if lj and lj.get("status") != "scored":
        chk("LLM judge", False, f"LLM judge {lj.get('status')} - not included", hard=False)
    passed = not any(c["result"] == "fail" for c in checks)
    result = {"experiment": exp_id, "baseline": baseline_id, "passed": passed, "checks": checks, "evaluated_at": now_iso(),
              "regression_count": cmp["regression_count"] if cmp else None}
    s["gate"] = result
    with tx(conn):
        conn.execute("UPDATE experiments SET summary=? WHERE id=?", (json.dumps(s), exp_id))
        ledger.append(conn, "GATE_EVALUATED", actor, exp_id, {"passed": passed, "baseline": baseline_id,
                                                               "failed": [c["check"] for c in checks if c["result"] == "fail"]})
    return result
