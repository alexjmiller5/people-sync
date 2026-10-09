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
        "people_sync.somadata.sql",
        return_value=[
            {"id": "partiful:abc123def", "raw": json.dumps(existing), "name": "Caroline Odia"}
        ],
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

    mocker.patch("people_sync.somadata.sql", side_effect=sql)
    ops = promote.event_ops(promote.load_event_rows(), set())
    assert [(o.kind, o.person_id, o.value, o.raw_r2_key) for o in ops] == [
        ("event", "p1", "ev1", "profiles/partiful/captures/g.json")
    ]
    insert = mocker.patch("people_sync.somadata.insert")
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
    assert promote.event_ops(promote.load_event_rows(), {edge["id"]}) == []


def test_guest_sections_follow_the_dialog_counts():
    rows = [
        {"name": "A", "plus_ones": 1},
        {"name": "B", "plus_ones": 0},
        {"name": "C", "plus_ones": 0},
        {"name": "D", "plus_ones": 0},
    ]
    out = partiful.assign_sections(rows, {"Going": 3, "Maybe": 1})
    assert [r["section"] for r in out] == ["Going", "Going", "Maybe", "Maybe"]
    assert partiful.assign_sections(rows, {}) == rows


def test_host_view_role_mapping_and_js_parse():
    import subprocess

    for js in (partiful.HOST_GUESTS_JS, partiful.DIALOG_SCROLL_JS):
        subprocess.run(["node", "-e", "new Function(process.argv[1])", js], check=True)
    assert partiful.ROLES["Going"] == "went" and partiful.ROLES["Invited"] == "invited"


def test_host_guest_array_is_found_without_a_relative_timestamp():
    """Older events render absolute RSVP dates (4/11/2025), not '3 days ago'; the
    guest array must still be reached from any leaf in the dialog."""
    import json
    import subprocess

    stub = """
    var fiber = {memoizedProps: {}, return: {memoizedProps: {itemData: [
      {id: 'g1', name: 'Ada', status: 'GOING', count: 2, userId: 'u1'}]}}};
    function leaf(t){var e={children: [], innerText: t}; e['__reactFiber$1']=fiber; return e;}
    var dialog = {innerText: 'Manage Guests', querySelectorAll: function(){return [leaf('Ada'), leaf('4/11/2025')]}};
    var document = {querySelector: function(){return dialog}};
    console.log(JSON.stringify(eval(process.argv[1])));
    """
    out = subprocess.run(
        ["node", "-e", stub, partiful.HOST_GUESTS_JS], check=True, capture_output=True, text=True
    )
    assert json.loads(out.stdout) == [
        {"name": "Ada", "guest_id": "g1", "status": "GOING", "count": 2, "uid": "u1"}
    ]


class _HostBrowser:
    """A host-view event page whose list data already carries each guest's uid."""

    def __init__(self):
        self.navigations = []

    def navigate(self, url, timeout):
        self.navigations.append(url)

    def wait_for(self, js, seconds):
        return True

    def click(self, selector):
        raise AssertionError("host view never clicks a row")

    def eval(self, js):
        if js == partiful.HOST_GUESTS_JS:
            return [
                {
                    "name": "Ada",
                    "guest_id": "g1",
                    "status": "GOING",
                    "count": 2,
                    "uid": "yLZHiv12FGcw23dNGrUvszlCS0o1",
                },
                {"name": "Bo", "guest_id": "g2", "status": "SENT", "count": 1, "uid": None},
                {
                    "name": "Cy",
                    "guest_id": "g3",
                    "status": "INTERESTED",
                    "count": 1,
                    "uid": "eifKC28usbT7AsVgehxrdldVwB13",
                },
                {
                    "name": "Di",
                    "guest_id": "g4",
                    "status": "DECLINED",
                    "count": 1,
                    "uid": "iUvno0JiaLhAWCLEiagA0EjK5VF3",
                },
            ]
        if "Manage Guests" in js or "[role=dialog]" in js:
            return True
        if js == partiful.DIALOG_SCROLL_JS:
            return 0
        return None


