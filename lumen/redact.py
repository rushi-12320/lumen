"""PII redaction (applied BEFORE anything is stored) and prompt-injection detection."""
import re

PATTERNS = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk|api|key)[-_][A-Za-z0-9_\-]{16,}\b", re.I)),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{3,5}\)?[ .-]?)\d{3,4}[ .-]?\d{3,4}(?!\w)")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules|messages)",
    r"disregard (the |your )?(system|previous) (prompt|instructions)",
    r"(reveal|print|show|repeat) (me )?(your|the) (system prompt|hidden instructions|instructions)",
    r"you are now (dan|in developer mode|unrestricted)",
    r"jailbreak", r"pretend (you have|there are) no (rules|restrictions|guidelines)",
    r"</?(system|assistant)>", r"act as (an? )?(unfiltered|uncensored)",
]
_INJ = [re.compile(p, re.I) for p in INJECTION]


def _luhn(digits):
    s, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        s += n
        alt = not alt
    return s % 10 == 0


def redact(text, enabled=True):
    """-> (redacted_text, {kind: count})"""
    if not text or not enabled:
        return text, {}
    counts = {}

    def sub(kind):
        def f(m):
            val = m.group(0)
            if kind == "CARD":
                digits = re.sub(r"\D", "", val)
                if not (13 <= len(digits) <= 19 and _luhn(digits)):
                    return val
            if kind == "PHONE" and len(re.sub(r"\D", "", val)) < 10:
                return val
            counts[kind] = counts.get(kind, 0) + 1
            return f"[{kind}]"
        return f

    out = str(text)
    for kind, rx in PATTERNS:
        out = rx.sub(sub(kind), out)
    return out, counts


def redact_value(value, enabled=True):
    """Redact strings recursively in JSON-like values."""
    if isinstance(value, str):
        return redact(value, enabled)
    if isinstance(value, list):
        redacted, counts = [], {}
        for item in value:
            item, item_counts = redact_value(item, enabled)
            redacted.append(item)
            for kind, count in item_counts.items():
                counts[kind] = counts.get(kind, 0) + count
        return redacted, counts
    if isinstance(value, dict):
        redacted, counts = {}, {}
        for key, item in value.items():
            item, item_counts = redact_value(item, enabled)
            redacted[key] = item
            for kind, count in item_counts.items():
                counts[kind] = counts.get(kind, 0) + count
        return redacted, counts
    return value, {}


def injection_hits(text):
    if not text:
        return []
    return [p.pattern for p in _INJ if p.search(text)]
