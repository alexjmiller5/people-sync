import json

from contact_sync import sources

GOOGLE_LIST_PAGE1 = json.dumps(
    {
        "contacts": [
            {"resource": "people/c1", "name": "Test Person", "birthday": "2000-01-01"},
            {"resource": "people/c2", "name": "Bare Person"},
            {"resource": "people/c3", "name": "Broken Person"},
        ],
        "nextPageToken": "tok1",
    }
)
GOOGLE_LIST_PAGE2 = json.dumps(
    {"contacts": [{"resource": "people/c4", "name": "Last Page Person"}]}
)

GOOGLE_RAW_C1 = json.dumps(
    {
        "resourceName": "people/c1",
        "names": [{"displayName": "Test Person", "metadata": {"primary": True}}],
        "memberships": [
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}}
        ],
        "organizations": [{"name": "Test Org"}],
        "birthdays": [{"text": "2000-01-01"}],
        "photos": [{"url": "https://example.com/c1.jpg", "metadata": {"primary": True}}],
        "emailAddresses": [{"value": "synthetic@example.com"}],
        "phoneNumbers": [{"value": "+1 (555) 010-0000"}],
    }
)
GOOGLE_RAW_C2 = json.dumps(
    {
        "resourceName": "people/c2",
        "names": [{"displayName": "Bare Person"}],
    }
)
GOOGLE_RAW_C4 = json.dumps(
    {
        "resourceName": "people/c4",
        "names": [{"displayName": "Last Page Person"}],
    }
)


def _fake_run_google(cmd, **kwargs):
    class Proc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    if cmd[:3] == ["gog", "contacts", "list"]:
        if "--page" in cmd:
            return Proc(GOOGLE_LIST_PAGE2)
        return Proc(GOOGLE_LIST_PAGE1)
    if cmd[:3] == ["gog", "contacts", "raw"]:
        rid = cmd[3]
        if rid == "people/c1":
            return Proc(GOOGLE_RAW_C1)
        if rid == "people/c2":
            return Proc(GOOGLE_RAW_C2)
        if rid == "people/c3":
            return Proc("not json", returncode=0)
        if rid == "people/c4":
            return Proc(GOOGLE_RAW_C4)
    raise AssertionError(f"unexpected command {cmd}")


def test_fetch_google_maps_full_contact(mocker):
    mocker.patch("contact_sync.sources.subprocess.run", side_effect=_fake_run_google)
    recs = {r.source_id: r for r in sources.fetch_google()}

    full = recs["people/c1"]
    assert full.source == "google_contacts"
    assert full.name == "Test Person"
    assert full.handle is None
    assert full.follows_me is None and full.i_follow is None
    assert full.raw["labels"] == [
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}}
    ]
    assert full.raw["org"] == [{"name": "Test Org"}]
    assert full.raw["photo_url"] == "https://example.com/c1.jpg"
    # phone/email never copied into raw - same boundary as Apple
    assert "emailAddresses" not in full.raw
    assert "phoneNumbers" not in full.raw
    assert "email" not in json.dumps(full.raw).lower() or "synthetic@example.com" not in json.dumps(
        full.raw
    )


def test_fetch_google_paginates_and_skips_malformed(mocker):
    mocker.patch("contact_sync.sources.subprocess.run", side_effect=_fake_run_google)
    recs = {r.source_id: r for r in sources.fetch_google()}

    # people/c3's raw call returns invalid JSON - must be skipped, not crash
    assert "people/c3" not in recs
    # bare contact still produces a record with empty label/org lists
    bare = recs["people/c2"]
    assert bare.raw["labels"] == []
    assert bare.raw["org"] == []
    assert bare.raw["photo_url"] is None
    # second page (via nextPageToken) was fetched
    assert "people/c4" in recs
    assert len(recs) == 3  # c1, c2, c4 (c3 skipped)


APPLE_ROWS_DB1 = json.dumps(
    [
        {
            "id": "AAAA1111-0000-0000-0000-000000000000:ABPerson",
            "first": "Test",
            "last": "Person",
            "middle": None,
            "nick": None,
            "org": "Test Org",
            "title": None,
            "birthday": "2000-01-01",
            "phone_count": 2,
            "email_count": 0,
        },
        {
            "id": None,
            "first": None,
            "last": None,
            "middle": None,
            "nick": None,
            "org": "Orphan Org",
            "title": None,
            "birthday": None,
            "phone_count": 0,
            "email_count": 0,
        },
    ]
)
APPLE_ROWS_DB2 = json.dumps(
    [
        {
            "id": "BBBB2222-0000-0000-0000-000000000000:ABPerson",
            "first": None,
            "last": None,
            "middle": None,
            "nick": "Nicky",
            "org": None,
            "title": None,
            "birthday": None,
            "phone_count": 0,
            "email_count": 1,
        }
    ]
)


def _fake_run_apple(cmd, **kwargs):
    class Proc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    assert cmd[0] == "sqlite3"
    db_arg = cmd[2]
    if "db1" in db_arg:
        return Proc(APPLE_ROWS_DB1)
    if "db2" in db_arg:
        return Proc(APPLE_ROWS_DB2)
    raise AssertionError(f"unexpected command {cmd}")


def test_fetch_apple_maps_and_skips_missing_id(mocker):
    mocker.patch(
        "contact_sync.sources._db_paths", return_value=["/fake/db1.abcddb", "/fake/db2.abcddb"]
    )
    mocker.patch("contact_sync.sources.subprocess.run", side_effect=_fake_run_apple)

    recs = {r.source_id: r for r in sources.fetch_apple()}

    # the row with id=None is skipped, not crashed on
    assert len(recs) == 2

    full = recs["AAAA1111-0000-0000-0000-000000000000:ABPerson"]
    assert full.source == "apple_contacts"
    assert full.handle is None
    assert full.name == "Test Person"
    assert full.raw["org"] == "Test Org"
    assert full.raw["birthday"] == "2000-01-01"
    assert full.raw["has_phone"] is True
    assert full.raw["has_email"] is False
    # hard privacy rule: no actual phone/email values ever appear
    assert "phone" not in full.raw or isinstance(full.raw.get("has_phone"), bool)
    assert not any(
        k
        for k in full.raw
        if k
        not in {
            "first",
            "last",
            "middle",
            "nick",
            "org",
            "title",
            "birthday",
            "has_phone",
            "has_email",
        }
    )

    nick = recs["BBBB2222-0000-0000-0000-000000000000:ABPerson"]
    assert nick.name == "Nicky"  # falls back to nickname when no first/last
    assert nick.raw["has_phone"] is False
    assert nick.raw["has_email"] is True


def test_fetch_apple_no_databases_found(mocker):
    mocker.patch("contact_sync.sources._db_paths", return_value=[])
    run = mocker.patch("contact_sync.sources.subprocess.run")
    assert sources.fetch_apple() == []
    run.assert_not_called()
