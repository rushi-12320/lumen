"""Public demo mode (LUMEN_DEMO=sandbox): every visitor gets a private working copy."""
import os
import tempfile
import time

import pytest

os.environ.setdefault("LUMEN_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from fastapi.testclient import TestClient  # noqa: E402

from lumen import db, prompts, sandbox  # noqa: E402
from lumen.app import app  # noqa: E402

PID = "PRJ-SUPPORT"


@pytest.fixture()
def demo(monkeypatch):
    monkeypatch.setattr(sandbox, "ENABLED", True)
    monkeypatch.setattr(sandbox, "WRITES_PER_MIN", 1000)
    with TestClient(app) as warm:        # runs startup: seeds the template once
        warm.get("/api/projects")
    yield


def visitor():
    return TestClient(app)


def template_count():
    c = db.connect(db.DB_PATH)
    try:
        return db.one(c, "SELECT COUNT(*) n FROM traces WHERE project_id=?", (PID,))["n"]
    finally:
        c.close()


def traces(cl):
    return next(p for p in cl.get("/api/projects").json() if p["id"] == PID)["traces"]


def test_each_visitor_gets_a_private_working_copy(demo):
    a, b = visitor(), visitor()
    base = template_count()
    assert traces(a) == base and traces(b) == base
    assert sandbox.COOKIE in a.cookies and a.cookies[sandbox.COOKIE] != b.cookies[sandbox.COOKIE]
    r = a.post("/api/sim/tick", json={"project": PID, "n": 7})
    assert r.status_code == 200, r.text
    assert traces(a) == base + 7
    assert traces(b) == base, "another visitor must not see A's changes"
    assert template_count() == base, "the shared template is never written"
    assert a.get("/api/demo").json()["demo"] is True


def test_rollback_approval_works_and_stays_private(demo):
    a, b = visitor(), visitor()
    rb = next(p for p in a.get(f"/api/proposals?project={PID}").json() if p["kind"] == "rollback_prompt")
    r = a.post(f"/api/proposals/{rb['id']}/decision", json={"decision": "approve", "by": "Demo visitor"}).json()
    assert r["ok"] and r["production"] == 2
    assert a.get(f"/api/proposals?project={PID}").json()[0]  # still readable
    other = next(p for p in b.get(f"/api/proposals?project={PID}").json() if p["id"] == rb["id"])
    assert other["status"] == "pending_approval"
    c = db.connect(db.DB_PATH)
    assert prompts.label_version(c, PID, "rag_answer", "production") == 3
    c.close()


def test_sdk_key_routes_to_the_visitors_sandbox(demo):
    a, b = visitor(), visitor()
    key = a.post("/api/keys", json={"project": PID, "name": "my app", "by": "Demo visitor"}).json()["key"]
    sdk = TestClient(app)  # no cookie, like a real SDK
    before_a, before_b = traces(a), traces(b)
    r = sdk.post("/v1/traces", headers={"X-Lumen-Key": key},
                 json={"name": "chat", "input": "hi, mail me at a@b.com", "output": "hello", "model": "m"})
    assert r.status_code == 200 and r.json()["added"] == 1 and r.json()["pii_redacted"] >= 1
    assert traces(a) == before_a + 1 and traces(b) == before_b
    assert sdk.post("/v1/traces", headers={"X-Lumen-Key": "lmn_wrong"}, json={"name": "x"}).status_code == 401
    assert sdk.get("/v1/prompts/rag_answer", headers={"X-Lumen-Key": key}).status_code == 200


def test_unsafe_actions_are_blocked(demo):
    a = visitor()
    r = a.put("/api/llm/config", json={"changes": {"base_url": "http://169.254.169.254"}, "by": "x"})
    assert r.status_code == 403
    assert a.put("/api/policy", json={"changes": {"llm_allow_non_local": True}, "by": "x"}).status_code == 403
    assert a.post("/api/llm/requests/whatever/decision", json={"decision": "approve", "by": "x"}).status_code == 403


def test_activity_feed_is_per_visitor(demo):
    a, b = visitor(), visitor()
    a.post("/api/sim/tick", json={"project": PID, "n": 3})
    assert any(e["kind"] == "traces" for e in a.get("/api/events/recent").json())
    assert not any(e.get("sbx") == a.cookies[sandbox.COOKIE] for e in b.get("/api/events/recent").json())


def test_reset_gives_a_fresh_copy(demo):
    a = visitor()
    base = traces(a)
    a.post("/api/sim/tick", json={"project": PID, "n": 5})
    old = a.cookies[sandbox.COOKIE]
    assert a.post("/api/demo/reset").json()["ok"]
    a.cookies.clear()
    assert traces(a) == base
    assert a.cookies[sandbox.COOKIE] != old and not sandbox.exists(old)


def test_rate_limit(demo, monkeypatch):
    monkeypatch.setattr(sandbox, "WRITES_PER_MIN", 3)
    a = visitor()
    codes = [a.post("/api/sim/tick", json={"project": PID, "n": 1}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


def test_old_sandboxes_expire(demo, monkeypatch):
    a = visitor()
    old = a.get("/api/demo") and a.cookies[sandbox.COOKIE]
    past = time.time() - 10 * 3600
    os.utime(sandbox._path(old), (past, past))
    visitor().get("/api/projects")  # creating a new sandbox cleans up
    assert not sandbox.exists(old)


def test_demo_off_by_default():
    assert sandbox.ENABLED is False or os.environ.get("LUMEN_DEMO") == "sandbox"
    with TestClient(app) as c:
        assert sandbox.COOKIE not in c.get("/api/projects").cookies


def test_health_check_does_not_create_a_sandbox(demo):
    with visitor() as c:
        response = c.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert sandbox.COOKIE not in response.cookies
