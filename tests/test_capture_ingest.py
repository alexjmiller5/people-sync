import base64
import hashlib
import json
import sqlite3

import pytest

from people_sync import captures, cli, ledger, parsers, photos, replay, sources


@pytest.fixture
def retained(monkeypatch, tmp_path):
    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    return stored


@pytest.fixture
def synthetic_ledger(monkeypatch):
    """Run real ledger SQL against isolated synthetic rows, never a life database."""
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE people_sync_records (
            id TEXT PRIMARY KEY, source TEXT, source_id TEXT, handle TEXT, name TEXT,
            raw TEXT, follows_me INTEGER, i_follow INTEGER, status TEXT,
            person_id TEXT, suggested_person_id TEXT, first_seen TEXT, last_seen TEXT,
            deleted_at TEXT, updated_at TEXT
        )""")
        db.execute("""CREATE TABLE provenance (
            id TEXT PRIMARY KEY, from_kind TEXT, from_ref TEXT, to_kind TEXT, to_ref TEXT,
            rel TEXT, field TEXT, detail TEXT, asserted_by TEXT, deleted_at TEXT
        )""")

        def sql(query):
            cursor = db.execute(query)
            return [dict(row) for row in cursor.fetchall()] if cursor.description else []

        def insert(table, rows):
            assert table in {"people_sync_records", "people_sync_profiles", "provenance"}
            for row in rows:
                columns = ",".join(row)
                placeholders = ",".join("?" for _ in row)
                db.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(row.values())
                )

        monkeypatch.setattr(ledger.lifedata, "sql", sql)
        monkeypatch.setattr(ledger.lifedata, "insert", insert)
        monkeypatch.setattr(ledger.lifedata, "now_iso", lambda: "2026-09-12T00:00:00.000Z")
        yield db


@pytest.mark.parametrize(
    "status,deleted_at", [("matched", None), ("ignored", None), ("matched", "2026-01-02")]
)
@pytest.mark.parametrize("field", ["Company", "First Name"])
def test_real_ingest_holds_existing_excluded_rows_and_continues_eligible_rows(
    monkeypatch, tmp_path, retained, synthetic_ledger, capsys, status, deleted_at, field
):
    import csv

    db = synthetic_ledger
    db.execute(
        """INSERT INTO people_sync_records
        (id, source, source_id, handle, name, raw, status, person_id,
         suggested_person_id, first_seen, last_seen, deleted_at, updated_at)
        VALUES (?, 'linkedin', 'existing', 'existing', 'Preserved Name', ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "linkedin:existing",
            '{"Company":"Established Studio","Position":"Senior Engineer"}',
            status,
            "a" * 32,
            "b" * 32,
            "2026-01-01",
            "2026-01-02",
            deleted_at,
            "2026-01-02",
        ),
    )
    db.execute("""INSERT INTO people_sync_records (id, source, source_id, raw, status)
        VALUES ('linkedin:safe', 'linkedin', 'safe', '{}', 'ignored')""")
    before = dict(
        db.execute("SELECT * FROM people_sync_records WHERE id='linkedin:existing'").fetchone()
    )
    path = tmp_path / "Connections.csv"
    rows = []
    for sid in ["existing", "safe", "new-safe", "new-filtered"]:
        row = {
            "First Name": "Example",
            "Last Name": "Person",
            "URL": f"https://linkedin.com/in/{sid}",
            "Company": "Refreshed Studio",
            "Position": "Engineer",
            "Email Address": "synthetic@example.test",
        }
        if sid in {"existing", "new-filtered"}:
            row[field] = "3D Studio"
        rows.append(row)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    original = path.read_bytes()
    cli.main(["ingest", "linkedin", "--path", str(path)])
    result = json.loads(capsys.readouterr().out)
    key = next(iter(retained))
    assert (
        dict(
            db.execute("SELECT * FROM people_sync_records WHERE id='linkedin:existing'").fetchone()
        )
        == before
    )
    assert result == {
        "new": 2,
        "updated": 1,
        "held": [
            {
                "record_id": "linkedin:existing",
                "capture_key": key,
                "reason": "permitted-field-exclusions",
            }
        ],
    }
    safe = dict(db.execute("SELECT * FROM people_sync_records WHERE id='linkedin:safe'").fetchone())
    assert json.loads(safe["raw"])["Company"] == "Refreshed Studio"
    assert safe["status"] == "ignored"
    new = dict(
        db.execute("SELECT * FROM people_sync_records WHERE id='linkedin:new-filtered'").fetchone()
    )
    assert json.loads(new["raw"])[field] == "" and new["status"] == "pending"
    assert "hold_existing" not in new and "hold_existing" not in json.loads(new["raw"])
    assert path.read_bytes() == original


