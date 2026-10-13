"""`people-sync push google`: a fake People API behind gog, never a real account."""

import base64
import hashlib
import json
import re

import pytest

from people_sync import push

IMAGE = b"\xff\xd8\xff synthetic contact picture"
SHA = hashlib.sha256(IMAGE).hexdigest()
DEFAULT_PHOTO = {"default": True, "metadata": {"source": {"type": "CONTACT"}}}
OWN_PHOTO = {"metadata": {"source": {"type": "CONTACT"}, "primary": True}}
GET = ["api", "call", "people", "v1", "people.getBatchGet"]
UPDATE = ["api", "call", "people", "v1", "people.batchUpdateContacts"]
PHOTO = ["api", "call", "people", "v1", "people.updateContactPhoto"]


def _row(person="p1", google="people/c1", circles='["Springfield", "Lakeside"]', **over):
    row = {
        "id": person,
        "name": "Nova Quill",
        "circles": circles,
        "contact_photo_id": None,
        "photo_sha": None,
        "photo_key": None,
        "google_id": google,
    }
    row.update(over)
    return row


def _with_photo(**over):
    return _row(contact_photo_id="ph1", photo_sha=SHA, photo_key="photos/people/p1/x.jpg", **over)


def _contact(org=None, title=None, photos=(DEFAULT_PHOTO,), client=None):
    person = {"photos": list(photos)}
    if org or title:
        person["organizations"] = [
            {
                "metadata": {"primary": True},
                **({"name": org} if org else {}),
                **({"title": title} if title else {}),
            }
        ]
    if client:
        person["clientData"] = [{"key": k, "value": v} for k, v in client.items()]
    return person


class Estate:
    def __init__(self, rows, employments=()):
        self.rows, self.employments = list(rows), list(employments)

    def sql(self, query):
        if re.search(r"\bFROM person_(phones|emails|locations)\b", query):
            return []
        if re.search(r"\bFROM person_employments\b", query):
            return list(self.employments)
        if re.search(r"\bFROM people p\b", query):
            return list(self.rows)
        raise AssertionError(query)


class Gog:
    """getBatchGet, batchUpdateContacts and updateContactPhoto over an in-memory book."""

    def __init__(self, book, photo_fails=False, update_drops=()):
        self.book = {r: json.loads(json.dumps(p)) for r, p in book.items()}
        self.etags = {r: 0 for r in book}
        self.updates: list[dict] = []
        self.uploads: list[str] = []
        self.calls: list[list[str]] = []
        self.photo_fails, self.update_drops = photo_fails, update_drops

    def _person(self, resource):
        return {"resourceName": resource, "etag": f"e{self.etags[resource]}", **self.book[resource]}

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        if args[:5] == GET:
            params = json.loads(args[args.index("--params") + 1])
            assert len(params["resourceNames"]) <= 200
            return json.dumps(
                {
                    "responses": [
                        {
                            "requestedResourceName": r,
                            "httpStatusCode": 200,
                            "person": self._person(r),
                        }
                        if r in self.book
                        else {"requestedResourceName": r, "httpStatusCode": 404}
                        for r in params["resourceNames"]
                    ]
                }
            )
        if args[:5] == PHOTO:
            resource = json.loads(args[args.index("--params") + 1])["resourceName"]
            assert base64.b64decode(json.loads(stdin)["photoBytes"]) == IMAGE
            if self.photo_fails:
                raise push.port.GogError("gog api call people failed", resource)
            self.uploads.append(resource)
            self.book[resource]["photos"] = [OWN_PHOTO]
            self.etags[resource] += 1
            return json.dumps({"person": {"resourceName": resource}})
        if args[:2] == ["contacts", "raw"]:
            return json.dumps(self._person(args[2]))
        if args[:5] == UPDATE:
            assert {"--allow-write", "--force"} <= set(args)
            body = json.loads(stdin)
            assert len(body["contacts"]) <= 200
            self.updates.append(body)
            mask = body["updateMask"].split(",")
            result = {}
            for resource, person in body["contacts"].items():
                assert person["etag"] == f"e{self.etags[resource]}", "stale etag"
                for field in mask:
                    if field not in self.update_drops:
                        self.book[resource][field] = person.get(field, [])
                self.etags[resource] += 1
                result[resource] = {"httpStatusCode": 200, "person": self._person(resource)}
            return json.dumps({"updateResult": result})
        raise AssertionError(f"unexpected gog call {args}")


