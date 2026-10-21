"""Ops agent pipeline: Planner -> Monitor -> Investigator -> Curator -> Remediator -> Critic -> Reporter -> Auditor.

Deterministic: the same trace snapshot gives the same decisions, so every run can be replayed and verified.
Agents read and propose; changes (rollbacks, dataset edits, LLM calls) need a named human approval.
"""
import json
import uuid
from datetime import datetime, timedelta

from . import events, ledger, llm_gate, metrics
from .db import jl, now_iso, one, rows, tx
from .guardrails import BudgetExceeded, authorize, evaluate, get_policy
from .prompts import label_version

HIGHER_IS_WORSE = {"error_rate", "p95_latency_ms", "p50_latency_ms", "cost_total", "cost_per_request", "pii_leaks",
                   "injection_attempts", "tokens_out_avg", "requests"}
DEDUPE_HOURS = 6


def breached(op, v, thr):
    if v is None:
        return False
    return {"gt": v > thr, "lt": v < thr, "gte": v >= thr, "lte": v <= thr}[op]


class RunContext:
    def __init__(self, conn, project_id, params, dry=False):
        self.conn, self.pid, self.params, self.dry = conn, project_id, params, dry
        self.run_id = ("REPLAY-" if dry else "RUN-") + uuid.uuid4().hex[:8].upper()
        self.policy = {**get_policy(conn), **params.get("policy", {})}
        self.steps, self.n, self._dry = [], 0, 0
        self.created = {"incidents": [], "proposals": []}

    def step(self, agent, title, detail=None, citations=None, status="ok"):
        self.n += 1
        if self.n > self.policy["max_steps_per_run"]:
            raise BudgetExceeded(f"step budget {self.policy['max_steps_per_run']} exceeded")
        s = {"n": self.n, "agent": agent, "title": title, "detail": detail or {}, "citations": citations or [],
             "status": status, "t": now_iso()}
        self.steps.append(s)
        if not self.dry:
            events.publish("agent_step", run_id=self.run_id, agent=agent, title=title, status=status)
        return s

    def authorize(self, agent, action, target, context=None):
        if self.dry:
            return evaluate(self.policy, f"agent:{agent}", action, context)
        return authorize(self.conn, f"agent:{agent}", action, target, self.pid, self.run_id, context, self.policy)

    def new_id(self, prefix):
        if self.dry:
            self._dry += 1
            return f"DRY-{prefix}-{self._dry}"
        return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"

    def recent_dup(self, table, kind_col, kind, key):
        ref = self.params["started_at"]
        since = (datetime.fromisoformat(ref) - timedelta(hours=DEDUPE_HOURS)).isoformat()
        return one(self.conn, f"SELECT id, title FROM {table} WHERE project_id=? AND {kind_col}=? AND title LIKE ? "
                              "AND created_at>=? AND created_at<? LIMIT 1", (self.pid, kind, f"%{key}%", since, ref))


class Snapshot:
    def __init__(self, conn, pid, params):
        self.as_of = params["as_of"]
        self.baseline_hours = params["baseline_hours"]
        self.rules = [r for r in rows(conn, "SELECT * FROM alert_rules WHERE project_id=? AND enabled=1 ORDER BY id", (pid,))]
        longest = max([r["window_min"] for r in self.rules] + [60])
        self.since = (datetime.fromisoformat(self.as_of) - timedelta(minutes=longest, hours=self.baseline_hours)).isoformat()
        self.traces = metrics.load(conn, pid, self.since, self.as_of)
        self.input_hash = ledger.sha256(ledger.canonical({
            "traces": [(t["id"], t["status"], t["latency_ms"], t["cost"], t["eval_passed"], t["prompt_version"], t["model"],
                        sorted((k, round(e["score"], 4)) for k, e in t["_evals"].items())) for t in self.traces],
            "rules": [(r["id"], r["metric"], r["op"], r["threshold"], r["window_min"], r["filter"]) for r in self.rules]}))

    def window(self, minutes, flt=None, before_minutes=0):
        end = datetime.fromisoformat(self.as_of) - timedelta(minutes=before_minutes)
        start = end - timedelta(minutes=minutes)
        s, e = start.isoformat(), end.isoformat()
        return [t for t in self.traces if s < t["ts"] <= e and all(not v or t.get(k) == v for k, v in (flt or {}).items())]