def test_replay_hold_marker_follows_exact_role_and_ordinal(monkeypatch, tmp_path, retained):
    # Following has an excluded value at ordinal 0; followers' ordinal 0 belongs
    # to a different safe identity. Superseded input must not hold a safe proposal.
    (tmp_path / "followers.json").write_text(
        json.dumps(
            [
                {"string_list_data": [{"value": "safe"}]},
                {"string_list_data": [{"value": "superseded", "href": "https://foreign.test/"}]},
                {
                    "string_list_data": [
                        {"value": "superseded", "href": "https://instagram.com/superseded"}
                    ]
                },
            ]
        )
    )
    (tmp_path / "following.json").write_text(
        json.dumps(
            {
                "relationships_following": [
                    {"string_list_data": [{"value": "affected", "timestamp": -1}]},
                ]
            }
        )
    )
    capture = captures.capture_export("instagram", tmp_path)
    records = sources.retained_records(capture)
    assert {r.source_id: r.hold_existing for r in records} == {
        "safe": False,
        "superseded": False,
        "affected": True,
    }
    result = replay.replay_capture(capture)
    assert all("hold_existing" not in r for r in replay.normalized(result)["records"])


@pytest.mark.parametrize("last_excluded", [False, True])
def test_duplicate_linkedin_hold_tracks_winning_proposal(
    tmp_path, retained, synthetic_ledger, last_excluded
):
    db = synthetic_ledger
    db.execute("""INSERT INTO people_sync_records (id, source, source_id, name, raw, status)
        VALUES ('linkedin:example', 'linkedin', 'example', 'Preserved Name', '{"Company":"Prior Studio"}', 'matched')""")
    before = dict(db.execute("SELECT * FROM people_sync_records").fetchone())
    companies = (
        ["Refreshed Studio", "3D Studio"] if last_excluded else ["3D Studio", "Refreshed Studio"]
    )
    path = tmp_path / "Connections.csv"
    path.write_text(
        "First Name,Last Name,URL,Company\n"
        + "".join(
            f"Example,Person,https://linkedin.com/in/example,{company}\n" for company in companies
        )
    )
    records = sources.retained_records(captures.capture_export("linkedin", path))
    assert [r.hold_existing for r in records] == [not last_excluded, last_excluded]
    result = ledger.upsert(records)
    after = dict(db.execute("SELECT * FROM people_sync_records").fetchone())
    if last_excluded:
        assert after == before and result["new"] == result["updated"] == 0
        assert result["held"][0]["capture_key"] in retained
    else:
        assert result == {"new": 0, "updated": 1}
        assert json.loads(after["raw"])["Company"] == "Refreshed Studio"
        assert after["status"] == "matched"


@pytest.mark.parametrize("failure", [None, "upload", "readback"])
def test_cli_retains_malformed_export_before_parsing(monkeypatch, tmp_path, retained, failure):
    export = tmp_path / "friends.json"
    original = b'{"friends_v2":[{"name":"Example Person"},{"timestamp":7}]}'
    export.write_bytes(original)
    written = []
    monkeypatch.setattr(ledger, "upsert", lambda rows: written.extend(rows) or {})
    parse = parsers.parse_facebook

    def checked_parse(path):
        assert retained, "parser ran before retention"
        assert failure is None, "parser ran after archive failure"
        return parse(path)

    monkeypatch.setattr(parsers, "parse_facebook", checked_parse)
    if failure == "upload":

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic-secret")

        monkeypatch.setattr(photos, "put_object", fail)
    elif failure == "readback":
        monkeypatch.setattr(photos, "get_object", lambda key: b"wrong bytes")
    if failure:
        with pytest.raises(SystemExit):
            cli.main(["ingest", "facebook", "--path", str(export)])
        assert written == []
    else:
        cli.main(["ingest", "facebook", "--path", str(export)])
        key, body = next(iter(retained.items()))
        capture = json.loads(body)
        assert base64.b64decode(capture["payload"]["files"][0]["data"]) == original
        assert len(written) == 1
        assert written[0].capture_key == key
    assert export.read_bytes() == original


