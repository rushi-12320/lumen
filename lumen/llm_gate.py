"""Local-LLM permission gate (Ollama).

* No cloud provider code, no API keys, environment variables are never read, no auth headers are sent.
* Every LLM job is a request in a queue: purpose, model, endpoint, number of calls, estimated tokens and a
  redacted preview of exactly what will be sent. Nothing is sent until a named person approves it.
* The endpoint must be on this machine or a private network unless an admin allows otherwise.
* Prompts are PII-redacted again just before sending.
"""
import ipaddress
import json
import threading
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

from . import events, ledger
from .db import get_setting, jl, now_iso, one, set_setting, tx
from .guardrails import authorize, get_policy
from .redact import redact

DEFAULT_LLM = {"enabled": False, "base_url": "http://localhost:11434", "model": "llama3.1", "timeout_s": 120,
               "max_output_tokens": 256}
_handlers = {}  # purpose -> function(conn, request, results)


def on_complete(purpose):
    def deco(fn):
        _handlers[purpose] = fn
        return fn
    return deco


def config(conn):
    return {**DEFAULT_LLM, **(get_setting(conn, "llm", {}) or {})}


def set_config(conn, changes, by):
    d = authorize(conn, f"human:{by}", "change_llm_settings", "llm", context={"admin": True})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    cfg = config(conn)
    for k in ("enabled", "base_url", "model", "timeout_s", "max_output_tokens"):
        if k in changes:
            cfg[k] = changes[k] if k in ("base_url", "model") else (bool(changes[k]) if k == "enabled" else int(changes[k]))
    ok, reason = check_endpoint(cfg["base_url"], get_policy(conn)["llm_allow_non_local"])
    with tx(conn):
        set_setting(conn, "llm", cfg)
        ledger.append(conn, "LLM_SETTINGS_CHANGED", f"human:{by}", "llm",
                      {"enabled": cfg["enabled"], "base_url": cfg["base_url"], "model": cfg["model"], "endpoint_ok": ok})
    return {"ok": True, "config": cfg, "endpoint_ok": ok, "endpoint_reason": reason}


def check_endpoint(url, allow_non_local=False):
    try:
        u = urlparse(url)
    except ValueError:
        return False, "invalid URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "endpoint must be an http(s) URL"
    host = u.hostname
    local = host in ("localhost",) or host.endswith(".localhost")
    if not local:
        try:
            ip = ipaddress.ip_address(host)
            local = ip.is_loopback or ip.is_private
        except ValueError:
            local = False
    if local:
        return True, "local / private endpoint"
    if allow_non_local:
        return True, "non-local endpoint allowed by admin policy"
    return False, f"'{host}' is not a local or private address (data residency policy)"


def estimate_tokens(prompts, max_out):
    return int(sum(len(p) for p in prompts) / 4) + max_out * len(prompts)


def create_request(conn, project_id, purpose, target, prompts, created_by, meta=None):
    cfg, pol = config(conn), get_policy(conn)
    clean = [redact(p, True)[0] for p in prompts]
    if not clean:
        return {"ok": False, "decision": "block", "reasons": ["nothing to send"]}
    actor = created_by if ":" in created_by else f"human:{created_by}"
    d = authorize(conn, actor, "request_llm", target, project_id)
    if d["decision"] != "allow":
        return {"ok": False, **d}
    rid = "LLM-" + uuid.uuid4().hex[:8].upper()
    req = {"id": rid, "project_id": project_id, "created_at": now_iso(), "created_by": actor, "purpose": purpose,
           "target": target, "model": cfg["model"], "endpoint": cfg["base_url"], "n_calls": len(clean),
           "est_tokens": estimate_tokens(clean, cfg["max_output_tokens"]), "preview": clean[0][:1500] if clean else "",
           "payload": json.dumps({"prompts": clean, "meta": meta or {}}), "status": "pending"}
    with tx(conn):
        conn.execute("INSERT INTO llm_requests(id,project_id,created_at,created_by,purpose,target,model,endpoint,n_calls,"
                     "est_tokens,preview,payload,status) VALUES(:id,:project_id,:created_at,:created_by,:purpose,:target,"
                     ":model,:endpoint,:n_calls,:est_tokens,:preview,:payload,:status)", req)
        ledger.append(conn, "LLM_REQUEST_CREATED", actor, rid,
                      {"purpose": purpose, "target": target, "n_calls": len(clean), "est_tokens": req["est_tokens"],
                       "endpoint": cfg["base_url"], "model": cfg["model"], "prompts_sha256": ledger.sha256("".join(clean))})
    events.publish("llm_request", id=rid, purpose=purpose, n_calls=len(clean))
    return {"ok": True, "id": rid, "n_calls": len(clean), "est_tokens": req["est_tokens"],
            "within_limit": len(clean) <= pol["max_llm_calls_per_request"]}


