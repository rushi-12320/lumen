"""Lumen Python SDK - standard library only. Copy this file into your app.

    from lumen_sdk import Lumen
    lumen = Lumen(api_key="lmn_...", host="http://127.0.0.1:8100")

    @lumen.trace(name="rag_answer", prompt_name="rag_answer")
    def answer(question): ...

    with lumen.start_trace("rag_answer", input=q, prompt_name="rag_answer", prompt_version=3) as t:
        with t.span("retrieval", name="kb_search", input=q) as s:
            s.output = docs
        with t.span("llm", name="generate", model="smart-model") as s:
            s.input, s.output, s.tokens_in, s.tokens_out = prompt, text, 812, 64
        t.output = text

    tpl = lumen.get_prompt("rag_answer")      # current production prompt (version, template, model)

Traces are sent in the background in batches; failures never break your app.
"""
import atexit
import functools
import json
import queue
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Span:
    def __init__(self, trace, type_, name=None, model=None, input=None, parent_id=None):
        self.trace, self.type, self.name, self.model, self.input = trace, type_, name or type_, model, input
        self.output = self.error = None
        self.tokens_in = self.tokens_out = None
        self.attrs = {}
        self.id = f"s{len(trace.spans) + 1}"
        self.parent_id = parent_id
        self._t0 = None

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, et, ev, tb):
        if ev is not None:
            self.error = f"{et.__name__}: {ev}"
        self.trace.spans.append({"id": self.id, "parent_id": self.parent_id, "type": self.type, "name": self.name, "model": self.model,
                                 "input": self.input, "output": self.output, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                                 "latency_ms": round((time.perf_counter() - self._t0) * 1000, 1),
                                 "start_ms": round((self._t0 - self.trace._t0) * 1000, 1),
                                 "status": "error" if self.error else "ok", "error": self.error, "attrs": self.attrs})
        return False


class Trace:
    def __init__(self, client, name, input=None, user_id=None, session_id=None, prompt_name=None, prompt_version=None, tags=None, **metadata):
        self.client, self.name, self.input, self.output = client, name, input, None
        self.user_id, self.session_id, self.tags, self.metadata = user_id, session_id, tags or [], metadata
        self.prompt_name, self.prompt_version = prompt_name, prompt_version
        self.spans, self.error, self.id = [], None, uuid.uuid4().hex
        self._t0 = None

    def span(self, type_, **kw):
        return Span(self, type_, **kw)

    def __enter__(self):
        self._t0 = time.perf_counter()
        self._ts = _now()
        return self

    def __exit__(self, et, ev, tb):
        if ev is not None:
            self.error = f"{et.__name__}: {ev}"
        self.client._enqueue({"id": self.id, "name": self.name, "ts": self._ts, "user_id": self.user_id, "session_id": self.session_id,
                              "tags": self.tags, "metadata": self.metadata, "prompt_name": self.prompt_name,
                              "prompt_version": self.prompt_version, "input": self.input, "output": self.output, "spans": self.spans,
                              "latency_ms": round((time.perf_counter() - self._t0) * 1000, 1), "status": "error" if self.error else "ok"})
        return False


class Lumen:
    def __init__(self, api_key, host="http://127.0.0.1:8100", batch_size=20, flush_interval=2.0, enabled=True, timeout=5):
        self.api_key, self.host, self.enabled, self.timeout = api_key, host.rstrip("/"), enabled, timeout
        self.batch_size, self.flush_interval = batch_size, flush_interval
        self._q, self._prompts, self.dropped = queue.Queue(maxsize=10000), {}, 0
        if enabled:
            threading.Thread(target=self._worker, daemon=True).start()
            atexit.register(self.flush)

    # ---- tracing
    def start_trace(self, name, **kw):
        return Trace(self, name, **kw)

    def trace(self, name=None, **kw):
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                inp = args[0] if len(args) == 1 and not kwargs else {"args": [repr(a) for a in args], "kwargs": {k: repr(v) for k, v in kwargs.items()}}
                with self.start_trace(name or fn.__name__, input=inp, **kw) as t:
                    with t.span("llm", name=fn.__name__, input=inp) as s:
                        out = fn(*args, **kwargs)
                        s.output = out if isinstance(out, str) else repr(out)
                    t.output = s.output
                return out
            return wrapper
        return deco

    # ---- prompt registry
    def get_prompt(self, name, label="production", ttl=60):
        k = (name, label)
        hit = self._prompts.get(k)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        req = urllib.request.Request(f"{self.host}/v1/prompts/{name}?label={label}", headers={"X-Lumen-Key": self.api_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            p = json.loads(r.read().decode())
        self._prompts[k] = (time.time(), p)
        return p

    # ---- transport
    def _enqueue(self, t):
        if not self.enabled:
            return
        try:
            self._q.put_nowait(t)
        except queue.Full:
            self.dropped += 1

    def _send(self, batch):
        body = json.dumps({"traces": batch}, default=str).encode()
        req = urllib.request.Request(f"{self.host}/v1/traces", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Lumen-Key": self.api_key})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        self.dropped += len(batch)

    def _drain(self):
        batch = []
        while len(batch) < self.batch_size:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    def _worker(self):
        while True:
            time.sleep(self.flush_interval)
            b = self._drain()
            while b:
                self._send(b)
                b = self._drain()

    def flush(self):
        b = self._drain()
        while b:
            self._send(b)
            b = self._drain()
