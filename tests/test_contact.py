"""People-owned contact fields: `pull google` and `push google --field contact`.
Synthetic people and a fake People API; no real contact details."""

import json
import re

import pytest

from people_sync import contact, push

PHONE = {"value": "(555) 010-0001", "canonicalForm": "+15550100001", "type": "mobile"}
PHONE2 = {"value": "+44 20 7946 0000", "canonicalForm": "+442079460000", "type": "work"}
EMAIL = {"value": "Nova@Example.test", "type": "home"}
ADDRESS = {
    "streetAddress": "1 Synthetic Way",
    "city": "Testville",
    "region": "TS",
    "postalCode": "00001",
    "country": "United States",
    "type": "home",
}
NAMES = [{"givenName": "Nova", "familyName": "Quill", "displayName": "Nova Quill"}]


def _person(pid="p1", **over):
    row = {
        "id": pid,
        "name": "Nova Quill",
        "first_name": None,
        "middle_name": None,
        "last_name": None,
        "birthday": None,
        "google_id": "people/c1",
    }
    row.update(over)
    return row


def _google(**over):
    p = {"names": NAMES}
    p.update(over)
    return p


# --- normalization --------------------------------------------------------------


def test_google_state_keys_values_the_way_they_compare():
    g = contact.google_state(
        _google(
            phoneNumbers=[PHONE, {"value": "555 010 0002"}],
            emailAddresses=[EMAIL],
            addresses=[ADDRESS],
            birthdays=[{"date": {"month": 3, "day": 5}}],
        )
    )
    assert set(g["phones"]) == {"5550100001", "5550100002"}
    assert g["phones"]["5550100001"]["number"] == "+15550100001"
    assert g["phones"]["5550100002"]["number"] == "555 010 0002"
    assert set(g["emails"]) == {"nova@example.test"}
    assert g["emails"]["nova@example.test"]["address"] == "Nova@Example.test"
    (address,) = g["addresses"].values()
    assert address["street"] == "1 Synthetic Way" and address["postal_code"] == "00001"
    assert g["birthday"] == "--03-05"
    assert g["names"] == {"given": "Nova", "middle": None, "family": "Quill"}


def test_fingerprint_ignores_order_and_google_metadata():
    a = contact.google_state(_google(phoneNumbers=[PHONE, PHONE2]))
    b = contact.google_state(
        _google(phoneNumbers=[PHONE2 | {"metadata": {"primary": False}}, PHONE])
    )
    assert contact.fingerprint(a) == contact.fingerprint(b)
    c = contact.google_state(_google(phoneNumbers=[PHONE]))
    assert contact.fingerprint(a) != contact.fingerprint(c)


# --- diff -----------------------------------------------------------------------


def _soma(person=None, phones=(), emails=(), addresses=()):
    return contact.soma_state(person or _person(), list(phones), list(emails), list(addresses))


def test_a_new_contact_adds_what_soma_lacks_and_asks_about_conflicts():
    person = _person(birthday="2001-03-06", first_name="Nova", last_name="Quill")
    soma = _soma(
        person,
        phones=[
            {"id": "p1:5550100009", "number": "+15550100009", "label": "mobile", "is_primary": 1}
        ],
    )
    google = contact.google_state(
        _google(
            phoneNumbers=[PHONE],
            emailAddresses=[EMAIL],
            birthdays=[{"date": {"year": 2001, "month": 3, "day": 5}}],
        )
    )
    items = contact.diff(soma, google, "new")
    kinds = {(i["kind"], i["field"]) for i in items}
    assert ("add", "phones") in kinds and ("add", "emails") in kinds
    assert ("conflict", "birthday") in kinds
    assert not any(i["kind"] == "missing" for i in items)  # never pushed yet, not a deletion


