import copy
import json
import sqlite3

import pytest

from people_sync import captures, cli, lifedata, photos


def export_capture(tmp_path):
    path = tmp_path / "friends.json"
    path.write_text(json.dumps({"friends_v2": [{"name": "Example", "timestamp": 1}] * 2}))
    return captures.capture_export("facebook", path)


def profile_capture():
    return captures.build_capture(
        "spotify",
        "profile",
        {"eval": {"name": "Example"}, "captured": []},
        record_id="spotify:example",
        completeness="extracted-only",
    )


def test_cli_preview_is_read_only_and_keeps_duplicate_source_entries(tmp_path, monkeypatch, capsys):
    path = tmp_path / "capture.json"
    path.write_bytes(captures.encode(export_capture(tmp_path)))

    def forbidden(*args, **kwargs):
        raise AssertionError("preview touched a service")

    monkeypatch.setattr(lifedata, "sql", forbidden)
    monkeypatch.setattr(lifedata, "insert", forbidden)
    monkeypatch.setattr(photos, "get_object", forbidden)
    args = cli.build_parser().parse_args(["observations", "--input", str(path)])
    args.func(args)
    result = json.loads(capsys.readouterr().out)
    assert result["observations"] == 3  # one scope plus two distinct original entries
    assert result["captures"] == 1 and result["applied"] is False


def test_duplicate_names_use_capture_scope_and_ordinal_not_identity(tmp_path):
    from people_sync import observations

    capture = export_capture(tmp_path)
    rows = observations.plan(capture)
    assert len(rows) == 3
    assert [r["entry_ordinal"] for r in rows] == [None, 0, 1]
    assert len({r["id"] for r in rows}) == 3
    assert rows == observations.plan(copy.deepcopy(capture))
    assert rows[1]["entry_sha256"] == rows[2]["entry_sha256"]
    assert all(r["scope"] == "export" for r in rows)
    assert "Example" not in json.dumps(rows)
    other = copy.deepcopy(capture)
    other["capture_id"] = "a" * 32
    assert not {r["id"] for r in rows} & {r["id"] for r in observations.plan(other)}


def test_profile_scope_and_entry_do_not_copy_source_fields():
    from people_sync import observations

    rows = observations.plan(profile_capture())
    assert len(rows) == 2
    assert [r["entry_ordinal"] for r in rows] == [None, 0]
    assert all(r["kind"] == "profile" and r["scope"] == "profile" for r in rows)
    assert "eval" not in json.dumps(rows)


def test_contact_failed_page_and_failed_entry_remain_visible():
    from people_sync import observations, sources

    capture = sources.contacts_capture(
        "google",
        {
            "format": "google-contacts-v1",
            "complete": False,
            "pages": [
                {
                    "ordinal": 0,
                    "status": "complete",
                    "has_next": True,
                    "entries": [
                        {
                            "ordinal": 0,
                            "status": "ok",
                            "resource": "people/c1",
                            "person": {"resourceName": "people/c1"},
                        },
                        {
                            "ordinal": 1,
                            "status": "failed",
                            "resource": "people/c2",
                            "reason": "acquisition-failed",
                        },
                    ],
                },
                {
                    "ordinal": 1,
                    "status": "failed",
                    "has_next": None,
                    "entries": [],
                    "reason": "acquisition-failed",
                },
            ],
        },
    )
    rows = observations.plan(capture)
    assert [(r["scope"], r["entry_ordinal"]) for r in rows] == [
        ("pages:0", None),
        ("pages:0", 0),
        ("pages:0", 1),
        ("pages:1", None),
    ]
    assert all(r["completeness"] == "partial" for r in rows)


def test_list_original_ordinals_and_empty_terminal_scope(monkeypatch):
    from people_sync import observations
    from people_sync.scrape import snapshot

    kept = []
    monkeypatch.setattr(captures, "retain", lambda c: kept.append(c) or "unused")
    snapshot.retain_list(
        "facebook",
        [{"name": "Example", "href": "https://www.facebook.com/example"}],
        ordinal=2,
        scope="friends",
        entry_start=7,
    )
    rows = observations.plan(kept[0])
    assert [r["entry_ordinal"] for r in rows] == [None, 7]
    snapshot.retain_list("facebook", [], ordinal=3, scope="friends")
    assert len(observations.plan(kept[1])) == 1


@pytest.fixture
def estate(monkeypatch):
    from people_sync import observations

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    cols = ",".join(f'"{c}" TEXT' for c in observations.COLUMNS if c != "entry_ordinal")
    db.execute(
        f"CREATE TABLE people_sync_observations ({cols}, entry_ordinal INTEGER, deleted_at TEXT)"
    )
    db.execute(
        "CREATE TABLE provenance (id TEXT, from_kind TEXT, from_ref TEXT, to_kind TEXT, to_ref TEXT, rel TEXT, field TEXT, detail TEXT, asserted_by TEXT, deleted_at TEXT)"
    )

    def sql(query):
        return [dict(r) for r in db.execute(query).fetchall()]

    def insert(table, rows):
        for row in rows:
            keys = list(row)
            db.execute(
                f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [row[k] for k in keys],
            )

    monkeypatch.setattr(lifedata, "sql", sql)
    monkeypatch.setattr(lifedata, "insert", insert)
    return db


def remote(monkeypatch, capture_list):
    objects = {
        f"profiles/{c['source']}/captures/{c['capture_id']}.json": captures.encode(c)
        for c in capture_list
    }
    monkeypatch.setattr(photos, "get_object", objects.__getitem__)
    return objects


