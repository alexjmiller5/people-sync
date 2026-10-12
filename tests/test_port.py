"""`people-sync port google`: synthetic address-book rows and a fake gog, never a real
book or account. The canary values below must never reach stdout, the ledger or the
provenance edge."""

import base64
import json
import re
import subprocess

import httpx
import pytest

from people_sync import comm, port

APPLE_A = "AAAAAAAA-0000-0000-0000-000000000001:ABPerson"
APPLE_B = "BBBBBBBB-0000-0000-0000-000000000002:ABPerson"
APPLE_C = "CCCCCCCC-0000-0000-0000-000000000003:ABPerson"
CANARY_PHONE = "+1 (555) 010-7788"
CANARY_PHONE_2 = "5550107788"  # the same number without its country code
CANARY_OTHER = "+44 20 7946 0999"
CANARY_EMAIL = "canary.mail@example.org"
CANARY_BIRTHDAY = "1991-07-14"
IMAGE = b"\xff\xd8\xff synthetic full image"
THUMB = b"\xff\xd8\xff synthetic thumbnail, larger than the full image"
EXT_REF = "DDDDDDDD-0000-0000-0000-000000000004"
TOKEN = "ya29.canary-token"
API = "https://people.googleapis.com/v1/"
DEFAULT_PHOTO = {"default": True, "metadata": {"source": {"type": "CONTACT"}}}
OWN_PHOTO = {"metadata": {"source": {"type": "CONTACT"}, "primary": True}}


def _inline(data: bytes) -> str:
    """A Core Data blob as `hex()` returns it: marker 0x01, then the bytes."""
    return ("01" + data.hex()).upper()


def _external(name: str) -> str:
    """Marker 0x02, then the external file's name, NUL-terminated."""
    return (b"\x02" + name.encode() + b"\x00").hex().upper()


