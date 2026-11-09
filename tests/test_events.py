"""Live activity stream: payload ids (incident, proposal, request ids) must not break the event sequence."""
from lumen import events


def test_text_ids_in_events_do_not_break_the_stream():
    p = events.publish("proposal", id="PRP-TEST", title="roll back")
    t = events.publish("traces", count=3)
    assert isinstance(p["seq"], int) and t["seq"] == p["seq"] + 1
    assert p["id"] == "PRP-TEST", "the payload id is kept for display"
    got = events.since(p["seq"] - 1)          # used to raise TypeError: str > int
    assert [e["seq"] for e in got][-2:] == [p["seq"], t["seq"]]
    assert events.since(t["seq"]) == []
