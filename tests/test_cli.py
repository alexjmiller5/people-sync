import csv
import json
import sqlite3

import pytest

from people_sync import cli
from people_sync.scrape import cdp
from people_sync.scrape import login as scrape_login


@pytest.mark.parametrize("source", ["instagram", "facebook", "snapchat", "linkedin"])
def test_ingest_exports_replay_verified_bytes(source, monkeypatch, tmp_path, capsys):
    from people_sync import photos, ledger

    stored, written = {}, []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    monkeypatch.setattr(ledger, "upsert", lambda rows: written.extend(rows) or {"new": len(rows)})
    path = tmp_path / "export.json"
    if source == "instagram":
        path = tmp_path
        (path / "followers.json").write_text('[{"string_list_data":[{"value":"example"}]}]')
        (path / "following.json").write_text('{"relationships_following":[]}')
    else:
        path.write_text(
            {
                "facebook": '{"friends_v2":[{"name":"Example Person"}]}',
                "snapchat": '{"Friends":[{"Username":"example"}]}',
                "linkedin": "First Name,Last Name,URL\nExample,Person,https://linkedin.com/in/example\n",
            }[source]
        )
    cli.main(["ingest", source, "--path", str(path)])
    assert json.loads(capsys.readouterr().out) == {"new": 1}
    assert len(stored) == 1
    assert written[0].capture_key in stored
    assert written[0].source == source


def test_ingest_google_calls_fetch_with_no_path_and_upserts(mocker, capsys):
    fetch = mocker.patch("people_sync.sources.fetch_google", return_value=["rec"])
    upsert = mocker.patch("people_sync.ledger.upsert", return_value={"new": 2, "updated": 0})

    cli.main(["ingest", "google"])

    fetch.assert_called_once_with()
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 2, "updated": 0}


def test_ingest_apple_calls_fetch_with_no_path_and_upserts(mocker, capsys):
    fetch = mocker.patch("people_sync.sources.fetch_apple", return_value=["rec"])
    upsert = mocker.patch("people_sync.ledger.upsert", return_value={"new": 0, "updated": 4})

    cli.main(["ingest", "apple"])

    fetch.assert_called_once_with()
    upsert.assert_called_once_with(["rec"])
    assert json.loads(capsys.readouterr().out) == {"new": 0, "updated": 4}


