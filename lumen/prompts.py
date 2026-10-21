"""Prompt registry with labels. 'production' can only move to a version with a passing release gate
(promotion) or to a version the Critic accepted as a rollback target - always with a named human approval."""
import json

from . import events, ledger
from .db import jl, now_iso, one, rows, tx
from .guardrails import authorize


def create_version(conn, project_id, name, template, model, params, by, note=""):
    r = one(conn, "SELECT MAX(version) v FROM prompts WHERE project_id=? AND name=?", (project_id, name))
    v = (r["v"] or 0) + 1
    with tx(conn):
        conn.execute("INSERT INTO prompts VALUES(?,?,?,?,?,?,?,?,?)",
                     (project_id, name, v, template, model, json.dumps(params or {}), now_iso(), by, note))
        ledger.append(conn, "PROMPT_VERSION_CREATED", f"human:{by}", f"{name}:v{v}",
                      {"project": project_id, "sha256": ledger.sha256(template), "model": model})
    return v


def label_version(conn, project_id, name, label):
    r = one(conn, "SELECT version FROM prompt_labels WHERE project_id=? AND name=? AND label=?", (project_id, name, label))
    return r["version"] if r else None


def get(conn, project_id, name, label=None, version=None):
    if version is None:
        version = label_version(conn, project_id, name, label or "production")
    if version is None:
        return None
    p = one(conn, "SELECT * FROM prompts WHERE project_id=? AND name=? AND version=?", (project_id, name, int(version)))
    if p:
        p["params"] = jl(p["params"], {})
    return p


def listing(conn, project_id):
    out = {}
    for p in rows(conn, "SELECT project_id,name,version,model,created_at,created_by,note FROM prompts WHERE project_id=? "
                        "ORDER BY name, version DESC", (project_id,)):
        out.setdefault(p["name"], {"name": p["name"], "versions": [], "labels": {}})["versions"].append(p)
    for l in rows(conn, "SELECT * FROM prompt_labels WHERE project_id=?", (project_id,)):
        if l["name"] in out:
            out[l["name"]]["labels"][l["label"]] = {"version": l["version"], "updated_at": l["updated_at"], "updated_by": l["updated_by"]}
    for p in out.values():
        for v in p["versions"]:
            gates = rows(conn, "SELECT id, summary FROM experiments WHERE project_id=? AND prompt_name=? AND prompt_version=? "
                               "ORDER BY created_at DESC", (project_id, p["name"], str(v["version"])))
            g = [jl(x["summary"], {}).get("gate") for x in gates]
            g = [x for x in g if x]
            v["gate"] = {"experiment": g[0]["experiment"], "passed": g[0]["passed"]} if g else None
    return list(out.values())


def gate_passed(conn, project_id, name, version):
    for x in rows(conn, "SELECT summary FROM experiments WHERE project_id=? AND prompt_name=? AND prompt_version=? "
                        "ORDER BY created_at DESC", (project_id, name, str(version))):
        g = jl(x["summary"], {}).get("gate")
        if g:
            return bool(g["passed"])
    return False


def _set_label(conn, project_id, name, label, version, by):
    conn.execute("INSERT INTO prompt_labels VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,name,label) DO UPDATE SET "
                 "version=excluded.version, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                 (project_id, name, label, version, now_iso(), by))


def promote(conn, project_id, name, version, by, note="", approved=True):
    by = (by or "").strip()
    if not by:
        raise ValueError("a named approver is required")
    if not one(conn, "SELECT 1 x FROM prompts WHERE project_id=? AND name=? AND version=?", (project_id, name, version)):
        raise ValueError("unknown prompt version")
    ok = gate_passed(conn, project_id, name, version)
    d = authorize(conn, f"human:{by}", "promote_prompt", f"{name}:v{version}", project_id,
                  context={"approved": approved, "gate_passed": ok})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    cur = label_version(conn, project_id, name, "production")
    with tx(conn):
        if cur is not None and cur != version:
            _set_label(conn, project_id, name, "previous_production", cur, by)
        _set_label(conn, project_id, name, "production", version, by)
        ledger.append(conn, "PROMPT_PROMOTED", f"human:{by}", f"{name}:v{version}",
                      {"project": project_id, "from": cur, "to": version, "note": note})
    events.publish("prompt", name=name, version=version, action="promoted", by=by)
    return {"ok": True, "production": version, "previous": cur}


def rollback(conn, project_id, name, to_version, by, critic_status, proposal_id=None, note=""):
    d = authorize(conn, f"human:{by}", "rollback_prompt", f"{name}:v{to_version}", project_id,
                  context={"approved": True, "critic_status": critic_status})
    if d["decision"] != "allow":
        return {"ok": False, **d}
    cur = label_version(conn, project_id, name, "production")
    with tx(conn):
        if cur is not None:
            _set_label(conn, project_id, name, "previous_production", cur, by)
        _set_label(conn, project_id, name, "production", to_version, by)
        ledger.append(conn, "PROMPT_ROLLED_BACK", f"human:{by}", f"{name}:v{to_version}",
                      {"project": project_id, "from": cur, "to": to_version, "proposal": proposal_id, "note": note})
    events.publish("prompt", name=name, version=to_version, action="rolled back", by=by)
    return {"ok": True, "production": to_version, "previous": cur}