def _incident(ctx, rule, title, severity, detail, evidence):
    dup = ctx.recent_dup("incidents", "rule_id", rule["id"], rule["name"])
    if dup:
        ctx.step("monitor", f"Already open as {dup['id']} - not repeated", {"rule": rule["name"]}, status="info")
        return None
    d = ctx.authorize("monitor", "raise_incident", f"rule:{rule['id']}")
    if d["decision"] != "allow":
        ctx.step("monitor", f"Incident blocked by guardrail: {title}", d, status="blocked")
        return None
    inc = {"id": ctx.new_id("INC"), "project_id": ctx.pid, "rule_id": rule["id"], "created_at": now_iso(), "severity": severity,
           "title": title, "detail": detail, "evidence": evidence, "status": "open", "run_id": ctx.run_id}
    if not ctx.dry:
        with tx(ctx.conn):
            ctx.conn.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (inc["id"], ctx.pid, rule["id"], inc["created_at"], severity, title, json.dumps(detail, default=str),
                              json.dumps(evidence), "open", ctx.run_id))
            ledger.append(ctx.conn, "INCIDENT_RAISED", "agent:monitor", inc["id"], {"rule": rule["name"], "severity": severity})
        events.publish("incident", id=inc["id"], title=title, severity=severity)
    ctx.created["incidents"].append(inc)
    return inc


def _proposal(ctx, agent, kind, title, key, payload, evidence, action):
    dup = ctx.recent_dup("proposals", "kind", kind, key)
    if dup:
        ctx.step(agent, f"{dup['id']} ({dup['title']}) is recent - not repeated", {}, status="info")
        return None
    today = one(ctx.conn, "SELECT COUNT(*) n FROM proposals WHERE project_id=? AND created_at>=? AND created_at<?",
                (ctx.pid, (datetime.fromisoformat(ctx.params["started_at"]) - timedelta(hours=24)).isoformat(), ctx.params["started_at"]))["n"]
    d = ctx.authorize(agent, action, f"proposal:{kind}", {"proposals_today": today})
    if d["decision"] != "allow":
        ctx.step(agent, f"Proposal blocked by guardrail: {title}", d, status="blocked")
        return None
    p = {"id": ctx.new_id("PRP"), "created_at": now_iso(), "kind": kind, "title": title, "payload": payload,
         "evidence": evidence, "critic": None, "status": "critic_review"}
    if not ctx.dry:
        with tx(ctx.conn):
            ctx.conn.execute("INSERT INTO proposals(id,project_id,created_at,kind,title,payload,evidence,status,run_id) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (p["id"], ctx.pid, p["created_at"], kind, title,
                                                            json.dumps(payload, default=str), json.dumps(evidence), p["status"], ctx.run_id))
            ledger.append(ctx.conn, "PROPOSAL_CREATED", f"agent:{agent}", p["id"], {"kind": kind, "title": title})
        events.publish("proposal", id=p["id"], title=title)
    ctx.created["proposals"].append(p)
    return p


def _worst(ts, metric, n=10):
    if metric in ("p95_latency_ms", "p50_latency_ms"):
        key = lambda t: -t["latency_ms"]
    elif metric in ("cost_total", "cost_per_request"):
        key = lambda t: -(t["cost"] or 0)
    elif metric == "groundedness_avg":
        ts = [t for t in ts if "groundedness" in t["_evals"]]
        key = lambda t: t["_evals"]["groundedness"]["score"]
    elif metric == "error_rate":
        ts = [t for t in ts if t["status"] == "error"]
        key = lambda t: t["ts"]
    elif metric == "pii_leaks":
        ts = [t for t in ts if t["_evals"].get("pii_leak", {}).get("passed") == 0]
        key = lambda t: t["ts"]
    elif metric == "injection_attempts":
        ts = [t for t in ts if t["injection"]]
        key = lambda t: t["ts"]
    else:
        ts = [t for t in ts if not t["eval_passed"]]
        key = lambda t: t["ts"]
    return [t["id"] for t in sorted(ts, key=key)[:n]]