def test_match_runs_and_prints_result(mocker, capsys):
    run = mocker.patch(
        "people_sync.match.run_match",
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
    sql = mocker.patch("people_sync.lifedata.sql", return_value=rows)

    cli.main(["queue"])

    sql.assert_called_once_with(cli.QUEUE_QUERY)
    assert json.loads(capsys.readouterr().out) == rows


def test_queue_query_orders_suggested_first_and_joins_person_name():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE people_sync_records (id TEXT, source TEXT, handle TEXT, name TEXT, "
        "status TEXT, suggested_person_id TEXT)"
    )
    conn.execute("CREATE TABLE people (id TEXT, name TEXT)")
    conn.execute("INSERT INTO people VALUES ('p1', 'Suggested Person')")
    conn.executemany(
        "INSERT INTO people_sync_records VALUES (?,?,?,?,?,?)",
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
        "people_sync.notion_people.create_stub",
        return_value="1a80-3953-a8af-80ab-000bfe407316",
    )
    insert = mocker.patch("people_sync.lifedata.insert")

    cli.main(["new-person", "--name", "Test Person"])

    create.assert_called_once_with("Test Person")
    table, rows = insert.call_args.args
    assert table == "people"
    assert rows == [{"id": "1a803953a8af80ab000bfe407316", "name": "Test Person"}]
    assert capsys.readouterr().out.strip() == "1a803953a8af80ab000bfe407316"


def test_new_person_exits_with_clear_error_when_token_missing(monkeypatch, mocker):
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    insert = mocker.patch("people_sync.lifedata.insert")

    with pytest.raises(SystemExit):
        cli.main(["new-person", "--name", "Test Person"])

    insert.assert_not_called()


def test_new_person_reports_orphaned_page_and_reraises_when_insert_fails(
    mocker, monkeypatch, capsys
):
    monkeypatch.setenv("NOTION_API_TOKEN", "test-token")
    mocker.patch(
        "people_sync.notion_people.create_stub",
        return_value="1a80-3953-a8af-80ab-000bfe407316",
    )
    mocker.patch("people_sync.lifedata.insert", side_effect=RuntimeError("life insert failed"))

    with pytest.raises(RuntimeError, match="life insert failed"):
        cli.main(["new-person", "--name", "Test Person"])

    err = capsys.readouterr().err
    assert "1a80-3953-a8af-80ab-000bfe407316" in err
    assert "life-data insert failed" in err


def test_scrape_passes_endpoint_data_dir_and_approve_command_through(mocker, capsys, monkeypatch):
    monkeypatch.setenv("LIFE_HUB_URL", "https://hub.test")
    monkeypatch.setenv("LIFE_HUB_TOKEN", "token-synthetic")
    scrape = mocker.patch(
        "people_sync.scrape.run.scrape",
        return_value={"done": 1, "skipped": 0, "halted": None},
    )

    cli.main(
        [
            "scrape",
            "instagram",
            "--endpoint",
            "mini.local:9333",
            "--data-dir",
            "/tmp/instagram-profile",
            "--approve-command",
            "approve-helper 25",
        ]
    )

    scrape.assert_called_once_with(
        "instagram",
        max_n=None,
        state_path=cli.DEFAULT_STATE_PATH,
        endpoint="mini.local:9333",
        data_dir="/tmp/instagram-profile",
        approve_command="approve-helper 25",
    )
    assert json.loads(capsys.readouterr().out) == {"done": 1, "skipped": 0, "halted": None}


def test_scrape_defaults_endpoint_and_data_dir_to_none(mocker, capsys, monkeypatch):
    monkeypatch.setenv("LIFE_HUB_URL", "https://hub.test")
    monkeypatch.setenv("LIFE_HUB_TOKEN", "token-synthetic")
    scrape = mocker.patch(
        "people_sync.scrape.run.scrape",
        return_value={"done": 0, "skipped": 0, "halted": None},
    )

    cli.main(["scrape", "instagram"])

    scrape.assert_called_once_with(
        "instagram",
        max_n=None,
        state_path=cli.DEFAULT_STATE_PATH,
        endpoint=None,
        data_dir=None,
        approve_command=None,
    )


def test_photos_store_prints_r2_key_on_new_photo(mocker, tmp_path, capsys):
    file_path = tmp_path / "avatar.jpg"
    file_path.write_bytes(b"image-bytes")
    store = mocker.patch(
        "people_sync.photos.store_photo",
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
    mocker.patch("people_sync.photos.store_photo", return_value=None)

    cli.main(
        ["photos", "store", "--person", "p1", "--platform", "instagram", "--file", str(file_path)]
    )

    assert capsys.readouterr().out.strip() == "duplicate"


def test_login_passes_endpoint_data_dir_and_approve_command_through(mocker, capsys):
    login = mocker.patch(
        "people_sync.scrape.login.login",
        return_value={"platform": "instagram", "status": "logged-in", "reason": None},
    )

    cli.main(
        [
            "login",
            "instagram",
            "--endpoint",
            "127.0.0.1:9333",
            "--data-dir",
            "/tmp/profiles/ig",
            "--approve-command",
            "approve-helper 25",
        ]
    )

    login.assert_called_once_with(
        "instagram",
        endpoint="127.0.0.1:9333",
        data_dir="/tmp/profiles/ig",
        approve_command="approve-helper 25",
    )
    assert json.loads(capsys.readouterr().out)["status"] == "logged-in"


def test_login_exits_non_zero_when_the_flow_halts(mocker, capsys):
    mocker.patch(
        "people_sync.scrape.login.login",
        return_value={"platform": "facebook", "status": "halted", "reason": "challenge page"},
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["login", "facebook"])

    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "challenge page"


def test_login_reports_a_missing_credential_command_without_a_traceback(mocker):
    mocker.patch(
        "people_sync.scrape.login.login",
        side_effect=scrape_login.LoginError("PEOPLE_SYNC_CREDENTIAL_COMMAND is not set"),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["login", "venmo"])

    assert "PEOPLE_SYNC_CREDENTIAL_COMMAND" in str(exit_info.value.code)


@pytest.mark.parametrize("command", ["scrape", "login"])
def test_approve_command_help_names_its_environment_fallback(command, capsys):
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])

    help_text = capsys.readouterr().out
    assert "--approve-command" in help_text
    assert cdp.CDP_APPROVE_COMMAND_ENV in help_text


