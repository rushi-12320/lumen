"""Lumen Python SDK - standard library only, one file.

Two lines, and every OpenAI, Anthropic and Google Gemini call in your app is traced:

    pip install https://<your-lumen>/sdk/lumen_sdk-0.2.0-py3-none-any.whl     (or copy this file)

    import lumen_sdk
    lumen_sdk.init(api_key="lmn_...", host="https://<your-lumen>")   # or set LUMEN_API_KEY and LUMEN_HOST

Each call is recorded with its model, prompt, answer, tokens, latency and errors, for normal, async and streaming
calls. Providers used through the openai package (Azure OpenAI, Groq, Together, DeepSeek, vLLM, Ollama) count too.

One request = one trace (for a RAG answer: the retrieval and the LLM call):

    with lumen_sdk.trace("rag_answer", input=question, user_id=uid) as t:
        with t.span("retrieval", name="kb_search", input=question) as s:
            s.output = docs                                    # turns on the groundedness check
        answer = client.chat.completions.create(...)           # recorded inside this trace
        t.output = answer.choices[0].message.content

    @lumen_sdk.observe("support_answer")                      # each call of the function is one trace
    def support_answer(question): ...

    p = lumen_sdk.get_prompt("rag_answer")                     # the production prompt from the registry

The client from version 0.1 still works:  lumen = Lumen(api_key=..., host=...); lumen.start_trace(...)

Traces are sent in the background in batches. Lumen never changes your calls or their results, and a problem
inside Lumen never breaks your app: it is logged (logger "lumen_sdk") and the trace is dropped.
"""
import atexit
import contextvars
import functools
import inspect
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

__version__ = "0.2.0"
__all__ = ["init", "trace", "observe", "flush", "get_prompt", "instrument", "Lumen", "Trace", "Span"]

log = logging.getLogger("lumen_sdk")
MAX_TEXT = 100_000                   # characters sent per text field
_CURRENT = contextvars.ContextVar("lumen_sdk_trace", default=None)
client = None                        # the Lumen client set up by init()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def _repr(v):
    """repr() that never raises (objects whose repr fails, e.g. a closed database session, are named instead)."""
    try:
        return repr(v)
    except Exception:
        return f"<{type(v).__name__}>"


def _cut(v):
    if v is None or isinstance(v, (int, float, bool)):
        return v
    try:
        s = v if isinstance(v, str) else json.dumps(v, default=_repr, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else _repr(v)
    except Exception:
        s = _repr(v)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + " ...[cut]"


class Span:
    def __init__(self, trace, type_, name=None, model=None, input=None, parent_id=None):
        self.trace, self.type, self.name, self.model, self.input = trace, type_, name or type_, model, input
        self.output = self.error = None
        self.tokens_in = self.tokens_out = None
        self.attrs = {}
        self.id = trace._next_id()
        self.parent_id = parent_id
        self._t0 = None

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, et, ev, tb):
        try:
            if ev is not None:
                self.error = _err(ev)
            t0 = self._t0 if self._t0 is not None else time.perf_counter()
            self.trace.spans.append({"id": self.id, "parent_id": self.parent_id, "type": self.type, "name": self.name,
                                     "model": self.model, "input": _cut(self.input), "output": _cut(self.output),
                                     "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                                     "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                                     "start_ms": round((t0 - self.trace._t0) * 1000, 1) if self.trace._t0 else 0,
                                     "status": "error" if self.error else "ok", "error": self.error, "attrs": self.attrs})
        except Exception:
            log.debug("lumen_sdk: could not record a span", exc_info=True)
        return False