def ollama_generate(base_url, model, prompt, timeout=120, max_tokens=256, opener=None):
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0, "num_predict": max_tokens}}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
        j = json.loads(r.read().decode())
    return {"text": j.get("response", ""), "tokens_in": j.get("prompt_eval_count"), "tokens_out": j.get("eval_count")}


def test_connection(conn, by):
    cfg = config(conn)
    ok, reason = check_endpoint(cfg["base_url"], get_policy(conn)["llm_allow_non_local"])
    if not ok:
        return {"ok": False, "reason": reason}
    try:  # sends no prompt data: only lists installed models
        with urllib.request.urlopen(cfg["base_url"].rstrip("/") + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode())
        models = [m.get("name") for m in tags.get("models", [])]
        return {"ok": True, "models": models, "model_installed": any(m.split(":")[0] == cfg["model"].split(":")[0] for m in models)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "reason": f"cannot reach {cfg['base_url']}: {e}"}


def decide(conn, rid, decision, by, run_async=True):
    by = (by or "").strip()
    if not by:
        raise ValueError("a named approver is required")
    req = one(conn, "SELECT * FROM llm_requests WHERE id=?", (rid,))
    if not req:
        raise ValueError("request not found")
    if req["status"] != "pending":
        raise ValueError(f"request is already {req['status']}")
    if decision == "reject":
        with tx(conn):
            conn.execute("UPDATE llm_requests SET status='rejected', decided_by=?, decided_at=? WHERE id=?", (by, now_iso(), rid))
            ledger.append(conn, "LLM_REQUEST_REJECTED", f"human:{by}", rid, {})
        return {"ok": True, "status": "rejected"}
    cfg, pol = config(conn), get_policy(conn)
    ok, reason = check_endpoint(cfg["base_url"], pol["llm_allow_non_local"])
    d = authorize(conn, f"human:{by}", "run_llm", rid, req["project_id"],
                  context={"approved": True, "endpoint_ok": ok and cfg["base_url"] == req["endpoint"],
                           "endpoint_reason": reason if not ok else "endpoint changed since the request was made",
                           "n_calls": req["n_calls"], "llm_enabled": cfg["enabled"]})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    with tx(conn):
        conn.execute("UPDATE llm_requests SET status='running', decided_by=?, decided_at=? WHERE id=?", (by, now_iso(), rid))
        ledger.append(conn, "LLM_REQUEST_APPROVED", f"human:{by}", rid, {"n_calls": req["n_calls"]})
    if run_async:
        import contextvars
        ctx = contextvars.copy_context()  # keep the request's database (demo sandbox)
        threading.Thread(target=ctx.run, args=(_execute, rid), daemon=True).start()
        return {"ok": True, "status": "running"}
    return _execute(rid, conn)


def _execute(rid, conn=None):
    from .db import connect
    own = conn is None
    conn = conn or connect()
    try:
        req = one(conn, "SELECT * FROM llm_requests WHERE id=?", (rid,))
        cfg = config(conn)
        prompts = jl(req["payload"], {}).get("prompts", [])
        results, errors = [], 0
        for p in prompts:
            try:
                results.append(ollama_generate(req["endpoint"], req["model"], p, cfg["timeout_s"], cfg["max_output_tokens"]))
            except Exception as e:  # keep going, record the error
                errors += 1
                results.append({"error": str(e)[:200]})
        status = "executed" if errors < len(prompts) or not prompts else "failed"
        handled = None
        h = _handlers.get(req["purpose"])
        if h and status == "executed":
            try:
                handled = h(conn, req, results)
            except Exception as e:
                handled = {"handler_error": str(e)[:200]}
        summary = {"calls": len(prompts), "errors": errors, "handled": handled,
                   "tokens_in": sum(r.get("tokens_in") or 0 for r in results), "tokens_out": sum(r.get("tokens_out") or 0 for r in results)}
        with tx(conn):
            conn.execute("UPDATE llm_requests SET status=?, result=? WHERE id=?", (status, json.dumps(summary), rid))
            ledger.append(conn, "LLM_REQUEST_EXECUTED", "system", rid,
                          {**{k: summary[k] for k in ("calls", "errors", "tokens_in", "tokens_out")},
                           "outputs_sha256": ledger.sha256(json.dumps(results, sort_keys=True))})
        events.publish("llm_request", id=rid, status=status)
        return {"ok": status == "executed", "status": status, **summary}
    finally:
        if own:
            conn.close()
