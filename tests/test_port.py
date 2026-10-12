"""`people-sync port google`: synthetic address-book rows and a fake gog, never a real
book or account. The canary values below must never reach stdout, the ledger or the
provenance edge."""

import json
import re

import pytest

from people_sync import port

APPLE_A = "AAAAAAAA-0000-0000-0000-000000000001:ABPerson"
APPLE_B = "BBBBBBBB-0000-0000-0000-000000000002:ABPerson"
APPLE_C = "CCCCCCCC-0000-0000-0000-000000000003:ABPerson"
CANARY_PHONE = "+1 (555) 010-7788"
CANARY_PHONE_2 = "5550107788"  # the same number without its country code
CANARY_OTHER = "+44 20 7946 0999"
CANARY_EMAIL = "canary.mail@example.org"
CANARY_BIRTHDAY = "1991-07-14"


def _card(apple=APPLE_A, **over):
    card = {
        "apple": apple,
        "first": "Nova",
        "middle": None,
        "last": "Quill",
        "birthday": None,
        "phones": json.dumps([{"value": CANARY_PHONE, "label": "_$!<Mobile>!$_"}]),
        "emails": json.dumps([]),
        "has_org": 0,
        "has_nick": 0,
        "has_note": 0,
        "addresses": 0,
        "photo": 0,
    }
    card.update(over)
    return card


def _candidate(person="p1", apple=APPLE_A, **over):
    row = {
        "id": person,
        "name": "Nova Quill",
        "first_name": "Nova",
        "middle_name": None,
        "last_name": "Quill",
        "apple_id": apple,
    }
    row.update(over)
    return row


# --- phone types --------------------------------------------------------------


@pytest.mark.parametrize(
    "label, expected",
    [
        ("_$!<Mobile>!$_", ("mobile", False)),
        ("iPhone", ("mobile", False)),
        ("_$!<Home>!$_", ("home", False)),
        ("_$!<Work>!$_", ("work", False)),
        ("_$!<Main>!$_", ("main", False)),
        ("_$!<HomeFAX>!$_", ("homeFax", False)),
        ("_$!<WorkFAX>!$_", ("workFax", False)),
        ("_$!<OtherFAX>!$_", ("otherFax", False)),
        ("_$!<Pager>!$_", ("pager", False)),
        ("_$!<Other>!$_", ("other", False)),
        (None, ("mobile", True)),
        ("", ("mobile", True)),
        ("Studio", ("mobile", True)),
        ("_$!<School>!$_", ("mobile", True)),
    ],
)
def test_phone_type(label, expected):
    assert port.phone_type(label) == expected


# --- names --------------------------------------------------------------------


def test_name_comes_from_people_and_a_context_surname_is_flagged():
    person = _candidate(name="Zed", first_name=None, last_name=None)
    parts, flags = port.name_parts(person, [_card(first="Zed", last="Pat's Friend")])
    assert parts == {"givenName": "Zed"}
    assert any("Pat's Friend" in f and "differs" in f for f in flags)


def test_people_name_parts_win_without_a_flag_when_apple_agrees():
    parts, flags = port.name_parts(_candidate(), [_card()])
    assert parts == {"givenName": "Nova", "familyName": "Quill"}
    assert flags == []


def test_apple_middle_name_absent_from_people_is_flagged():
    _, flags = port.name_parts(_candidate(), [_card(middle="Q.")])
    assert len(flags) == 1 and "differs" in flags[0]


def test_multi_word_name_without_parts_is_split_and_flagged():
    person = _candidate(name="Ira Vance", first_name=None, last_name=None)
    parts, flags = port.name_parts(person, [_card(first="Ira", last="Vance")])
    assert parts == {"givenName": "Ira", "familyName": "Vance"}
    assert any("split" in f for f in flags)


def test_parts_that_do_not_spell_the_people_name_are_flagged():
    person = _candidate(name="Novella Quill")
    parts, flags = port.name_parts(person, [_card()])
    assert parts == {"givenName": "Nova", "familyName": "Quill"}
    assert any("do not spell" in f for f in flags)