def test_a_yearless_birthday_is_not_a_conflict_and_the_year_is_added():
    named = {"first_name": "Nova", "last_name": "Quill"}
    soma = _soma(_person(birthday="--03-05", **named))
    google = contact.google_state(
        _google(birthdays=[{"date": {"year": 2001, "month": 3, "day": 5}}])
    )
    (item,) = contact.diff(soma, google, "new")
    assert (item["kind"], item["field"], item["google"]) == ("add", "birthday", "2001-03-05")
    soma = _soma(_person(birthday="2001-03-05", **named))
    google = contact.google_state(_google(birthdays=[{"date": {"month": 3, "day": 5}}]))
    assert contact.diff(soma, google, "new") == []


def test_google_name_parts_fill_empty_people_parts_when_they_spell_the_name():
    (item,) = contact.diff(_soma(), contact.google_state(_google()), "new")
    assert (item["kind"], item["field"]) == ("add", "names")
    other = contact.google_state(_google(names=[{"givenName": "Nova"}]))
    (item,) = contact.diff(_soma(), other, "new")
    assert (item["kind"], item["field"]) == ("conflict", "names")


def test_an_unchanged_contact_needs_no_decision():
    soma = _soma(_person(first_name="Nova", last_name="Quill"))
    google = contact.google_state(_google(phoneNumbers=[PHONE]))
    assert contact.diff(soma, google, "unchanged") == []  # soma dropped it since: push removes


def test_a_changed_contact_asks_about_values_google_lost():
    phone = {"id": "p1:5550100001", "number": "+15550100001", "label": "mobile", "is_primary": 1}
    soma = _soma(_person(first_name="Nova", last_name="Quill"), phones=[phone])
    google = contact.google_state(_google(phoneNumbers=[PHONE2]))
    items = {(i["kind"], i["field"], i["key"]) for i in contact.diff(soma, google, "changed")}
    assert items == {("missing", "phones", "5550100001"), ("add", "phones", "442079460000")}


# --- the fake estate and People API --------------------------------------------


class Estate:
    def __init__(self, people, phones=(), emails=(), addresses=()):
        self.people = {p["id"]: dict(p) for p in people}
        self.tables = {
            "person_phones": [dict(r) for r in phones],
            "person_emails": [dict(r) for r in emails],
            "person_locations": [dict(r) for r in addresses],
            "provenance": [],
        }
        self.updates: list[str] = []

    def sql(self, query):
        q = query.strip()
        if q.startswith("UPDATE"):
            self.updates.append(q)
            m = re.match(r"UPDATE people SET (.*) WHERE id = '([^']+)'", q, re.S)
            if m:
                for col, val in re.findall(r"(\w+) = '((?:[^']|'')*)'", m.group(1)):
                    self.people[m.group(2)][col] = val.replace("''", "'")
            m = re.match(r"UPDATE (person_\w+) SET (.*) WHERE id = '([^']+)'", q, re.S)
            if m and m.group(1) != "people":
                for row in self.tables[m.group(1)]:
                    if row["id"] == m.group(3):
                        for col, val in re.findall(r"\"?(\w+)\"? = '((?:[^']|'')*)'", m.group(2)):
                            row[col] = val
            return []
        if "FROM people p" in q:
            return [dict(p) for p in self.people.values()]
        for table in ("person_phones", "person_emails", "person_locations"):
            if f"FROM {table}" in q:
                return [
                    dict(r)
                    for r in self.tables[table]
                    if not r.get("deleted_at") and not r.get("end")
                ]
        raise AssertionError(q)

    def insert(self, table, rows):
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)