def test_host_view_reads_uids_from_the_list_without_visiting_guests(retained, monkeypatch):
    monkeypatch.setattr(partiful.time, "sleep", lambda s: None)
    real_retain, retains = snapshot.retain_list, []
    monkeypatch.setattr(
        snapshot, "retain_list", lambda *a, **k: retains.append(k) or real_retain(*a, **k)
    )
    browser = _HostBrowser()
    out = list(partiful.harvest_event_guests(browser, "EV1"))
    assert browser.navigations == [partiful.EVENT_URL.format(event_id="EV1")]
    assert len(retains) == 1 and retains[0]["expected_total"] == 3
    assert [ref["entry_ordinal"] for _, _, ref in out] == [0, 1, 2]
    assert [(g["name"], g.get("uid"), g["section"], g["plus_ones"]) for _, g, _ in out] == [
        ("Ada", "yLZHiv12FGcw23dNGrUvszlCS0o1", "Going", 1),
        ("Bo", None, "Invited", 0),
        ("Cy", "eifKC28usbT7AsVgehxrdldVwB13", "Interested", 0),
    ]
    assert partiful.ROLES["Interested"] == "interested"


def test_guest_role_comes_from_the_guest_section_not_the_event(mocker):
    mocker.patch("people_sync.somadata.sql", return_value=[])
    upsert = mocker.patch("people_sync.ledger.upsert")
    header = {"title": "Rave", "when": "Sat, Jan 25, 2026"}
    event = {"id": "EV1", "title": "Rave", "status": "HOSTING"}
    ref = {
        "capture_key": "profiles/partiful/captures/x",
        "scope": "event_guests",
        "ordinal": 0,
        "entry_ordinal": 0,
    }
    partiful.ingest_guest(header, event, {"uid": "u1", "name": "Ada", "section": "Going"}, ref)
    [record] = upsert.call_args.args[0]
    assert record.raw["events"][0]["role"] == "went"


def test_ingest_guests_batches_one_select_and_one_upsert(mocker):
    sql = mocker.patch("people_sync.somadata.sql", return_value=[])
    upsert = mocker.patch("people_sync.ledger.upsert")
    header = {"title": "Rave", "when": "Sat, Jan 25, 2026"}
    event = {"id": "EV1", "title": "Rave", "status": "WENT"}
    ref = {
        "capture_key": "profiles/partiful/captures/x",
        "scope": "event_guests",
        "ordinal": 0,
        "entry_ordinal": 0,
    }
    guests = [
        ({"uid": "yLZHiv12FGcw23dNGrUvszlCS0o1", "name": "Ada", "section": "Went"}, ref),
        ({"uid": None, "name": "Bo", "section": "Went"}, ref),
        ({"uid": "eifKC28usbT7AsVgehxrdldVwB13", "name": "Cy", "section": "Maybe"}, ref),
    ]
    ids = partiful.ingest_guests(header, event, guests)
    assert ids == ["partiful:yLZHiv12FGcw23dNGrUvszlCS0o1", "partiful:eifKC28usbT7AsVgehxrdldVwB13"]
    assert sql.call_count == 1 and "IN (" in sql.call_args.args[0]
    [records] = upsert.call_args.args
    assert [r.raw["events"][0]["role"] for r in records] == ["went", "maybe"]


def test_event_dates_infer_the_missing_year_from_newest_first_order():
    from datetime import date

    today = date(2026, 10, 9)  # a Friday
    whens = [
        "Yesterday at 8pm",
        "Last Tue at 6:30pm",
        "Sat 1/10 at 6pm",
        "Sun 12/14 at 4pm",  # crosses into the previous year
        "Fri 4/11 at 9pm",
        "Sun 9/1 at 8pm",
        "Sat 7/1 at 8pm",  # 2024-07-01 is a Monday: the weekday skips a year
        "Sat 1/10 at 6pm",  # bounded by the event above, not by today
        "somewhere at 9pm",
        None,
    ]
    assert partiful.event_dates([{"when": w} for w in whens], today) == [
        date(2026, 10, 8),
        date(2026, 10, 6),
        date(2026, 1, 10),
        date(2025, 12, 14),
        date(2025, 4, 11),
        date(2024, 9, 1),
        date(2023, 7, 1),
        date(2015, 1, 10),
        None,
        None,
    ]