# --- plan ---------------------------------------------------------------------


def test_two_cards_merge_distinct_values():
    cards = [
        _card(
            phones=json.dumps(
                [
                    {"value": CANARY_PHONE_2, "label": None},
                    {"value": CANARY_OTHER, "label": "_$!<Work>!$_"},
                ]
            ),
            emails=json.dumps([{"value": CANARY_EMAIL, "label": "_$!<Home>!$_"}]),
        ),
        _card(
            apple=APPLE_B,
            phones=json.dumps([{"value": CANARY_PHONE, "label": "_$!<Mobile>!$_"}]),
            emails=json.dumps([{"value": CANARY_EMAIL.upper(), "label": None}]),
            birthday=CANARY_BIRTHDAY,
        ),
    ]
    plan = port.plan_person([_candidate(), _candidate(apple=APPLE_B)], cards)
    phones = plan.body["phoneNumbers"]
    # the +1 form wins over the bare 10 digits, and the explicit Mobile over the assumption
    assert {"value": CANARY_PHONE, "type": "mobile"} in phones
    assert {"value": CANARY_OTHER, "type": "work"} in phones
    assert len(phones) == 2 and plan.assumed == 0
    assert plan.body["emailAddresses"] == [{"value": CANARY_EMAIL, "type": "home"}]
    assert plan.body["birthdays"] == [{"date": {"year": 1991, "month": 7, "day": 14}}]
    assert plan.skip is None


def test_yearless_birthday_has_no_year():
    plan = port.plan_person([_candidate()], [_card(birthday="--07-14")])
    assert plan.body["birthdays"] == [{"date": {"month": 7, "day": 14}}]


def test_unknown_label_is_assumed_mobile():
    plan = port.plan_person(
        [_candidate()], [_card(phones=json.dumps([{"value": "1", "label": "x"}]))]
    )
    assert plan.body["phoneNumbers"] == [{"value": "1", "type": "mobile"}]
    assert plan.assumed == 1


def test_body_never_carries_context_fields():
    card = _card(has_org=1, has_nick=1, has_note=1, addresses=2, photo=1)
    plan = port.plan_person([_candidate()], [card])
    assert set(plan.body) <= {"names", "phoneNumbers", "emailAddresses", "birthdays"}
    assert any("org" in s for s in plan.skipped)
    assert any("note" in s for s in plan.skipped)
    assert any("nickname" in s for s in plan.skipped)
    assert any("2 postal addresses" in s for s in plan.skipped)
    assert any("photo" in s for s in plan.skipped)


def test_a_missing_card_skips_the_person():
    plan = port.plan_person([_candidate(), _candidate(apple=APPLE_B)], [_card()])
    assert plan.skip and "1 of 2" in plan.skip


def test_a_card_with_nothing_to_port_is_skipped():
    plan = port.plan_person([_candidate()], [_card(phones=json.dumps([]))])
    assert plan.skip and "nothing to port" in plan.skip


# --- the command --------------------------------------------------------------


class Estate:
    """somadata.sql/insert double: candidates, ledger rows and inserted rows."""

    def __init__(self, candidates, ledger=()):
        self.candidates = list(candidates)
        self.ledger = list(ledger)
        self.inserted: dict[str, list[dict]] = {}
        self.statements: list[str] = []

    def sql(self, query):
        self.statements.append(query)
        if re.search(r"\bFROM people p\b", query):
            return list(self.candidates)
        if re.search(r"\bFROM people_sync_records\b", query):
            return [dict(r) for r in self.ledger]
        return []

    def insert(self, table, rows):
        self.inserted.setdefault(table, []).extend(rows)


