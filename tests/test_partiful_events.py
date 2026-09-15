"""Partiful events: retained guest lists, attendance on records, promotion. Synthetic only."""

import json

import pytest

from people_sync import photos, promote
from people_sync.scrape import partiful, snapshot


@pytest.fixture
def retained(monkeypatch, tmp_path):
    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    return stored


def test_event_when_parses_with_a_known_year():
    assert partiful.parse_event_when("Sat 9/5 at 8:30pm", 2026) == "2026-09-05T20:30"
    assert partiful.parse_event_when("Sun 1/10 at 12am", 2026) == "2026-01-10T00:00"
    assert partiful.parse_event_when("Sat 9/5 at 8:30pm", None) == "Sat 9/5 at 8:30pm"
    assert partiful.parse_event_when(None, 2026) is None


def test_event_and_guest_captures_validate_and_refuse_unknown_fields(retained):
    page, key = snapshot.retain_list(
        "partiful",
        [
            {
                "id": "KflZTnG0PFfRmh63ZD8a",
                "title": "Rave",
                "when": "Sat 1/25 at 9:30pm",
                "status": "WENT",
                "junk": 1,
            }
        ],
        ordinal=0,
        scope="events",
        expected_total=1,
        complete=True,
    )
    assert page["entries"] == [
        {
            "id": "KflZTnG0PFfRmh63ZD8a",
            "title": "Rave",
            "when": "Sat 1/25 at 9:30pm",
            "status": "WENT",
        }
    ]
    assert "unknown-fields" in page["exclusions"] and key.startswith("profiles/partiful/captures/")
    guests, _ = snapshot.retain_list(
        "partiful",
        [
            {
                "event_id": "KflZTnG0PFfRmh63ZD8a",
                "name": "Quyen Le",
                "section": "Went",
                "plus_ones": 1,
                "uid": "yLZHiv12FGcw23dNGrUvszlCS0o1",
            },
            {"event_id": "bad id!", "name": "x", "section": None, "plus_ones": 0, "uid": None},
        ],
        ordinal=0,
        scope="event_guests",
        expected_total=2,
    )
    assert guests["entries"][0]["uid"] == "yLZHiv12FGcw23dNGrUvszlCS0o1"
    assert (
        "event_id" not in guests["entries"][1]
        and "unsafe-or-invalid-values" in guests["exclusions"]
    )


def test_ingest_guest_adds_the_event_and_keeps_the_record(mocker):
    existing = {
        "url": "https://partiful.com/u/abc123def",
        "last_seen": "3 months ago",
        "events": [{"id": "old", "title": "Old", "capture_key": "k0"}],
    }
    sql = mocker.patch(
        "people_sync.lifedata.sql",
        return_value=[{"raw": json.dumps(existing), "name": "Caroline Odia"}],
    )
    upsert = mocker.patch("people_sync.ledger.upsert")
    header = {"title": "💥ALEX 21ST BIRTHDAY RAVE💥", "when": "Saturday, Jan 25, 2025"}
    event = {
        "id": "KflZTnG0PFfRmh63ZD8a",
        "title": "💥ALEX 21ST BIRTHDAY RAVE💥",
        "when": "Sat 1/25 at 9:30pm",
        "status": "WENT",
    }
    guest = {
        "event_id": event["id"],
        "name": "Caroline O",
        "section": "Went",
        "plus_ones": 0,
        "uid": "abc123def",
    }
    ref = {
        "capture_key": "profiles/partiful/captures/x.json",
        "scope": "event_guests",
        "ordinal": 0,
        "entry_ordinal": 3,
    }
    assert partiful.ingest_guest(header, event, guest, ref) == "partiful:abc123def"
    [record] = upsert.call_args.args[0]
    assert record.name == "Caroline Odia" and record.raw["last_seen"] == "3 months ago"
    assert [e["id"] for e in record.raw["events"]] == ["old", event["id"]]
    added = record.raw["events"][1]
    assert added["starts_at"] == "2025-01-25T21:30" and added["role"] == "went"
    assert added["capture_key"] == ref["capture_key"] and record.capture_refs == (ref,)
    assert "FROM people_sync_records" in sql.call_args.args[0]
    assert partiful.ingest_guest(header, event, {**guest, "uid": None}, ref) is None


def test_event_ops_promote_once_with_evidence(mocker):
    raw = {
        "events": [
            {
                "id": "ev1",
                "title": "Rave",
                "starts_at": "2025-01-25T21:30",
                "role": "went",
                "capture_key": "profiles/partiful/captures/g.json",
            },
            {"id": "ev2", "title": "No key"},
        ]
    }

    def sql(query):
        if "FROM people_sync_records" in query:
            return [{"record_id": "partiful:u1", "person_id": "p1", "raw": json.dumps(raw)}]
        if "FROM person_events" in query:
            return []
        return []

    mocker.patch("people_sync.lifedata.sql", side_effect=sql)
    ops = promote.event_ops(set())
    assert [(o.kind, o.person_id, o.value, o.raw_r2_key) for o in ops] == [
        ("event", "p1", "ev1", "profiles/partiful/captures/g.json")
    ]
    insert = mocker.patch("people_sync.lifedata.insert")
    promote.apply(ops)
    tables = [c.args[0] for c in insert.call_args_list]
    assert tables == ["person_events", "provenance"]
    row = insert.call_args_list[0].args[1][0]
    assert row == {
        "id": "partiful:ev1:p1",
        "person_id": "p1",
        "platform": "partiful",
        "event_id": "ev1",
        "title": "Rave",
        "starts_at": "2025-01-25T21:30",
        "role": "went",
        "url": "https://partiful.com/e/ev1",
    }
    edge = insert.call_args_list[1].args[1][0]
    assert (
        edge["from_kind"] == "takeout"
        and edge["from_ref"] == "profiles/partiful/captures/g.json"
        and edge["to_ref"] == "partiful:ev1:p1"
    )
    # already promoted: nothing planned
    assert promote.event_ops({edge["id"]}) == []