# ------------------------------------------------------------------ agents
def planner(ctx, snap):
    win = snap.window(60)
    prompts = rows(ctx.conn, "SELECT name, label, version FROM prompt_labels WHERE project_id=? AND label='production'", (ctx.pid,))
    d = {"as_of": snap.as_of, "traces_loaded": len(snap.traces), "traces_last_hour": len(win), "rules": len(snap.rules),
         "production_prompts": {p["name"]: p["version"] for p in prompts},
         "plan": ["Evaluate every enabled alert rule (window vs baseline)", "Slice breaches by route, model, prompt version, segment",
                  "Collect failing traces for the regression dataset", "Propose remediations", "Critic challenges every proposal",
                  "Report with trace citations", "Audit ledger, citations and approvals"]}
    ctx.step("planner", f"Plan: {len(snap.rules)} rules over {len(snap.traces)} traces", d)
    if not snap.traces:
        ctx.step("planner", "No traces in the window - nothing to monitor", status="info")


def monitor(ctx, snap):
    out = []
    for r in snap.rules:
        flt = jl(r["filter"], {}) or {}
        w = snap.window(r["window_min"], flt)
        base = snap.window(snap.baseline_hours * 60, flt, before_minutes=r["window_min"])
        v, b = metrics.value(w, r["metric"]), metrics.value(base, r["metric"])
        hit = breached(r["op"], v, r["threshold"]) and (len(w) >= 5 or r["metric"] in ("pii_leaks", "injection_attempts"))
        res = {"rule": r["id"], "name": r["name"], "metric": r["metric"], "value": v, "baseline": b, "threshold": r["threshold"],
               "op": r["op"], "window_min": r["window_min"], "n_window": len(w), "n_baseline": len(base), "breached": hit, "filter": flt}
        out.append(res)
        label = f"{r['name']}: {metrics.METRICS[r['metric']]} = {v} ({r['op']} {r['threshold']}; baseline {b})"
        if hit:
            ev = _worst(w, r["metric"])
            ctx.step("monitor", "BREACH " + label, res, citations=ev, status="alert")
            inc = _incident(ctx, r, f"{r['name']}: {v} vs threshold {r['threshold']}", r["severity"], dict(res), ev)
            res["incident"] = inc["id"] if inc else None
            res["evidence"] = ev
        else:
            ctx.step("monitor", "ok " + label, res)
    return out


