"""WhatsApp snapshot evidence: synthetic metadata only, never a native database."""

import base64
import hashlib
import json
import os
import sqlite3

import pytest

from people_sync import captures, cli, lifedata, photos, replay, whatsapp
from tests.test_capture_ingest import synthetic_ledger  # noqa: F401

JPEG = b"\xff\xd8\xff\xe0" + bytes(20) + b"\xff\xd9"
PHONE_A = "15550001111@s.whatsapp.net"
LID_A = "200000000001@lid"
PHONE_B = "15550002222@s.whatsapp.net"
LID_C = "200000000003@lid"
SELF = "15550009999@s.whatsapp.net"


def _snapshot(path, *, phone_only=False, escape_dir=None):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE ZWACHATSESSION (Z_PK INT, ZREMOVED INT, ZSESSIONTYPE INT, "
        "ZCONTACTIDENTIFIER TEXT, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, "
        "ZLASTMESSAGEDATE NUM, ZCONTACTABID INT)"
    )
    db.execute(
        "CREATE TABLE ZWAPROFILEPICTUREITEM (Z_PK INT, ZREQUESTDATE NUM, ZJID TEXT, "
        "ZPATH TEXT, ZPICTUREID TEXT)"
    )
    db.execute("CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INT, ZJID TEXT, ZPUSHNAME TEXT)")
    sessions = [
        (1, 0, 0, LID_A, PHONE_A, "Ada Example", 800000000.5, 0),
        (2, 0, 0, None, SELF, "Me", 800000001.0, 0),
        (3, 0, 1, None, "1234@g.us", "Group", 1.0, 0),
        (4, 0, 3, "9@lid.status", "1@status", "Status", 1.0, 0),
        (5, 0, 2, None, "status@broadcast", "Broadcast", 1.0, 0),
        (6, 0, 4, None, "5678@g.us", "Community", 1.0, 0),
        (7, 1, 0, "300000000009@lid", "15550003333@s.whatsapp.net", "Removed", 1.0, 0),
    ]
    if phone_only:
        sessions.append((8, 0, 0, None, PHONE_B, "+1 555 000 2222", None, 0))
    if escape_dir:
        sessions.append((9, 0, 0, LID_C, None, "Cy Escape", 1.0, 0))
    db.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?,?,?)", sessions)
    pictures = [
        (1, 700000000.0, LID_A, None, "100"),
        (2, 800000002.0, PHONE_A, "Media/Profile/15550001111-111", "111"),
        (3, 800000003.0, PHONE_B, "Media/Profile/15550002222-222", "222"),
    ]
    if escape_dir == "traversal":
        pictures.append((4, 1.0, LID_C, "../outside/secret", "333"))
    if escape_dir == "symlink":
        pictures.append((4, 1.0, LID_C, "Media/Profile/link", "333"))
    db.executemany("INSERT INTO ZWAPROFILEPICTUREITEM VALUES (?,?,?,?,?)", pictures)
    db.executemany(
        "INSERT INTO ZWAPROFILEPUSHNAME VALUES (?,?,?)",
        [(1, LID_A, "ada.push"), (2, PHONE_B, "Bea Push"), (3, SELF, "self.push")],
    )
    db.commit()
    db.close()


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "snap.sqlite"
    _snapshot(path)
    media = tmp_path / "media"
    (media / "Media/Profile").mkdir(parents=True)
    (media / "Media/Profile/15550001111-111.thumb").write_bytes(JPEG)
    return {"path": path, "media": media, "state": tmp_path / "state"}


def _collect(snapshot, **kw):
    return whatsapp.collect(
        snapshot["path"], snapshot["media"], state_dir=snapshot["state"], self_id=SELF, **kw
    )