class Trace:
    def __init__(self, client, name, input=None, user_id=None, session_id=None, prompt_name=None, prompt_version=None, tags=None, **metadata):
        self.client, self.name, self.input, self.output = client, name, input, None
        self.user_id, self.session_id, self.tags, self.metadata = user_id, session_id, tags or [], metadata
        self.prompt_name, self.prompt_version = prompt_name, prompt_version
        self.spans, self.error, self.id = [], None, uuid.uuid4().hex
        self._t0, self._ts, self._n, self._user, self._token, self._closed = None, None, 0, None, None, False

    def _next_id(self):
        self._n += 1
        return f"s{self._n}"

    def span(self, type_, **kw):
        return Span(self, type_, **kw)

    def _add(self, span, t0, user_text):
        """A span recorded by the automatic instrumentation while this trace is open."""
        span["id"] = self._next_id()
        span["start_ms"] = round((t0 - self._t0) * 1000, 1) if self._t0 else 0
        if self._user is None and user_text:
            self._user = user_text
        self.spans.append(span)

    def __enter__(self):
        self._t0 = time.perf_counter()
        self._ts = _now()
        try:
            self._token = _CURRENT.set(self)
        except Exception:
            self._token = None
        return self

    def __exit__(self, et, ev, tb):
        if self._token is not None:
            try:
                _CURRENT.reset(self._token)
            except Exception:                 # exited in another context (e.g. another task): nothing to undo
                pass
        if ev is not None:
            self.error = _err(ev)
        self._closed = True
        try:
            self.client._enqueue({"id": self.id, "name": self.name, "ts": self._ts or _now(), "user_id": self.user_id,
                                  "session_id": self.session_id, "tags": self.tags, "metadata": self.metadata,
                                  "prompt_name": self.prompt_name, "prompt_version": self.prompt_version,
                                  "input": _cut(self.input if self.input is not None else self._user), "output": _cut(self.output),
                                  "spans": self.spans, "latency_ms": round((time.perf_counter() - (self._t0 or time.perf_counter())) * 1000, 1),
                                  "status": "error" if self.error else "ok"})
        except Exception:
            log.debug("lumen_sdk: could not queue the trace", exc_info=True)
        return False