class Gog:
    """The People API as gog shows it: batch create, then raw read-back."""

    def __init__(self, readback_edit=None, fail_create=False):
        self.created: list[dict] = []
        self.calls: list[list[str]] = []
        self.readback_edit = readback_edit
        self.fail_create = fail_create

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        if args[:3] == ["contacts", "batch", "create"]:
            assert args[3:5] == ["--from-file", "-"]
            if self.fail_create:
                raise port.GogError("contacts batch create", "people/c999")
            body = json.loads(stdin)
            assert len(body) == 1
            self.created.append(body[0])
            person = {"resourceName": f"people/c{len(self.created)}", **body[0]}
            return json.dumps(
                {
                    "operation": "contacts.batch.create",
                    "requested": 1,
                    "completed": 1,
                    "batches": [
                        {
                            "input_indexes": [0],
                            "state": "completed",
                            "response": {"createdPeople": [{"person": person}]},
                        }
                    ],
                }
            )
        if args[:2] == ["contacts", "raw"]:
            index = int(args[2].removeprefix("people/c")) - 1
            person = json.loads(json.dumps(self.created[index]))
            person["resourceName"] = args[2]
            for name in person.get("names", []):
                name["displayName"] = " ".join(
                    name[k] for k in ("givenName", "middleName", "familyName") if name.get(k)
                )
            for phone in person.get("phoneNumbers", []):
                phone["formattedType"] = phone["type"].capitalize()
            if self.readback_edit:
                self.readback_edit(person)
            return json.dumps(person)
        raise AssertionError(f"unexpected gog call {args}")


@pytest.fixture
def world(mocker):
    def make(candidates, cards, ledger=(), gog=None, books=("local", "remote")):
        estate = Estate(candidates, ledger)
        gog = gog or Gog()
        mocker.patch("people_sync.somadata.sql", side_effect=estate.sql)
        mocker.patch("people_sync.somadata.insert", side_effect=estate.insert)
        mocker.patch("people_sync.somadata.now_iso", return_value="2026-01-01T00:00:00.000Z")
        mocker.patch("people_sync.comm.read_address_books", return_value=(list(cards), list(books)))
        mocker.patch("people_sync.port._gog", side_effect=gog)
        link = mocker.patch("people_sync.port.reconcile.link")
        return estate, gog, link

    return make


CANARIES = (CANARY_PHONE, CANARY_PHONE_2, "0107788", CANARY_EMAIL, CANARY_BIRTHDAY, "1991")


def _assert_no_canary(text: str):
    for canary in CANARIES:
        assert canary.casefold() not in text.casefold(), canary


def test_dry_run_previews_without_values_or_writes(world, capsys):
    card = _card(
        emails=json.dumps([{"value": CANARY_EMAIL, "label": None}]), birthday=CANARY_BIRTHDAY
    )
    estate, gog, link = world([_candidate()], [card])
    port.main(["google"])
    out = capsys.readouterr().out
    _assert_no_canary(out)
    assert "Nova Quill" in out and "p1" in out
    assert "phones: 1 (mobile)" in out
    assert "emails: 1" in out
    assert "birthday: yes" in out
    assert "photo: no" in out
    assert estate.inserted == {} and gog.calls == [] and not link.called


def test_person_filter(world, capsys):
    candidates = [
        _candidate(),
        _candidate("p2", APPLE_C, name="Ira Vance", first_name="Ira", last_name="Vance"),
    ]
    world(candidates, [_card(), _card(APPLE_C, first="Ira", last="Vance")])
    port.main(["google", "--person", "p2"])
    out = capsys.readouterr().out
    assert "PORT Ira Vance" in out and "PORT Nova Quill" not in out