def investigate(ctx, snap, breaches):
    findings = []
    for b in [x for x in breaches if x["breached"]]:
        metric = b["metric"]
        w = snap.window(b["window_min"], b["filter"])
        base = snap.window(snap.baseline_hours * 60, b["filter"], before_minutes=b["window_min"])
        overall_w, overall_b = metrics.value(w, metric), metrics.value(base, metric)
        if metric in ("pii_leaks", "injection_attempts", "requests", "cost_total"):
            rate = lambda g: (metrics.value(g, metric) or 0) / max(1, len(g))
        else:
            rate = lambda g: metrics.value(g, metric)
        hyps = []
        for dim in metrics.DIMENSIONS:
            gw, gb = {}, {}
            for t in w:
                gw.setdefault(t.get(dim) or "–", []).append(t)
            for t in base:
                gb.setdefault(t.get(dim) or "–", []).append(t)
            if len(gw) < 2 and len(gb) < 2:
                continue
            for val, g in gw.items():
                vw = rate(g)
                vb = rate(gb[val]) if val in gb else None
                if vw is None:
                    continue
                share = len(g) / max(1, len(w))
                # 1) contrast inside the window (strongest evidence) 2) else contrast with other values in history
                in_win = [t for t in w if (t.get(dim) or "–") != val]
                hist = in_win + [t for t in base if (t.get(dim) or "–") != val]
                vo_w = rate(in_win) if len(in_win) >= 5 else None
                vo_h = rate(hist) if len(hist) >= 5 else None
                vo, basis, weight = (vo_w, "same window", 1.0) if vo_w is not None else (vo_h, "history", 0.5)
                if vo is None:
                    continue
                effect = (vw - vo) if metric in HIGHER_IS_WORSE else (vo - vw)
                hyps.append({"dimension": dim, "value": val, "window_value": round(vw, 4), "baseline_value": None if vb is None else round(vb, 4),
                             "others_value": round(vo, 4), "compared_with": basis, "share": round(share, 3),
                             "new_in_window": val not in gb, "impact": round(effect * share * weight, 6), "isolates": effect > 0,
                             "n": len(g), "evidence": _worst(g, metric, 8)})
        hyps = [h for h in hyps if h["impact"] > 0 and h["isolates"]]
        hyps.sort(key=lambda h: (-(h["impact"] * (1.5 if h["new_in_window"] else 1.0)), h["dimension"], str(h["value"])))
        total = sum(h["impact"] for h in hyps if h["dimension"] == (hyps[0]["dimension"] if hyps else None)) or 1
        for h in hyps:
            h["confidence"] = round(min(1.0, h["impact"] / total), 2)
        first_seen = None
        if hyps and hyps[0]["dimension"] == "prompt_version":
            seen = [t["ts"] for t in snap.traces if t.get("prompt_version") == hyps[0]["value"]]
            first_seen = min(seen) if seen else None
        f = {"rule": b["name"], "metric": metric, "window_value": overall_w, "baseline_value": overall_b,
             "hypotheses": hyps[:5], "first_seen": first_seen, "evidence": b.get("evidence", []),
             "routes": sorted({t["name"] for t in w if t["id"] in set(b.get("evidence", []))})}
        findings.append(f)
        if hyps:
            h = hyps[0]
            ctx.step("investigator", f"{b['name']}: most likely cause {h['dimension']}={h['value']} "
                     f"({metric} {h['window_value']} vs {h['others_value']} for other values, {h['compared_with']}; {h['share']:.0%} of traffic"
                     + (", new in this window" if h["new_in_window"] else "") + (f", first seen {first_seen[11:16]} UTC" if first_seen else "") + ")",
                     f, citations=h["evidence"], status="alert")
        else:
            ctx.step("investigator", f"{b['name']}: no single segment explains it (broad regression)", f, status="info")
    return findings


def curator(ctx, snap):
    w = snap.window(ctx.policy["data_window_hours"] * 60)
    used = {r["source_trace"] for r in rows(ctx.conn, "SELECT source_trace FROM dataset_items i JOIN datasets d ON d.id=i.dataset_id "
                                                      "WHERE d.project_id=? AND source_trace IS NOT NULL", (ctx.pid,))}
    failing = [t for t in w if t["status"] == "ok" and not t["eval_passed"] and t["id"] not in used]
    failing.sort(key=lambda t: (t["ts"], t["id"]))
    picked = failing[-20:]
    ctx.step("curator", f"{len(failing)} failing traces not yet in any dataset", {"picked": [t["id"] for t in picked]},
             citations=[t["id"] for t in picked])
    if len(picked) >= 3:
        reasons = {}
        for t in picked:
            for e in jl(t["eval_failed"], []):
                reasons[e] = reasons.get(e, 0) + 1
        return _proposal(ctx, "curator", "dataset_items", f"Add {len(picked)} failing production traces to the regression set",
                         "regression set", {"trace_ids": [t["id"] for t in picked], "failed_evaluators": reasons, "dataset": "regression-set"},
                         [t["id"] for t in picked], "propose_dataset_items")
    return None


