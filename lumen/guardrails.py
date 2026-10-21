"""Policy engine for agents and people. Every change goes through authorize(); unknown actions are denied."""
import copy
import json

from . import ledger
from .db import get_setting, now_iso, set_setting, tx

DEFAULT_POLICY = {
    "agents_paused": False,
    "redact_pii": True,
    "slo_ms": 4000,
    "data_window_hours": 24,
    "baseline_hours": 24,
    "min_evidence_traces": 5,
    "max_proposals_per_day": 10,
    "max_steps_per_run": 400,
    "gate_min_pass_rate": 0.90,
    "gate_max_regression_pct": 2.0,
    "gate_max_cost_increase_pct": 15.0,
    "gate_block_on_pii": True,
    "max_llm_calls_per_request": 200,
    "llm_allow_non_local": False,
    "permissions": {
        "read_data": "auto",
        "raise_incident": "auto",
        "propose_rollback": "auto",
        "propose_dataset_items": "auto",
        "propose_advisory": "auto",
        "generate_report": "auto",
        "request_llm": "auto",
        "run_llm": "human_approval",
        "rollback_prompt": "human_approval",
        "promote_prompt": "human_approval",
        "add_dataset_items": "human_approval",
        "acknowledge_advisory": "human_approval",
        "create_api_key": "admin",
        "change_policy": "admin",
        "change_llm_settings": "admin",
        "delete_data": "forbidden",
        "modify_ledger": "forbidden",
        "call_cloud_llm": "forbidden",
        "read_secrets": "forbidden",
        "export_raw_data": "forbidden",
    },
}
LOGGED_ALLOWS = {"run_llm", "rollback_prompt", "promote_prompt", "add_dataset_items", "create_api_key", "change_policy",
                 "change_llm_settings", "acknowledge_advisory"}


class BudgetExceeded(Exception):
    pass


def get_policy(conn):
    pol = copy.deepcopy(DEFAULT_POLICY)
    stored = get_setting(conn, "policy", {}) or {}
    pol.update({k: v for k, v in stored.items() if k != "permissions"})
    pol["permissions"] = {**pol["permissions"], **stored.get("permissions", {})}
    return pol


def evaluate(policy, actor, action, context=None):
    context = context or {}
    agent = actor.startswith("agent:")
    perm = policy["permissions"].get(action)
    if perm is None:
        return {"decision": "block", "reasons": [f"unknown action '{action}' (default deny)"]}
    if perm == "forbidden":
        return {"decision": "block", "reasons": [f"'{action}' is forbidden by policy"]}
    if agent and policy.get("agents_paused") and action != "read_data":
        return {"decision": "block", "reasons": ["kill switch: agents are paused"]}
    if perm == "admin" and (agent or not context.get("admin")):
        return {"decision": "block", "reasons": [f"'{action}' needs a human administrator"]}
    reasons = []
    if action == "run_llm":
        if not context.get("endpoint_ok"):
            reasons.append(context.get("endpoint_reason") or "LLM endpoint not allowed")
        if context.get("n_calls", 0) > policy["max_llm_calls_per_request"]:
            reasons.append(f"{context['n_calls']} calls exceed limit {policy['max_llm_calls_per_request']}")
        if not context.get("llm_enabled"):
            reasons.append("local LLM is disabled in settings")
    if action == "promote_prompt" and not context.get("gate_passed"):
        reasons.append("no passing release-gate experiment for this prompt version")
    if action == "rollback_prompt" and context.get("critic_status") != "passed":
        reasons.append("Critic did not pass this rollback")
    if action.startswith("propose") and context.get("proposals_today", 0) >= policy["max_proposals_per_day"]:
        reasons.append("daily proposal limit reached")
    if reasons:
        return {"decision": "block", "reasons": reasons}
    if perm == "human_approval":
        if agent:
            return {"decision": "needs_approval", "reasons": ["agents may only propose; a named person must approve"]}
        if not context.get("approved"):
            return {"decision": "needs_approval", "reasons": ["no recorded human approval"]}
    return {"decision": "allow", "reasons": ["permitted by policy"]}


def authorize(conn, actor, action, target, project_id=None, run_id=None, context=None, policy=None):
    policy = policy or get_policy(conn)
    res = evaluate(policy, actor, action, context)
    with tx(conn):
        conn.execute("INSERT INTO guard_log(ts,project_id,actor,action,target,decision,reasons,run_id) VALUES(?,?,?,?,?,?,?,?)",
                     (now_iso(), project_id, actor, action, target, res["decision"], json.dumps(res["reasons"]), run_id))
        if res["decision"] != "allow" or action in LOGGED_ALLOWS:
            ledger.append(conn, "GUARD_DECISION", actor, target,
                          {"action": action, "decision": res["decision"], "reasons": res["reasons"], "project": project_id})
    return res


def set_policy(conn, changes, by):
    d = authorize(conn, f"human:{by}", "change_policy", "policy", context={"admin": True})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    old = get_policy(conn)
    new = copy.deepcopy(old)
    for k, v in changes.items():
        if k == "permissions":
            for a, p in v.items():
                if DEFAULT_POLICY["permissions"].get(a) == "forbidden" and p != "forbidden":
                    return {"ok": False, "decision": "block", "reasons": [f"'{a}' is hard-forbidden"]}
                new["permissions"][a] = p
        elif k in DEFAULT_POLICY:
            t = type(DEFAULT_POLICY[k])
            new[k] = (v in (True, "true", "1", 1)) if t is bool else t(v)
    diff = {k: {"from": old[k], "to": new[k]} for k in new if k != "permissions" and old[k] != new[k]}
    with tx(conn):
        set_setting(conn, "policy", new)
        ledger.append(conn, "POLICY_CHANGED", f"human:{by}", "policy", diff or {"note": "no change"})
    return {"ok": True, "diff": diff, "policy": new}