class Lumen:
    def __init__(self, api_key=None, host=None, batch_size=20, flush_interval=2.0, enabled=True, timeout=5):
        self.api_key = api_key or os.environ.get("LUMEN_API_KEY") or ""
        self.host = (host or os.environ.get("LUMEN_HOST") or "http://127.0.0.1:8100").rstrip("/")
        self.enabled, self.timeout = bool(enabled), timeout
        if self.enabled and not self.api_key:
            log.warning("lumen_sdk: no API key (pass api_key= or set LUMEN_API_KEY); traces are not sent")
            self.enabled = False
        self.batch_size, self.flush_interval = batch_size, flush_interval
        self._q, self._prompts, self.dropped = queue.Queue(maxsize=10000), {}, 0
        self._pid, self._thread, self._lock, self._warned = None, None, threading.Lock(), set()
        self.instrumented = []
        if self.enabled:
            atexit.register(self.flush)

    # ---- tracing
    def start_trace(self, name, **kw):
        return Trace(self, name, **kw)

    def trace(self, name=None, **kw):
        """Decorator from version 0.1: the function becomes one trace with one llm span."""
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                inp = _args_text(args, kwargs)
                with self.start_trace(name or fn.__name__, input=inp, **kw) as t:
                    with t.span("llm", name=fn.__name__, input=inp) as s:
                        out = fn(*args, **kwargs)
                        s.output = _out_text(out)
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
        self._ensure_worker()
        try:
            self._q.put_nowait(t)
        except queue.Full:
            self.dropped += 1

    def _ensure_worker(self):
        """Start the sender thread on first use, and again in a forked worker process (gunicorn, celery)."""
        if self._pid == os.getpid() and self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._pid != os.getpid() or self._thread is None or not self._thread.is_alive():
                self._pid = os.getpid()
                self._thread = threading.Thread(target=self._worker, name="lumen-sdk", daemon=True)
                self._thread.start()

    def _warn(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg)

    def _send(self, batch):
        body = json.dumps({"traces": batch}, default=str).encode()
        req = urllib.request.Request(f"{self.host}/v1/traces", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Lumen-Key": self.api_key,
                                              "User-Agent": f"lumen-sdk-python/{__version__}"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 408:        # wrong key, limit reached, bad request: retrying will not help
                    self._warn(e.code, f"lumen_sdk: {self.host} refused traces with HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
                    break
            except Exception:
                pass
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
            try:
                b = self._drain()
                while b:
                    self._send(b)
                    b = self._drain()
            except Exception:
                log.debug("lumen_sdk: send failed", exc_info=True)

    def flush(self):
        b = self._drain()
        while b:
            self._send(b)
            b = self._drain()


# ====================================================================== module-level API
_OFF = None


def _client():
    global _OFF
    if client is not None:
        return client
    if _OFF is None:
        _OFF = Lumen(api_key="-", enabled=False)
    return _OFF


def init(api_key=None, host=None, instrument=True, **kw):
    """Set up the client (api_key / host default to LUMEN_API_KEY / LUMEN_HOST) and trace the LLM libraries
    that are installed: openai, anthropic, google-genai (instrument=False to skip, or a list of names)."""
    global client
    if client is not None:
        client.flush()
    client = Lumen(api_key, host, **kw)
    if instrument:
        names = instrument if isinstance(instrument, (list, tuple, set)) else None
        client.instrumented = globals()["instrument"](names)
    return client


def trace(name, **kw):
    """with lumen_sdk.trace("rag_answer", input=q) as t: ...  (LLM calls inside become spans of this trace)"""
    return _client().start_trace(name, **kw)


def _args_text(args, kwargs):
    if len(args) == 1 and not kwargs:
        return args[0] if isinstance(args[0], str) else _repr(args[0])
    return {"args": [_repr(a) for a in args], "kwargs": {str(k): _repr(v) for k, v in kwargs.items()}}


def _out_text(v):
    return v if isinstance(v, str) else _repr(v)


def observe(name=None, **kw):
    """Decorator: every call of the function is one trace (its argument is the input, its result the output)."""
    def deco(fn, tname):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a, **k):
                with _client().start_trace(tname, input=_args_text(a, k), **kw) as t:
                    out = await fn(*a, **k)
                    t.output = _out_text(out)
                    return out
            return awrapper

        @functools.wraps(fn)
        def wrapper(*a, **k):
            with _client().start_trace(tname, input=_args_text(a, k), **kw) as t:
                out = fn(*a, **k)
                t.output = _out_text(out)
                return out
        return wrapper
    if callable(name):                       # used as @observe without brackets
        return deco(name, name.__name__)
    return lambda fn: deco(fn, name or fn.__name__)


def flush():
    if client is not None:
        client.flush()


def get_prompt(name, label="production", ttl=60):
    return _client().get_prompt(name, label, ttl)


# ====================================================================== automatic instrumentation
def _g(o, *path, default=None):
    """o.a.b or o["a"]["b"] (or o[0]) without ever raising."""
    for p in path:
        if o is None:
            return default
        try:
            if isinstance(p, int):
                o = o[p]
            elif isinstance(o, dict):
                o = o.get(p)
            else:
                o = getattr(o, p, None)
        except Exception:
            return default
    return default if o is None else o


def _text_of(content):
    """Message content (a string, or a list of parts / blocks) as text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            typ, txt = _g(part, "type", default=""), _g(part, "text")
            if isinstance(txt, str):
                out.append(txt)
            elif typ in ("tool_use", "function_call", "server_tool_use"):
                out.append(f"[tool call {_g(part, 'name', default='')}]")
            elif typ == "tool_result":
                out.append(_text_of(_g(part, "content")))
            elif typ:
                out.append(f"[{typ}]")
        return "\n".join(x for x in out if x)
    return str(content)


def _simple(v):
    return v if isinstance(v, (str, int, float, bool)) and not isinstance(v, type) else None


def _attrs(provider, host, kw, **more):
    a = {"provider": provider, "host": host, "stream": bool(kw.get("stream") is True)}
    for k in ("temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens", "seed", "reasoning_effort"):
        v = _simple(kw.get(k))
        if v is not None:
            a[k] = v
    tools = kw.get("tools")
    if isinstance(tools, (list, tuple)) and tools:
        a["tools"] = [_g(t, "function", "name") or _g(t, "name") or _g(t, "type") for t in tools][:20]
    a.update({k: v for k, v in more.items() if v is not None})
    return a


def _host_of(resource):
    try:
        return urlparse(str(_g(resource, "_client", "base_url", default=""))).hostname or None
    except Exception:
        return None


def _active():
    """Record calls when init() set up a client, or inside a trace of any enabled client."""
    c, t = client, _CURRENT.get()
    return (c is not None and c.enabled) or (t is not None and t.client.enabled)


def _record(provider, op, model, prompt, user_text, output, tin, tout, t0, t1, error=None, attrs=None, trace=None):
    if tin is None and tout is None and (prompt or output):             # streamed without usage: estimate
        tin, tout = len(prompt or "") // 4 or None, len(output or "") // 4 or None
        attrs = {**(attrs or {}), "tokens_estimated": True}
    span = {"type": "llm", "name": f"{provider}.{op}", "model": _simple(model), "input": _cut(prompt), "output": _cut(output),
            "tokens_in": tin if isinstance(tin, int) else None, "tokens_out": tout if isinstance(tout, int) else None,
            "latency_ms": round((t1 - t0) * 1000, 1), "status": "error" if error else "ok", "error": error, "attrs": attrs or {}}
    t = trace if trace is not None else _CURRENT.get()
    if t is not None and not t._closed and t.client.enabled:
        t._add(span, t0, user_text)
        return
    c = client
    if c is None or not c.enabled:
        return
    c._enqueue({"id": uuid.uuid4().hex, "name": f"{provider}.{op}", "ts": _iso(time.time() - (t1 - t0)),
                "input": _cut(user_text if user_text else prompt), "output": _cut(output), "metadata": {"auto": provider},
                "spans": [{**span, "id": "s1", "start_ms": 0}], "latency_ms": span["latency_ms"], "status": span["status"]})


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        log.debug("lumen_sdk: could not record an LLM call", exc_info=True)


def _err(e):
    try:
        return f"{type(e).__name__}: {e}"[:2000]
    except Exception:                      # str() of some exceptions fails; the caller's exception must still be re-raised
        return type(e).__name__


def _skip(kw):
    """Calls made through .with_raw_response / .with_streaming_response return raw HTTP objects: leave them alone."""
    h = kw.get("extra_headers")
    return isinstance(h, dict) and any(str(k).lower() == "x-stainless-raw-response" for k in h)


class _Adapter:
    """How to read one provider method's request and answer. Subclasses fill in the three hooks."""
    provider, op = "llm", "call"

    def __init__(self, kw, host, t0):
        self.kw, self.host, self.t0, self.trace = kw, host, t0, _CURRENT.get()
        self.parts, self.model, self.tin, self.tout, self.extra = [], None, None, None, {}

    def request(self):            # -> (prompt text, last user message)
        return "", None

    def response(self, res):      # a complete answer
        pass

    def chunk(self, ch):          # one streamed piece
        pass

    def finish(self, error=None):
        prompt, user = self.request()
        _record(self.provider, self.op, self.model or self.kw.get("model"), prompt, user, "".join(self.parts), self.tin, self.tout,
                self.t0, time.perf_counter(), error, _attrs(self.provider, self.host, self.kw, **self.extra), self.trace)


class _SyncStream:
    """Wraps a streamed answer: passes every chunk through untouched and records the call when it ends."""

    def __init__(self, stream, adapter):
        self._s, self._a, self._it, self._done = stream, adapter, None, False

    def _end(self, error=None):
        if not self._done:
            self._done = True
            _safe(self._a.finish, error)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            if self._it is None:
                self._it = iter(self._s)
            item = next(self._it)
        except StopIteration:
            self._end()
            raise
        except Exception as e:
            self._end(_err(e))
            raise
        _safe(self._a.chunk, item)
        return item

    def __enter__(self):
        if hasattr(self._s, "__enter__"):
            self._s.__enter__()
        return self

    def __exit__(self, et, ev, tb):
        try:
            return self._s.__exit__(et, ev, tb) if hasattr(self._s, "__exit__") else False
        finally:
            self._end(_err(ev) if ev is not None else None)

    def close(self):
        try:
            return self._s.close() if hasattr(self._s, "close") else None
        finally:
            self._end()

    def __getattr__(self, k):
        return getattr(self._s, k)


class _AsyncStream:
    def __init__(self, stream, adapter):
        self._s, self._a, self._it, self._done = stream, adapter, None, False

    def _end(self, error=None):
        if not self._done:
            self._done = True
            _safe(self._a.finish, error)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            if self._it is None:
                self._it = self._s.__aiter__()
            item = await self._it.__anext__()
        except StopAsyncIteration:
            self._end()
            raise
        except Exception as e:
            self._end(_err(e))
            raise
        _safe(self._a.chunk, item)
        return item

    async def __aenter__(self):
        if hasattr(self._s, "__aenter__"):
            await self._s.__aenter__()
        return self

    async def __aexit__(self, et, ev, tb):
        try:
            return await self._s.__aexit__(et, ev, tb) if hasattr(self._s, "__aexit__") else False
        finally:
            self._end(_err(ev) if ev is not None else None)

    async def close(self):
        try:
            if hasattr(self._s, "close"):
                r = self._s.close()
                if inspect.isawaitable(r):
                    await r
                return r
            if hasattr(self._s, "aclose"):
                await self._s.aclose()
        finally:
            self._end()

    def __getattr__(self, k):
        return getattr(self._s, k)


def _stap(it, adapter):
    err = None
    try:
        for item in it:
            _safe(adapter.chunk, item)
            yield item
    except GeneratorExit:
        raise
    except Exception as e:
        err = _err(e)
        raise
    finally:
        _safe(adapter.finish, err)


async def _atap(it, adapter):
    err = None
    try:
        async for item in it:
            _safe(adapter.chunk, item)
            yield item
    except GeneratorExit:
        raise
    except Exception as e:
        err = _err(e)
        raise
    finally:
        _safe(adapter.finish, err)


def _tap(stream, adapter, is_async):
    """Record a streamed answer as it is read. The provider's stream object is returned itself (so isinstance checks
    and every attribute still work) with its internal iterator wrapped; plain generators are wrapped by a generator."""
    it = getattr(stream, "_iterator", None)
    if is_async:
        if it is not None and hasattr(it, "__anext__"):
            stream._iterator = _atap(it, adapter)
            return stream
        if inspect.isasyncgen(stream):
            return _atap(stream, adapter)
        return _AsyncStream(stream, adapter)
    if it is not None and hasattr(it, "__next__"):
        stream._iterator = _stap(it, adapter)
        return stream
    if inspect.isgenerator(stream):
        return _stap(stream, adapter)
    return _SyncStream(stream, adapter)


def _failed(adapter_cls, kw, host, t0, error):
    adapter_cls(kw, host, t0).finish(error)


def _wrap(orig, adapter_cls, is_async, stream_always=False):
    """The patched method: calls the original exactly once with the same arguments and returns its result."""
    if is_async:
        @functools.wraps(orig)
        async def awrapper(self, *args, **kw):
            if args or _skip(kw) or not _active():
                return await orig(self, *args, **kw)
            t0 = time.perf_counter()
            try:
                res = await orig(self, *args, **kw)
            except Exception as e:
                _safe(_failed, adapter_cls, kw, _host_of(self), t0, _err(e))
                raise
            try:
                a = adapter_cls(kw, _host_of(self), t0)
                if stream_always or kw.get("stream") is True:
                    return _tap(res, a, True)
                a.response(res)
                a.finish()
            except Exception:
                log.debug("lumen_sdk: could not record an LLM call", exc_info=True)
            return res
        awrapper._lumen = True
        return awrapper

    @functools.wraps(orig)
    def wrapper(self, *args, **kw):
        if args or _skip(kw) or not _active():
            return orig(self, *args, **kw)
        t0 = time.perf_counter()
        try:
            res = orig(self, *args, **kw)
        except Exception as e:
            _safe(_failed, adapter_cls, kw, _host_of(self), t0, _err(e))
            raise
        try:
            a = adapter_cls(kw, _host_of(self), t0)
            if stream_always or kw.get("stream") is True:
                return _tap(res, a, False)
            a.response(res)
            a.finish()
        except Exception:
            log.debug("lumen_sdk: could not record an LLM call", exc_info=True)
        return res
    wrapper._lumen = True
    return wrapper


# ---------------------------------------------------------------------- OpenAI (and OpenAI-compatible providers)
def _chat_request(messages):
    lines, user = [], None
    if isinstance(messages, (list, tuple)):              # never iterate a generator the caller still needs
        for m in messages:
            role = _g(m, "role", default="user")
            txt = _text_of(_g(m, "content"))
            calls = _g(m, "tool_calls")
            if isinstance(calls, (list, tuple)) and calls:
                txt = (txt + " " if txt else "") + " ".join(f"[tool call {_g(x, 'function', 'name', default='')}]" for x in calls)
            if role == "user" and txt:
                user = txt
            lines.append(f"{role}: {txt}")
    return "\n".join(lines), user


class _OpenAIChat(_Adapter):
    provider, op = "openai", "chat"

    def request(self):
        return _chat_request(self.kw.get("messages"))

    def response(self, res):
        self.model = _g(res, "model")
        msg = _g(res, "choices", 0, "message")
        content = _g(msg, "content")
        self.parts = [content if isinstance(content, str) else _text_of(content)]
        calls = _g(msg, "tool_calls")
        if isinstance(calls, (list, tuple)) and calls:
            self.parts.append("\n".join(f"[tool call] {_g(x, 'function', 'name', default='')}({_g(x, 'function', 'arguments', default='')})"
                                        for x in calls))
        self.tin, self.tout = _g(res, "usage", "prompt_tokens"), _g(res, "usage", "completion_tokens")
        self.extra["finish_reason"] = _simple(_g(res, "choices", 0, "finish_reason"))

    def chunk(self, ch):
        self.model = self.model or _g(ch, "model")
        d = _g(ch, "choices", 0, "delta")
        txt = _g(d, "content")
        if isinstance(txt, str):
            self.parts.append(txt)
        for x in _g(d, "tool_calls", default=[]) or []:
            name = _g(x, "function", "name")
            if name:
                self.parts.append(f"[tool call] {name}")
        fr = _g(ch, "choices", 0, "finish_reason")
        if fr:
            self.extra["finish_reason"] = _simple(fr)
        if _g(ch, "usage") is not None:
            self.tin, self.tout = _g(ch, "usage", "prompt_tokens"), _g(ch, "usage", "completion_tokens")


class _OpenAIResponses(_Adapter):
    provider, op = "openai", "responses"

    def request(self):
        lines, user = [], None
        ins = self.kw.get("instructions")
        if isinstance(ins, str):
            lines.append(f"system: {ins}")
        inp = self.kw.get("input")
        if isinstance(inp, str):
            lines.append(f"user: {inp}")
            user = inp
        elif isinstance(inp, (list, tuple)):
            for it in inp:
                role = _g(it, "role") or _g(it, "type") or "user"
                txt = _text_of(_g(it, "content")) if _g(it, "content") is not None else _text_of(_g(it, "output"))
                if role == "user" and txt:
                    user = txt
                lines.append(f"{role}: {txt}")
        return "\n".join(lines), user

    def response(self, res):
        self.model = _g(res, "model")
        text = _g(res, "output_text")
        if not isinstance(text, str) or not text:
            out = []
            for it in _g(res, "output", default=[]) or []:
                if _g(it, "type") == "message":
                    out += [_g(p, "text", default="") for p in _g(it, "content", default=[]) or [] if _g(p, "type") == "output_text"]
                elif _g(it, "type") == "function_call":
                    out.append(f"[tool call] {_g(it, 'name', default='')}({_g(it, 'arguments', default='')})")
            text = "\n".join(x for x in out if x)
        self.parts = [text]
        self.tin, self.tout = _g(res, "usage", "input_tokens"), _g(res, "usage", "output_tokens")

    def chunk(self, ev):
        typ = _g(ev, "type", default="")
        if typ == "response.output_text.delta":
            d = _g(ev, "delta")
            if isinstance(d, str):
                self.parts.append(d)
        elif typ in ("response.completed", "response.incomplete", "response.failed"):
            r = _g(ev, "response")
            self.model = _g(r, "model") or self.model
            self.tin, self.tout = _g(r, "usage", "input_tokens"), _g(r, "usage", "output_tokens")
            if not self.parts:
                self.response(r)


# ---------------------------------------------------------------------- Anthropic
def _anthropic_request(kw):
    lines, user = [], None
    system = kw.get("system")
    if isinstance(system, (str, list, tuple)):
        lines.append(f"system: {_text_of(system)}")
    msgs = kw.get("messages")
    if isinstance(msgs, (list, tuple)):
        for m in msgs:
            role, txt = _g(m, "role", default="user"), _text_of(_g(m, "content"))
            if role == "user" and txt:
                user = txt
            lines.append(f"{role}: {txt}")
    return "\n".join(lines), user


def _anthropic_text(blocks):
    out = []
    for b in blocks or []:
        typ = _g(b, "type")
        if typ == "text":
            out.append(_g(b, "text", default=""))
        elif typ in ("tool_use", "server_tool_use"):
            out.append(f"[tool call] {_g(b, 'name', default='')}({json.dumps(_g(b, 'input', default={}), default=str)[:500]})")
    return "\n".join(x for x in out if x)


def _anthropic_usage(u):
    if u is None:
        return None
    n = [_g(u, k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    return sum(x for x in n if isinstance(x, int)) if any(isinstance(x, int) for x in n) else None


class _AnthropicMessages(_Adapter):
    provider, op = "anthropic", "messages"

    def request(self):
        return _anthropic_request(self.kw)

    def response(self, res):
        self.model = _g(res, "model")
        self.parts = [_anthropic_text(_g(res, "content"))]
        self.tin, self.tout = _anthropic_usage(_g(res, "usage")), _g(res, "usage", "output_tokens")
        self.extra["stop_reason"] = _simple(_g(res, "stop_reason"))

    def chunk(self, ev):
        typ = _g(ev, "type", default="")
        if typ == "message_start":
            self.model = _g(ev, "message", "model")
            self.tin = _anthropic_usage(_g(ev, "message", "usage"))
        elif typ == "content_block_start" and _g(ev, "content_block", "type") in ("tool_use", "server_tool_use"):
            self.parts.append(f"[tool call] {_g(ev, 'content_block', 'name', default='')}")
        elif typ == "content_block_delta" and _g(ev, "delta", "type") == "text_delta":
            self.parts.append(_g(ev, "delta", "text", default=""))
        elif typ == "message_delta":
            self.tout = _g(ev, "usage", "output_tokens", default=self.tout)
            self.extra["stop_reason"] = _simple(_g(ev, "delta", "stop_reason"))


class _AnthropicStreamManager:
    """client.messages.stream(...): records the call when the `with` block ends (from the stream's snapshot)."""

    def __init__(self, mgr, kw, host):
        self._m, self._kw, self._host, self._s, self._a = mgr, kw, host, None, None

    def _done(self, ev):
        try:
            snap, fn = None, getattr(self._s, "_current_snapshot_or_none", None)
            if callable(fn):
                snap = fn()
            if snap is None:
                try:
                    snap = self._s.current_message_snapshot
                except Exception:
                    snap = None
            if snap is not None:
                self._a.response(snap)
            self._a.finish(_err(ev) if ev is not None else None)
        except Exception:
            log.debug("lumen_sdk: could not record an LLM call", exc_info=True)

    def __enter__(self):
        self._a = _AnthropicMessages(self._kw, self._host, time.perf_counter())
        self._s = self._m.__enter__()
        return self._s

    def __exit__(self, et, ev, tb):
        try:
            return self._m.__exit__(et, ev, tb)
        finally:
            self._done(ev)

    async def __aenter__(self):
        self._a = _AnthropicMessages(self._kw, self._host, time.perf_counter())
        self._s = await self._m.__aenter__()
        return self._s

    async def __aexit__(self, et, ev, tb):
        try:
            return await self._m.__aexit__(et, ev, tb)
        finally:
            self._done(ev)

    def __getattr__(self, k):
        return getattr(self._m, k)


def _wrap_anthropic_stream(orig):
    @functools.wraps(orig)
    def stream(self, *args, **kw):
        mgr = orig(self, *args, **kw)
        if args or _skip(kw) or not _active():
            return mgr
        try:
            return _AnthropicStreamManager(mgr, kw, _host_of(self))
        except Exception:
            return mgr
    stream._lumen = True
    return stream


# ---------------------------------------------------------------------- Google Gemini (google-genai)
def _gemini_text(c):
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, (list, tuple)):
        return "\n".join(x for x in (_gemini_text(p) for p in c) if x)
    parts = _g(c, "parts")
    if isinstance(parts, (list, tuple)):
        return _gemini_text(list(parts))
    if getattr(c, "thought", None) is True or (isinstance(c, dict) and c.get("thought") is True):
        return ""
    t = _g(c, "text")
    if isinstance(t, str):
        return t
    fc = _g(c, "function_call")
    if fc is not None:
        return f"[tool call] {_g(fc, 'name', default='')}({json.dumps(_g(fc, 'args', default={}), default=str)[:500]})"
    if _g(c, "inline_data") is not None or _g(c, "file_data") is not None:
        return "[file]"
    return ""


class _Gemini(_Adapter):
    provider, op = "gemini", "generate_content"

    def request(self):
        lines, user = [], None
        si = _g(self.kw.get("config"), "system_instruction")
        if si is not None:
            lines.append(f"system: {_gemini_text(si)}")
        contents = self.kw.get("contents")
        items = contents if isinstance(contents, (list, tuple)) else [contents] if contents is not None else []
        if items and all(isinstance(x, str) for x in items):
            user = "\n".join(items)
            lines.append(f"user: {user}")
        else:
            for c in items:
                role = "user" if isinstance(c, str) else (_g(c, "role") or "user")
                txt = _gemini_text(c)
                if role == "user" and txt:
                    user = txt
                lines.append(f"{role}: {txt}")
        return "\n".join(lines), user

    def _usage(self, res):
        u = _g(res, "usage_metadata")
        if u is not None:
            self.tin = _g(u, "prompt_token_count", default=self.tin)
            out = [_g(u, k) for k in ("candidates_token_count", "thoughts_token_count")]
            if any(isinstance(x, int) for x in out):
                self.tout = sum(x for x in out if isinstance(x, int))
        self.model = _g(res, "model_version") or self.model

    def response(self, res):
        self.parts = [_gemini_text(_g(res, "candidates", 0, "content"))]
        self._usage(res)

    def chunk(self, ch):
        self.parts.append(_gemini_text(_g(ch, "candidates", 0, "content")))
        self._usage(ch)

    def finish(self, error=None):
        m = self.kw.get("model")
        if isinstance(m, str):
            self.kw = {**self.kw, "model": m.split("/")[-1]}
        super().finish(error)


# ---------------------------------------------------------------------- patching
def _patch(cls, name, wrapped_factory):
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_lumen", False):
        return False
    setattr(cls, name, wrapped_factory(orig))
    return True


def _instrument_openai():
    from openai.resources.chat.completions import AsyncCompletions, Completions
    done = 0
    for cls, is_async in ((Completions, False), (AsyncCompletions, True)):
        done += _patch(cls, "create", lambda o, a=is_async: _wrap(o, _OpenAIChat, a))
        done += _patch(cls, "parse", lambda o, a=is_async: _wrap(o, _OpenAIChat, a))
    try:
        from openai.resources.responses import AsyncResponses, Responses
        for cls, is_async in ((Responses, False), (AsyncResponses, True)):
            done += _patch(cls, "create", lambda o, a=is_async: _wrap(o, _OpenAIResponses, a))
            done += _patch(cls, "parse", lambda o, a=is_async: _wrap(o, _OpenAIResponses, a))
    except ImportError:
        pass
    return done


def _instrument_anthropic():
    from anthropic.resources.messages import AsyncMessages, Messages
    done = 0
    for cls, is_async in ((Messages, False), (AsyncMessages, True)):
        done += _patch(cls, "create", lambda o, a=is_async: _wrap(o, _AnthropicMessages, a))
        done += _patch(cls, "stream", _wrap_anthropic_stream)
    return done


def _instrument_gemini():
    from google.genai.models import AsyncModels, Models
    done = _patch(Models, "generate_content", lambda o: _wrap(o, _Gemini, False))
    done += _patch(Models, "generate_content_stream", lambda o: _wrap(o, _Gemini, False, stream_always=True))
    done += _patch(AsyncModels, "generate_content", lambda o: _wrap(o, _Gemini, True))
    done += _patch(AsyncModels, "generate_content_stream", lambda o: _wrap(o, _Gemini, True, stream_always=True))
    return done


_LIBS = {"openai": _instrument_openai, "anthropic": _instrument_anthropic, "gemini": _instrument_gemini}


def instrument(names=None):
    """Trace calls of the installed LLM libraries. Returns the names it hooked into. Safe to call twice."""
    out = []
    for name, fn in _LIBS.items():
        if names and name not in names:
            continue
        try:
            fn()
            out.append(name)
        except ImportError:
            pass
        except Exception:
            log.warning("lumen_sdk: could not instrument %s", name, exc_info=True)
    return out