def test_scrape_refuses_to_start_without_the_r2_token(mocker, monkeypatch):
    monkeypatch.delenv("LIFE_HUB_TOKEN", raising=False)
    scrape = mocker.patch("people_sync.scrape.run.scrape")

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scrape", "instagram"])

    assert "LIFE_HUB_TOKEN" in str(exit_info.value.code)
    scrape.assert_not_called()


def test_capture_inventory_replay_and_diff_use_private_files(monkeypatch, tmp_path, capsys):
    from people_sync import photos, captures

    storage = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: storage.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", storage.__getitem__)
    source = tmp_path / "friends.json"
    source.write_text('{"friends_v2":[{"name":"Example","timestamp":1},{}]}')
    cli.main(["capture", "facebook", "--path", str(source)])
    retained = json.loads(capsys.readouterr().out)
    c = captures.validate(json.loads(storage[retained["key"]]))
    local = tmp_path / "state" / "people-sync" / "captures" / f"{c['capture_id']}.json"
    cli.main(["captures", "--verify"])
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["captures"][0]["capture_id"] == c["capture_id"]
    assert inventory["captures"][0]["verification"] == "payload-checksum"
    proposal = tmp_path / "proposal.json"
    cli.main(["replay", "--input", str(local), "--output", str(proposal)])
    a = json.loads(capsys.readouterr().out)
    assert a == json.loads(proposal.read_bytes()) and a["status"] == "ok"
    assert a["observations"][1]["status"] == "skipped"
    assert proposal.stat().st_mode & 0o777 == 0o600
    cli.main(["replay", "--input", str(local), "--compare", str(proposal)])
    same = json.loads(capsys.readouterr().out)
    assert same["comparison"]["changed"] is False
    prior = json.loads(proposal.read_text())
    prior["records"][0]["name"] = "Before"
    proposal.write_text(json.dumps(prior))
    cli.main(["replay", "--input", str(local), "--compare", str(proposal)])
    changed = json.loads(capsys.readouterr().out)
    assert changed["comparison"]["changed"] is True
    assert "Before" in changed["comparison"]["diff"] and "Example" in changed["comparison"]["diff"]


def test_capture_filters_linkedin_email_before_retention(monkeypatch, tmp_path, capsys):
    import base64
    from people_sync import photos, replay

    storage = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: storage.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", storage.__getitem__)
    source = tmp_path / "Connections.csv"
    source.write_text(
        'Notes:\n"private preamble"\n\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n'
        "Example,Person,https://linkedin.com/in/example,synthetic@example.invalid,Example Co,Role,1 Jan 2026\n"
        ",,,,,,2 Jan 2026\n\n"
    )
    cli.main(["capture", "linkedin", "--path", str(source)])
    key = json.loads(capsys.readouterr().out)["key"]
    c = json.loads(storage[key])
    file = c["payload"]["files"][0]
    raw = base64.b64decode(file["data"])
    assert b"synthetic@example.invalid" not in raw and b"private preamble" not in raw
    assert c["exclusions"] and file["filename"] == "Connections.csv" and file["format"] == "csv"
    result = replay.replay_capture(c)
    assert result["records"][0]["name"] == "Example Person"
    assert [row["ordinal"] for row in result["observations"]] == [0, 1, 2]
    assert [row["status"] for row in result["observations"]] == ["parsed", "skipped", "skipped"]


