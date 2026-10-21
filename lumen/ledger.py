"""Append-only, SHA-256 hash-chained audit ledger.

Every entry stores prev_hash; entry_hash = SHA-256 over the canonical JSON of the entry.
Changing any stored entry (or deleting one) breaks the chain at that point, which verify() reports.
"""
import hashlib
import json

from .db import now_iso, one, rows

GENESIS_PREV = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_hash(e) -> str:
    return sha256(canonical({k: e[k] for k in ("seq", "ts", "event_type", "actor", "target", "payload", "prev_hash")}))


def append(conn, event_type, actor, target, payload) -> dict:
    """Caller holds the write transaction."""
    last = one(conn, "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1")
    e = {"seq": (last["seq"] + 1) if last else 0, "ts": now_iso(), "event_type": event_type,
         "actor": actor, "target": target or "", "payload": canonical(payload or {}),
         "prev_hash": last["entry_hash"] if last else GENESIS_PREV}
    e["entry_hash"] = _entry_hash(e)
    conn.execute("INSERT INTO ledger(seq,ts,event_type,actor,target,payload,prev_hash,entry_hash) "
                 "VALUES(:seq,:ts,:event_type,:actor,:target,:payload,:prev_hash,:entry_hash)", e)
    return e


def merkle_root(hashes):
    if not hashes:
        return None
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def verify_entries(entries):
    prev = GENESIS_PREV
    for i, e in enumerate(entries):
        if e["seq"] != i:
            return {"ok": False, "broken_at": e["seq"], "reason": f"sequence gap: expected {i}, found {e['seq']}"}
        if e["prev_hash"] != prev:
            return {"ok": False, "broken_at": e["seq"], "reason": "prev_hash does not match previous entry"}
        if _entry_hash(e) != e["entry_hash"]:
            return {"ok": False, "broken_at": e["seq"], "reason": "entry content changed after it was sealed"}
        prev = e["entry_hash"]
    return {"ok": True, "broken_at": None, "reason": None}


def verify(conn) -> dict:
    entries = rows(conn, "SELECT * FROM ledger ORDER BY seq")
    res = verify_entries(entries)
    res.update({"count": len(entries), "merkle_root": merkle_root([e["entry_hash"] for e in entries]),
                "head": entries[-1]["entry_hash"] if entries else None})
    return res


def tamper_demo(conn, seq=None) -> dict:
    """Shows what verification reports if someone edits a sealed entry. Works on an in-memory copy only."""
    entries = rows(conn, "SELECT * FROM ledger ORDER BY seq")
    if len(entries) < 2:
        return {"error": "ledger too short"}
    seq = seq if seq is not None else len(entries) // 2
    seq = max(0, min(seq, len(entries) - 1))
    copy = [dict(e) for e in entries]
    payload = json.loads(copy[seq]["payload"] or "{}")
    payload["_tampered"] = "value edited outside the application"
    copy[seq]["payload"] = canonical(payload)
    before = verify_entries(entries)
    after = verify_entries(copy)
    return {"tampered_seq": seq, "event_type": entries[seq]["event_type"], "original_chain": before,
            "after_tampering": after,
            "explanation": "The stored entry_hash no longer matches its content, so the chain breaks at "
                           f"seq {after['broken_at']}. Every later entry depends on this hash too."}
