"""In-memory event bus for the live activity stream (Server-Sent Events)."""
import itertools
import threading

from .db import now_iso

_lock = threading.Lock()
_events = []
_ids = itertools.count(1)


def publish(kind, **data):
    with _lock:
        e = {"id": next(_ids), "ts": now_iso(), "kind": kind, **data}
        _events.append(e)
        del _events[:-500]
    return e


def since(last_id):
    with _lock:
        return [e for e in _events if e["id"] > last_id]


def recent(n=50):
    with _lock:
        return list(_events[-n:])