class Gog:
    def __init__(self, book, drops=()):
        self.drops = drops
        self.book = {r: json.loads(json.dumps(p)) for r, p in book.items()}
        self.etags = dict.fromkeys(book, 0)
        self.updates: list[dict] = []

    def _person(self, r):
        return {"resourceName": r, "etag": f"e{self.etags[r]}", **self.book[r]}

    def __call__(self, args, stdin=None):
        if args[:5] == push.API + ["people.getBatchGet"]:
            names = json.loads(args[args.index("--params") + 1])["resourceNames"]
            return json.dumps(
                {
                    "responses": [
                        {
                            "requestedResourceName": r,
                            "httpStatusCode": 200,
                            "person": self._person(r),
                        }
                        for r in names
                    ]
                }
            )
        if args[:5] == push.API + ["people.batchUpdateContacts"]:
            body = json.loads(stdin)
            self.updates.append(body)
            result = {}
            for r, person in body["contacts"].items():
                assert person["etag"] == f"e{self.etags[r]}"
                for field in body["updateMask"].split(","):
                    if field not in self.drops:
                        self.book[r][field] = person.get(field, [])
                self.etags[r] += 1
                result[r] = {"httpStatusCode": 200, "person": self._person(r)}
            return json.dumps({"updateResult": result})
        raise AssertionError(args)


@pytest.fixture
def world(mocker):
    def make(people, book, drops=(), **tables):
        estate, gog = Estate(people, **tables), Gog(book, drops)
        mocker.patch("people_sync.somadata.sql", side_effect=estate.sql)
        mocker.patch("people_sync.somadata.insert", side_effect=estate.insert)
        mocker.patch("people_sync.somadata.now_iso", return_value="2026-01-01T00:00:00.000Z")
        mocker.patch("people_sync.port._gog", side_effect=gog)
        return estate, gog

    return make


def _mark(gog, r="people/c1"):
    return {e["key"]: e["value"] for e in gog.book[r].get("clientData", [])}.get(contact.MARK_KEY)


# --- pull -----------------------------------------------------------------------


def test_pull_dry_run_writes_nothing(world, capsys):
    estate, gog = world([_person()], {"people/c1": _google(phoneNumbers=[PHONE])})
    contact.main(["google"])
    out = capsys.readouterr().out
    assert "add phones" in out and estate.tables["person_phones"] == [] and gog.updates == []


def test_pull_apply_imports_adds_with_evidence_and_marks_the_contact(world, capsys):
    book = {
        "people/c1": _google(
            phoneNumbers=[PHONE],
            emailAddresses=[EMAIL],
            addresses=[ADDRESS],
            birthdays=[{"date": {"year": 2001, "month": 3, "day": 5}}],
        )
    }
    estate, gog = world([_person()], book)
    contact.main(["google", "--apply"])
    (phone,) = estate.tables["person_phones"]
    assert phone["id"] == "p1:5550100001" and phone["number"] == "+15550100001"
    assert phone["label"] == "mobile" and phone["person_id"] == "p1"
    (email,) = estate.tables["person_emails"]
    assert email["id"] == "p1:nova@example.test" and email["address"] == "Nova@Example.test"
    (address,) = estate.tables["person_locations"]
    assert address["street"] == "1 Synthetic Way" and address["source"] == "google_contacts"
    assert address["id"].startswith("addr:p1:")
    assert estate.people["p1"]["birthday"] == "2001-03-05"
    assert (
        estate.people["p1"]["first_name"] == "Nova" and estate.people["p1"]["last_name"] == "Quill"
    )
    edges = estate.tables["provenance"]
    assert {e["from_ref"] for e in edges} == {"google_contacts:people/c1"}
    assert {e["to_kind"] for e in edges} >= {
        "person_phones",
        "person_emails",
        "person_locations",
        "people",
    }
    assert _mark(gog) == contact.fingerprint(contact.google_state(book["people/c1"]))


def test_pull_leaves_a_contact_with_open_decisions_unmarked(world, capsys):
    book = {"people/c1": _google(birthdays=[{"date": {"year": 2001, "month": 3, "day": 5}}])}
    estate, gog = world(
        [_person(birthday="2002-03-05", first_name="Nova", last_name="Quill")], book
    )
    contact.main(["google", "--apply", "--output", "/dev/null"])
    out = capsys.readouterr().out
    assert "conflict birthday" in out and _mark(gog) is None
    assert estate.people["p1"]["birthday"] == "2002-03-05"


