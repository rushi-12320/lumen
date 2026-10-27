"""Lumen API (/v1 = key-authenticated ingestion & CI, /api = web console) + web UI.  Run: python -m lumen"""
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agents, approvals, db, evals, evaluators, events, ingest, ledger, llm_gate, metrics, prompts, redteam, sandbox, simulator
from .guardrails import DEFAULT_POLICY, authorize, get_policy, set_policy

STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


@asynccontextmanager
async def lifespan(_app):
    c = db.connect()
    pid = simulator.seed_demo(c)
    if pid:
        agents.run_cycle(c, pid)
    c.close()
    yield


app = FastAPI(title="Lumen - LLM observability, evaluation & ops agent", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def demo_sandbox(request, call_next):
    """LUMEN_DEMO=sandbox: route each visitor to a private working copy of the demo (see lumen/sandbox.py)."""
    if not sandbox.ENABLED:
        return await call_next(request)
    from fastapi.responses import JSONResponse
    path, method = request.url.path, request.method
    if path.startswith("/static") or path in ("/", "/favicon.ico", "/healthz"):
        return await call_next(request)
    new_cookie = None
    if path.startswith("/v1/"):
        sid = sandbox.sandbox_for_key(request.headers.get("x-lumen-key"))
        if not sid:
            return JSONResponse({"detail": "invalid or missing X-Lumen-Key (create a key under Settings & Keys in your demo)"}, 401)
    else:
        sid = request.cookies.get(sandbox.COOKIE)
        if not sandbox.exists(sid):
            sid = new_cookie = sandbox.create()
    sandbox.activate(sid)
    sandbox.touch(sid)
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        msg = sandbox.blocked(method, path)
        if msg:
            return JSONResponse({"detail": msg}, 403)
        if not sandbox.allow_write(sid):
            return JSONResponse({"detail": "Too many changes per minute in the demo - wait a moment."}, 429)
    response = await call_next(request)
    if new_cookie:
        response.set_cookie(sandbox.COOKIE, new_cookie, max_age=sandbox.TTL_S, httponly=True, samesite="lax",
                            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    return response


@app.get("/api/demo")
def demo_info():
    return sandbox.info(sandbox.current_id())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/demo/reset")
def demo_reset():
    from fastapi.responses import JSONResponse
    sid = sandbox.current_id()
    if not (sandbox.ENABLED and sid):
        raise HTTPException(400, "not in demo mode")
    sandbox.reset(sid)
    r = JSONResponse({"ok": True, "note": "Your demo was reset. Reload the page for a fresh copy."})
    r.delete_cookie(sandbox.COOKIE)
    return r


def conn():
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def need(v, what="name"):
    if not (v or "").strip():
        raise HTTPException(400, f"{what} required")
    return v.strip()


def _proj(c, project):
    p = db.one(c, "SELECT * FROM projects WHERE id=?", (project,))
    if not p:
        raise HTTPException(404, "project not found")
    return p


def key_project(x_lumen_key: Optional[str] = Header(None), c=Depends(conn)):
    pid = ingest.project_for_key(c, x_lumen_key)
    if not pid:
        raise HTTPException(401, "invalid or missing X-Lumen-Key")
    return pid


def _window(c, pid, hours):
    last = db.one(c, "SELECT MAX(ts) ts FROM traces WHERE project_id=?", (pid,))
    until = max(last["ts"], db.now_iso()) if last and last["ts"] else db.now_iso()
    return db.iso_minus(until, hours=hours), until


# ================================================================== /v1 (SDK, REST, CI)
class TracesIn(BaseModel):
    traces: list = Field(default_factory=list)


@app.post("/v1/traces")
def v1_traces(body: dict, pid=Depends(key_project), c=Depends(conn)):
    traces = body.get("traces") if isinstance(body.get("traces"), list) else [body]
    if not traces or len(traces) > 1000:
        raise HTTPException(400, "send 1-1000 traces")
    res = ingest.ingest_traces(c, pid, traces, actor="sdk")
    events.publish("traces", count=res["added"], project=pid)
    return res


@app.get("/v1/prompts/{name}")
def v1_prompt(name: str, label: str = "production", version: Optional[int] = None, pid=Depends(key_project), c=Depends(conn)):
    p = prompts.get(c, pid, name, label, version)
    if not p:
        raise HTTPException(404, "prompt not found")
    return {"name": name, "version": p["version"], "template": p["template"], "model": p["model"], "params": p["params"]}


@app.get("/v1/datasets/{name}")
def v1_dataset(name: str, pid=Depends(key_project), c=Depends(conn)):
    ds = db.one(c, "SELECT * FROM datasets WHERE project_id=? AND name=?", (pid, name))
    if not ds:
        raise HTTPException(404, "dataset not found")
    return {"id": ds["id"], "name": name, "items": db.rows(c, "SELECT id, input, expected, context FROM dataset_items WHERE dataset_id=? ORDER BY id", (ds["id"],))}


class V1ExpIn(BaseModel):
    dataset: str
    name: str
    prompt_name: Optional[str] = None
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    evaluators: list = Field(default_factory=lambda: ["not_empty", "groundedness", "answer_f1", "pii_leak", "refusal"])
    outputs: dict
    by: str = "ci"


@app.post("/v1/experiments")
def v1_experiment(body: V1ExpIn, pid=Depends(key_project), c=Depends(conn)):
    ds = db.one(c, "SELECT * FROM datasets WHERE project_id=? AND name=?", (pid, body.dataset))
    if not ds:
        raise HTTPException(404, "dataset not found")
    try:
        res = evals.create_experiment(c, pid, ds["id"], body.name, body.prompt_name, body.prompt_version, body.model,
                                      [e for e in body.evaluators if e != "llm_judge"], body.outputs, f"ci:{body.by}", "ci")
    except ValueError as e:
        raise HTTPException(400, str(e))
    g = evals.gate(c, res["id"], actor=f"ci:{body.by}")
    return {"experiment": res["id"], "summary": res["summary"], "gate": g}


# ================================================================== /api (console)
@app.get("/api/meta")
def meta():
    return {"metrics": metrics.METRICS, "dimensions": metrics.DIMENSIONS, "evaluators": evaluators.DESCRIPTIONS,
            "online": list(evaluators.ONLINE), "offline": list(evaluators.OFFLINE) + ["llm_judge"]}


@app.get("/api/projects")
def projects(c=Depends(conn)):
    out = db.rows(c, "SELECT id, name, created_at FROM projects ORDER BY created_at")
    for p in out:
        p["traces"] = db.one(c, "SELECT COUNT(*) n FROM traces WHERE project_id=?", (p["id"],))["n"]
    return out


class ProjectIn(BaseModel):
    name: str
    by: str


@app.post("/api/projects")
def create_project(body: ProjectIn, c=Depends(conn)):
    pid = "PRJ-" + uuid.uuid4().hex[:6].upper()
    with db.tx(c):
        c.execute("INSERT INTO projects VALUES(?,?,?,?)", (pid, need(body.name), db.now_iso(), "{}"))
        ledger.append(c, "PROJECT_CREATED", f"human:{need(body.by)}", pid, {"name": body.name})
    return {"id": pid}


@app.get("/api/overview")
def overview(project: str, hours: float = 24, c=Depends(conn)):
    _proj(c, project)
    since, until = _window(c, project, hours)
    ts = metrics.load(c, project, since, until)
    last_hour = [t for t in ts if t["ts"] > db.iso_minus(until, hours=1)]
    lr = db.one(c, "SELECT id, started_at, status, output_hash FROM agent_runs WHERE project_id=? AND kind='cycle' ORDER BY started_at DESC LIMIT 1", (project,))
    chain = ledger.verify(c)
    return {"window": {"since": since, "until": until}, "summary": metrics.summary(ts), "last_hour": metrics.summary(last_hour),
            "series": metrics.timeseries(ts, since, until, 60 if hours >= 12 else 15),
            "by_route": metrics.breakdown(ts, "name"), "by_model": metrics.breakdown(ts, "model"),
            "by_prompt": metrics.breakdown([t for t in ts if t["prompt_name"]], "prompt_version"), "evals": metrics.eval_rates(ts),
            "open_incidents": db.one(c, "SELECT COUNT(*) n FROM incidents WHERE project_id=? AND status='open'", (project,))["n"],
            "pending_proposals": db.one(c, "SELECT COUNT(*) n FROM proposals WHERE project_id=? AND status='pending_approval'", (project,))["n"],
            "pending_llm": db.one(c, "SELECT COUNT(*) n FROM llm_requests WHERE status='pending'")["n"],
            "ledger": {"ok": chain["ok"], "count": chain["count"], "merkle_root": chain["merkle_root"]},
            "agents_paused": get_policy(c)["agents_paused"], "llm": llm_gate.config(c), "last_run": lr,
            "production_prompts": {p["name"]: p["version"] for p in db.rows(c, "SELECT name, version FROM prompt_labels WHERE project_id=? AND label='production'", (project,))}}


@app.get("/api/traces")
def traces(project: str, limit: int = 50, offset: int = 0, name: str = None, model: str = None, status: str = None,
           failed: bool = False, injection: bool = False, pii: bool = False, prompt_version: str = None, q: str = None, c=Depends(conn)):
    sql, args = "SELECT * FROM traces WHERE project_id=?", [project]
    for k, v in (("name", name), ("model", model), ("status", status), ("prompt_version", prompt_version)):
        if v:
            sql += f" AND {k}=?"
            args.append(v)
    if failed:
        sql += " AND eval_passed=0 AND status='ok'"
    if injection:
        sql += " AND injection=1"
    if pii:
        sql += " AND id IN (SELECT trace_id FROM evals WHERE evaluator='pii_leak' AND passed=0)"
    if q:
        sql += " AND (input LIKE ? OR output LIKE ? OR id LIKE ? OR user_id LIKE ?)"
        args += [f"%{q}%"] * 4
    total = db.one(c, sql.replace("SELECT *", "SELECT COUNT(*) n"), args)["n"]
    items = db.rows(c, sql + " ORDER BY ts DESC LIMIT ? OFFSET ?", args + [limit, offset])
    for t in items:
        t["eval_failed"] = db.jl(t["eval_failed"], [])
        t["input"] = (t["input"] or "")[:160]
        t["output"] = (t["output"] or "")[:160]
    return {"total": total, "items": items}


@app.get("/api/traces/{tid}")
def trace(tid: str, c=Depends(conn)):
    t = db.one(c, "SELECT * FROM traces WHERE id=?", (tid,))
    if not t:
        raise HTTPException(404, "trace not found")
    for k in ("tags", "metadata", "eval_failed"):
        t[k] = db.jl(t[k], [] if k != "metadata" else {})
    spans = db.rows(c, "SELECT * FROM spans WHERE trace_id=? ORDER BY start_ms, id", (tid,))
    for s in spans:
        s["attrs"] = db.jl(s["attrs"], {})
    return {"trace": t, "spans": spans, "evals": db.rows(c, "SELECT evaluator, score, passed, detail FROM evals WHERE trace_id=? ORDER BY evaluator", (tid,))}


class RuleIn(BaseModel):
    project: str
    name: str
    metric: str
    op: str
    threshold: float
    window_min: int = 60
    filter: dict = Field(default_factory=dict)
    severity: str = "medium"
    by: str


@app.get("/api/rules")
def rules(project: str, c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM alert_rules WHERE project_id=? ORDER BY id", (project,))
    for r in rs:
        r["filter"] = db.jl(r["filter"], {})
    return rs


@app.post("/api/rules")
def create_rule(body: RuleIn, c=Depends(conn)):
    if body.metric not in metrics.METRICS or body.op not in ("gt", "lt", "gte", "lte"):
        raise HTTPException(400, "unknown metric or operator")
    rid = "RULE-" + uuid.uuid4().hex[:6].upper()
    with db.tx(c):
        c.execute("INSERT INTO alert_rules VALUES(?,?,?,?,?,?,?,?,?,1,?,?)", (rid, body.project, need(body.name), body.metric, body.op,
                  body.threshold, body.window_min, json.dumps(body.filter), body.severity, db.now_iso(), need(body.by)))
        ledger.append(c, "RULE_CREATED", f"human:{body.by}", rid, body.model_dump(exclude={"by"}))
    return {"id": rid}


class ToggleIn(BaseModel):
    enabled: bool
    by: str


@app.post("/api/rules/{rid}/toggle")
def toggle_rule(rid: str, body: ToggleIn, c=Depends(conn)):
    with db.tx(c):
        c.execute("UPDATE alert_rules SET enabled=? WHERE id=?", (int(body.enabled), rid))
        ledger.append(c, "RULE_TOGGLED", f"human:{need(body.by)}", rid, {"enabled": body.enabled})
    return {"ok": True}


@app.get("/api/incidents")
def incidents(project: str, c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM incidents WHERE project_id=? ORDER BY created_at DESC LIMIT 200", (project,))
    for r in rs:
        r["detail"], r["evidence"] = db.jl(r["detail"], {}), db.jl(r["evidence"], [])
    return rs


class ByIn(BaseModel):
    by: str


@app.post("/api/incidents/{iid}/ack")
def ack(iid: str, body: ByIn, c=Depends(conn)):
    with db.tx(c):
        n = c.execute("UPDATE incidents SET status='acknowledged' WHERE id=? AND status='open'", (iid,)).rowcount
        if n:
            ledger.append(c, "INCIDENT_ACKNOWLEDGED", f"human:{need(body.by)}", iid, {})
    return {"ok": bool(n)}


# ------------------------------------------------------------------ agents
class RunIn(BaseModel):
    project: str


@app.post("/api/agents/run")
def run_agents(body: RunIn, c=Depends(conn)):
    _proj(c, body.project)
    r = agents.run_cycle(c, body.project)
    return {k: r.get(k) for k in ("run_id", "status", "input_hash", "output_hash", "steps", "audit", "critic")}


@app.get("/api/runs")
def runs(project: str, c=Depends(conn)):
    rs = db.rows(c, "SELECT id, kind, started_at, status, input_hash, output_hash, replay_of, summary FROM agent_runs WHERE project_id=? "
                    "ORDER BY started_at DESC LIMIT 100", (project,))
    for r in rs:
        s = db.jl(r.pop("summary"), {}) or {}
        r["audit"] = (s.get("audit") or {}).get("verdict")
        r["match"] = s.get("match")
    return rs


@app.get("/api/runs/{rid}")
def run(rid: str, c=Depends(conn)):
    r = db.one(c, "SELECT * FROM agent_runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "run not found")
    for k in ("steps", "summary", "params"):
        r[k] = db.jl(r[k], {})
    return r


@app.post("/api/runs/{rid}/replay")
def replay(rid: str, c=Depends(conn)):
    try:
        return agents.replay(c, rid)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ evaluation
@app.get("/api/datasets")
def datasets(project: str, c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM datasets WHERE project_id=? ORDER BY created_at", (project,))
    for d in rs:
        d["items"] = db.one(c, "SELECT COUNT(*) n FROM dataset_items WHERE dataset_id=?", (d["id"],))["n"]
    return rs


class DatasetIn(BaseModel):
    project: str
    name: str
    description: str = ""
    by: str


@app.post("/api/datasets")
def create_dataset(body: DatasetIn, c=Depends(conn)):
    return {"id": evals.create_dataset(c, body.project, need(body.name), body.description, need(body.by))}


@app.get("/api/datasets/{did}")
def dataset(did: str, c=Depends(conn)):
    d = db.one(c, "SELECT * FROM datasets WHERE id=?", (did,))
    if not d:
        raise HTTPException(404, "dataset not found")
    d["items"] = db.rows(c, "SELECT * FROM dataset_items WHERE dataset_id=? ORDER BY created_at, id", (did,))
    return d


class ItemsIn(BaseModel):
    text: Optional[str] = None
    trace_ids: list = Field(default_factory=list)
    by: str


@app.post("/api/datasets/{did}/items")
def add_items(did: str, body: ItemsIn, c=Depends(conn)):
    d = db.one(c, "SELECT * FROM datasets WHERE id=?", (did,))
    if not d:
        raise HTTPException(404, "dataset not found")
    try:
        items = evals.parse_items(body.text) if body.text else evals.items_from_traces(c, d["project_id"], body.trace_ids)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"could not parse items: {e}")
    if not items:
        raise HTTPException(400, "no items")
    return evals.add_items(c, did, items, need(body.by), True, "console")


@app.get("/api/experiments")
def experiments(project: str, c=Depends(conn)):
    rs = db.rows(c, "SELECT e.*, d.name dataset_name FROM experiments e JOIN datasets d ON d.id=e.dataset_id WHERE e.project_id=? "
                    "ORDER BY e.created_at DESC", (project,))
    for r in rs:
        r["summary"], r["evaluators"] = db.jl(r["summary"], {}), db.jl(r["evaluators"], [])
    return rs


@app.get("/api/experiments/{eid}")
def experiment(eid: str, c=Depends(conn)):
    e = db.one(c, "SELECT * FROM experiments WHERE id=?", (eid,))
    if not e:
        raise HTTPException(404, "experiment not found")
    e["summary"], e["evaluators"] = db.jl(e["summary"], {}), db.jl(e["evaluators"], [])
    e["results"] = db.rows(c, "SELECT r.*, i.input, i.expected FROM experiment_results r JOIN dataset_items i ON i.id=r.item_id "
                              "WHERE r.experiment_id=? ORDER BY r.passed, i.id", (eid,))
    for r in e["results"]:
        r["scores"] = db.jl(r["scores"], {})
    return e


class ExpIn(BaseModel):
    project: str
    dataset_id: str
    name: str
    prompt_name: Optional[str] = None
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    evaluators: list = Field(default_factory=list)
    outputs_text: str
    by: str


@app.post("/api/experiments")
def create_experiment(body: ExpIn, c=Depends(conn)):
    """outputs_text: JSONL lines {"item_id"|"input", "output", "latency_ms"?, "cost"?}"""
    items = db.rows(c, "SELECT id, input FROM dataset_items WHERE dataset_id=?", (body.dataset_id,))
    by_input = {i["input"]: i["id"] for i in items}
    outs = {}
    try:
        for line in body.outputs_text.strip().splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            iid = o.get("item_id") or by_input.get(o.get("input"))
            if iid:
                outs[iid] = o
    except ValueError as e:
        raise HTTPException(400, f"bad JSONL: {e}")
    if not outs:
        raise HTTPException(400, "no outputs matched dataset items (use item_id or the exact input)")
    try:
        return evals.create_experiment(c, body.project, body.dataset_id, need(body.name), body.prompt_name, body.prompt_version,
                                       body.model, body.evaluators, outs, need(body.by), "upload")
    except ValueError as e:
        raise HTTPException(400, str(e))


class LocalRunIn(BaseModel):
    project: str
    dataset_id: str
    prompt_name: str
    prompt_version: int
    evaluators: list = Field(default_factory=list)
    by: str


@app.post("/api/experiments/local-run")
def local_run(body: LocalRunIn, c=Depends(conn)):
    p = prompts.get(c, body.project, body.prompt_name, version=body.prompt_version)
    if not p:
        raise HTTPException(404, "prompt version not found")
    return evals.request_local_run(c, body.project, body.dataset_id, body.prompt_name, body.prompt_version, p["template"],
                                   body.evaluators, need(body.by))


@app.post("/api/experiments/{eid}/judge")
def judge(eid: str, body: ByIn, c=Depends(conn)):
    return evals.request_judge(c, eid, need(body.by))


class GateIn(BaseModel):
    baseline_id: Optional[str] = None
    by: str = "console"


@app.post("/api/experiments/{eid}/gate")
def gate(eid: str, body: GateIn, c=Depends(conn)):
    try:
        return evals.gate(c, eid, body.baseline_id, f"human:{body.by}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/compare")
def compare(a: str, b: str, c=Depends(conn)):
    try:
        return evals.compare(c, a, b)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ prompts
@app.get("/api/prompts")
def prompt_list(project: str, c=Depends(conn)):
    return prompts.listing(c, project)


@app.get("/api/prompts/{name}/{version}")
def prompt_get(name: str, version: int, project: str, c=Depends(conn)):
    p = prompts.get(c, project, name, version=version)
    if not p:
        raise HTTPException(404, "not found")
    return p


class PromptIn(BaseModel):
    project: str
    name: str
    template: str
    model: str
    note: str = ""
    by: str


@app.post("/api/prompts")
def prompt_create(body: PromptIn, c=Depends(conn)):
    return {"version": prompts.create_version(c, body.project, need(body.name), need(body.template, "template"), body.model, {}, need(body.by), body.note)}


class PromoteIn(BaseModel):
    project: str
    name: str
    version: int
    by: str
    note: str = ""


@app.post("/api/prompts/promote")
def prompt_promote(body: PromoteIn, c=Depends(conn)):
    try:
        return prompts.promote(c, body.project, body.name, body.version, body.by, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ approvals & LLM gate
@app.get("/api/proposals")
def proposals(project: str, c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM proposals WHERE project_id=? ORDER BY created_at DESC LIMIT 200", (project,))
    for r in rs:
        r["payload"], r["evidence"], r["critic"] = db.jl(r["payload"], {}), db.jl(r["evidence"], []), db.jl(r["critic"], [])
    return rs


class DecisionIn(BaseModel):
    decision: str
    by: str
    note: str = ""


@app.post("/api/proposals/{pid}/decision")
def decide(pid: str, body: DecisionIn, c=Depends(conn)):
    try:
        return approvals.decide(c, pid, body.decision, body.by, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/llm/requests")
def llm_requests(c=Depends(conn)):
    rs = db.rows(c, "SELECT id, project_id, created_at, created_by, purpose, target, model, endpoint, n_calls, est_tokens, preview, "
                    "status, decided_by, decided_at, result FROM llm_requests ORDER BY created_at DESC LIMIT 100")
    for r in rs:
        r["result"] = db.jl(r["result"], None)
    return rs


@app.post("/api/llm/requests/{rid}/decision")
def llm_decide(rid: str, body: DecisionIn, c=Depends(conn)):
    try:
        return llm_gate.decide(c, rid, body.decision, body.by)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/llm/config")
def llm_config(c=Depends(conn)):
    cfg = llm_gate.config(c)
    ok, reason = llm_gate.check_endpoint(cfg["base_url"], get_policy(c)["llm_allow_non_local"])
    return {**cfg, "endpoint_ok": ok, "endpoint_reason": reason}


class LLMCfgIn(BaseModel):
    changes: dict
    by: str


@app.put("/api/llm/config")
def llm_config_put(body: LLMCfgIn, c=Depends(conn)):
    return llm_gate.set_config(c, body.changes, need(body.by))


@app.post("/api/llm/test")
def llm_test(body: ByIn, c=Depends(conn)):
    return llm_gate.test_connection(c, need(body.by))


# ------------------------------------------------------------------ governance
@app.get("/api/policy")
def policy(c=Depends(conn)):
    return {"policy": get_policy(c), "defaults": DEFAULT_POLICY}


class PolicyIn(BaseModel):
    changes: dict
    by: str


@app.put("/api/policy")
def put_policy(body: PolicyIn, c=Depends(conn)):
    if sandbox.ENABLED and set(body.changes) & sandbox.BLOCKED_POLICY_KEYS:
        raise HTTPException(403, "The data-residency policy cannot be changed in the hosted demo.")
    return set_policy(c, body.changes, need(body.by))


class KillIn(BaseModel):
    paused: bool
    by: str


@app.post("/api/policy/killswitch")
def kill(body: KillIn, c=Depends(conn)):
    r = set_policy(c, {"agents_paused": body.paused}, need(body.by))
    events.publish("killswitch", paused=body.paused, by=body.by)
    return r


@app.get("/api/guard-log")
def guard_log(limit: int = 200, c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM guard_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rs:
        r["reasons"] = db.jl(r["reasons"], [])
    return rs


@app.post("/api/redteam")
def run_redteam(c=Depends(conn)):
    return redteam.run_suite(c)


@app.get("/api/ledger")
def ledger_list(limit: int = 50, offset: int = 0, event_type: str = None, c=Depends(conn)):
    sql, args = "SELECT * FROM ledger", []
    if event_type:
        sql += " WHERE event_type=?"
        args.append(event_type)
    total = db.one(c, sql.replace("SELECT *", "SELECT COUNT(*) n"), args)["n"]
    return {"total": total, "items": db.rows(c, sql + " ORDER BY seq DESC LIMIT ? OFFSET ?", args + [limit, offset]),
            "types": [r["event_type"] for r in db.rows(c, "SELECT DISTINCT event_type FROM ledger ORDER BY 1")]}


@app.get("/api/ledger/verify")
def ledger_verify(c=Depends(conn)):
    return ledger.verify(c)


@app.get("/api/ledger/tamper-demo")
def tamper(seq: int = None, c=Depends(conn)):
    return ledger.tamper_demo(c, seq)


# ------------------------------------------------------------------ settings
@app.get("/api/keys")
def keys(project: str, c=Depends(conn)):
    return db.rows(c, "SELECT id, name, prefix, created_at, created_by, revoked FROM api_keys WHERE project_id=? ORDER BY created_at DESC", (project,))


class KeyIn(BaseModel):
    project: str
    name: str
    by: str


@app.post("/api/keys")
def create_key(body: KeyIn, c=Depends(conn)):
    _proj(c, body.project)
    return ingest.create_key(c, body.project, need(body.name), need(body.by))


@app.post("/api/keys/{kid}/revoke")
def revoke_key(kid: str, body: ByIn, c=Depends(conn)):
    with db.tx(c):
        n = c.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (kid,)).rowcount
        ledger.append(c, "API_KEY_REVOKED", f"human:{need(body.by)}", kid, {})
    return {"ok": bool(n)}


@app.get("/api/pricing")
def get_pricing(c=Depends(conn)):
    return ingest.pricing(c)


class PricingIn(BaseModel):
    pricing: dict
    by: str


@app.put("/api/pricing")
def put_pricing(body: PricingIn, c=Depends(conn)):
    clean = {m: {"in": float(v["in"]), "out": float(v["out"])} for m, v in body.pricing.items() if m.strip()}
    with db.tx(c):
        db.set_setting(c, "pricing", clean)
        ledger.append(c, "PRICING_CHANGED", f"human:{need(body.by)}", "pricing", clean)
    return {"ok": True, "pricing": ingest.pricing(c)}


class TickIn(BaseModel):
    project: str
    n: int = 20


@app.post("/api/sim/tick")
def sim_tick(body: TickIn, c=Depends(conn)):
    r = simulator.generate(c, body.project, max(1, min(body.n, 500)))
    events.publish("traces", count=r["added"], project=body.project)
    return r


@app.get("/api/events/stream")
async def stream(last_id: int = 0):
    sbx = sandbox.current_id()

    async def gen():
        last = last_id
        for _ in range(3600):
            for e in events.since(last, sbx):
                last = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e, default=str)}\n\n"
            yield ": keep-alive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/events/recent")
def recent():
    return events.recent(50, sandbox.current_id())


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