def test_cli_consumes_filtered_linkedin_bytes(monkeypatch, tmp_path, retained):
    export = tmp_path / "Connections.csv"
    original = b"First Name,Last Name,URL,Email Address\nExample,Person,https://linkedin.com/in/example,synthetic@example.com\n"
    export.write_bytes(original)
    written = []
    monkeypatch.setattr(ledger, "upsert", lambda rows: written.extend(rows) or {})
    cli.main(["ingest", "linkedin", "--path", str(export)])
    assert len(written) == 1
    assert "Email Address" not in written[0].raw
    assert b"synthetic@example.com" not in b"".join(retained.values())
    assert export.read_bytes() == original


def google_run(cmd):
    if cmd[2] == "list":
        if "--page" in cmd:
            raise RuntimeError("synthetic-secret")
        return json.dumps(
            {
                "contacts": [
                    {"resource": "people/c123456789012", "phone": "5550109999"},
                    {"email": "synthetic@example.com"},
                    {"resource": "people/c2"},
                ],
                "nextPageToken": "synthetic-secret",
            }
        )
    if cmd[3] == "people/c2":
        raise RuntimeError("synthetic-secret")
    return json.dumps(
        {
            "resourceName": "people/c123456789012",
            "names": [
                {"displayName": "Other Name"},
                {
                    "displayName": "Example Person",
                    "metadata": {"primary": True, "email": "synthetic@example.com"},
                },
            ],
            "birthdays": [{"date": {"year": 2000, "month": 1, "day": 2}}],
            "organizations": [{"name": "Studio 54", "phoneNumbers": ["5550109999"]}],
            "photos": [{"url": "https://example.com/photo.jpg"}],
            "emailAddresses": [{"value": "synthetic@example.com"}],
        }
    )


def test_google_preserves_source_values_and_partial_outcomes(monkeypatch, retained):
    monkeypatch.setattr(sources, "_run", google_run)
    payload = sources.collect_google()
    assert payload["complete"] is False
    assert payload["pages"][0]["has_next"] is True
    assert [e["ordinal"] for e in payload["pages"][0]["entries"]] == [0, 1, 2]
    assert payload["pages"][1]["status"] == "failed"
    assert payload["pages"][0]["entries"][2]["status"] == "failed"
    body = captures.encode(payload)
    for forbidden in (b"5550109999", b"synthetic@example.com", b"synthetic-secret"):
        assert forbidden not in body
    person = payload["pages"][0]["entries"][0]["person"]
    assert len(person["names"]) == 2
    assert person["birthdays"][0]["date"]["year"] == 2000
    capture = sources.contacts_capture("google", payload)
    assert capture["completeness"] == "partial"
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert result["records"][0]["name"] == "Example Person"
    assert result["records"][0]["source_id"] == "people/c123456789012"
    assert result["records"][0]["raw"]["org"] == [{"name": "Studio 54"}]
    assert result["limitations"]


@pytest.mark.parametrize("source", ["google", "apple"])
def test_contacts_archive_failure_blocks_parse_and_ledger(monkeypatch, retained, source):
    if source == "google":
        monkeypatch.setattr(sources, "_run", google_run)
    else:
        monkeypatch.setattr(sources, "_db_paths", lambda: [])
    monkeypatch.setattr(photos, "get_object", lambda key: b"mismatch")
    monkeypatch.setattr(
        sources, f"parse_{source}", lambda payload: pytest.fail("parsed before verification")
    )
    monkeypatch.setattr(ledger, "upsert", lambda rows: pytest.fail("wrote before verification"))
    with pytest.raises(SystemExit):
        cli.main(["ingest", source])