def _card(apple=APPLE_A, **over):
    card = {
        "apple": apple,
        "first": "Nova",
        "middle": None,
        "last": "Quill",
        "birthday": None,
        "phones": json.dumps([{"value": CANARY_PHONE, "label": "_$!<Mobile>!$_"}]),
        "emails": json.dumps([]),
        "org": None,
        "title": None,
        "nick": None,
        "has_note": 0,
        "addresses": 0,
        "image": None,
        "thumbnail": None,
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


def test_two_cards_with_the_same_name_flag_it_once():
    _, flags = port.name_parts(_candidate(), [_card(last=None), _card(APPLE_B, last=None)])
    assert len(flags) == 1


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
    card = _card(
        org="Sam's friend",
        title="Chef",
        nick="Q",
        has_note=1,
        addresses=2,
        image=_inline(IMAGE),
    )
    second = _card(APPLE_B, org="Sam's friend")
    plan = port.plan_person([_candidate(), _candidate(apple=APPLE_B)], [card, second])
    assert set(plan.body) <= {"names", "phoneNumbers", "emailAddresses", "birthdays"}
    # the context is shown so the reviewer can check People holds it, once per value
    assert [s for s in plan.skipped if s.startswith("org")] == [
        "org/title \"Sam's friend\", 'Chef' (context stays in People)"
    ]
    assert any("note" in s for s in plan.skipped)
    assert "nickname 'Q' (People holds nicknames)" in plan.skipped
    assert any("2 postal addresses" in s for s in plan.skipped)
    # the photo is not a body field: it is uploaded separately after the create
    assert not any("photo" in s for s in plan.skipped) and plan.image == IMAGE


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
        self.photos: dict[str, list[dict]] = {}
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
            person["photos"] = self.photos.get(args[2], [DEFAULT_PHOTO])
            if self.readback_edit:
                self.readback_edit(person)
            return json.dumps(person)
        raise AssertionError(f"unexpected gog call {args}")


@pytest.fixture
def world(mocker, monkeypatch):
    monkeypatch.delenv(port.TOKEN_COMMAND_ENV, raising=False)

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


CANARIES = (
    CANARY_PHONE,
    CANARY_PHONE_2,
    "0107788",
    CANARY_EMAIL,
    CANARY_BIRTHDAY,
    "1991",
    TOKEN,
    base64.b64encode(IMAGE).decode(),
    IMAGE.hex(),
    "synthetic full image",
)


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
    assert (a["org"], a["has_note"], a["addresses"]) == ("Acme", 1, 1)
    # image blobs arrive as hex, marker byte included; decoding is card_photo's job
    assert (a["image"], a["thumbnail"]) == (None, "01FF")
    assert (b["org"], b["has_note"], b["addresses"], b["image"], b["thumbnail"]) == (
        None,
        0,
        0,
        None,
        None,
    )
    assert "canary club" not in json.dumps(cards)
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


# --- photos -------------------------------------------------------------------


def test_card_photo_prefers_the_full_image_over_a_larger_thumbnail():
    card = _card(image=_inline(IMAGE), thumbnail=_inline(THUMB))
    assert port.card_photo(card) == (IMAGE, True)


def test_card_photo_falls_back_to_the_thumbnail():
    assert port.card_photo(_card(thumbnail=_inline(THUMB))) == (THUMB, False)


def test_card_photo_reads_an_external_file(mocker):
    read = mocker.patch("people_sync.comm.read_external_data", return_value=IMAGE)
    assert port.card_photo(_card(image=_external(EXT_REF))) == (IMAGE, True)
    read.assert_called_once_with(EXT_REF)


def test_an_unreadable_external_image_falls_back_to_the_thumbnail(mocker):
    mocker.patch("people_sync.comm.read_external_data", return_value=None)
    card = _card(image=_external(EXT_REF), thumbnail=_inline(THUMB))
    assert port.card_photo(card) == (THUMB, False)


def test_no_photo_and_an_unknown_storage_marker_read_as_none():
    assert port.card_photo(_card()) is None
    assert port.card_photo(_card(image="FFD8FF00")) is None


def test_two_cards_take_the_full_image_then_the_larger():
    rows = [_candidate(), _candidate(apple=APPLE_B)]
    bigger = IMAGE + b" and more"
    cards = [_card(image=_inline(IMAGE)), _card(APPLE_B, image=_inline(bigger))]
    assert port.plan_person(rows, cards).image == bigger
    cards = [_card(thumbnail=_inline(THUMB)), _card(APPLE_B, image=_inline(IMAGE))]
    assert port.plan_person(rows, cards).image == IMAGE


def test_a_photo_that_cannot_be_read_is_skipped_with_the_reason(world, capsys, mocker):
    mocker.patch("people_sync.comm.read_external_data", return_value=None)
    world([_candidate()], [_card(image=_external(EXT_REF))])
    port.main(["google"])
    assert "photo: skipped (the Apple image could not be read" in capsys.readouterr().out


def test_dry_run_says_the_photo_will_upload_when_a_token_command_is_set(world, capsys, monkeypatch):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, "exit 7")  # a dry run never runs it
    world([_candidate()], [_card(image=_inline(IMAGE))])
    port.main(["google"])
    out = capsys.readouterr().out
    assert "photo: yes (will upload)" in out
    _assert_no_canary(out)


def test_without_a_token_command_the_photo_is_skipped_and_says_why(world, capsys):
    world([_candidate()], [_card(image=_inline(IMAGE))])
    port.main(["google"])
    assert f"photo: skipped ({port.TOKEN_COMMAND_ENV} is not set" in capsys.readouterr().out


class PhotoApi:
    """httpx.patch double for updateContactPhoto; a 200 sets the contact's own photo."""

    def __init__(self, gog, status=200, sets_photo=True):
        self.gog, self.status, self.sets_photo = gog, status, sets_photo
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        resource = url.removeprefix(API).removesuffix(":updateContactPhoto")
        if self.status == 200 and self.sets_photo:
            self.gog.photos[resource] = [OWN_PHOTO, DEFAULT_PHOTO | {"metadata": {}}]
        body = {"person": {}} if self.status == 200 else {"error": {"status": "INVALID"}}
        return httpx.Response(self.status, json=body, request=httpx.Request("PATCH", url))


@pytest.fixture
def photo_api(mocker):
    def make(gog, **kw):
        api = PhotoApi(gog, **kw)
        mocker.patch("people_sync.port.httpx.patch", side_effect=api)
        return api

    return make