@pytest.fixture
def world(mocker):
    def make(rows, book, employments=(), **gog_kw):
        estate, gog = Estate(rows, employments), Gog(book, **gog_kw)
        mocker.patch("people_sync.somadata.sql", side_effect=estate.sql)
        mocker.patch("people_sync.port._gog", side_effect=gog)
        mocker.patch("people_sync.photos.get_object", return_value=IMAGE)
        return gog

    return make


def _orgs(gog, resource="people/c1"):
    return [(o.get("name"), o.get("title")) for o in gog.book[resource].get("organizations", [])]


def _client(gog, resource="people/c1"):
    return {e["key"]: e["value"] for e in gog.book[resource].get("clientData", [])}


# --- company ------------------------------------------------------------------


def test_company_is_the_circles_in_order():
    assert push.company('["Springfield", "Lakeside"]') == "Springfield, Lakeside"
    assert push.company('["StateU", " ", "StateU"]') == "StateU"
    assert push.company("[]") is None and push.company(None) is None


def test_dry_run_previews_the_company_without_writing(world, capsys):
    gog = world([_row()], {"people/c1": _contact(org="Springfield")})
    push.main(["google"])
    out = capsys.readouterr().out
    assert "PUSH Nova Quill (p1) -> people/c1" in out
    assert "company: 'Springfield' -> 'Springfield, Lakeside'" in out
    assert gog.updates == [] and _orgs(gog) == [("Springfield", None)]
    assert "1 contact to update" in out


def test_apply_writes_the_company_and_marks_it_ours(world, capsys):
    gog = world([_row()], {"people/c1": _contact(org="Springfield", client={"other": "keep"})})
    push.main(["google", "--apply"])
    out = capsys.readouterr().out
    (update,) = gog.updates
    assert update["updateMask"] == "organizations,clientData"
    assert _orgs(gog) == [("Springfield, Lakeside", None)]
    assert _client(gog) == {"other": "keep", push.COMPANY_KEY: "Springfield, Lakeside"}
    assert "WROTE people/c1" in out


def test_a_contact_already_in_sync_is_left_alone(world, capsys):
    book = {"people/c1": _contact(org="Springfield, Lakeside")}
    gog = world([_row()], book)
    push.main(["google", "--apply"])
    assert gog.updates == []
    assert "1 in sync" in capsys.readouterr().out


def test_a_google_company_people_does_not_hold_is_never_overwritten(world, capsys):
    gog = world([_row()], {"people/c1": _contact(org="G Court Neighbor")})
    push.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert gog.updates == []
    assert (
        "SKIP Nova Quill (p1) -> people/c1: Google company 'G Court Neighbor' is not in People"
        in out
    )


def test_an_employment_in_people_counts_as_recorded(world, capsys):
    gog = world(
        [_row()],
        {"people/c1": _contact(org="Acme", title="Engineer")},
        employments=[{"person_id": "p1", "company": "ACME", "title": "engineer"}],
    )
    push.main(["google", "--apply"])
    assert _orgs(gog) == [("Springfield, Lakeside", None)]


def test_an_unrecorded_title_blocks_the_write(world, capsys):
    gog = world([_row()], {"people/c1": _contact(org="Springfield", title="Neighbor")})
    push.main(["google", "--apply"])
    assert gog.updates == []
    assert "Google title 'Neighbor' is not in People" in capsys.readouterr().out


def test_a_value_push_wrote_earlier_is_replaced_after_a_circle_change(world, capsys):
    book = {
        "people/c1": _contact(
            org="Springfield, Lakeside", client={push.COMPANY_KEY: "Springfield, Lakeside"}
        )
    }
    gog = world([_row(circles='["Springfield"]')], book)
    push.main(["google", "--apply"])
    assert _orgs(gog) == [("Springfield", None)]
    assert _client(gog)[push.COMPANY_KEY] == "Springfield"


