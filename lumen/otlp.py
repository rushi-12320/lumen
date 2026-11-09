"""OpenTelemetry intake: POST /v1/otlp/v1/traces (OTLP over HTTP, protobuf or JSON, optionally gzip).

Point any OpenTelemetry exporter at Lumen and its LLM spans become Lumen traces:

    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://<your-lumen>/v1/otlp/v1/traces
    OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-lumen-key=lmn_...

Reads the OpenTelemetry GenAI conventions (gen_ai.*), OpenInference (Arize Phoenix: llm.*, input.value,
retrieval.documents.*), OpenLLMetry (traceloop) and the Vercel AI SDK (ai.*).

Exporters send spans in batches as they end, so one request's spans can arrive in different batches. They are held
in memory (never on disk, so unredacted text is never stored) until the trace's root span arrives, or for at most
HOLD_S seconds, then stored as one trace through the normal ingestion (redaction, injection detection, evaluators).
Spans that arrive after their trace was stored are added to it; a late root span or LLM answer updates the trace
and it is scored again. Memory is bounded: per request (traces, spans, values, nesting) and in total (held traces
and bytes; the oldest are stored early). Traces are counted for plan limits when they are stored (ON_STORED).

The protobuf decoder below reads the protobuf wire format of the OTLP trace messages, so no protobuf package is needed.
"""
import base64
import json
import logging
import math
import os
import struct
import threading
import time
import zlib
from collections import OrderedDict
from datetime import datetime, timezone

from . import ingest

log = logging.getLogger("lumen.otlp")
HOLD_S = float(os.environ.get("LUMEN_OTLP_HOLD_S", "15"))
MAX_BODY = 10 * 1024 * 1024            # bytes as received
MAX_PROTOBUF = 20 * 1024 * 1024        # bytes after gzip
MAX_JSON = 5 * 1024 * 1024             # bytes after gzip (JSON becomes many Python objects at once)
MAX_TRACES = 1000                      # traces per request (exporters send 512 spans per batch by default)
MAX_REQ_SPANS = 20_000                 # spans per request
MAX_ITEMS = 300_000                    # attributes, events and values per request
MAX_DEPTH = 16                         # nesting of attribute values
MAX_EVENTS = 64                        # events kept per span
MAX_PENDING = 2000                     # traces held at once
MAX_PENDING_BYTES = 48 * 1024 * 1024   # memory held at once (estimated); past either limit the oldest are stored early
ON_STORED = []                         # functions(database path, project id, new traces): Lumen Cloud meters with this
_pending, _plock = OrderedDict(), threading.Lock()
_held = [0]                            # estimated bytes held in _pending


class _Budget:
    """Per-request limits on how much a decoder may build."""
    __slots__ = ("items", "spans")

    def __init__(self):
        self.items, self.spans = MAX_ITEMS, MAX_REQ_SPANS

    def take(self, n=1):
        self.items -= n
        if self.items < 0:
            raise ValueError(f"more than {MAX_ITEMS:,} attributes, events and values in one request: lower the exporter's batch size")

    def span(self):
        self.spans -= 1
        if self.spans < 0:
            raise ValueError(f"more than {MAX_REQ_SPANS:,} spans in one request: lower the exporter's batch size")


def _finite(x):
    return x if math.isfinite(x) else str(x)