def remediate(ctx, snap, findings):
    out = []
    for f in findings:
        if not f["hypotheses"]:
            if f["metric"] in ("pii_leaks", "injection_attempts") and f["evidence"]:
                routes = ", ".join(f["routes"]) or "affected routes"
                advice = (f"Add an output PII filter on {routes} before responses reach users" if f["metric"] == "pii_leaks"
                          else f"Add an input guard (injection classifier / allow-list) on {routes}")
                p = _proposal(ctx, "remediator", "advisory", f"{f['rule']}: {advice}", f["rule"],
                              {"rule": f["rule"], "metric": f["metric"], "routes": f["routes"], "action": advice}, f["evidence"],
                              "propose_advisory")
                if p:
                    out.append(p)
            continue
        h = f["hypotheses"][0]
        if h["dimension"] == "prompt_version":
            names = sorted({t["prompt_name"] for t in snap.traces if t.get("prompt_version") == h["value"] and t.get("prompt_name")})
            for name in names:
                prod = label_version(ctx.conn, ctx.pid, name, "production")
                prev = label_version(ctx.conn, ctx.pid, name, "previous_production")
                if prod is not None and str(prod) == str(h["value"]) and prev is not None:
                    p = _proposal(ctx, "remediator", "rollback_prompt", f"Roll back prompt '{name}' v{prod} → v{prev}", f"'{name}' v{prod}",
                                  {"prompt": name, "from": prod, "to": prev, "metric": f["metric"], "rule": f["rule"],
                                   "window_value": h["window_value"], "others_value": h["others_value"], "first_seen": f["first_seen"]},
                                  h["evidence"], "propose_rollback")
                    if p:
                        out.append(p)
                else:
                    ctx.step("remediator", f"prompt '{name}' v{h['value']} is not production or has no previous version - advisory only", status="info")
            continue
        advice = {
            "model": f"Shift traffic for affected routes off model '{h['value']}' or add a timeout / fallback model",
            "name": f"Inspect route '{h['value']}' - it drives the regression",
            "user_segment": f"Segment '{h['value']}' is affected most - check its inputs / context size",
        }.get(h["dimension"], "Investigate")
        routes = ", ".join(f["routes"]) or "affected routes"
        if f["metric"] == "pii_leaks":
            advice = f"Add an output PII filter on {routes} before responses reach users"
        if f["metric"] == "injection_attempts":
            advice = f"Add an input guard (injection classifier / allow-list) on {routes}"
        p = _proposal(ctx, "remediator", "advisory", f"{f['rule']}: {advice}", f"{f['rule']}",
                      {"rule": f["rule"], "metric": f["metric"], "hypothesis": {k: h[k] for k in ("dimension", "value", "window_value",
                                                                                                 "others_value", "share")}, "action": advice},
                      h["evidence"], "propose_advisory")
        if p:
            out.append(p)
    return out


def critique(ctx, snap, p):
    pol, f = ctx.policy, []

    def add(check, ok, detail, warn=False):
        f.append({"check": check, "result": "pass" if ok else ("warn" if warn else "fail"), "detail": detail})

    need = 3 if p["kind"] == "dataset_items" else 1 if p["payload"].get("metric") in ("pii_leaks", "injection_attempts") \
        else pol["min_evidence_traces"]
    add("Enough evidence", len(p["evidence"]) >= need, f"{len(p['evidence'])} traces cited (min {need})")
    known = {t["id"] for t in snap.traces}
    missing = [e for e in p["evidence"] if e not in known]
    add("Evidence exists in snapshot", not missing, "all cited traces found" if not missing else f"missing {missing[:3]}")
    if p["kind"] == "rollback_prompt":
        pl = p["payload"]
        target = one(ctx.conn, "SELECT 1 x FROM prompts WHERE project_id=? AND name=? AND version=?", (ctx.pid, pl["prompt"], pl["to"]))
        add("Rollback target exists", bool(target), f"'{pl['prompt']}' v{pl['to']}")
        metric = pl["metric"]
        tgt = [t for t in snap.traces if t.get("prompt_name") == pl["prompt"] and str(t.get("prompt_version")) == str(pl["to"])]
        cur = [t for t in snap.window(24 * 60) if t.get("prompt_name") == pl["prompt"] and str(t.get("prompt_version")) == str(pl["from"])]
        vt, vc = metrics.value(tgt, metric), metrics.value(cur, metric)
        better = vt is not None and vc is not None and ((vt < vc) if metric in HIGHER_IS_WORSE else (vt > vc))
        add("Target performed better on this metric", better, f"v{pl['to']}: {vt} vs v{pl['from']}: {vc} ({len(tgt)} / {len(cur)} traces)")
        add("Target was production before", label_version(ctx.conn, ctx.pid, pl["prompt"], "previous_production") == pl["to"],
            f"previous production version is v{label_version(ctx.conn, ctx.pid, pl['prompt'], 'previous_production')}", warn=True)
    if p["kind"] == "dataset_items":
        from .redact import redact
        raw = [t for t in snap.traces if t["id"] in set(p["evidence"]) and redact(t["input"] or "")[1]]
        add("No unredacted PII in items", not raw, "inputs already redacted" if not raw else f"{len(raw)} with PII")
    status = "critic_blocked" if any(x["result"] == "fail" for x in f) else "pending_approval"
    return status, f