def test_apply_uploads_the_photo_after_linking_and_reads_it_back(
    world, photo_api, capsys, monkeypatch
):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, f"printf '%s\\n' {TOKEN}")
    estate, gog, link = world([_candidate()], [_card(image=_inline(IMAGE))])
    api = photo_api(gog)
    order = []
    link.side_effect = lambda *a, **k: order.append("link")
    api_call = api.__call__

    def patch(*a, **k):
        order.append("photo")
        return api_call(*a, **k)

    port.httpx.patch.side_effect = patch
    port.main(["google", "--apply"])
    out = capsys.readouterr().out
    (call,) = api.calls
    assert call["url"] == API + "people/c1:updateContactPhoto"
    assert call["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert base64.b64decode(call["json"]["photoBytes"]) == IMAGE
    assert call["json"]["personFields"] == "photos"
    assert order == ["link", "photo"]
    assert "PHOTO people/c1: uploaded and read back" in out
    _assert_no_canary(out)
    _assert_no_canary(json.dumps(estate.inserted))


def test_a_refused_upload_keeps_the_link_and_says_how_to_retry(
    world, photo_api, capsys, monkeypatch
):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, f"printf %s {TOKEN}")
    estate, gog, link = world([_candidate()], [_card(image=_inline(IMAGE))])
    photo_api(gog, status=400)
    port.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert "PHOTO FAILED people/c1: updateContactPhoto returned HTTP 400 INVALID" in out
    assert "--photos-only --person p1 --apply" in out
    link.assert_called_once()
    _assert_no_canary(out)


def test_an_upload_the_read_back_does_not_show_is_reported(world, photo_api, capsys, monkeypatch):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, f"printf %s {TOKEN}")
    estate, gog, link = world([_candidate()], [_card(image=_inline(IMAGE))])
    photo_api(gog, sets_photo=False)
    port.main(["google", "--apply"])
    assert "PHOTO FAILED people/c1: the read-back shows only the default photo" in (
        capsys.readouterr().out
    )


def test_a_failing_token_command_never_calls_the_api(world, photo_api, capsys, monkeypatch):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, f"echo {TOKEN}; exit 3")
    estate, gog, link = world([_candidate()], [_card(image=_inline(IMAGE))])
    api = photo_api(gog)
    port.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert f"PHOTO FAILED people/c1: {port.TOKEN_COMMAND_ENV} failed" in out
    assert api.calls == [] and TOKEN not in out
    link.assert_called_once()


def test_apply_without_a_token_command_ports_without_the_photo(world, photo_api, capsys):
    estate, gog, link = world([_candidate()], [_card(image=_inline(IMAGE))])
    api = photo_api(gog)
    port.main(["google", "--apply"])
    assert api.calls == [] and "PHOTO" not in capsys.readouterr().out
    link.assert_called_once()


def test_google_token_strips_output_and_never_names_it(monkeypatch):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, f"printf '  {TOKEN} \\n'")
    assert port.google_token() == TOKEN
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, "printf ''")
    with pytest.raises(port.PhotoError, match="printed nothing"):
        port.google_token()
    monkeypatch.delenv(port.TOKEN_COMMAND_ENV)
    with pytest.raises(port.PhotoError, match="is not set"):
        port.google_token()


def _ported(person, apple, google, **names):
    return _candidate(person, apple, **names) | {"google_id": google}


def test_photos_only_uploads_where_google_shows_only_the_default_photo(
    world, photo_api, capsys, monkeypatch
):
    monkeypatch.setenv(port.TOKEN_COMMAND_ENV, f"printf %s {TOKEN}")
    gog = Gog()
    gog.created += [{"names": [{"givenName": "x"}]}] * 3
    gog.photos["people/c2"] = [OWN_PHOTO]
    ira = {"name": "Ira Vance", "first_name": "Ira", "last_name": "Vance"}
    candidates = [
        _ported("p1", APPLE_A, "people/c1"),
        _ported("p2", APPLE_B, "people/c2", **ira),
        _ported("p3", APPLE_C, "people/c3", name="Lux", first_name="Lux", last_name=None),
    ]
    cards = [
        _card(image=_inline(IMAGE)),
        _card(APPLE_B, image=_inline(IMAGE)),
        _card(APPLE_C),
    ]
    estate, gog, link = world(candidates, cards, gog=gog)
    api = photo_api(gog)
    port.main(["google", "--photos-only"])
    out = capsys.readouterr().out
    assert "PHOTO Nova Quill (p1) -> people/c1: yes (will upload)" in out
    assert "SKIP Ira Vance (p2) -> people/c2: Google already has its own photo" in out
    assert "Lux" not in out
    assert "1 photo to upload, 1 already in Google, 1 without an Apple photo" in out
    assert api.calls == [] and estate.inserted == {}
    port.main(["google", "--photos-only", "--apply"])
    out = capsys.readouterr().out
    assert [c["url"] for c in api.calls] == [API + "people/c1:updateContactPhoto"]
    assert "PHOTO people/c1: uploaded and read back" in out
    assert estate.inserted == {} and not link.called
    _assert_no_canary(out)
    port.main(["google", "--photos-only"])
    assert "0 photos to upload, 2 already in Google" in capsys.readouterr().out