def test_accepting_google_for_a_conflict_writes_soma_and_marks(world, tmp_path, capsys):
    book = {"people/c1": _google(birthdays=[{"date": {"year": 2001, "month": 3, "day": 5}}])}
    estate, gog = world(
        [_person(birthday="2002-03-05", first_name="Nova", last_name="Quill")], book
    )
    decisions = tmp_path / "d.json"
    decisions.write_text(json.dumps({"people/c1:birthday": "google"}))
    contact.main(["google", "--apply", "--accept", str(decisions)])
    assert estate.people["p1"]["birthday"] == "2001-03-05"
    assert _mark(gog) is not None


def test_keeping_soma_for_a_conflict_marks_without_writing_soma(world, tmp_path, capsys):
    book = {"people/c1": _google(birthdays=[{"date": {"year": 2001, "month": 3, "day": 5}}])}
    estate, gog = world(
        [_person(birthday="2002-03-05", first_name="Nova", last_name="Quill")], book
    )
    decisions = tmp_path / "d.json"
    decisions.write_text(json.dumps({"people/c1:birthday": "soma"}))
    contact.main(["google", "--apply", "--accept", str(decisions)])
    assert estate.people["p1"]["birthday"] == "2002-03-05" and _mark(gog) is not None


def test_accepting_google_for_a_lost_value_retires_it_in_soma(world, tmp_path, capsys):
    phone = {
        "id": "p1:5550100001",
        "person_id": "p1",
        "number": "+15550100001",
        "label": "mobile",
        "is_primary": 1,
    }
    google = _google(phoneNumbers=[PHONE2])
    stale = contact.fingerprint(contact.google_state(_google(phoneNumbers=[PHONE])))
    google["clientData"] = [{"key": contact.MARK_KEY, "value": stale}]
    estate, gog = world(
        [_person(first_name="Nova", last_name="Quill")], {"people/c1": google}, phones=[phone]
    )
    decisions = tmp_path / "d.json"
    decisions.write_text(json.dumps({"people/c1:phones:5550100001": "google"}))
    contact.main(["google", "--apply", "--accept", str(decisions)])
    assert any("UPDATE person_phones SET deleted_at" in u for u in estate.updates)
    assert [p["id"] for p in estate.tables["person_phones"]][-1] == "p1:442079460000"


# --- push -----------------------------------------------------------------------


def _push_rows(estate):
    return [
        push_row | {"circles": "[]", "contact_photo_id": None, "photo_sha": None, "photo_key": None}
        for push_row in estate.people.values()
    ]


@pytest.fixture
def push_world(world, mocker):
    def make(people, book, drops=(), **tables):
        estate, gog = world(people, book, drops, **tables)
        real = estate.sql

        def sql(q):
            if "FROM person_employments" in q:
                return []
            if "LEFT JOIN person_photos" in q:
                return _push_rows(estate)
            return real(q)

        mocker.patch("people_sync.somadata.sql", side_effect=sql)
        return estate, gog

    return make


def test_push_writes_soma_onto_a_reviewed_contact(push_world, capsys):
    google = _google(phoneNumbers=[PHONE])
    google["clientData"] = [
        {"key": contact.MARK_KEY, "value": contact.fingerprint(contact.google_state(google))}
    ]
    phones = [
        {
            "id": "p1:5550100001",
            "person_id": "p1",
            "number": "+15550100001",
            "label": "mobile",
            "is_primary": 1,
        },
        {
            "id": "p1:442079460000",
            "person_id": "p1",
            "number": "+442079460000",
            "label": "work",
            "is_primary": 0,
        },
    ]
    estate, gog = push_world(
        [_person(first_name="Nova", last_name="Quill", birthday="--03-05")],
        {"people/c1": google},
        phones=phones,
    )
    push.main(["google", "--field", "contact", "--apply"])
    out = capsys.readouterr().out
    book = gog.book["people/c1"]
    assert [p["value"] for p in book["phoneNumbers"]] == ["+15550100001", "+442079460000"]
    assert book["birthdays"] == [{"date": {"month": 3, "day": 5}}]
    assert book["names"] == [{"givenName": "Nova", "familyName": "Quill"}]
    assert _mark(gog) == contact.fingerprint(contact.google_state(book))
    assert "WROTE people/c1" in out