def critic(ctx, snap):
    verdicts = []
    for p in ctx.created["proposals"]:
        status, f = critique(ctx, snap, p)
        p["status"], p["critic"] = status, f
        fails = [x["check"] for x in f if x["result"] == "fail"]
        ctx.step("critic", ("BLOCKED: " if fails else "Passed: ") + p["title"] + (f" - failed: {', '.join(fails)}" if fails else ""),
                 {"proposal": p["id"], "findings": f}, status="blocked" if fails else "ok")
        if not ctx.dry:
            with tx(ctx.conn):
                ctx.conn.execute("UPDATE proposals SET critic=?, status=? WHERE id=?", (json.dumps(f), status, p["id"]))
                ledger.append(ctx.conn, "CRITIC_ASSESSMENT", "agent:critic", p["id"], {"status": status, "failed": fails})
        verdicts.append({"kind": p["kind"], "title": p["title"], "status": status})
    return verdicts


def reporter(ctx, snap, breaches, findings):
    cites = set()
    L = [f"# Ops report - {snap.as_of[:16].replace('T', ' ')} UTC", f"Run {ctx.run_id} - {len(snap.traces)} traces analysed", "",
         "## Alert rules"]
    for b in breaches:
        L.append(f"- {'BREACH' if b['breached'] else 'ok'} - {b['name']}: {b['value']} (threshold {b['op']} {b['threshold']}, baseline {b['baseline']})")
    L += ["", "## Root-cause analysis"]
    for f in findings:
        if f["hypotheses"]:
            h = f["hypotheses"][0]
            cites.update(h["evidence"][:5])
            L.append(f"- {f['rule']}: {h['dimension']} = {h['value']} ({f['metric']} {h['window_value']} vs {h['others_value']} elsewhere, "
                     f"{h['share']:.0%} of traffic, confidence {h['confidence']}). Examples: {', '.join(h['evidence'][:3])}")
        else:
            L.append(f"- {f['rule']}: broad regression, no single segment")
    if not findings:
        L.append("- no breaches")
    L += ["", "## Proposals (need human decision)"]
    for p in ctx.created["proposals"]:
        fails = [x["check"] for x in p["critic"] or [] if x["result"] == "fail"]
        L.append(f"- {p['title']} -> {'BLOCKED by Critic: ' + ', '.join(fails) if fails else 'awaiting approval'}")
    if not ctx.created["proposals"]:
        L.append("- none")
    body = "\n".join(L)
    d = ctx.authorize("reporter", "generate_report", "report")
    llm_req = None
    cfg = llm_gate.config(ctx.conn)
    if not ctx.dry and d["decision"] == "allow" and cfg["enabled"] and findings:
        prompt = ("Write a 4-sentence incident summary for an engineering on-call channel. Use only facts and numbers from "
                  "this report; do not invent causes.\n\n" + body)
        r = llm_gate.create_request(ctx.conn, ctx.pid, "incident_summary", ctx.run_id, [prompt], "agent:reporter",
                                    meta={"run_id": ctx.run_id, "facts": body})
        llm_req = r.get("id")
        ctx.step("reporter", f"Asked permission for a local-LLM summary ({llm_req}) - it runs only if you approve it",
                 {"request": llm_req}, status="info")
    ctx.step("reporter", f"Report drafted ({len(cites)} citations)" if d["decision"] == "allow" else "Report blocked by guardrail",
             {"llm_request": llm_req}, citations=sorted(cites), status="ok" if d["decision"] == "allow" else "blocked")
    return {"body": body, "citations": sorted(cites), "allowed": d["decision"] == "allow", "llm_request": llm_req}


