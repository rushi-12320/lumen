"""Wires Lumen Cloud into the FastAPI app (lumen/app.py calls setup() when LUMEN_MODE=cloud) and runs the
maintenance job: retention per plan, trial reminders, expired sessions and tokens."""
import asyncio
import logging
import os
from datetime import timedelta

from fastapi.staticfiles import StaticFiles

from .. import otlp, sandbox
from . import control, plans, public_url, tenants
from .middleware import CloudMiddleware
from .routes import router
from .site import SITE

log = logging.getLogger("lumen.cloud")
EVERY_S = 6 * 3600


def _key_hook(key_hash, sid):
    """An SDK key created inside an organisation's workspace -> remember which organisation it belongs to."""
    if sid and sid.startswith(tenants.PREFIX):
        control.register_key(key_hash, sid[len(tenants.PREFIX):])
        return True
    return False


def _meter_otlp(db_path, project_id, n):
    """OpenTelemetry traces count against the plan when they are stored (they can be held for a while first)."""
    oid = tenants.org_for_path(db_path)
    if not (oid and n):
        return
    with control.cx() as c:
        o = control.org(c, oid)
    if o:
        from .middleware import _meter
        _meter(o, n, plans.limits(o))


def setup(app):
    if _key_hook not in sandbox.KEY_HOOKS:
        sandbox.KEY_HOOKS.append(_key_hook)
    if _meter_otlp not in otlp.ON_STORED:
        otlp.ON_STORED.append(_meter_otlp)
    app.add_middleware(CloudMiddleware)
    app.mount("/site", StaticFiles(directory=os.path.join(SITE, "assets")), name="site")
    app.include_router(router)


def start():
    with control.cx():
        pass                                  # creates the control schema
    tenants.sync_llm_all()


def maintenance():
    now = control.now()
    with control.cx() as c:
        orgs = control.rows(c, "SELECT * FROM orgs")
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (control.iso(now),))
        c.execute("DELETE FROM tokens WHERE expires_at < ?", (control.iso(now - timedelta(days=30)),))
    deleted = 0
    for o in orgs:
        if not tenants.exists(o["id"]):
            continue
        try:
            deleted += tenants.apply_retention(o["id"], plans.limits(o)["retention_days"])
        except Exception:
            log.exception("retention failed for %s", o["id"])
        trial = control.parse(o.get("trial_ends_at"))
        if trial and now < trial < now + timedelta(days=3) and plans.effective(o)[1] == "trial" \
                and control.mark_notified(o["id"], "trial3"):
            from .billing import owners
            from .mailer import send
            with control.cx() as c:
                to = owners(c, o["id"])
            for r in to:
                send(r["email"], "Your Lumen Team trial ends soon",
                     [f"Hello {r['name']},", f"The Team trial of {o['name']} ends on {trial.strftime('%d %b %Y')}. "
                      "Add a payment method to keep Team limits; otherwise the organisation moves to the Free plan "
                      "and data older than the Free plan's retention is deleted."], ("Choose a plan", f"{public_url()}/app/settings#billing"))
    log.info("maintenance: %d orgs, %d old traces deleted", len(orgs), deleted)
    return {"orgs": len(orgs), "deleted": deleted}


async def loop():
    while True:
        try:
            await asyncio.to_thread(maintenance)
        except Exception:
            log.exception("maintenance failed")
        await asyncio.sleep(EVERY_S)