def test_no_circles_clears_a_recorded_company(world, capsys):
    book = {
        "people/c1": _contact(
            org="Springfield, Lakeside", client={push.COMPANY_KEY: "Springfield, Lakeside"}
        )
    }
    gog = world([_row(circles="[]")], book)
    push.main(["google", "--apply"])
    assert _orgs(gog) == [] and push.COMPANY_KEY not in _client(gog)


def test_person_filter(world, capsys):
    book = {"people/c1": _contact(), "people/c2": _contact()}
    gog = world([_row(), _row(person="p2", google="people/c2")], book)
    push.main(["google", "--person", "p2", "--apply"])
    assert [list(u["contacts"]) for u in gog.updates] == [["people/c2"]]


def test_an_unreadable_contact_is_skipped(world, capsys):
    gog = world([_row(google="people/c9")], {})
    push.main(["google", "--apply"])
    assert (
        "SKIP Nova Quill (p1) -> people/c9: Google did not return it (404)"
        in capsys.readouterr().out
    )
    assert gog.updates == []


def test_reads_and_writes_go_in_batches_of_200(world, capsys):
    rows = [_row(person=f"p{i}", google=f"people/c{i}") for i in range(250)]
    gog = world(rows, {f"people/c{i}": _contact() for i in range(250)})
    push.main(["google", "--apply"])
    assert [
        len(json.loads(c[c.index("--params") + 1])["resourceNames"])
        for c in gog.calls
        if c[:5] == GET
    ] == [200, 50]
    assert [len(u["contacts"]) for u in gog.updates] == [200, 50]


def test_a_write_the_read_back_does_not_show_is_reported(world, capsys):
    world([_row()], {"people/c1": _contact()}, update_drops=("organizations",))
    push.main(["google", "--apply"])
    assert "VERIFY FAILED people/c1: company not as written" in capsys.readouterr().out


def test_a_marker_the_read_back_does_not_show_is_reported(world, capsys):
    world([_row()], {"people/c1": _contact()}, update_drops=("clientData",))
    push.main(["google", "--apply"])
    assert "VERIFY FAILED people/c1: marker not as written" in capsys.readouterr().out


# --- photo --------------------------------------------------------------------


def test_no_contact_picture_leaves_the_google_photo_alone(world, capsys):
    gog = world(
        [_row(circles='["Springfield", "Lakeside"]')], {"people/c1": _contact(org="Springfield, Lakeside")}
    )
    push.main(["google", "--apply"])
    assert gog.uploads == [] and gog.updates == []