def test_apple_source_epoch_and_offset_replay_without_local_timezone(monkeypatch, retained):
    monkeypatch.setattr(sources, "_db_paths", lambda: ["/synthetic/one", "/synthetic/two"])

    def run(cmd):
        if "two" in cmd[2]:
            raise RuntimeError("private-path synthetic-secret")
        return json.dumps(
            [
                {
                    "id": "AAAA1111-0000-0000-0000-000000000000:ABPerson",
                    "first": "Example",
                    "last": "Person",
                    "birthday_epoch": 0,
                    "birthday_offset_seconds": -18000,
                    "phone_count": 2,
                    "email_count": 0,
                },
                {"id": None, "org": "Example Org"},
            ]
        )

    monkeypatch.setattr(sources, "_run", run)
    payload = sources.collect_apple()
    assert payload["complete"] is False
    assert payload["databases"][1]["status"] == "failed"
    assert len(payload["databases"][0]["entries"]) == 2
    row = payload["databases"][0]["entries"][0]["row"]
    assert row["birthday_epoch"] == 0
    assert row["phone_count"] == 2
    assert "birthday" not in row
    monkeypatch.setattr(sources, "_run", lambda cmd: pytest.fail("offline replay ran command"))
    result = replay.replay_capture(sources.contacts_capture("apple", payload))
    assert result["status"] == "ok"
    assert len(result["records"]) == 1
    assert result["records"][0]["raw"]["birthday"] == "2000-12-31"
    assert result["records"][0]["raw"]["has_phone"] is True
    assert "private-path" not in json.dumps(payload)


@pytest.mark.parametrize(
    "injection",
    [
        {"emailAddresses": [{"value": "synthetic@example.com"}]},
        {"names": [{"displayName": "synthetic@example.com"}]},
        {"photos": [{"url": "https://example.com/photo?access_token=synthetic-secret"}]},
        {"organizations": [{"name": "123 Example Street"}]},
        {"birthdays": [{"date": {"year": "5550109999"}}]},
    ],
)
def test_checksum_valid_contacts_cannot_bypass_privacy(monkeypatch, retained, injection):
    monkeypatch.setattr(sources, "_run", google_run)
    capture = sources.contacts_capture("google", sources.collect_google())
    capture["payload"]["pages"][0]["entries"][0]["person"].update(injection)
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    with pytest.raises(photos.ArchiveError):
        captures.retain(capture)
    assert retained == {}
    assert replay.replay_capture(capture)["status"] == "invalid"


def test_invalid_inventory_has_safe_distinct_file_identifiers(tmp_path, capsys):
    directory = tmp_path / "captures"
    directory.mkdir()
    for name in ("a" * 32, "b" * 32, "synthetic@example.com"):
        (directory / f"{name}.json").write_text("not json")
    cli.main(["captures", "--state-dir", str(tmp_path)])
    output = capsys.readouterr().out
    rows = json.loads(output)["captures"]
    assert {r.get("file_id") for r in rows if r.get("file_id")} == {"a" * 32, "b" * 32}
    assert "synthetic@example.com" not in output


def test_ingest_never_rereads_original_after_retention(monkeypatch, tmp_path, retained):
    export = tmp_path / "friends.json"
    export.write_text('{"friends_v2":[{"name":"Example Person"}]}')
    written = []

    def upload(key, body, **kwargs):
        retained[key] = body
        export.write_text('{"friends_v2":[{"name":"synthetic@example.com"}]}')

    monkeypatch.setattr(photos, "put_object", upload)
    monkeypatch.setattr(ledger, "upsert", lambda rows: written.extend(rows) or {})
    cli.main(["ingest", "facebook", "--path", str(export)])
    assert written[0].name == "Example Person"


def test_non_ok_replay_keeps_capture_but_never_writes(monkeypatch, tmp_path, retained):
    export = tmp_path / "friends.json"
    export.write_text('{"friends_v2":[{"name":"Example Person"}]}')
    monkeypatch.setattr(
        parsers,
        "parse_facebook",
        lambda path: (_ for _ in ()).throw(ValueError("synthetic-secret")),
    )
    monkeypatch.setattr(ledger, "upsert", lambda rows: pytest.fail("wrote failed replay"))
    with pytest.raises(SystemExit, match="replay could not be verified"):
        cli.main(["ingest", "facebook", "--path", str(export)])
    assert len(retained) == 1


def test_capture_key_seam_is_not_serialized_to_ledger(monkeypatch):
    rows = []
    monkeypatch.setattr(ledger.lifedata, "sql", lambda query: [])
    monkeypatch.setattr(
        ledger.lifedata,
        "insert",
        lambda table, batch: rows.extend(batch) if table == "people_sync_records" else None,
    )
    record = ledger.Record(
        "google_contacts",
        "people/c1",
        None,
        "Example Person",
        {},
        capture_key="profiles/synthetic/capture.json",
    )
    ledger.upsert([record])
    assert "capture_key" not in rows[0]
    assert json.loads(rows[0]["raw"]) == {}
    assert "profiles/synthetic" not in json.dumps(rows)


