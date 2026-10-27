"""In-memory event bus for the live activity stream (Server-Sent Events)."""
import itertools
import threading

from .db import now_iso

_lock = threading.Lock()
_events = []
_ids = itertools.count(1)


def publish(kind, **data):
    from .sandbox import current_id
    with _lock:
        e = {"id": next(_ids), "ts": now_iso(), "kind": kind, "sbx": current_id(), **data}
        _events.append(e)
        del _events[:-500]
    return e


def since(last_id, sbx=None):
    with _lock:
        return [e for e in _events if e["id"] > last_id and e.get("sbx") == sbx]


def recent(n=50, sbx=None):
    with _lock:
        return [e for e in _events if e.get("sbx") == sbx][-n:]