@pytest.mark.parametrize(
    "args",
    [["replay", "--input", "missing.json"], ["replay", "--input", "missing.json", "--apply"]],
)
def test_replay_errors_exit_without_tracebacks_or_apply_mode(args, capsys):
    with pytest.raises(SystemExit):
        cli.main(args)


def test_replay_malformed_json_and_inventory_tampering_are_explicit(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    directory = tmp_path / "people-sync" / "captures"
    directory.mkdir(parents=True)
    bad = directory / "bad.json"
    bad.write_text('{"secret":"synthetic-secret"')
    with pytest.raises(SystemExit):
        cli.main(["replay", "--input", str(bad)])
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err
    cli.main(["captures", "--verify"])
    result = json.loads(capsys.readouterr().out)
    assert result["captures"][0]["verification"] == "invalid"


def test_legacy_cli_uses_source_path_but_hashes_original_file_bytes(tmp_path, capsys):
    import hashlib

    path = tmp_path / "profiles" / "spotify" / "old.json"
    path.parent.mkdir(parents=True)
    original = b'{"eval": {"name":"Example","path":"/user/example"}, "captured": []}\n'
    path.write_bytes(original)
    cli.main(["replay", "--input", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result["profile"]["display_name"] == "Example"
    assert result["input_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["verification"] == "unverified"


@pytest.mark.parametrize(
    "column,contact",
    [
        ("Position", "synthetic@example.invalid"),
        ("Position", "+1 (202) 555-0148"),
        ("Position", "123 Example Street"),
        ("Position", "office 123, Example Street"),
        ("Position", "office 123:Example Street"),
        ("Position", "office 123%252C%2520Example Street"),
        ("URL", "https://linkedin.com/in/example%20?access_token=synthetic-secret"),
        ("URL", "https://linkedin.com/in/example%2520%253Faccess_token=synthetic-secret"),
        ("URL", "https://linkedin.com/in/example%09?access_token=synthetic-secret"),
        ("URL", "https://linkedin.com/in/example%20#synthetic-secret"),
    ],
)
def test_capture_cli_filters_contact_details_before_upload(
    monkeypatch, tmp_path, capsys, column, contact
):
    import base64
    from people_sync import photos, replay

    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    path = tmp_path / "Connections.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["First Name", "Last Name", column])
        writer.writerow(["Example", "Person", contact])
    original = path.read_bytes()
    cli.main(["capture", "linkedin", "--path", str(path)])
    assert len(stored) == 1
    capture = json.loads(next(iter(stored.values())))
    assert contact.encode() not in base64.b64decode(capture["payload"]["files"][0]["data"])
    assert replay.replay_capture(capture)["field_exclusions"]["entries"][0]["path"] == [column]
    assert path.read_bytes() == original
    output = capsys.readouterr()
    assert contact not in output.out + output.err
    assert "synthetic-secret" not in output.out + output.err


def test_capture_and_offline_output_preserve_good_names_and_context(monkeypatch, tmp_path, capsys):
    import base64
    from people_sync import photos

    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    source = tmp_path / "Connections.csv"
    original = (
        "First Name,Last Name,URL,Company,Position,Connected On\n"
        "Éxample,St. Sample,https://linkedin.com/in/example-sample-123,Studio 54 & 3M,Engineer II (.NET),01 Jan 2026\n"
    ).encode()
    source.write_bytes(original)
    cli.main(["capture", "linkedin", "--path", str(source)])
    key = json.loads(capsys.readouterr().out)["key"]
    capture = json.loads(stored[key])
    assert source.read_bytes() == original
    assert base64.b64decode(capture["payload"]["files"][0]["data"]) == original
    assert capture["payload"]["files"][0]["verbatim"] is True
    retained = tmp_path / "state" / "people-sync" / "captures" / f"{capture['capture_id']}.json"
    cli.main(["replay", "--input", str(retained)])
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["name"] == "Éxample St. Sample"
    assert result["records"][0]["raw"]["Company"] == "Studio 54 & 3M"
    assert result["records"][0]["raw"]["Position"] == "Engineer II (.NET)"
    assert result["records"][0]["raw"]["Connected On"] == "01 Jan 2026"
