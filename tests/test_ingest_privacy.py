import json


def test_ingestion_redacts_pii_from_structured_trace_fields(tmp_path):
    from lumen import db
    from lumen.ingest import ingest_traces

    conn = db.connect(str(tmp_path / "lumen.db"))
    try:
        trace = {
            "id": "privacy-test",
            "project_id": "test-project",
            "user_id": "alice@example.com",
            "session_id": "+1 415 555 0199",
            "input": "hello",
            "output": "world",
            "prompt_name": "prompt@example.com",
            "prompt_version": "version@example.com",
            "tags": ["customer@example.com"],
            "metadata": {"requester": "bob@example.com"},
            "spans": [{
                "type": "tool",
                "name": "lookup",
                "model": "model@example.com",
                "attrs": {"contact": "carol@example.com"},
            }],
        }

        result = ingest_traces(conn, "test-project", [trace])

        stored_trace = db.one(conn, "SELECT * FROM traces WHERE id='privacy-test'")
        stored_span = db.one(conn, "SELECT * FROM spans WHERE trace_id='privacy-test'")
        assert result["pii_redacted"] == 8
        assert stored_trace["redactions"] == 8
        assert stored_trace["user_id"] == "[EMAIL]"
        assert stored_trace["session_id"] == "[PHONE]"
        assert stored_trace["model"] == "[EMAIL]"
        assert stored_trace["prompt_name"] == "[EMAIL]"
        assert stored_trace["prompt_version"] == "[EMAIL]"
        assert json.loads(stored_trace["tags"]) == ["[EMAIL]"]
        assert json.loads(stored_trace["metadata"]) == {"requester": "[EMAIL]"}
        assert json.loads(stored_span["attrs"]) == {"contact": "[EMAIL]"}
        assert "@" not in json.dumps(stored_trace) + json.dumps(stored_span)
    finally:
        conn.close()