def test_apply_is_idempotent_and_repairs_interrupted_provenance(estate, monkeypatch, tmp_path):
    from people_sync import observations

    capture = export_capture(tmp_path)
    remote(monkeypatch, [capture])
    result = observations.index([capture], apply=True)
    assert result["inserted"] == 3 and result["failed"] == []
    estate.execute("DELETE FROM provenance")  # synthetic interrupted-write fixture
    result = observations.index([capture], apply=True)
    assert result["inserted"] == 0 and result["existing"] == 3
    assert estate.execute("SELECT count(*) FROM provenance").fetchone()[0] == 3
    assert estate.execute("SELECT count(*) FROM people_sync_observations").fetchone()[0] == 3


def test_tombstones_are_preserved_and_existing_content_conflicts_refused(estate, monkeypatch):
    from people_sync import observations

    capture = profile_capture()
    remote(monkeypatch, [capture])
    observations.index([capture], apply=True)
    estate.execute("UPDATE people_sync_observations SET deleted_at='2026-01-01T00:00:00.000Z'")
    estate.execute("DELETE FROM provenance")
    result = observations.index([capture], apply=True)
    assert result["held_deleted"] == 2 and result["inserted"] == 0
    assert estate.execute("SELECT count(*) FROM provenance").fetchone()[0] == 0
    estate.execute("UPDATE people_sync_observations SET deleted_at=NULL,entry_sha256='conflict'")
    with pytest.raises(ValueError, match="observation conflict"):
        observations.index([capture], apply=True)
    assert estate.execute("SELECT count(*) FROM provenance").fetchone()[0] == 0


def test_apply_verifies_envelope_bytes_and_only_indexes_verified_files(estate, monkeypatch):
    from people_sync import observations

    first, second = profile_capture(), profile_capture()
    objects = remote(monkeypatch, [first, second])
    altered = copy.deepcopy(first)
    altered["captured_at"] = "2026-01-02T00:00:00.000Z"
    objects[f"profiles/spotify/captures/{first['capture_id']}.json"] = captures.encode(altered)
    result = observations.index([first, second], apply=True)
    assert result["inserted"] == 2
    assert result["failed"] == [
        {"capture_id": first["capture_id"], "reason": "retained-file-unverified"}
    ]
    assert estate.execute("SELECT count(*) FROM people_sync_observations").fetchone()[0] == 2


def test_invalid_checksum_fails_before_writes(estate, monkeypatch):
    from people_sync import observations

    capture = profile_capture()
    capture["payload"]["eval"]["name"] = "Changed"
    with pytest.raises(ValueError):
        observations.index([capture], apply=True)
    assert estate.execute("SELECT count(*) FROM people_sync_observations").fetchone()[0] == 0


def test_cli_reports_invalid_and_legacy_files_without_exposing_contents(tmp_path, capsys):
    root = tmp_path / "captures"
    root.mkdir()
    (root / "legacy.json").write_text('{"eval":{"name":"synthetic-secret"}}')
    (root / "bad.json").write_text("synthetic-secret")
    args = cli.build_parser().parse_args(["observations", "--state-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output
    result = json.loads(output)
    assert len(result["failed_inputs"]) == 2
    assert {r["reason"] for r in result["failed_inputs"]} == {"invalid-input", "legacy-unverified"}


def test_cli_empty_directory_is_an_explicit_failure(tmp_path, capsys):
    args = cli.build_parser().parse_args(["observations", "--state-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["failed_inputs"] == [{"reason": "no-captures"}]


def test_cli_apply_and_repeat_capture_use_one_set_of_rows(estate, monkeypatch, tmp_path, capsys):
    from people_sync import observations

    c = profile_capture()
    remote(monkeypatch, [c])
    path = tmp_path / "capture.json"
    path.write_bytes(captures.encode(c))
    args = cli.build_parser().parse_args(["observations", "--input", str(path), "--apply"])
    args.func(args)
    assert json.loads(capsys.readouterr().out)["inserted"] == 2
    assert observations.index([c, c], apply=True)["existing"] == 2


def test_apple_and_whatsapp_empty_scopes_are_preserved():
    from people_sync import observations, sources, whatsapp

    apple = sources.contacts_capture(
        "apple",
        {
            "format": "apple-contacts-v1",
            "complete": False,
            "databases": [
                {"ordinal": 0, "status": "failed", "entries": [], "reason": "no-databases"}
            ],
        },
    )
    assert observations.plan(apple)[0]["scope"] == "databases:0"
    wa = captures.build_capture(
        "whatsapp",
        "contacts",
        {
            "format": whatsapp.FORMAT,
            "complete": True,
            "excluded": dict.fromkeys(whatsapp.EXCLUDED, 0),
            "counterparts": [],
        },
        completeness="privacy-filtered",
        exclusions=[whatsapp.POLICY],
    )
    assert len(observations.plan(wa)) == 1
    assert observations.plan(wa)[0]["scope"] == "counterparts"


def test_writes_are_batched_and_capture_identity_conflict_precedes_writes(
    estate, monkeypatch, tmp_path
):
    from people_sync import observations, ledger

    path = tmp_path / "friends.json"
    path.write_text(json.dumps({"friends_v2": [{"name": "Example"}] * (ledger.CHUNK + 1)}))
    c = captures.capture_export("facebook", path)
    remote(monkeypatch, [c])
    insert = lifedata.insert
    sizes = []

    def record(table, rows):
        if table == observations.TABLE:
            sizes.append(len(rows))
        insert(table, rows)

    monkeypatch.setattr(lifedata, "insert", record)
    assert observations.index([c], apply=True)["inserted"] == ledger.CHUNK + 2
    assert sizes == [ledger.CHUNK, 2]
    altered = copy.deepcopy(c)
    altered["captured_at"] = "2026-01-02T00:00:00.000Z"
    with pytest.raises(ValueError, match="capture identity conflict"):
        observations.index([c, altered], apply=True)
    assert sizes == [ledger.CHUNK, 2]
