"""Human decisions on agent proposals. Guardrails re-check at decision time."""
from . import events, evals, ledger, prompts
from .db import jl, now_iso, one, tx
from .guardrails import authorize


def decide(conn, pid, decision, by, note=""):
    by = (by or "").strip()
    if not by:
        raise ValueError("a named approver is required")
    p = one(conn, "SELECT * FROM proposals WHERE id=?", (pid,))
    if not p:
        raise ValueError("proposal not found")
    if p["status"] not in ("pending_approval", "critic_blocked"):
        raise ValueError(f"proposal is already {p['status']}")
    payload = jl(p["payload"], {})

    def close(status, extra=None):
        with tx(conn):
            conn.execute("UPDATE proposals SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?", (status, by, now_iso(), note, pid))
            ledger.append(conn, "HUMAN_APPROVAL" if status != "rejected" else "HUMAN_REJECTION", f"human:{by}", pid,
                          {"status": status, "note": note, **(extra or {})})
        events.publish("decision", id=pid, decision=status, by=by)
        return {"ok": True, "status": status, **(extra or {})}

    if decision == "reject":
        return close("rejected")
    if decision != "approve":
        raise ValueError("decision must be approve or reject")
    if p["status"] == "critic_blocked":
        return {"ok": False, "decision": "block", "reasons": ["the Critic blocked this proposal - it cannot be approved"]}

    if p["kind"] == "rollback_prompt":
        r = prompts.rollback(conn, p["project_id"], payload["prompt"], int(payload["to"]), by, "passed", pid, note)
        return close("applied", {"production": r["production"]}) if r["ok"] else r
    if p["kind"] == "dataset_items":
        ds = one(conn, "SELECT id FROM datasets WHERE project_id=? AND name=?", (p["project_id"], payload.get("dataset", "regression-set")))
        did = ds["id"] if ds else evals.create_dataset(conn, p["project_id"], payload.get("dataset", "regression-set"),
                                                       "Failing production traces collected by the Curator agent", by)
        r = evals.add_items(conn, did, evals.items_from_traces(conn, p["project_id"], payload["trace_ids"]), by, True, f"proposal:{pid}")
        return close("applied", {"dataset": did, "added": r.get("added")}) if r["ok"] else r
    g = authorize(conn, f"human:{by}", "acknowledge_advisory", pid, p["project_id"], context={"approved": True})
    if g["decision"] != "allow":
        return {"ok": False, **g}
    return close("approved", {"work_item": f"WI-{pid[4:]}"})