def test_collect_builds_privacy_filtered_capture(snapshot):
    capture = _collect(snapshot)
    text = captures.encode(capture).decode()
    for leak in ("15550", "@s.whatsapp.net", "Media/Profile", "Me", "self.push", "Removed"):
        assert leak not in text
    captures.validate(capture)
    assert capture["kind"] == "contacts" and capture["source"] == "whatsapp"
    assert capture["completeness"] == "privacy-filtered"
    assert capture["exclusions"] == [whatsapp.POLICY]
    payload = capture["payload"]
    assert payload["complete"] is True
    assert payload["excluded"] == {
        "self": 1,
        "group": 1,
        "status": 1,
        "broadcast": 1,
        "community": 1,
        "removed": 1,
        "other": 0,
        "names": 0,
    }
    [a] = payload["counterparts"]
    assert a["source_id"] == "lid-200000000001" and a["id_kind"] == "lid"
    assert a["partner_name"] == "Ada Example" and a["push_name"] == "ada.push"
    assert a["last_message_epoch"] == 800000000.5
    photo = a["photo"]
    assert photo["status"] == "retained" and photo["resolution"] == "thumbnail"
    assert photo["picture_id"] == "111" and photo["format"] == "jpeg"
    assert photo["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert base64.b64decode(photo["data"]) == JPEG


def test_phone_only_counterpart_gets_stable_local_id(tmp_path, snapshot):
    _snapshot(snapshot["path"].with_name("b.sqlite"), phone_only=True)
    snapshot["path"] = snapshot["path"].with_name("b.sqlite")
    first = _collect(snapshot)
    second = _collect(snapshot)
    b = [c for c in first["payload"]["counterparts"] if c["id_kind"] == "local"]
    assert len(b) == 1 and b[0]["source_id"].startswith("local-")
    assert second["payload"]["counterparts"][1]["source_id"] == b[0]["source_id"]
    assert b[0]["partner_name"] is None and b[0]["push_name"] == "Bea Push"
    assert first["payload"]["excluded"]["names"] == 1
    assert b[0]["photo"]["status"] == "missing-file" and "data" not in b[0]["photo"]
    mapping = snapshot["state"] / "whatsapp-ids.json"
    assert oct(mapping.stat().st_mode & 0o777) == "0o600"
    assert PHONE_B in json.loads(mapping.read_text())
    assert "15550002222" not in captures.encode(first).decode()


@pytest.mark.parametrize("escape", ["traversal", "symlink"])
def test_media_escape_is_refused_unread(tmp_path, snapshot, escape):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = JPEG + b"secret"
    (outside / "secret").write_bytes(secret)
    (outside / "secret.thumb").write_bytes(secret)
    if escape == "symlink":
        os.symlink(outside / "secret", snapshot["media"] / "Media/Profile/link")
    _snapshot(snapshot["path"].with_name("c.sqlite"), escape_dir=escape)
    snapshot["path"] = snapshot["path"].with_name("c.sqlite")
    capture = _collect(snapshot)
    c = [x for x in capture["payload"]["counterparts"] if x["source_id"] == "lid-200000000003"]
    assert c[0]["photo"]["status"] == "refused-path"
    assert hashlib.sha256(secret).hexdigest() not in captures.encode(capture).decode()


def test_invalid_image_and_no_path_are_distinguished(snapshot):
    (snapshot["media"] / "Media/Profile/15550001111-111.thumb").write_bytes(b"not an image")
    assert _collect(snapshot)["payload"]["counterparts"][0]["photo"]["status"] == "invalid-image"
    (snapshot["media"] / "Media/Profile/15550001111-111.thumb").unlink()
    assert _collect(snapshot)["payload"]["counterparts"][0]["photo"]["status"] == "missing-file"


def test_self_id_required_and_snapshot_untouched(snapshot):
    before = snapshot["path"].read_bytes()
    with pytest.raises(ValueError):
        whatsapp.collect(snapshot["path"], snapshot["media"], state_dir=snapshot["state"])
    _collect(snapshot)
    assert snapshot["path"].read_bytes() == before
    assert not snapshot["path"].with_name("snap.sqlite-wal").exists()


def test_unknown_schema_rejected(tmp_path, snapshot):
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(ValueError):
        whatsapp.collect(other, snapshot["media"], state_dir=snapshot["state"], self_id=SELF)


def test_replay_is_pure_and_offline(snapshot):
    capture = _collect(snapshot)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    [record] = result["records"]
    assert record["source"] == "whatsapp" and record["source_id"] == "lid-200000000001"
    assert record["name"] == "Ada Example" and record["handle"] is None
    assert record["raw"] == {
        "partner_name": "Ada Example",
        "push_name": "ada.push",
        "last_message_at": "2026-05-09T06:13:20.500Z",
        "id_kind": "lid",
        "contact_refs": [],
    }
    assert replay.normalized(result) == replay.normalized(replay.replay_capture(capture))
    assert result["captured_at"] == capture["captured_at"]
    assert result["input_sha256"] == capture["payload_sha256"]


@pytest.fixture
def estate(monkeypatch, tmp_path, synthetic_ledger):  # noqa: F811 - shared pytest fixture
    db = synthetic_ledger
    db.execute("""CREATE TABLE people_sync_profiles (
        id TEXT PRIMARY KEY, record_id TEXT, platform TEXT, profile_url TEXT,
        platform_id TEXT, display_name TEXT, bio TEXT, location TEXT, hometown TEXT,
        education TEXT, work TEXT, birthday TEXT, links TEXT, is_private INTEGER,
        is_verified INTEGER, follower_count INTEGER, following_count INTEGER,
        mutual_count INTEGER, avatar_r2_key TEXT, avatar_sha256 TEXT, raw_r2_key TEXT,
        scraped_at TEXT, deleted_at TEXT
    )""")
    events, stored = [], {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    def put(key, data, **kw):
        events.append(("put", key))
        stored[key] = data

    real_insert = lifedata.insert
    monkeypatch.setattr(photos, "put_object", put)
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    monkeypatch.setattr(
        lifedata, "insert", lambda t, rows: (events.append(("insert", t)), real_insert(t, rows))
    )
    return {"db": db, "events": events, "stored": stored}


def _rows(db, query):
    return [dict(r) for r in db.execute(query)]


def test_ingest_writes_pending_evidence_after_retention(snapshot, estate):
    db, events, stored = estate["db"], estate["events"], estate["stored"]
    report = whatsapp.ingest(
        snapshot["path"], snapshot["media"], state_dir=snapshot["state"], self_id=SELF
    )
    assert report["new"] == 1 and report["counterparts"] == 1 and report["photos"] == 1
    [record] = _rows(db, "SELECT * FROM people_sync_records")
    assert record["id"] == "whatsapp:lid-200000000001" and record["status"] == "pending"
    assert record["handle"] is None and record["name"] == "Ada Example"
    assert "15550" not in json.dumps(record)
    [profile] = _rows(db, "SELECT * FROM people_sync_profiles")
    sha = hashlib.sha256(JPEG).hexdigest()
    assert profile["platform"] == "whatsapp" and profile["display_name"] == "Ada Example"
    assert profile["profile_url"] == "" and profile["platform_id"] == "lid-200000000001"
    assert profile["avatar_r2_key"] == f"photos/records/whatsapp/lid-200000000001-{sha}.jpg"
    assert profile["avatar_sha256"] == sha and stored[profile["avatar_r2_key"]] == JPEG
    assert profile["raw_r2_key"].startswith("profiles/whatsapp/captures/")
    # Retention (capture, then photo bytes) precedes every estate write.
    kinds = [k for k, _ in events]
    assert kinds.index("insert") > kinds.index("put") and kinds[:2] == ["put", "put"]
    edges = _rows(db, "SELECT * FROM provenance ORDER BY to_kind")
    assert [(e["to_kind"], e["from_ref"]) for e in edges] == [
        ("people_sync_profiles", profile["raw_r2_key"]),
        ("people_sync_records", profile["raw_r2_key"]),
    ]


def test_ingest_repeat_preserves_decisions_and_existing_photo(snapshot, estate):
    db, events = estate["db"], estate["events"]
    args = (snapshot["path"], snapshot["media"])
    kw = {"state_dir": snapshot["state"], "self_id": SELF}
    whatsapp.ingest(*args, **kw)
    db.execute(
        "UPDATE people_sync_records SET status = 'matched', person_id = 'p1' "
        "WHERE id = 'whatsapp:lid-200000000001'"
    )
    first = _rows(db, "SELECT avatar_r2_key, avatar_sha256 FROM people_sync_profiles")[0]
    (snapshot["media"] / "Media/Profile/15550001111-111.thumb").unlink()
    events.clear()
    report = whatsapp.ingest(*args, **kw)
    assert report["new"] == 0 and report["updated"] == 1 and report["photos"] == 0
    [record] = _rows(db, "SELECT status, person_id FROM people_sync_records")
    assert record == {"status": "matched", "person_id": "p1"}
    [profile] = _rows(db, "SELECT avatar_r2_key, avatar_sha256 FROM people_sync_profiles")
    assert profile == first
    assert [k for k, _ in events if k == "put"] == ["put"]  # the new capture only
    assert len(_rows(db, "SELECT id FROM provenance")) == 4  # two captures x two rows


def test_cli_ingest_whatsapp(snapshot, estate, capsys):
    cli.main(
        [
            "ingest",
            "whatsapp",
            "--snapshot",
            str(snapshot["path"]),
            "--media-dir",
            str(snapshot["media"]),
            "--self-id",
            SELF,
            "--state-dir",
            str(snapshot["state"]),
            "--no-contacts",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["new"] == 1 and out["excluded"]["self"] == 1


def test_cli_ingest_uses_the_address_book_lookup_by_default(snapshot, estate, mocker, capsys):
    lookup = mocker.patch.object(whatsapp, "contact_lookup", return_value=lambda digits: [])
    cli.main(
        [
            "ingest",
            "whatsapp",
            "--snapshot",
            str(snapshot["path"]),
            "--media-dir",
            str(snapshot["media"]),
            "--self-id",
            SELF,
            "--state-dir",
            str(snapshot["state"]),
        ]
    )
    lookup.assert_called_once()
    assert json.loads(capsys.readouterr().out)["counterparts"] == 1


def test_ingest_same_picture_twice_uploads_once(snapshot, estate):
    kw = {"state_dir": snapshot["state"], "self_id": SELF}
    whatsapp.ingest(snapshot["path"], snapshot["media"], **kw)
    estate["events"].clear()
    report = whatsapp.ingest(snapshot["path"], snapshot["media"], **kw)
    assert report["photos"] == 0
    puts = [key for kind, key in estate["events"] if kind == "put"]
    assert len(puts) == 1 and puts[0].startswith("profiles/whatsapp/captures/")


def test_phone_lookup_retains_contact_pointers_never_the_number(snapshot):
    seen = []

    def contacts(digits):
        seen.append(digits)
        return {
            "5550001111": [
                "google_contacts:people/c1",
                "apple_contacts:11111111-1111-1111-1111-111111111111:ABPerson",
            ]
        }.get(digits, [])

    capture = _collect(snapshot, contacts=contacts)
    [a] = capture["payload"]["counterparts"]
    assert a["contact_refs"] == [
        "apple_contacts:11111111-1111-1111-1111-111111111111:ABPerson",
        "google_contacts:people/c1",
    ]
    assert seen == ["5550001111"] and "5550001111" not in captures.encode(capture).decode()
    [record] = replay.replay_capture(capture)["records"]
    assert record["raw"]["contact_refs"] == a["contact_refs"]
    # a capture written before cross-references existed still validates
    del capture["payload"]["counterparts"][0]["contact_refs"]
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "ok"


def test_contact_lookup_bridges_apple_rows_to_google_records(mocker):
    from people_sync import sources

    mocker.patch.object(
        sources,
        "phone_index",
        return_value={
            "5550001111": [
                {"apple": "11111111-1111-1111-1111-111111111111:ABPerson", "external": "abc123"}
            ]
        },
    )
    mocker.patch.object(
        sources, "google_contact_ids", return_value={"abc123": "google_contacts:people/c9"}
    )
    lookup = whatsapp.contact_lookup()
    assert lookup("5550001111") == [
        "apple_contacts:11111111-1111-1111-1111-111111111111:ABPerson",
        "google_contacts:people/c9",
    ]
    assert lookup("0000000000") == []
