"""Server-rendered website pages: site/pages/<name>.html inside one shared layout, with company details and plan
prices filled in from settings ({{name}} placeholders), so legal pages and prices change without editing HTML."""
import html
import os
import re

from fastapi.responses import HTMLResponse

from . import company, public_url
from . import plans as P

SITE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "site"))
PAGES = os.path.join(SITE, "pages")
_TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
NAV = [("/#product", "Product"), ("/pricing", "Pricing"), ("/docs", "Docs"), ("/security", "Security"), ("/demo", "Live demo")]
FOOT = [("Product", [("/#product", "Overview"), ("/pricing", "Pricing"), ("/demo", "Live demo"), ("/docs", "Docs"),
                     ("/docs#self-host", "Self-host")]),
        ("Company", [("/about", "About"), ("/contact", "Contact"), ("/security", "Security")]),
        ("Legal", [("/terms", "Terms of service"), ("/privacy", "Privacy policy"), ("/refunds", "Cancellation & refunds"),
                   ("/shipping", "Delivery policy")])]
LOGO = ('<svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true"><circle cx="16" cy="16" r="11" fill="none" '
        'stroke="currentColor" stroke-width="2.6"/><path d="M9 18l4-5 4 4 6-7" fill="none" stroke="#E07B39" '
        'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def esc(v):
    return html.escape(str(v if v is not None else ""))


def money(cur, amount):
    if amount is None:
        return "Custom"
    return (f"₹{amount:,.0f}" if cur == "INR" else f"${amount:,.0f}") if amount else ("₹0" if cur == "INR" else "$0")


def pricing_cards():
    out = []
    for key in P.ORDER:
        p = P.get(key)
        price = p.get("price")
        amounts = "".join(
            f'<span class="amt" data-cur="{cur}">{esc(money(cur, (price or {}).get(cur)))}'
            f'{"" if not price or not price.get(cur) else "<small>/month" + (" + GST" if cur == "INR" else "") + "</small>"}</span>'
            for cur in ("INR", "USD"))
        cta = (('<a class="btn" href="/contact">Talk to us</a>') if p.get("contact") else
               (f'<a class="btn primary" href="/signup?plan={key}">Start {p.get("trial_days", 14)}-day free trial</a>' if key == "team"
                else '<a class="btn" href="/signup">Start free</a>'))
        feats = "".join(f"<li>{esc(f)}</li>" for f in p["features"])
        out.append(f'<div class="plan{" featured" if key == "team" else ""}"><h3>{esc(p["name"])}</h3>'
                   f'<p class="muted">{esc(p["tagline"])}</p><p class="price">{amounts}</p>{cta}<ul class="ticks">{feats}</ul></div>')
    return '<div class="plans">' + "".join(out) + "</div>"


def plan_rows():
    rows = [("Projects", "projects", ""), ("Traces per month", "traces_per_month", ""), ("Data kept for", "retention_days", " days"),
            ("Team members", "members", ""), ("LLM calls a month on our GPU (each approved)", "llm_calls_per_month", "")]
    head = "".join(f"<th>{esc(P.get(k)['name'])}</th>" for k in P.ORDER)
    body = ""
    for label, field, unit in rows:
        cells = []
        for k in P.ORDER:
            v = P.get(k)[field]
            cells.append("<td>Custom</td>" if k == "enterprise" else f"<td>{v:,}{unit}</td>")
        body += f"<tr><th>{esc(label)}</th>{''.join(cells)}</tr>"
    return f'<div class="table-wrap"><table class="compare"><tr><th></th>{head}</tr>{body}</table></div>'


RAW = ("pricing_cards", "plan_rows", "draft_note")


def draft_note():
    """Shown on legal pages until LUMEN_POLICIES_FINAL=1 (after a lawyer has reviewed them)."""
    from . import env
    if env("LUMEN_POLICIES_FINAL") == "1":
        return ""
    return ('<p class="note"><b>Draft.</b> This policy is a starting draft. Fill in the company details in the server settings '
            'and have a lawyer review it, then set LUMEN_POLICIES_FINAL=1 to remove this note.</p>')


def fill(text, extra=None):
    from .. import sdkdist
    vals = {**company(), "public_url": public_url(), "draft_note": draft_note(), "sdk_wheel": sdkdist.info()["wheel"], **(extra or {})}
    vals.setdefault("company", vals["name"])
    return _TOKEN.sub(lambda m: str(vals.get(m.group(1), m.group(0))) if m.group(1) in RAW
                      else esc(vals.get(m.group(1), m.group(0))), text)


def _header(ctx):
    u = (ctx or {}).get("user")
    links = "".join(f'<a href="{h}">{esc(t)}</a>' for h, t in NAV)
    right = ('<a class="btn small" href="/app">Open console</a>' if u else
             '<a href="/login">Log in</a><a class="btn small primary" href="/signup">Start free</a>')
    return (f'<header class="site-head"><div class="wrap head-row"><a class="logo" href="/">{LOGO}<span>{esc(company()["brand"])}</span></a>'
            f'<button class="menu-btn" aria-label="Menu" data-menu>☰</button>'
            f'<nav class="site-nav">{links}</nav><div class="head-right">{right}</div></div></header>')


def _footer():
    cols = "".join(f'<div><h4>{esc(t)}</h4>' + "".join(f'<a href="{h}">{esc(x)}</a>' for h, x in links) + "</div>" for t, links in FOOT)
    c = company()
    return (f'<footer class="site-foot"><div class="wrap foot-grid"><div><a class="logo" href="/">{LOGO}<span>{esc(c["brand"])}</span></a>'
            f'<p class="muted">LLM observability, evaluation and safe fixes, with a human approving every change.</p>'
            f'<p class="muted small">© {esc(c["name"])} · {esc(c["address"])}</p></div>{cols}</div></footer>')


def page(name, title, desc="", ctx=None, extra=None, app=False, status=200):
    with open(os.path.join(PAGES, f"{name}.html"), encoding="utf-8") as f:
        body = f.read()
    blocks = {"pricing_cards": pricing_cards() if "{{pricing_cards}}" in body.replace(" ", "") else "",
              "plan_rows": plan_rows() if "{{plan_rows}}" in body.replace(" ", "") else ""}
    body = fill(body, {**blocks, **(extra or {})})
    brand = company()["brand"]
    full = f"{title} · {brand}" if title else f"{brand} · LLM observability with human-approved fixes"
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full)}</title><meta name="description" content="{esc(desc or 'Trace, evaluate and safely fix your LLM app. Data stored in India.')}">
<link rel="icon" href="/site/favicon.svg"><link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap">
<link rel="stylesheet" href="/site/site.css"></head><body class="{'app' if app else 'site'}">
{_header(ctx)}<main>{body}</main>{'' if app else _footer()}<script src="/site/site.js"></script></body></html>"""
    return HTMLResponse(doc, status_code=status)