def test_choose_events_skips_retained_guest_lists_unless_refreshed():
    from datetime import date

    today = date(2026, 10, 9)
    events = [
        {"id": "EVnew0001", "title": "Fresh", "when": "Sat 10/3 at 8pm", "status": "WENT"},
        {"id": "EVmaybe01", "title": "Unsure", "when": "Fri 10/2 at 8pm", "status": "MAYBE"},
        {"id": "EVdone001", "title": "Walked", "when": "Sat 9/5 at 8pm", "status": "HOSTING"},
        {"id": "EVold0001", "title": "Old", "when": "Sat 1/10 at 6pm", "status": "WENT"},
        {"id": "EVundated", "title": "Odd", "when": "somewhere at 9pm", "status": "WENT"},
    ]
    retained = {"EVdone001"}

    chosen, already = partiful.choose_events(events, retained, today=today)
    assert [e["id"] for e in chosen] == ["EVnew0001", "EVold0001", "EVundated"]
    assert already == ["EVdone001"]
    assert chosen[0]["date"] == "2026-10-03" and chosen[2]["date"] is None

    chosen, already = partiful.choose_events(events, retained, refresh=True, today=today)
    assert [e["id"] for e in chosen] == ["EVnew0001", "EVdone001", "EVold0001", "EVundated"]
    assert already == []

    # --since drops what the list dates before it; an undatable event is kept
    chosen, _ = partiful.choose_events(events, retained, since=date(2026, 9, 1), today=today)
    assert [e["id"] for e in chosen] == ["EVnew0001", "EVundated"]

    # an explicit --event-id is walked even when retained, whatever its status
    chosen, already = partiful.choose_events(
        events, retained, wanted={"EVdone001", "EVmaybe01"}, today=today
    )
    assert [e["id"] for e in chosen] == ["EVmaybe01", "EVdone001"] and already == []


def test_retained_event_ids_read_the_ledger_attendance(mocker):
    sql = mocker.patch("people_sync.somadata.sql", return_value=[{"id": "EVdone001"}, {"id": None}])
    assert partiful.retained_event_ids() == {"EVdone001"}
    query = sql.call_args.args[0]
    assert "people_sync_records" in query and "'$.events'" in query and "capture_key" in query


def test_list_partiful_events_walks_only_new_events_and_prints_their_counts(
    mocker, monkeypatch, capsys
):
    from people_sync import cli

    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.invalid")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "t")
    mocker.patch("people_sync.scrape.cdp.Browser.connect")
    events = [
        {"id": "EVnew0001", "title": "Fresh", "when": "Sat 10/3 at 8pm", "status": "WENT"},
        {"id": "EVhidden1", "title": "Ticketed", "when": "Fri 10/2 at 8pm", "status": "WENT"},
        {"id": "EVdone001", "title": "Walked", "when": "Sat 9/5 at 8pm", "status": "HOSTING"},
        {"id": "EVold0001", "title": "Old", "when": "Sat 1/10 at 6pm", "status": "WENT"},
    ]
    mocker.patch.object(partiful, "harvest_events", return_value=(events, "k"))
    mocker.patch.object(partiful, "retained_event_ids", return_value={"EVdone001"})
    header = {"title": "Fresh", "when": "Saturday, Oct 3, 2026"}
    ref = {"capture_key": "c", "scope": "event_guests", "ordinal": 0, "entry_ordinal": 0}

    def guests(browser, event_id):
        if event_id == "EVhidden1":
            raise partiful.ExtractError("guest-list-hidden")
        yield header, {"uid": "u1", "name": "Ada", "section": "Went"}, ref
        yield header, {"uid": None, "name": "Bo", "section": "Went"}, ref

    walk = mocker.patch.object(partiful, "harvest_event_guests", side_effect=guests)
    mocker.patch.object(partiful, "ingest_guests", return_value=["partiful:u1"])

    cli.main(["list", "partiful-events", "--since", "2026-09-01"])

    assert [c.args[1] for c in walk.call_args_list] == ["EVnew0001", "EVhidden1"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["listed"] == 4 and out["already_retained"] == 1 and out["failed"] == 1
    assert out["guests"] == 2 and out["records"] == 1
    assert out["events"] == [
        {
            "id": "EVnew0001",
            "title": "Fresh",
            "date": "2026-10-03",
            "status": "WENT",
            "guests": 2,
            "records": 1,
        },
        {
            "id": "EVhidden1",
            "title": "Ticketed",
            "date": "2026-10-02",
            "status": "WENT",
            "guests": 0,
            "records": 0,
            "error": "guest-list-hidden",
        },
    ]