# ====================================================================== protobuf wire format
def _varint(b, i):
    shift = result = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        x = b[i]
        i += 1
        result |= (x & 0x7F) << shift
        if not x & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(b):
    """(field number, wire type, value) of one message: an int for varints, bytes for everything else."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        if i > n:
            raise ValueError("truncated message")
        yield f, wt, v


def _str(v):
    return bytes(v).decode("utf-8", "replace")


def _any_pb(b, bud, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("attribute values nested too deeply")
    bud.take()
    for f, wt, v in _fields(b):
        if f == 1 and wt == 2:
            return _str(v)
        if f == 2 and wt == 0:
            return bool(v)
        if f == 3 and wt == 0:
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 4 and wt == 1:
            return _finite(struct.unpack("<d", v)[0])
        if f == 5 and wt == 2:
            return [_any_pb(x, bud, depth + 1) for ff, wt2, x in _fields(v) if ff == 1 and wt2 == 2]
        if f == 6 and wt == 2:
            return dict(_kv_pb(x, bud, depth + 1) for ff, wt2, x in _fields(v) if ff == 1 and wt2 == 2)
        if f == 7 and wt == 2:
            return base64.b64encode(bytes(v)[:1024]).decode()
    return None


def _kv_pb(b, bud, depth=0):
    bud.take()
    key, val = "", None
    for f, wt, v in _fields(b):
        if f == 1 and wt == 2:
            key = _str(v)
        elif f == 2 and wt == 2:
            val = _any_pb(v, bud, depth)
    return key, val


def _span_pb(b, bud):
    bud.span()
    s = {"attrs": {}, "events": [], "parent_id": "", "status": 0, "status_msg": "", "kind": 0, "start": 0, "end": 0, "name": ""}
    for f, wt, v in _fields(b):
        if f == 1 and wt == 2:
            s["trace_id"] = bytes(v[:32]).hex()
        elif f == 2 and wt == 2:
            s["span_id"] = bytes(v[:32]).hex()
        elif f == 4 and wt == 2:
            s["parent_id"] = bytes(v[:32]).hex()
        elif f == 5 and wt == 2:
            s["name"] = _str(v[:1000])
        elif f == 6 and wt == 0:
            s["kind"] = v
        elif f == 7 and wt == 1:
            s["start"] = int.from_bytes(v, "little")
        elif f == 8 and wt == 1:
            s["end"] = int.from_bytes(v, "little")
        elif f == 9 and wt == 2:
            k, val = _kv_pb(v, bud)
            s["attrs"][k] = val
        elif f == 11 and wt == 2:
            bud.take()
            if len(s["events"]) >= MAX_EVENTS:
                continue
            ev = {"name": "", "attrs": {}}
            for ff, wt2, x in _fields(v):
                if ff == 2 and wt2 == 2:
                    ev["name"] = _str(x[:200])
                elif ff == 3 and wt2 == 2:
                    k, val = _kv_pb(x, bud)
                    ev["attrs"][k] = val
            s["events"].append(ev)
        elif f == 15 and wt == 2:
            for ff, wt2, x in _fields(v):
                if ff == 2 and wt2 == 2:
                    s["status_msg"] = _str(x[:2000])
                elif ff == 3 and wt2 == 0:
                    s["status"] = x
    return s


def decode_protobuf(b, bud=None):
    """ExportTraceServiceRequest -> flat list of spans (dicts) with their resource attributes and scope name."""
    bud, out = bud or _Budget(), []
    for f, wt, rs in _fields(memoryview(b)):
        if f != 1 or wt != 2:
            continue
        res, scopes = {}, []
        for f2, wt2, v2 in _fields(rs):
            if f2 == 1 and wt2 == 2:
                res = dict(_kv_pb(x, bud) for f3, wt3, x in _fields(v2) if f3 == 1 and wt3 == 2)
            elif f2 == 2 and wt2 == 2:
                scopes.append(v2)
        for ss in scopes:
            name, spans = "", []
            for f3, wt3, v3 in _fields(ss):
                if f3 == 1 and wt3 == 2:
                    name = next((_str(x[:200]) for f4, wt4, x in _fields(v3) if f4 == 1 and wt4 == 2), "")
                elif f3 == 2 and wt3 == 2:
                    spans.append(_span_pb(v3, bud))
            for s in spans:
                s["resource"], s["scope"] = res, name
                out.append(s)
    return out


# ====================================================================== OTLP/JSON
def _get(d, *names):
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d:
            return d[n]
    return None


def _int(v, enum=None):
    """A non-negative 64-bit integer from client data (times, kinds, codes), or 0."""
    if isinstance(v, str) and enum and v in enum:
        return enum[v]
    if isinstance(v, bool):
        return int(v)
    try:
        x = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return x if 0 <= x < 1 << 64 else 0


def _hexid(v):
    if not isinstance(v, str) or not v:
        return ""
    v = v[:64]
    if all(c in "0123456789abcdefABCDEF" for c in v) and len(v) in (16, 32):
        return v.lower()
    try:
        return base64.b64decode(v + "=" * (-len(v) % 4)).hex()[:64]      # some older exporters send base64
    except Exception:
        return ""


def _any_json(v, bud, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("attribute values nested too deeply")
    bud.take()
    if not isinstance(v, dict):
        return v if isinstance(v, (str, int, bool)) or v is None else _finite(v) if isinstance(v, float) else str(v)[:1000]
    for k in ("stringValue", "string_value"):
        if k in v:
            return str(v[k])
    for k in ("boolValue", "bool_value"):
        if k in v:
            return bool(v[k])
    for k in ("intValue", "int_value"):
        if k in v:
            try:
                x = int(v[k])
            except (TypeError, ValueError, OverflowError):
                return None
            return x if -(1 << 63) <= x < 1 << 63 else None
    for k in ("doubleValue", "double_value"):
        if k in v:
            try:
                return _finite(float(v[k]))
            except (TypeError, ValueError, OverflowError):
                return None
    arr = _get(v, "arrayValue", "array_value")
    if isinstance(arr, dict):
        vals = arr.get("values")
        return [_any_json(x, bud, depth + 1) for x in vals] if isinstance(vals, list) else []
    kvl = _get(v, "kvlistValue", "kvlist_value")
    if isinstance(kvl, dict):
        return _attrs_json(kvl.get("values"), bud, depth + 1)
    b = _get(v, "bytesValue", "bytes_value")
    return b[:1400] if isinstance(b, str) else None


def _attrs_json(lst, bud, depth=0):
    out = {}
    for kv in lst if isinstance(lst, list) else []:
        bud.take()
        if isinstance(kv, dict) and isinstance(kv.get("key"), str):
            out[kv["key"]] = _any_json(kv.get("value"), bud, depth)
    return out


KINDS = {"SPAN_KIND_UNSPECIFIED": 0, "SPAN_KIND_INTERNAL": 1, "SPAN_KIND_SERVER": 2, "SPAN_KIND_CLIENT": 3,
         "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5}
CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


def _list(v):
    return v if isinstance(v, list) else []


def decode_json(obj, bud=None):
    bud, out = bud or _Budget(), []
    for rs in _list(_get(obj, "resourceSpans", "resource_spans")):
        res = _attrs_json(_get(_get(rs, "resource"), "attributes"), bud)
        for ss in _list(_get(rs, "scopeSpans", "scope_spans")):
            scope = _get(ss, "scope")
            name = str(scope.get("name") or "")[:200] if isinstance(scope, dict) else ""
            for sp in _list(_get(ss, "spans")):
                if not isinstance(sp, dict):
                    continue
                bud.span()
                st = _get(sp, "status")
                st = st if isinstance(st, dict) else {}
                events = []
                for e in _list(sp.get("events")):
                    bud.take()
                    if isinstance(e, dict) and len(events) < MAX_EVENTS:
                        events.append({"name": str(e.get("name") or "")[:200], "attrs": _attrs_json(e.get("attributes"), bud)})
                out.append({"trace_id": _hexid(_get(sp, "traceId", "trace_id")), "span_id": _hexid(_get(sp, "spanId", "span_id")),
                            "parent_id": _hexid(_get(sp, "parentSpanId", "parent_span_id")), "name": str(sp.get("name") or "")[:1000],
                            "kind": _int(sp.get("kind"), KINDS), "start": _int(_get(sp, "startTimeUnixNano", "start_time_unix_nano")),
                            "end": _int(_get(sp, "endTimeUnixNano", "end_time_unix_nano")),
                            "attrs": _attrs_json(sp.get("attributes"), bud), "events": events,
                            "status": _int(_get(st, "code"), CODES), "status_msg": str(_get(st, "message") or "")[:2000],
                            "resource": res, "scope": name})
    return out


def _gunzip(body, limit):
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(body, limit + 1)
    except zlib.error as e:
        raise ValueError(f"bad gzip body: {e}")
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError(f"body larger than {limit // (1024 * 1024)} MB after decompression: lower the exporter's batch size")
    if not d.eof:
        raise ValueError("truncated gzip body")
    return out


def decode(body, content_type="", content_encoding=""):
    """Raw request -> spans. Raises ValueError for anything that is not a readable OTLP trace export within the limits."""
    ct = (content_type or "").split(";")[0].strip().lower()
    is_json = ct == "application/json"
    if not is_json and ct not in ("application/x-protobuf", "application/protobuf", "application/octet-stream", ""):
        raise ValueError(f"unsupported content type {ct}: send application/x-protobuf or application/json")
    limit = MAX_JSON if is_json else MAX_PROTOBUF
    enc = (content_encoding or "").strip().lower()
    if enc == "gzip":
        body = _gunzip(body, limit)
    elif enc not in ("", "identity"):
        raise ValueError(f"unsupported content encoding {enc}: send gzip or nothing")
    if len(body) > limit:
        raise ValueError(f"body larger than {limit // (1024 * 1024)} MB: lower the exporter's batch size")
    try:
        if is_json:
            return decode_json(json.loads(body or b"{}"))
        return decode_protobuf(body)
    except ValueError:
        raise
    except (IndexError, TypeError, AttributeError, OverflowError, RecursionError, struct.error, UnicodeError) as e:
        raise ValueError(f"unreadable OTLP {'JSON' if is_json else 'protobuf'}: {str(e)[:200]}")


# ====================================================================== OpenTelemetry span -> Lumen span
OI_KINDS = {"LLM": "llm", "RETRIEVER": "retrieval", "RERANKER": "retrieval", "EMBEDDING": "embedding", "TOOL": "tool",
            "CHAIN": "chain", "AGENT": "agent", "GUARDRAIL": "guardrail", "EVALUATOR": "chain"}
# attributes that carry prompts, answers, documents or other free text: read into input/output, never kept as attributes
TEXT_KEYS = {"input.value", "output.value", "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
             "traceloop.entity.input", "traceloop.entity.output", "ai.prompt", "ai.prompt.messages", "ai.response.text",
             "ai.result.text", "gen_ai.prompt", "gen_ai.completion", "ai.response.toolCalls", "ai.toolCall.args",
             "ai.toolCall.result", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "llm.prompt_template.variables",
             "llm.prompt_template.template", "db.query.text", "db.statement"}
TEXT_PREFIXES = ("gen_ai.prompt.", "gen_ai.completion.", "llm.input_messages.", "llm.output_messages.", "retrieval.documents.",
                 "llm.prompts.", "llm.completions.", "embedding.embeddings.", "reranker.", "ai.prompt.", "ai.response.",
                 "ai.result.", "ai.telemetry.metadata.", "message.", "tool_call.")


def _first(a, *keys):
    for k in keys:
        v = a.get(k)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    """A token count from client data, or None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if 0 <= x < 10 ** 12 else None