def test_push_skips_a_contact_google_changed_since_the_review(push_world, capsys):
    google = _google(phoneNumbers=[PHONE])
    google["clientData"] = [{"key": contact.MARK_KEY, "value": "0" * 64}]
    estate, gog = push_world([_person(first_name="Nova", last_name="Quill")], {"people/c1": google})
    push.main(["google", "--field", "contact", "--apply"])
    assert gog.updates == []
    assert "contact: skipped (Google changed since the last pull" in capsys.readouterr().out


def test_push_skips_a_contact_never_pulled(push_world, capsys):
    estate, gog = push_world([_person()], {"people/c1": _google(phoneNumbers=[PHONE])})
    push.main(["google", "--field", "contact", "--apply"])
    assert gog.updates == []
    assert "1 never pulled" in capsys.readouterr().out


def test_push_leaves_a_contact_already_equal_to_soma(push_world, capsys):
    phones = [
        {
            "id": "p1:5550100001",
            "person_id": "p1",
            "number": "+15550100001",
            "label": "mobile",
            "is_primary": 1,
        }
    ]
    google = _google(phoneNumbers=[PHONE], names=[{"givenName": "Nova", "familyName": "Quill"}])
    google["clientData"] = [
        {"key": contact.MARK_KEY, "value": contact.fingerprint(contact.google_state(google))}
    ]
    estate, gog = push_world(
        [_person(first_name="Nova", last_name="Quill")], {"people/c1": google}, phones=phones
    )
    push.main(["google", "--field", "contact", "--apply"])
    assert gog.updates == [] and "1 in sync" in capsys.readouterr().out


def test_number_formatting_is_not_a_difference():
    typed = contact.google_state(
        _google(phoneNumbers=[{"value": "(555) 010-0001", "type": "mobile"}])
    )
    e164 = contact.google_state(_google(phoneNumbers=[{"value": "+15550100001", "type": "mobile"}]))
    assert contact.fingerprint(typed) == contact.fingerprint(e164)
    relabeled = contact.google_state(
        _google(phoneNumbers=[{"value": "+15550100001", "type": "work"}])
    )
    assert contact.fingerprint(relabeled) != contact.fingerprint(e164)


def test_a_contact_write_the_read_back_does_not_show_is_reported(push_world, capsys):
    google = _google(phoneNumbers=[PHONE])
    google["clientData"] = [
        {"key": contact.MARK_KEY, "value": contact.fingerprint(contact.google_state(google))}
    ]
    phones = [
        {
            "id": "p1:442079460000",
            "person_id": "p1",
            "number": "+442079460000",
            "label": "work",
            "is_primary": 1,
        }
    ]
    estate, gog = push_world(
        [_person(first_name="Nova", last_name="Quill")],
        {"people/c1": google},
        drops=("phoneNumbers",),
        phones=phones,
    )
    push.main(["google", "--field", "contact", "--apply"])
    assert "VERIFY FAILED people/c1: contact fields not as written" in capsys.readouterr().out


def test_apples_year_1604_placeholder_is_an_unknown_year():
    g = contact.google_state(_google(birthdays=[{"date": {"year": 1604, "month": 8, "day": 2}}]))
    assert g["birthday"] == "--08-02"
    soma = _soma(_person(birthday="1992-08-02", first_name="Nova", last_name="Quill"))
    assert contact.diff(soma, g, "new") == []
