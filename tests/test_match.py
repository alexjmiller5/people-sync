import json

from contact_sync.match import letters, normalize, run_match


def _sql_router(people, pending):
    """Route lifedata.sql() calls by table, same as a real backend would."""

    def _fn(query):
        if "FROM people" in query:
            return people
        if "FROM contact_records" in query:
            return pending
        return []  # UPDATE statements have no rows to return

    return _fn


def test_normalize():
    assert normalize("  José  O'Brien-2 ") == "jose obrien"
    assert letters("José O'Brien") == "joseobrien"


def test_single_word_never_automatches(mocker):
    # person "Madonna" + record name "Madonna" -> suggested, not matched
    people = [
        {"id": "p1", "name": "Madonna", "first_name": None, "last_name": None, "nickname": None}
    ]
    pending = [
        {
            "id": "linkedin:m1",
            "source": "linkedin",
            "source_id": "m1",
            "handle": "m1",
            "name": "Madonna",
            "raw": json.dumps({"URL": "https://linkedin.com/in/m1"}),
        }
    ]
    sql = mocker.patch("contact_sync.lifedata.sql", side_effect=_sql_router(people, pending))
    ins = mocker.patch("contact_sync.lifedata.insert")

    out = run_match()

    ins.assert_not_called()
    updates = [c.args[0] for c in sql.call_args_list if c.args[0].startswith("UPDATE")]
    assert len(updates) == 1
    assert "suggested_person_id = 'p1'" in updates[0]
    assert "status" not in updates[0]
    assert "linkedin:m1" in updates[0]
    assert out == {"auto": 0, "suggested": 1, "left_pending": 0}


def test_ambiguous_two_people_stays_pending(mocker):
    # two people normalize to "test person" -> no auto, no suggestion
    people = [
        {
            "id": "p1",
            "name": "Test Person",
            "first_name": None,
            "last_name": None,
            "nickname": None,
        },
        {
            "id": "p2",
            "name": None,
            "first_name": "Test",
            "last_name": "Person",
            "nickname": None,
        },
    ]
    pending = [
        {
            "id": "facebook:f1",
            "source": "facebook",
            "source_id": "f1",
            "handle": None,
            "name": "Test Person",
            "raw": json.dumps({"name": "Test Person"}),
        }
    ]
    sql = mocker.patch("contact_sync.lifedata.sql", side_effect=_sql_router(people, pending))
    ins = mocker.patch("contact_sync.lifedata.insert")

    out = run_match()

    ins.assert_not_called()
    updates = [c.args[0] for c in sql.call_args_list if c.args[0].startswith("UPDATE")]
    assert updates == []
    assert out == {"auto": 0, "suggested": 0, "left_pending": 1}


def test_exact_unique_automatch_writes_account(mocker):
    # one person "Test Person", one linkedin record "Test Person"
    # -> status matched + person_accounts insert with url from raw
    people = [
        {
            "id": "p1",
            "name": "Test Person",
            "first_name": "Test",
            "last_name": "Person",
            "nickname": None,
        }
    ]
    pending = [
        {
            "id": "linkedin:t1",
            "source": "linkedin",
            "source_id": "t1",
            "handle": "t1",
            "name": "Test Person",
            "raw": json.dumps({"URL": "https://linkedin.com/in/t1"}),
        }
    ]
    sql = mocker.patch("contact_sync.lifedata.sql", side_effect=_sql_router(people, pending))
    ins = mocker.patch("contact_sync.lifedata.insert")

    out = run_match()

    updates = [c.args[0] for c in sql.call_args_list if c.args[0].startswith("UPDATE")]
    assert len(updates) == 1
    assert "status = 'matched'" in updates[0]
    assert "person_id = 'p1'" in updates[0]
    assert "linkedin:t1" in updates[0]

    assert ins.call_args.args[0] == "person_accounts"
    rows = ins.call_args.args[1]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "linkedin:p1:t1"
    assert row["person_id"] == "p1"
    assert row["platform"] == "linkedin"
    assert row["handle"] == "t1"
    assert row["url"] == "https://linkedin.com/in/t1"
    assert row["active"] == 1

    assert out == {"auto": 1, "suggested": 0, "left_pending": 0}


def test_record_side_ambiguity_stays_pending(mocker):
    # one person "Test Person", two pending records FROM THE SAME SOURCE both keying
    # to them -> which account is really theirs is ambiguous, so neither is touched:
    # no auto, no suggestion, both stay pending
    people = [
        {
            "id": "p1",
            "name": "Test Person",
            "first_name": "Test",
            "last_name": "Person",
            "nickname": None,
        }
    ]
    pending = [
        {
            "id": "linkedin:t1",
            "source": "linkedin",
            "source_id": "t1",
            "handle": "t1",
            "name": "Test Person",
            "raw": json.dumps({"URL": "https://linkedin.com/in/t1"}),
        },
        {
            "id": "linkedin:t2",
            "source": "linkedin",
            "source_id": "t2",
            "handle": "t2",
            "name": "Test Person",
            "raw": json.dumps({"URL": "https://linkedin.com/in/t2"}),
        },
    ]
    sql = mocker.patch("contact_sync.lifedata.sql", side_effect=_sql_router(people, pending))
    ins = mocker.patch("contact_sync.lifedata.insert")

    out = run_match()

    ins.assert_not_called()
    updates = [c.args[0] for c in sql.call_args_list if c.args[0].startswith("UPDATE")]
    assert updates == []
    assert out == {"auto": 0, "suggested": 0, "left_pending": 2}


def test_cross_source_records_both_automatch(mocker):
    # one person "Test Person" present in BOTH google and apple contacts.
    # Cross-source co-occurrence is confirmation, not ambiguity: both auto-link.
    people = [
        {
            "id": "p1",
            "name": "Test Person",
            "first_name": "Test",
            "last_name": "Person",
            "nickname": None,
        }
    ]
    pending = [
        {
            "id": "google_contacts:people/c1",
            "source": "google_contacts",
            "source_id": "people/c1",
            "handle": None,
            "name": "Test Person",
            "raw": json.dumps({"names": [{"displayName": "Test Person"}]}),
        },
        {
            "id": "apple_contacts:u1:ABPerson",
            "source": "apple_contacts",
            "source_id": "u1:ABPerson",
            "handle": None,
            "name": "Test Person",
            "raw": json.dumps({"name": "Test Person"}),
        },
    ]
    sql = mocker.patch("contact_sync.lifedata.sql", side_effect=_sql_router(people, pending))
    ins = mocker.patch("contact_sync.lifedata.insert")

    out = run_match()

    updates = [c.args[0] for c in sql.call_args_list if c.args[0].startswith("UPDATE")]
    assert len(updates) == 2
    assert all("status = 'matched'" in u and "person_id = 'p1'" in u for u in updates)

    rows = ins.call_args.args[1]
    assert sorted(r["platform"] for r in rows) == ["apple_contacts", "google_contacts"]
    assert {r["person_id"] for r in rows} == {"p1"}
    assert sorted(r["id"] for r in rows) == [
        "apple_contacts:p1:u1:ABPerson",
        "google_contacts:p1:people/c1",
    ]

    assert out == {"auto": 2, "suggested": 0, "left_pending": 0}