def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(_text(x) for x in v if x is not None)
    if isinstance(v, dict):
        for k in ("content", "text", "value"):
            if isinstance(v.get(k), (str, list)):
                return _text(v[k])
    try:
        return json.dumps(v, default=str, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        return str(v)[:1000]


def _parts_text(parts):
    """Message parts of the GenAI conventions ([{type: text, content}]) or of the Vercel AI SDK ([{type: text, text}])."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return _text(parts)
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            t = str(p.get("type") or "")
            if isinstance(p.get("content"), str) and t in ("", "text", "reasoning"):
                out.append(p["content"])
            elif isinstance(p.get("text"), str):
                out.append(p["text"])
            elif t in ("tool_call", "tool-call"):
                out.append(f"[tool call {p.get('name') or p.get('toolName') or ''}]")
            elif t:
                out.append(f"[{t}]")
    return "\n".join(x for x in out if x)


def _json_messages(v):
    """[(role, text)] from a JSON message list (GenAI conventions, Vercel AI SDK, plain {role, content})."""
    try:
        if isinstance(v, str):
            v = json.loads(v)
        if isinstance(v, dict):
            out = [("system", v["system"])] if isinstance(v.get("system"), str) else []
            if isinstance(v.get("prompt"), str):
                out.append(("user", v["prompt"]))
            return out + _json_messages(v.get("messages") or [])
        out = []
        for m in v if isinstance(v, list) else []:
            if isinstance(m, dict):
                body = m.get("parts") if m.get("parts") is not None else m.get("content")
                out.append((str(m.get("role") or "user"), _parts_text(body)))
        return out
    except (ValueError, TypeError, AttributeError, RecursionError):
        return []


def _indexed(a, prefix, role_key, content_key):
    out, i = [], 0
    while i < 500 and (f"{prefix}.{i}.{content_key}" in a or f"{prefix}.{i}.{role_key}" in a):
        out.append((str(a.get(f"{prefix}.{i}.{role_key}") or "user"), _text(a.get(f"{prefix}.{i}.{content_key}"))))
        i += 1
    return out


def _event_messages(events, names):
    out = []
    for e in events:
        if e["name"] in names:
            ea = e["attrs"]
            role = e["name"].split(".")[1] if e["name"].count(".") >= 2 else "user"
            out.append((role, _text(_first(ea, "content", "gen_ai.prompt", "gen_ai.completion", "message"))))
    return out


def _messages_in(a, events):
    return (_json_messages(a.get("gen_ai.input.messages")) or _indexed(a, "llm.input_messages", "message.role", "message.content")
            or _indexed(a, "gen_ai.prompt", "role", "content") or _indexed(a, "llm.prompts", "role", "content")
            or _event_messages(events, ("gen_ai.system.message", "gen_ai.user.message", "gen_ai.assistant.message",
                                        "gen_ai.tool.message", "gen_ai.content.prompt"))
            or _json_messages(a.get("ai.prompt.messages")) or _json_messages(a.get("ai.prompt")))


def _messages_out(a, events):
    out = (_json_messages(a.get("gen_ai.output.messages")) or _indexed(a, "llm.output_messages", "message.role", "message.content")
           or _indexed(a, "gen_ai.completion", "role", "content") or _indexed(a, "llm.completions", "role", "content"))
    if out:
        return "\n".join(t for _, t in out if t)
    for e in events:
        if e["name"] in ("gen_ai.choice", "gen_ai.content.completion"):
            ea = e["attrs"]
            msg = ea.get("message")
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except (ValueError, RecursionError):
                    pass
            return _text(msg if msg is not None else _first(ea, "content", "gen_ai.completion"))
    return _text(_first(a, "ai.response.text", "ai.result.text", "gen_ai.completion"))


def _kind(s):
    a = s["attrs"]
    k = str(a.get("openinference.span.kind") or "").upper()
    if k in OI_KINDS:
        return OI_KINDS[k]
    op = str(a.get("gen_ai.operation.name") or "")
    if op in ("chat", "text_completion", "generate_content", "completion"):
        return "llm"
    if op in ("embeddings", "embedding"):
        return "embedding"
    if op == "execute_tool":
        return "tool"
    if op in ("invoke_agent", "create_agent"):
        return "agent"
    tl = str(a.get("traceloop.span.kind") or "")
    if tl in ("workflow", "task"):
        return "chain"
    if tl in ("agent", "tool"):
        return tl
    if a.get("llm.request.type") in ("chat", "completion"):
        return "llm"
    aop = str(a.get("ai.operationId") or "")
    if aop.endswith((".doGenerate", ".doStream")):
        return "llm"
    if aop.startswith(("ai.embed", "ai.embedMany")):
        return "embedding"
    if aop == "ai.toolCall":
        return "tool"
    if aop.startswith("ai."):
        return "chain"
    if any(k in a for k in ("gen_ai.request.model", "gen_ai.system", "gen_ai.provider.name", "llm.model_name")):
        return "llm"
    if "retrieval.documents.0.document.content" in a:
        return "retrieval"
    return "span"


def _keep(k, v):
    """Small, non-text attributes are kept on the span (and redacted when stored); text goes to input/output."""
    if not isinstance(k, str) or k in TEXT_KEYS or k.startswith(TEXT_PREFIXES):
        return False
    if isinstance(v, str):
        return len(v) <= 500
    if isinstance(v, (list, dict)):
        try:
            return len(json.dumps(v, default=str)) <= 2000
        except (TypeError, ValueError, RecursionError):
            return False
    return True


def _lumen_span(s, t0):
    a, kind = s["attrs"], _kind(s)
    user, prompt, out = None, None, None
    if kind == "llm":
        msgs = _messages_in(a, s["events"])
        if msgs:
            prompt = "\n".join(f"{r}: {t}" for r, t in msgs)
            user = next((t for r, t in reversed(msgs) if r == "user" and t), None)
        else:
            prompt = _text(_first(a, "input.value", "traceloop.entity.input", "gen_ai.prompt"))
        out = _messages_out(a, s["events"]) or _text(_first(a, "output.value", "traceloop.entity.output"))
    elif kind == "retrieval":
        prompt = _text(_first(a, "input.value", "traceloop.entity.input", "db.query.text"))
        docs, i = [], 0
        while i < 200 and f"retrieval.documents.{i}.document.content" in a:
            docs.append(_text(a[f"retrieval.documents.{i}.document.content"]))
            i += 1
        out = "\n\n".join(docs) or _text(_first(a, "output.value", "traceloop.entity.output"))
    elif kind == "tool":
        prompt = _text(_first(a, "input.value", "gen_ai.tool.call.arguments", "ai.toolCall.args", "traceloop.entity.input"))
        out = _text(_first(a, "output.value", "gen_ai.tool.call.result", "ai.toolCall.result", "traceloop.entity.output"))
    else:
        prompt = _text(_first(a, "input.value", "traceloop.entity.input"))
        out = _text(_first(a, "output.value", "traceloop.entity.output"))
    err = None
    if s.get("status") == 2:
        exc = next((e["attrs"].get("exception.message") for e in s["events"] if e["name"] == "exception"), None)
        err = _text(s.get("status_msg") or exc or "error")[:2000]
    model = _first(a, "gen_ai.response.model", "gen_ai.request.model", "llm.model_name", "ai.response.model", "ai.model.id",
                   "llm.request.model")
    small = {k: v for k, v in a.items() if _keep(k, v)}
    small["otel.kind"], small["otel.start_ns"] = s.get("kind", 0), s.get("start", 0)
    start, end = s.get("start") or t0, s.get("end") or s.get("start") or t0
    return {"id": s["span_id"] or None, "parent_id": s.get("parent_id") or None, "type": kind, "name": s.get("name") or kind,
            "model": str(model)[:200] if model is not None else None, "input": prompt or None, "output": out or None,
            "tokens_in": _num(_first(a, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt",
                                     "ai.usage.promptTokens", "ai.usage.inputTokens", "llm.usage.prompt_tokens")),
            "tokens_out": _num(_first(a, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion",
                                      "ai.usage.completionTokens", "ai.usage.outputTokens", "llm.usage.completion_tokens")),
            "latency_ms": round(max(0, end - start) / 1e6, 1), "start_ms": round(max(0, start - t0) / 1e6, 1),
            "status": "error" if err else "ok", "error": err, "attrs": small, "_user": user}


def _iso_ns(ns):
    if not ns:
        return None
    try:
        return datetime.fromtimestamp(ns / 1e9, timezone.utc).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _roots(spans):
    ids = {s["span_id"] for s in spans}
    return [s for s in spans if not s.get("parent_id") or s["parent_id"] not in ids]


def _complete(spans):
    """The trace's root has arrived: a span without a parent, or a service entry span (server / consumer)."""
    return any(not s.get("parent_id") or s.get("kind") in (2, 5) for s in spans)


def to_trace(trace_id, spans):
    spans = sorted(spans, key=lambda s: s.get("start") or 0)
    t0 = min((s["start"] for s in spans if s.get("start")), default=0)
    roots = _roots(spans)
    root = roots[0] if roots else spans[0]
    ls = [_lumen_span(s, t0) for s in spans]
    lroot = ls[spans.index(root)]
    llm = [x for x in ls if x["type"] == "llm"]
    every = {**root.get("resource", {})}
    for s in spans:
        every.update({k: v for k, v in s["attrs"].items() if k not in every})
    inp = lroot["input"] if lroot["type"] != "llm" and lroot["input"] else (llm[0]["_user"] or llm[0]["input"]) if llm else lroot["input"]
    out = lroot["output"] if lroot["type"] != "llm" and lroot["output"] else None
    service = root.get("resource", {}).get("service.name")
    end = max((s.get("end") or 0) for s in spans)

    def ident(*keys):
        v = _first(every, *keys)
        return str(v)[:200] if isinstance(v, (str, int)) and not isinstance(v, bool) else None
    return {"id": trace_id, "name": root.get("name") or "trace", "ts": _iso_ns(t0), "input": inp, "output": out, "spans": ls,
            "latency_ms": round(max(0, end - t0) / 1e6, 1) if t0 else None,
            "user_id": ident("user.id", "enduser.id", "ai.telemetry.metadata.userId"),
            "session_id": ident("session.id", "gen_ai.conversation.id", "ai.telemetry.metadata.sessionId"),
            "prompt_name": ident("lumen.prompt_name", "llm.prompt_template.name"),
            "prompt_version": ident("lumen.prompt_version", "llm.prompt_template.version"),
            "tags": ["otel"] + ([str(service)[:100]] if isinstance(service, str) and service else []),
            "metadata": {"source": "otel", "service": service if isinstance(service, str) else None,
                         "scope": root.get("scope"), "otel_t0": t0}}


# ====================================================================== holding partial traces, storing
def _weight(v):
    """Rough bytes a decoded value keeps in memory (Python objects are much bigger than their text)."""
    n, stack = 0, [v]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            n += 60 + len(x)
        elif isinstance(x, dict):
            n += 100 + 40 * len(x)
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            n += 60 + 8 * len(x)
            stack.extend(x)
        else:
            n += 32
    return n


def _size(s):
    return 400 + _weight(s.get("name")) + _weight(s["attrs"]) + _weight(s.get("events")) + _weight(s.get("resource"))


def _stored_ids(conn, project_id, tids):
    out, tl = set(), [t[:200] for t in tids]
    for i in range(0, len(tl), 500):
        chunk = tl[i:i + 500]
        q = ",".join("?" * len(chunk))
        out.update(r[0] for r in conn.execute(f"SELECT id FROM traces WHERE project_id=? AND id IN ({q})", (project_id, *chunk)))
    return out


def accept(conn, project_id, spans):
    """Take decoded spans for a project. -> {"added": new traces seen, "stored": traces written now, "held": waiting}"""
    path, now, groups = getattr(conn, "lumen_path", None) or "", time.monotonic(), {}
    for s in spans:
        if s.get("trace_id") and s.get("span_id"):
            groups.setdefault(s["trace_id"], {})[s["span_id"]] = s
    if len(groups) > MAX_TRACES:
        raise ValueError(f"more than {MAX_TRACES:,} traces in one request: lower the exporter's batch size")
    stored_ids = _stored_ids(conn, project_id, list(groups))
    ready, others, new = [], [], 0
    with _plock:
        for tid, g in groups.items():
            key = (path, project_id, tid)
            ent = _pending.pop(key, None)
            if ent:
                _held[0] -= ent["bytes"]
            stored = tid[:200] in stored_ids
            if ent is None and not stored:
                new += 1
            merged = {**(ent["spans"] if ent else {}), **g}
            if stored or _complete(merged.values()) or len(merged) >= ingest.MAX_SPANS:   # late spans of a stored trace go at once
                ready.append((tid, list(merged.values())))
            else:
                size = (ent["bytes"] if ent else 0) + sum(_size(s) for s in g.values())
                _pending[key] = {"first": ent["first"] if ent else now, "spans": merged, "bytes": size}
                _held[0] += size
        for key in [k for k, e in _pending.items() if k[:2] == (path, project_id) and now - e["first"] >= HOLD_S]:
            e = _pending.pop(key)
            _held[0] -= e["bytes"]
            ready.append((key[2], list(e["spans"].values())))
        while _pending and (len(_pending) > MAX_PENDING or _held[0] > MAX_PENDING_BYTES):     # memory stays bounded
            key, e = _pending.popitem(last=False)
            _held[0] -= e["bytes"]
            item = (key[2], list(e["spans"].values()))
            if key[:2] == (path, project_id):
                ready.append(item)
            else:
                others.append((key[0], key[1], item))
        held = sum(1 for k in _pending if k[:2] == (path, project_id))
    stored = store(conn, project_id, ready)
    if others:
        _store_in_their_databases(others)
    return {"added": new, "stored": stored, "held": held, "spans": len(spans)}


def _late(conn, project_id, tid, spans):
    """Spans of a trace that is already stored: add them; a late root span or LLM answer updates the trace."""
    row = conn.execute("SELECT metadata FROM traces WHERE id=? AND project_id=?", (tid[:200], project_id)).fetchone()
    if not row:
        return False
    try:
        t0 = json.loads(row[0] or "{}").get("otel_t0")
    except (ValueError, TypeError, AttributeError):
        t0 = None
    t0 = t0 if isinstance(t0, int) and t0 > 0 else min((s.get("start") or 0 for s in spans), default=0)
    ls = [_lumen_span(s, t0) for s in spans]
    ingest.append_spans(conn, project_id, tid[:200], ls)
    changes = {}
    roots = [(s, x) for s, x in zip(spans, ls) if not s.get("parent_id")]
    if roots:
        s, x = min(roots, key=lambda r: r[0].get("start") or 0)
        changes["name"] = s.get("name") or None
        begin = min(s.get("start") or t0, t0) if t0 else s.get("start")
        if s.get("end") and begin:
            changes["latency_ms"] = round(max(0, s["end"] - begin) / 1e6, 1)
            changes["ts"] = _iso_ns(begin)
        if x["type"] != "llm":
            changes["input"], changes["output"] = x["input"], x["output"]
    llm = [x for x in ls if x["type"] == "llm"]
    if llm:
        changes["fill_input"], changes["fill_output"] = llm[0]["_user"] or llm[0]["input"], llm[-1]["output"]
    if changes or any(x["type"] == "retrieval" for x in ls):
        ingest.reevaluate(conn, project_id, tid[:200], **changes)
    return True


def store(conn, project_id, ready):
    """Write ready traces (new ones through ingestion, late spans onto stored ones). -> new traces stored.
    A trace that cannot be converted is logged and skipped; it never fails the others."""
    traces = []
    for tid, spans in ready:
        try:
            if not _late(conn, project_id, tid, spans):
                traces.append(to_trace(tid, spans))
        except Exception:
            log.exception("OpenTelemetry trace %s skipped", tid[:64])
    if not traces:
        return 0
    added = ingest.ingest_traces(conn, project_id, traces, actor="otlp")["added"]
    for fn in ON_STORED:
        try:
            fn(getattr(conn, "lumen_path", None) or "", project_id, added)
        except Exception:
            log.exception("trace metering failed")
    return added


def _store_in_their_databases(items):
    from . import db
    work = {}
    for path, pid, item in items:
        work.setdefault((path, pid), []).append(item)
    n = 0
    for (path, pid), batch in work.items():
        if not path or not os.path.exists(path):
            continue                                # a demo copy or organisation deleted in the meantime
        c = db.connect(path)
        try:
            n += store(c, pid, batch)
        except Exception:
            log.exception("could not store held OpenTelemetry traces")
        finally:
            c.close()
    return n


def flush_stale(force=False):
    """Store held traces whose root never came (HOLD_S passed), in whichever database they belong to."""
    now, items = time.monotonic(), []
    with _plock:
        for key in [k for k, e in _pending.items() if force or now - e["first"] >= HOLD_S]:
            e = _pending.pop(key)
            _held[0] -= e["bytes"]
            items.append((key[0], key[1], (key[2], list(e["spans"].values()))))
    return _store_in_their_databases(items) if items else 0