def test_apply_creates_verifies_records_and_links(world, capsys):
    card = _card(emails=json.dumps([{"value": CANARY_EMAIL, "label": None}]))
    estate, gog, link = world([_candidate()], [card])
    port.main(["google", "--apply"])
    out = capsys.readouterr().out
    _assert_no_canary(out)
    assert gog.created[0]["phoneNumbers"] == [{"value": CANARY_PHONE, "type": "mobile"}]
    (record,) = estate.inserted["people_sync_records"]
    assert record["id"] == "google_contacts:people/c1"
    assert record["status"] == "pending" and record["source_id"] == "people/c1"
    assert json.loads(record["raw"]) == {"ported_from": ["apple_contacts:" + APPLE_A]}
    (edge,) = estate.inserted["provenance"]
    assert edge["from_kind"] == "people_sync_records"
    assert edge["from_ref"] == "apple_contacts:" + APPLE_A
    assert edge["to_ref"] == "google_contacts:people/c1"
    assert edge["rel"] == "imported_from"
    _assert_no_canary(json.dumps(estate.inserted))
    link.assert_called_once()
    assert link.call_args.args[:2] == ("p1", "google_contacts:people/c1")
    assert "verified" in out


def test_apply_does_not_link_when_the_read_back_differs(world, capsys):
    def drop_type(person):
        person["phoneNumbers"][0]["type"] = "other"

    estate, gog, link = world([_candidate()], [_card()], gog=Gog(readback_edit=drop_type))
    port.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert "VERIFY FAILED" in out and "phone types" in out
    _assert_no_canary(out)
    assert not link.called
    assert estate.inserted["people_sync_records"][0]["status"] == "pending"


def test_rerun_resumes_an_unlinked_port_without_creating_again(world, capsys):
    gog = Gog()
    gog.created.append(
        {
            "names": [{"givenName": "Nova", "familyName": "Quill"}],
            "phoneNumbers": [{"value": CANARY_PHONE, "type": "mobile"}],
        }
    )
    ledger = [
        {
            "id": "google_contacts:people/c1",
            "source_id": "people/c1",
            "status": "pending",
            "raw": json.dumps({"ported_from": ["apple_contacts:" + APPLE_A]}),
        }
    ]
    estate, gog, link = world([_candidate()], [_card()], ledger=ledger, gog=gog)
    port.main(["google", "--apply"])
    assert not any(c[:3] == ["contacts", "batch", "create"] for c in gog.calls)
    assert "people_sync_records" not in estate.inserted
    link.assert_called_once()


def test_a_failed_create_names_the_resource_and_stops(world, capsys):
    estate, gog, link = world([_candidate()], [_card()], gog=Gog(fail_create=True))
    with pytest.raises(SystemExit) as exc:
        port.main(["google", "--apply"])
    assert "people/c999" in str(exc.value)
    assert estate.inserted == {} and not link.called


def test_nothing_to_do_says_so(world, capsys):
    world([], [])
    port.main(["google"])
    assert "0 to port" in capsys.readouterr().out


