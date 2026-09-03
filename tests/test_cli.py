import json
import sqlite3

import pytest

from contact_sync import cli


def test_ingest_instagram_reads_directory_and_upserts(mocker, capsys):
    parse = mocker.patch("contact_sync.parsers.parse_instagram", return_value=["rec"])
    upsert = mocker.patch("contact_sync.ledger.upsert", return_value={"new": 1, "updated": 0})

    cli.main(["ingest", "instagram", "--path", "/tmp/export"])

    parse.assert_called_once_with("/tmp/export/followers.json", "/tmp/export/following.json")
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 1, "updated": 0}


def test_ingest_facebook_reads_file_and_upserts(mocker, capsys):
    parse = mocker.patch("contact_sync.parsers.parse_facebook", return_value=["rec"])
    upsert = mocker.patch("contact_sync.ledger.upsert", return_value={"new": 0, "updated": 1})

    cli.main(["ingest", "facebook", "--path", "/tmp/your_friends.json"])

    parse.assert_called_once_with("/tmp/your_friends.json")
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 0, "updated": 1}


def test_ingest_snapchat_reads_file_and_upserts(mocker, capsys):
    parse = mocker.patch("contact_sync.parsers.parse_snapchat", return_value=["rec"])
    upsert = mocker.patch("contact_sync.ledger.upsert", return_value={"new": 1, "updated": 1})

    cli.main(["ingest", "snapchat", "--path", "/tmp/friends.json"])

    parse.assert_called_once_with("/tmp/friends.json")
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 1, "updated": 1}


def test_ingest_linkedin_reads_file_and_upserts(mocker, capsys):
    parse = mocker.patch("contact_sync.parsers.parse_linkedin", return_value=["rec"])
    upsert = mocker.patch("contact_sync.ledger.upsert", return_value={"new": 3, "updated": 0})

    cli.main(["ingest", "linkedin", "--path", "/tmp/Connections.csv"])

    parse.assert_called_once_with("/tmp/Connections.csv")
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 3, "updated": 0}


def test_ingest_google_calls_fetch_with_no_path_and_upserts(mocker, capsys):
    fetch = mocker.patch("contact_sync.sources.fetch_google", return_value=["rec"])
    upsert = mocker.patch("contact_sync.ledger.upsert", return_value={"new": 2, "updated": 0})

    cli.main(["ingest", "google"])

    fetch.assert_called_once_with()
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 2, "updated": 0}


def test_ingest_apple_calls_fetch_with_no_path_and_upserts(mocker, capsys):
    fetch = mocker.patch("contact_sync.sources.fetch_apple", return_value=["rec"])
    upsert = mocker.patch("contact_sync.ledger.upsert", return_value={"new": 0, "updated": 4})

    cli.main(["ingest", "apple"])

    fetch.assert_called_once_with()
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 0, "updated": 4}


def test_match_runs_and_prints_result(mocker, capsys):
    run = mocker.patch(
        "contact_sync.match.run_match",
        return_value={"auto": 1, "suggested": 2, "left_pending": 3},
    )

    cli.main(["match"])

    run.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == {"auto": 1, "suggested": 2, "left_pending": 3}


def test_cmd_queue_runs_fixed_query_and_prints_result(mocker, capsys):
    rows = [
        {
            "id": "instagram:alice",
            "source": "instagram",
            "handle": "alice",
            "name": None,
            "suggested_person_id": "p1",
            "suggested_name": "Alice Smith",
        }
    ]
    sql = mocker.patch("contact_sync.lifedata.sql", return_value=rows)

    cli.main(["queue"])

    sql.assert_called_once_with(cli.QUEUE_QUERY)
    assert json.loads(capsys.readouterr().out) == rows


def test_queue_query_orders_suggested_first_and_joins_person_name():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE contact_records (id TEXT, source TEXT, handle TEXT, name TEXT, "
        "status TEXT, suggested_person_id TEXT)"
    )
    conn.execute("CREATE TABLE people (id TEXT, name TEXT)")
    conn.execute("INSERT INTO people VALUES ('p1', 'Suggested Person')")
    conn.executemany(
        "INSERT INTO contact_records VALUES (?,?,?,?,?,?)",
        [
            ("instagram:noone", "instagram", "noone", None, "pending", None),
            ("instagram:alice", "instagram", "alice", None, "pending", "p1"),
            ("instagram:done", "instagram", "done", None, "matched", None),
        ],
    )

    rows = conn.execute(cli.QUEUE_QUERY).fetchall()

    assert [r[0] for r in rows] == ["instagram:alice", "instagram:noone"]
    assert rows[0][-1] == "Suggested Person"
    assert rows[1][-1] is None


def test_new_person_creates_stub_inserts_dashstripped_row_and_prints_id(
    mocker, monkeypatch, capsys
):
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    create = mocker.patch(
        "contact_sync.notion_people.create_stub",
        return_value="1a80-3953-a8af-80ab-000bfe407316",
    )
    insert = mocker.patch("contact_sync.lifedata.insert")

    cli.main(["new-person", "--name", "Test Person"])

    create.assert_called_once_with("Test Person")
    table, rows = insert.call_args.args
    assert table == "people"
    assert rows == [{"id": "1a803953a8af80ab000bfe407316", "name": "Test Person"}]
    assert capsys.readouterr().out.strip() == "1a803953a8af80ab000bfe407316"


def test_new_person_exits_with_clear_error_when_token_missing(mocker, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    create = mocker.patch("contact_sync.notion_people.create_stub")
    insert = mocker.patch("contact_sync.lifedata.insert")

    with pytest.raises(SystemExit):
        cli.main(["new-person", "--name", "Test Person"])

    create.assert_not_called()
    insert.assert_not_called()


def test_photos_store_prints_r2_key_on_new_photo(mocker, tmp_path, capsys):
    file_path = tmp_path / "avatar.jpg"
    file_path.write_bytes(b"image-bytes")
    store = mocker.patch(
        "contact_sync.photos.store_photo",
        return_value="photos/people/p1/instagram-abcd1234.jpg",
    )

    cli.main(
        ["photos", "store", "--person", "p1", "--platform", "instagram", "--file", str(file_path)]
    )

    store.assert_called_once_with("p1", "instagram", b"image-bytes", "jpg")
    assert capsys.readouterr().out.strip() == "photos/people/p1/instagram-abcd1234.jpg"


def test_photos_store_prints_duplicate_when_store_returns_none(mocker, tmp_path, capsys):
    file_path = tmp_path / "avatar.png"
    file_path.write_bytes(b"image-bytes")
    mocker.patch("contact_sync.photos.store_photo", return_value=None)

    cli.main(
        ["photos", "store", "--person", "p1", "--platform", "instagram", "--file", str(file_path)]
    )

    assert capsys.readouterr().out.strip() == "duplicate"
