"""In-memory event bus for the live activity stream (Server-Sent Events)."""
import itertools
import threading

from .db import now_iso

_lock = threading.Lock()
_events = []
_ids = itertools.count(1)


def publish(kind, **data):
    """`seq` numbers the stream. It is set after the payload, so a payload `id` (incident, proposal, request id)
    can no longer overwrite it: a text id there used to crash every live stream."""
    from .sandbox import current_id
    with _lock:
        e = {**data, "seq": next(_ids), "ts": now_iso(), "kind": kind, "sbx": current_id()}
        _events.append(e)
        del _events[:-500]
    return e


def since(last_seq, sbx=None):
    with _lock:
        return [e for e in _events if e["seq"] > last_seq and e.get("sbx") == sbx]


def recent(n=50, sbx=None):
    with _lock:
        return [e for e in _events if e.get("sbx") == sbx][-n:]