def test_card_sql_reads_a_synthetic_book(tmp_path, mocker, monkeypatch):
    """The real query against a minimal AddressBook-shaped file, through comm's reader."""
    import sqlite3

    db = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZUNIQUEID TEXT, ZFIRSTNAME TEXT,
            ZMIDDLENAME TEXT, ZLASTNAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT,
            ZJOBTITLE TEXT, ZBIRTHDAY REAL, ZIMAGEDATA BLOB, ZTHUMBNAILIMAGEDATA BLOB);
        CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZFULLNUMBER TEXT, ZLABEL TEXT, ZORDERINGINDEX INTEGER);
        CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZADDRESS TEXT, ZLABEL TEXT, ZORDERINGINDEX INTEGER);
        CREATE TABLE ZABCDPOSTALADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER);
        CREATE TABLE ZABCDNOTE (Z_PK INTEGER PRIMARY KEY, ZCONTACT INTEGER, ZTEXT TEXT);
        """
    )
    # 1991-07-14 12:00 UTC and Apple's yearless sentinel year 1604, as seconds since 2001
    conn.execute(
        "INSERT INTO ZABCDRECORD VALUES (1, ?, 'Nova', NULL, 'Quill', NULL, 'Acme', NULL, "
        "-298814400, NULL, x'01ff')",
        (APPLE_A,),
    )
    conn.execute(
        "INSERT INTO ZABCDRECORD VALUES (2, ?, 'Ira', NULL, NULL, NULL, NULL, NULL, "
        "-12511281600, NULL, NULL)",
        (APPLE_B,),
    )
    conn.execute(
        "INSERT INTO ZABCDRECORD VALUES (3, ?, 'Other', NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL)",
        (APPLE_C,),
    )
    conn.execute(
        "INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, ?, '_$!<Mobile>!$_', 1)", (CANARY_PHONE,)
    )
    conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (2, 1, '', NULL, 2)")
    conn.execute("INSERT INTO ZABCDEMAILADDRESS VALUES (1, 2, ?, NULL, 0)", (CANARY_EMAIL,))
    conn.execute("INSERT INTO ZABCDPOSTALADDRESS VALUES (1, 1)")
    conn.execute("INSERT INTO ZABCDNOTE VALUES (1, 1, 'met at the canary club')")
    conn.commit()
    conn.close()
    mocker.patch("people_sync.sources._db_paths", return_value=[str(db)])
    monkeypatch.delenv("PEOPLE_SYNC_ADDRESSBOOK_HOST", raising=False)
    # noon UTC is the same calendar day in every zone the reader's localtime can be in
    cards, books = port.read_cards([APPLE_A, APPLE_B])
    assert books == ["local"]
    by_id = {c["apple"]: c for c in cards}
    assert set(by_id) == {APPLE_A, APPLE_B}
    a, b = by_id[APPLE_A], by_id[APPLE_B]
    assert json.loads(a["phones"]) == [{"value": CANARY_PHONE, "label": "_$!<Mobile>!$_"}]
    assert a["birthday"] == "1991-07-14" and b["birthday"] == "--07-14"
    assert (a["has_org"], a["has_note"], a["addresses"], a["photo"]) == (1, 1, 1, 1)
    assert (b["has_org"], b["has_note"], b["addresses"], b["photo"]) == (0, 0, 0, 0)
    assert json.loads(b["emails"]) == [{"value": CANARY_EMAIL, "label": None}]


def test_invisible_marks_and_spaces_are_cleaned_from_written_values():
    phones = json.dumps([{"value": "‪" + CANARY_PHONE + "‬ ", "label": None}])
    emails = json.dumps([{"value": " " + CANARY_EMAIL + "‎", "label": None}])
    plan = port.plan_person([_candidate()], [_card(phones=phones, emails=emails)])
    assert plan.body["phoneNumbers"][0]["value"] == CANARY_PHONE
    assert plan.body["emailAddresses"] == [{"value": CANARY_EMAIL}]


def test_a_refused_provenance_edge_leaves_the_port_unlinked_and_a_rerun_repairs(world, capsys):
    estate, gog, link = world([_candidate()], [_card()])
    insert = estate.insert

    def refuse_provenance(table, rows):
        if table == "provenance":
            raise RuntimeError("rejected")
        insert(table, rows)

    from people_sync import somadata

    somadata.insert.side_effect = refuse_provenance
    port.main(["google", "--apply"])
    assert "NOT LINKED" in capsys.readouterr().out and not link.called
    estate.ledger = estate.inserted["people_sync_records"]
    somadata.insert.side_effect = insert
    port.main(["google", "--apply"])
    assert len(gog.created) == 1
    assert estate.inserted["provenance"][0]["to_ref"] == "google_contacts:people/c1"
    link.assert_called_once()


def test_birthdays_agree_when_only_the_year_is_unknown():
    cards = [_card(birthday="--07-14"), _card(APPLE_B, birthday=CANARY_BIRTHDAY)]
    plan = port.plan_person([_candidate(), _candidate(apple=APPLE_B)], cards)
    assert plan.body["birthdays"] == [{"date": {"year": 1991, "month": 7, "day": 14}}]
    cards[0]["birthday"] = "--07-15"
    plan = port.plan_person([_candidate(), _candidate(apple=APPLE_B)], cards)
    assert "birthdays" not in plan.body and any("disagree" in f for f in plan.flags)