def test_photos_only_apply_needs_the_token_command(world, photo_api, capsys):
    gog = Gog()
    gog.created.append({"names": [{"givenName": "x"}]})
    world([_ported("p1", APPLE_A, "people/c1")], [_card(image=_inline(IMAGE))], gog=gog)
    port.main(["google", "--photos-only"])
    assert f"skipped ({port.TOKEN_COMMAND_ENV} is not set" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        port.main(["google", "--photos-only", "--apply"])
    assert port.TOKEN_COMMAND_ENV in str(exc.value)


def test_photos_only_respects_the_person_filter(world, photo_api, capsys):
    gog = Gog()
    gog.created += [{"names": [{"givenName": "x"}]}] * 2
    ira = {"name": "Ira Vance", "first_name": "Ira", "last_name": "Vance"}
    candidates = [_ported("p1", APPLE_A, "people/c1"), _ported("p2", APPLE_B, "people/c2", **ira)]
    cards = [_card(image=_inline(IMAGE)), _card(APPLE_B, image=_inline(IMAGE))]
    world(candidates, cards, gog=gog)
    port.main(["google", "--photos-only", "--person", "p2"])
    out = capsys.readouterr().out
    assert "Ira Vance" in out and "Nova Quill" not in out


# --- external image files -----------------------------------------------------


def test_read_external_data_finds_the_file_beside_a_local_book(tmp_path, mocker, monkeypatch):
    source = tmp_path / "Sources" / "S1"
    external = source / ".AddressBook-v22_SUPPORT" / "_EXTERNAL_DATA"
    external.mkdir(parents=True)
    (external / EXT_REF).write_bytes(IMAGE)
    mocker.patch(
        "people_sync.sources._db_paths", return_value=[str(source / "AddressBook-v22.abcddb")]
    )
    monkeypatch.delenv(comm.ADDRESSBOOK_HOST_ENV, raising=False)
    assert comm.read_external_data(EXT_REF) == IMAGE
    assert comm.read_external_data("EEEEEEEE-0000-0000-0000-000000000005") is None


def test_read_external_data_never_looks_up_a_name_that_is_not_a_uuid(mocker, monkeypatch):
    monkeypatch.setenv(comm.ADDRESSBOOK_HOST_ENV, "other-mac")
    paths = mocker.patch("people_sync.sources._db_paths", return_value=[])
    run = mocker.patch("people_sync.comm.subprocess.run")
    assert comm.read_external_data("../../secret") is None
    assert not run.called and not paths.called


def test_read_external_data_over_ssh_runs_a_script_that_finds_the_file(
    tmp_path, mocker, monkeypatch
):
    """The remote script, captured from the ssh call and run here against a fake HOME."""
    monkeypatch.setenv(comm.ADDRESSBOOK_HOST_ENV, "other-mac")
    mocker.patch("people_sync.sources._db_paths", return_value=[])
    external = (
        tmp_path
        / "Library/Application Support/AddressBook/Sources/S1"
        / ".AddressBook-v22_SUPPORT/_EXTERNAL_DATA"
    )
    external.mkdir(parents=True)
    (external / EXT_REF).write_bytes(IMAGE)
    real_run = subprocess.run
    seen = []

    def ssh(argv, **kw):
        seen.append(argv)
        return real_run(["sh", "-s"], **kw, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})

    mocker.patch("people_sync.comm.subprocess.run", side_effect=ssh)
    assert comm.read_external_data(EXT_REF) == IMAGE
    assert seen[0][:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert "other-mac" in seen[0]
    assert comm.read_external_data("EEEEEEEE-0000-0000-0000-000000000005") is None
