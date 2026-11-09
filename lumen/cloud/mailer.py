"""Transactional email: Amazon SES (LUMEN_SES_REGION, uses the server's AWS role), any SMTP server
(LUMEN_SMTP_HOST ...), or - with neither set - files in data/outbox/ for development and tests."""
import html
import json
import logging
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import company, data_dir, env, public_url

log = logging.getLogger("lumen.mail")


def _sender():
    brand = company()["brand"]
    return env("LUMEN_EMAIL_FROM", formataddr((brand, "no-reply@localhost")))


def outbox_dir():
    d = os.path.join(data_dir(), "outbox")
    os.makedirs(d, exist_ok=True)
    return d


def _build(to, subject, text, html_body):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = _sender(), to, subject
    m["Message-ID"] = make_msgid(domain="lumen")
    if env("LUMEN_SUPPORT_EMAIL"):
        m["Reply-To"] = env("LUMEN_SUPPORT_EMAIL")
    m.set_content(text)
    m.add_alternative(html_body, subtype="html")
    return m


def _deliver(m):
    region, host = env("LUMEN_SES_REGION"), env("LUMEN_SMTP_HOST")
    try:
        if region:
            import boto3   # only needed on the server
            boto3.client("ses", region_name=region).send_raw_email(RawMessage={"Data": m.as_bytes()})
        elif host:
            port, ctx = int(env("LUMEN_SMTP_PORT", "587")), ssl.create_default_context()
            implicit = port in (465, 2465)                    # TLS from the first byte; other ports upgrade with STARTTLS
            with (smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) if implicit else smtplib.SMTP(host, port, timeout=20)) as s:
                if not implicit:
                    s.starttls(context=ctx)
                if env("LUMEN_SMTP_USER"):
                    s.login(env("LUMEN_SMTP_USER"), env("LUMEN_SMTP_PASSWORD"))
                s.send_message(m)
        else:
            name = f"{time.time_ns():020d}-{abs(hash(m['Message-ID'])) % 10 ** 6:06d}.eml"   # sorts in sending order
            with open(os.path.join(outbox_dir(), name), "wb") as f:
                f.write(m.as_bytes())
            log.info("email (outbox): %s -> %s", m["Subject"], m["To"])
    except Exception as e:      # an email problem must never break sign-up or billing
        log.error("email to %s failed: %s", m["To"], e)


def send(to, subject, lines, button=None, wait=False):
    """Plain, branded email. lines: paragraphs (text). button: (label, url)."""
    brand = company()["brand"]
    text = "\n\n".join(lines + ([f"{button[0]}: {button[1]}"] if button else []) +
                       [f"- {brand}", f"{public_url()}"])
    paras = "".join(f'<p style="margin:0 0 14px">{html.escape(p)}</p>' for p in lines)
    btn = (f'<p style="margin:22px 0"><a href="{html.escape(button[1])}" style="background:#2A5DB0;color:#F6F5F1;'
           f'padding:11px 20px;border-radius:8px;text-decoration:none;font-weight:600">{html.escape(button[0])}</a></p>'
           f'<p style="margin:0 0 14px;color:#646E7B;font-size:13px">Or open: {html.escape(button[1])}</p>') if button else ""
    body = (f'<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#13233A;max-width:560px">'
            f'<p style="font-weight:700;font-size:18px;margin:0 0 18px">{html.escape(brand)}</p>{paras}{btn}'
            f'<p style="color:#646E7B;font-size:13px;margin-top:28px">{html.escape(company()["name"])} · '
            f'{html.escape(public_url())}</p></div>')
    m = _build(to, subject, text, body)
    if wait or env("LUMEN_MAIL_SYNC"):
        _deliver(m)
    else:
        threading.Thread(target=_deliver, args=(m,), daemon=True).start()


def latest(to=None):
    """Development helper (tests): (subject, plain text) of the newest outbox email, optionally to one address."""
    from email import policy
    from email.parser import BytesParser
    for f in sorted(os.listdir(outbox_dir()), reverse=True):
        with open(os.path.join(outbox_dir(), f), "rb") as fh:
            m = BytesParser(policy=policy.default).parsebytes(fh.read())
        if not to or to.lower() == str(m["To"]).lower():
            return str(m["Subject"]), m.get_body(preferencelist=("plain",)).get_content()
    return None, None


def jsonable(d):
    return json.loads(json.dumps(d, default=str))