def auditor(ctx, rep):
    conn, f = ctx.conn, []
    if not ctx.dry:
        v = ledger.verify(conn)
        f.append({"check": "Ledger hash chain intact", "ok": v["ok"], "detail": f"{v['count']} entries"})
    known = {r["id"] for r in rows(conn, "SELECT id FROM traces WHERE project_id=?", (ctx.pid,))}
    miss = [c for c in rep["citations"] if c not in known]
    f.append({"check": "Every citation exists", "ok": not miss, "detail": f"{len(rep['citations'])} cited"})
    for p in ctx.created["proposals"]:
        f.append({"check": f"{p['id']} assessed by Critic", "ok": bool(p["critic"]), "detail": p["status"]})
    bad = rows(conn, "SELECT name, label, updated_by FROM prompt_labels WHERE project_id=? AND (updated_by IS NULL OR updated_by LIKE 'agent:%')",
               (ctx.pid,))
    f.append({"check": "Every production label change was made by a person", "ok": not bad, "detail": f"{len(bad)} violations"})
    verdict = "AUDIT_PASSED" if all(x["ok"] for x in f) else "AUDIT_FAILED"
    ctx.step("auditor", f"{verdict}: {sum(x['ok'] for x in f)}/{len(f)} checks", {"findings": f}, status="ok" if verdict == "AUDIT_PASSED" else "alert")
    return {"verdict": verdict, "findings": f}


# ------------------------------------------------------------------ orchestration
def _digest(breaches, findings, verdicts):
    return {"breaches": sorted((b["rule"], b["breached"], b["value"]) for b in breaches),
            "causes": sorted((f["rule"], f["hypotheses"][0]["dimension"], str(f["hypotheses"][0]["value"])) if f["hypotheses"] else (f["rule"], "", "")
                             for f in findings),
            "proposals": sorted((v["kind"], v["title"], v["status"]) for v in verdicts)}


def run_cycle(conn, project_id, as_of=None, params=None, dry=False):
    pol = get_policy(conn)
    if params is None:
        last = one(conn, "SELECT MAX(ts) ts FROM traces WHERE project_id=?", (project_id,))
        params = {"as_of": as_of or (last["ts"] if last and last["ts"] else now_iso()), "baseline_hours": pol["baseline_hours"],
                  "started_at": now_iso(), "policy": {k: pol[k] for k in pol if k not in ("permissions", "agents_paused")}}
    ctx = RunContext(conn, project_id, params, dry)
    if not dry:
        with tx(conn):
            conn.execute("INSERT INTO agent_runs(id,project_id,kind,started_at,status,params) VALUES(?,?,?,?,?,?)",
                         (ctx.run_id, project_id, "cycle", now_iso(), "running", json.dumps(params)))
            ledger.append(conn, "RUN_STARTED", "orchestrator", ctx.run_id, {"project": project_id, **params})
        events.publish("run", run_id=ctx.run_id, status="running")
    snap = Snapshot(conn, project_id, params)
    status, digest, out = "completed", None, {}
    try:
        if ctx.authorize("planner", "read_data", "traces")["decision"] != "allow":
            raise BudgetExceeded("read denied")
        planner(ctx, snap)
        breaches = monitor(ctx, snap)
        findings = investigate(ctx, snap, breaches)
        curator(ctx, snap)
        remediate(ctx, snap, findings)
        verdicts = critic(ctx, snap)
        rep = reporter(ctx, snap, breaches, findings)
        audit = auditor(ctx, rep)
        digest = _digest(breaches, findings, verdicts)
        out = {"breaches": breaches, "findings": findings, "critic": verdicts, "report": rep, "audit": audit,
               "incidents": [i["id"] for i in ctx.created["incidents"]], "proposals": [p["id"] for p in ctx.created["proposals"]]}
    except BudgetExceeded as e:
        status = "aborted"
        ctx.steps.append({"n": ctx.n + 1, "agent": "orchestrator", "title": f"Run aborted by guardrail: {e}", "detail": {},
                          "citations": [], "status": "blocked", "t": now_iso()})
    oh = ledger.sha256(ledger.canonical(digest)) if digest else None
    if not dry:
        with tx(conn):
            conn.execute("UPDATE agent_runs SET finished_at=?, status=?, steps=?, input_hash=?, output_hash=?, summary=? WHERE id=?",
                         (now_iso(), status, json.dumps(ctx.steps, default=str), snap.input_hash, oh,
                          json.dumps({**out, "digest": digest}, default=str), ctx.run_id))
            ledger.append(conn, "RUN_COMPLETED", "orchestrator", ctx.run_id, {"status": status, "input_hash": snap.input_hash, "output_hash": oh})
        events.publish("run", run_id=ctx.run_id, status=status)
    return {"run_id": ctx.run_id, "status": status, "input_hash": snap.input_hash, "output_hash": oh, "digest": digest,
            "steps": ctx.steps, **out}