def test_google_cycle_and_bad_declared_value_remain_explicit(monkeypatch, retained, capsys):
    def run(cmd):
        if cmd[2] == "list":
            return '{"contacts":[{"resource":"people/c1"}],"nextPageToken":"synthetic-secret"}'
        return '{"names":[{"displayName":"synthetic@example.com"}]}'

    monkeypatch.setattr(sources, "_run", run)
    payload = sources.collect_google()
    assert payload["complete"] is False
    assert len(payload["pages"]) == 3
    assert payload["pages"][-1]["reason"] == "pagination-cycle"
    assert payload["pages"][0]["entries"][0]["reason"] == "invalid-source"
    assert "synthetic" not in json.dumps(payload) + capsys.readouterr().out
    assert replay.replay_capture(sources.contacts_capture("google", payload))["records"] == []


def apple_payload(row):
    return {
        "format": "apple-contacts-v1",
        "complete": True,
        "databases": [
            {
                "ordinal": 0,
                "status": "complete",
                "entries": [{"ordinal": 0, "status": "ok", "row": row}],
            }
        ],
    }


@pytest.mark.parametrize(
    "row",
    [
        {"first": "synthetic@example.com"},
        {"first": "5550109999"},
        {"title": "123 Example Street"},
        {"phone": "5550109999"},
        {"birthday_epoch": 0},
        {"birthday_epoch": 1e100, "birthday_offset_seconds": 0},
        {"phone_count": "5550109999"},
        {"birthday_offset_seconds": True},
    ],
)
def test_apple_revalidation_rejects_unsafe_or_ambiguous_inputs(row, retained):
    capture = sources.contacts_capture("apple", apple_payload({}))
    capture["payload"]["databases"][0]["entries"][0]["row"] = row
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"
    with pytest.raises(photos.ArchiveError):
        captures.retain(capture)
    assert not retained


def test_contacts_replay_is_pure_and_yearless_birthday_is_deterministic(monkeypatch):
    from datetime import datetime
    from people_sync import lifedata, notion_people

    epoch = (datetime(1604, 5, 2) - datetime(2001, 1, 1)).total_seconds()
    capture = sources.contacts_capture(
        "apple",
        apple_payload(
            {
                "id": "AAAA1111-0000-0000-0000-000000000000:ABPerson",
                "nick": "Example",
                "birthday_epoch": epoch,
                "birthday_offset_seconds": 0,
            }
        ),
    )
    original = captures.encode(capture)

    def forbidden(*args, **kwargs):
        pytest.fail("offline replay caused an external effect")

    for module, names in (
        (sources, ["_run", "_db_paths"]),
        (photos, ["get_object", "put_object", "fetch_url_photo"]),
        (lifedata, ["sql", "insert"]),
        (notion_people, ["create_stub"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    first = replay.replay_capture(capture)
    assert first == replay.replay_capture(capture)
    assert captures.encode(capture) == original
    assert first["records"][0]["raw"]["birthday"] == "--05-02"
    assert first["records"][0]["name"] == "Example"


def test_apple_query_uses_source_epoch_and_never_selects_contact_values():
    import sqlite3

    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE ZABCDRECORD (Z_PK INTEGER, ZUNIQUEID TEXT, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZMIDDLENAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT, ZJOBTITLE TEXT, ZBIRTHDAY REAL);
            CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
            CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
            INSERT INTO ZABCDRECORD VALUES (1, NULL, NULL, NULL, NULL, 'Example', NULL, NULL, 0.5);
            INSERT INTO ZABCDPHONENUMBER VALUES (1, '5550109999');
            INSERT INTO ZABCDEMAILADDRESS VALUES (1, 'synthetic@example.com');
        """)
        db.row_factory = sqlite3.Row
        row = dict(db.execute(sources._APPLE_QUERY).fetchone())
    assert row["birthday_epoch"] == 0.5
    assert type(row["birthday_offset_seconds"]) is int
    assert row["phone_count"] == row["email_count"] == 1
    assert "synthetic@example.com" not in json.dumps(row)
    assert "5550109999" not in json.dumps(row)


def test_collection_policy_cannot_be_removed_from_contacts_capture(monkeypatch):
    monkeypatch.setattr(sources, "_run", google_run)
    capture = sources.contacts_capture("google", sources.collect_google())
    capture["exclusions"] = []
    assert replay.replay_capture(capture)["status"] == "invalid"