def test_dry_run_previews_the_photo(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield, Lakeside")})
    push.main(["google"])
    out = capsys.readouterr().out
    assert "photo: yes (will upload)" in out and gog.uploads == []
    assert SHA not in out and base64.b64encode(IMAGE).decode() not in out


def test_apply_uploads_the_picture_then_records_its_hash(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield, Lakeside")})
    push.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert gog.uploads == ["people/c1"]
    (update,) = gog.updates
    assert update["updateMask"] == "clientData"
    assert _client(gog)[push.PHOTO_KEY] == SHA
    assert _orgs(gog) == [("Springfield, Lakeside", None)]
    assert "WROTE people/c1" in out


def test_a_pushed_picture_is_not_uploaded_again(world, capsys):
    book = {
        "people/c1": _contact(
            org="Springfield, Lakeside", photos=[OWN_PHOTO], client={push.PHOTO_KEY: SHA}
        )
    }
    gog = world([_with_photo()], book)
    push.main(["google", "--apply"])
    assert gog.uploads == [] and gog.updates == []


def test_a_picture_removed_in_google_is_uploaded_again(world, capsys):
    book = {"people/c1": _contact(org="Springfield, Lakeside", client={push.PHOTO_KEY: SHA})}
    gog = world([_with_photo()], book)
    push.main(["google", "--apply"])
    assert gog.uploads == ["people/c1"]


def test_a_new_contact_picture_replaces_the_pushed_one(world, capsys):
    book = {
        "people/c1": _contact(
            org="Springfield, Lakeside", photos=[OWN_PHOTO], client={push.PHOTO_KEY: "0" * 64}
        )
    }
    gog = world([_with_photo()], book)
    push.main(["google", "--apply"])
    assert gog.uploads == ["people/c1"] and _client(gog)[push.PHOTO_KEY] == SHA


def test_photo_and_company_land_in_one_update_with_a_fresh_etag(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield")})
    push.main(["google", "--apply"])
    (update,) = gog.updates
    assert update["updateMask"] == "organizations,clientData"
    assert _client(gog) == {push.COMPANY_KEY: "Springfield, Lakeside", push.PHOTO_KEY: SHA}


def test_a_failed_upload_records_no_hash_and_still_writes_the_company(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield")}, photo_fails=True)
    push.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert "PHOTO FAILED people/c1" in out
    assert _client(gog) == {push.COMPANY_KEY: "Springfield, Lakeside"}


def test_stored_bytes_that_do_not_match_the_hash_are_not_uploaded(world, mocker, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield, Lakeside")})
    mocker.patch("people_sync.photos.get_object", return_value=b"other bytes")
    push.main(["google", "--apply"])
    assert gog.uploads == []
    assert (
        "PHOTO FAILED people/c1: the stored photo does not match its hash"
        in capsys.readouterr().out
    )


def test_a_contact_picture_without_a_live_photo_is_reported(world, capsys):
    gog = world([_row(contact_photo_id="gone")], {"people/c1": _contact(org="Springfield, Lakeside")})
    push.main(["google", "--apply"])
    assert (
        "photo: skipped (contact_photo_id names no live photo of theirs)" in capsys.readouterr().out
    )
    assert gog.uploads == []


def test_an_unrecorded_company_blocks_only_the_company_not_the_photo(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="G Court Neighbor")})
    push.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert "Google company 'G Court Neighbor' is not in People" in out
    assert gog.uploads == ["people/c1"]
    assert _orgs(gog) == [("G Court Neighbor", None)]
    assert _client(gog) == {push.PHOTO_KEY: SHA}


# --- fields, same picture, apostrophes ------------------------------------------

OWN_WITH_URL = OWN_PHOTO | {"url": "https://lh3.googleusercontent.com/contacts/AAA=s100"}


def test_field_photo_leaves_the_company_alone(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield")})
    push.main(["google", "--field", "photo", "--apply"])
    assert gog.uploads == ["people/c1"]
    assert _orgs(gog) == [("Springfield", None)]
    assert _client(gog) == {push.PHOTO_KEY: SHA}


def test_field_company_leaves_the_photo_alone(world, capsys):
    gog = world([_with_photo()], {"people/c1": _contact(org="Springfield")})
    push.main(["google", "--field", "company", "--apply"])
    assert gog.uploads == []
    assert _client(gog) == {push.COMPANY_KEY: "Springfield, Lakeside"}


def test_the_same_picture_already_in_google_is_only_marked(world, mocker, capsys):
    fetch = mocker.patch("people_sync.photos.fetch_url_photo", return_value=IMAGE)
    book = {"people/c1": _contact(org="Springfield, Lakeside", photos=[OWN_WITH_URL])}
    gog = world([_with_photo()], book)
    push.main(["google", "--apply"])
    out = capsys.readouterr().out
    assert gog.uploads == [] and _client(gog) == {push.PHOTO_KEY: SHA}
    fetch.assert_called_once_with("https://lh3.googleusercontent.com/contacts/AAA=s0")
    assert "photo: already in Google (mark only)" in out


def test_a_different_picture_in_google_is_replaced(world, mocker, capsys):
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=b"another image")
    book = {"people/c1": _contact(org="Springfield, Lakeside", photos=[OWN_WITH_URL])}
    gog = world([_with_photo()], book)
    push.main(["google", "--apply"])
    assert gog.uploads == ["people/c1"]


def test_curly_and_straight_apostrophes_are_the_same_value(world, capsys):
    gog = world([_row(circles='["Kim’s sister"]')], {"people/c1": _contact(org="Kim's Sister")})
    push.main(["google", "--apply"])
    assert _orgs(gog) == [("Kim’s sister", None)]


def test_full_size_url():
    assert push.full_size("https://x/contacts/AAA=s100") == "https://x/contacts/AAA=s0"
    assert push.full_size("https://x/a-/BBB") == "https://x/a-/BBB=s0"