def replay(conn, run_id):
    orig = one(conn, "SELECT * FROM agent_runs WHERE id=?", (run_id,))
    if not orig or orig["kind"] != "cycle" or not orig["output_hash"]:
        raise ValueError("unknown or unfinished run")
    res = run_cycle(conn, orig["project_id"], params=json.loads(orig["params"]), dry=True)
    od = (jl(orig["summary"], {}) or {}).get("digest") or {}
    rd = json.loads(json.dumps(res["digest"] or {}, default=str))
    diffs = sorted(k for k in set(od) | set(rd) if od.get(k) != rd.get(k))
    match = res["output_hash"] == orig["output_hash"]
    rec = {"replay_id": res["run_id"], "replay_of": run_id, "match": match, "input_match": res["input_hash"] == orig["input_hash"],
           "original_input_hash": orig["input_hash"], "replay_input_hash": res["input_hash"],
           "original_output_hash": orig["output_hash"], "replay_output_hash": res["output_hash"], "differing_sections": diffs,
           "explanation": "Identical decisions from identical data - the run is reproducible." if match else
           ("Trace data changed since the original run, so decisions differ in: " if res["input_hash"] != orig["input_hash"]
            else "Same data but different decisions in: ") + ", ".join(diffs)}
    with tx(conn):
        conn.execute("INSERT INTO agent_runs(id,project_id,kind,started_at,finished_at,status,steps,input_hash,output_hash,summary,params,replay_of) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (res["run_id"], orig["project_id"], "replay", now_iso(), now_iso(),
                                                         "match" if match else "diverged", json.dumps(res["steps"], default=str),
                                                         res["input_hash"], res["output_hash"], json.dumps(rec), orig["params"], run_id))
        ledger.append(conn, "REPLAY_EXECUTED", "orchestrator", run_id, {"replay_id": res["run_id"], "match": match})
    return rec


@llm_gate.on_complete("incident_summary")
def _summary_done(conn, req, results):
    from .redact import redact
    meta = jl(req["payload"], {})["meta"]
    text = (results[0] or {}).get("text", "") if results else ""
    import re
    facts = meta["facts"]
    allowed = set(re.findall(r"\d+(?:\.\d+)?", facts))
    nums = [n for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r"\b(?:v|RUN-|INC-|PRP-)\w*", " ", text)) if n not in allowed]
    ok = not nums
    run = one(conn, "SELECT summary FROM agent_runs WHERE id=?", (meta["run_id"],))
    if run:
        s = jl(run["summary"], {})
        s.setdefault("report", {})["ai_summary"] = {"text": redact(text)[0] if ok else None, "grounded": ok, "ungrounded_numbers": nums[:10],
                                                     "request": req["id"], "model": req["model"]}
        with tx(conn):
            conn.execute("UPDATE agent_runs SET summary=? WHERE id=?", (json.dumps(s, default=str), meta["run_id"]))
    return {"grounded": ok, "ungrounded_numbers": nums[:10]}
