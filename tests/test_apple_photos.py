"""`people-sync ingest apple_photos`: a synthetic Photos.sqlite, never a real library."""

import sqlite3

import pytest

from people_sync import apple_photos, captures, replay

UUID_A = "AAAAAAAA-0000-0000-0000-000000000001"
UUID_B = "BBBBBBBB-0000-0000-0000-000000000002"
UUID_C = "CCCCCCCC-0000-0000-0000-000000000003"
CARD = "DDDDDDDD-0000-0000-0000-000000000004:ABPerson"


def _library(tmp_path, rows):
    db = tmp_path / "Synthetic.photoslibrary" / "database"
    db.mkdir(parents=True)
    con = sqlite3.connect(db / "Photos.sqlite")
    con.execute(
        "CREATE TABLE ZPERSON (Z_PK INTEGER PRIMARY KEY, ZPERSONUUID TEXT, ZFULLNAME TEXT, "
        "ZDISPLAYNAME TEXT, ZFACECOUNT INTEGER, ZPERSONURI TEXT, ZMERGETARGETPERSON INTEGER, "
        "ZCONTACTMATCHINGDICTIONARY BLOB)"
    )
    con.executemany(
        "INSERT INTO ZPERSON (ZPERSONUUID, ZFULLNAME, ZDISPLAYNAME, ZFACECOUNT, ZPERSONURI, "
        "ZMERGETARGETPERSON, ZCONTACTMATCHINGDICTIONARY) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return str(tmp_path / "Synthetic.photoslibrary")


ROWS = [
    (UUID_A, "Nova Quill", "Nova", 120, CARD, None, b"secret contact blob"),
    (UUID_B, "Rex Vale", None, 3, None, None, None),
    (UUID_C, "Old Merge", None, 9, None, 1, None),  # merged into another person
    ("EEEEEEEE-0000-0000-0000-000000000005", "", None, 40, None, None, None),  # unnamed
    ("FFFFFFFF-0000-0000-0000-000000000006", None, None, 50, None, None, None),
]


def test_named_unmerged_people_become_records(tmp_path):
    payload = apple_photos.collect(_library(tmp_path, ROWS))
    records = apple_photos.parse_records(payload)
    assert [(r.source, r.source_id, r.name) for r in records] == [
        ("apple_photos", UUID_A, "Nova Quill"),
        ("apple_photos", UUID_B, "Rex Vale"),
    ]
    assert records[0].raw == {
        "display_name": "Nova",
        "face_count": 120,
        "contact_ref": "apple_contacts:" + CARD,
    }
    assert records[1].raw["contact_ref"] is None
    assert payload["complete"] is True


def test_only_metadata_reaches_the_payload(tmp_path):
    payload = apple_photos.collect(_library(tmp_path, ROWS))
    encoded = captures.encode(payload).decode()
    assert "secret contact blob" not in encoded and "Synthetic.photoslibrary" not in encoded


def test_an_unreadable_library_is_an_incomplete_read(tmp_path):
    payload = apple_photos.collect(str(tmp_path / "missing.photoslibrary"))
    assert payload["complete"] is False and payload["people"] == []
    assert apple_photos.parse_records(payload) == []


def test_capture_validates_and_replays_to_the_same_records(tmp_path):
    payload = apple_photos.collect(_library(tmp_path, ROWS))
    capture = apple_photos.capture(payload)
    captures.validate(capture)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert [r["source_id"] for r in result["records"]] == [UUID_A, UUID_B]


@pytest.mark.parametrize(
    "bad",
    [
        {
            "uuid": "not-a-uuid",
            "name": "X",
            "display_name": None,
            "face_count": 1,
            "contact_ref": None,
        },
        {"uuid": UUID_A, "name": "X", "display_name": None, "face_count": -1, "contact_ref": None},
        {"uuid": UUID_A, "name": "X", "display_name": None, "face_count": 1, "contact_ref": "x"},
        {
            "uuid": UUID_A,
            "name": "X",
            "display_name": None,
            "face_count": 1,
            "contact_ref": None,
            "path": "/x",
        },
    ],
)
def test_unknown_or_unsafe_fields_are_refused(bad):
    payload = {"format": apple_photos.FORMAT, "complete": True, "people": [bad]}
    with pytest.raises(ValueError):
        apple_photos.validate_payload(payload)


def test_duplicate_uuids_are_refused():
    row = {"uuid": UUID_A, "name": "X", "display_name": None, "face_count": 1, "contact_ref": None}
    with pytest.raises(ValueError):
        apple_photos.validate_payload(
            {"format": apple_photos.FORMAT, "complete": True, "people": [row, row]}
        )


def test_default_library_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PEOPLE_SYNC_PHOTOS_LIBRARY", "/somewhere/Lib.photoslibrary")
    assert apple_photos.default_library() == "/somewhere/Lib.photoslibrary"
    monkeypatch.delenv("PEOPLE_SYNC_PHOTOS_LIBRARY")
    assert apple_photos.default_library().endswith("Pictures/Photos Library.photoslibrary")


def test_a_row_missing_a_field_is_refused():
    row = {"uuid": UUID_A, "name": "X", "display_name": None, "contact_ref": None}
    with pytest.raises(ValueError):
        apple_photos.validate_payload(
            {"format": apple_photos.FORMAT, "complete": True, "people": [row]}
        )
