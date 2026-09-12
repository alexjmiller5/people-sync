"""Exact retained observations, using only synthetic in-memory storage."""

import json

import pytest

from people_sync import ledger, promote
from people_sync.scrape.profile import Profile, upsert_profile
from tests.test_capture_ingest import synthetic_ledger  # noqa: F401


@pytest.fixture
def estate(synthetic_ledger):  # noqa: F811 - imported shared pytest fixture
    db = synthetic_ledger
    db.execute("""CREATE TABLE people_sync_profiles (
        id TEXT PRIMARY KEY, record_id TEXT, platform TEXT, profile_url TEXT,
        platform_id TEXT, display_name TEXT, bio TEXT, location TEXT, hometown TEXT,
        education TEXT, work TEXT, birthday TEXT, links TEXT, is_private INTEGER,
        is_verified INTEGER, follower_count INTEGER, following_count INTEGER,
        mutual_count INTEGER, avatar_r2_key TEXT, avatar_sha256 TEXT, raw_r2_key TEXT,
        scraped_at TEXT, deleted_at TEXT
    )""")
    return db


def record(key, **kw):
    return ledger.Record(
        "spotify", "example", "example", "Example Person", {}, capture_key=key, **kw
    )


def test_promoted_observations_reference_their_own_capture(mocker):
    inserts = mocker.patch.object(promote.lifedata, "insert")
    mocker.patch.object(promote.lifedata, "sql", return_value=[])
    edges = []
    for ordinal in (1, 2):
        key = f"profiles/spotify/captures/observation-{ordinal}.json"
        ops = promote.plan(
            [
                {
                    "record_id": "spotify:example",
                    "person_id": "person-1",
                    "platform": "spotify",
                    "location": "Example City",
                    "raw_r2_key": key,
                }
            ],
            {"person-1": {"id": "person-1"}},
            [],
            [],
            [],
            set(),
        )
        promote.apply(ops)
        edge = inserts.call_args.args[1][0]
        assert edge["from_kind"] == "takeout"
        assert edge["from_ref"] == key
        assert "display_name" not in json.loads(edge["detail"])
        edges.append(edge)
    assert edges[0]["id"] != edges[1]["id"]


def test_duplicate_records_keep_all_keys_and_retry_repairs_edges(estate, monkeypatch):
    keys = [f"profiles/spotify/captures/observation-{i}.json" for i in range(4)]
    rows = [
        record(keys[0]),
        record(
            keys[1],
            capture_refs=tuple(
                {"capture_key": k, "scope": "following", "ordinal": i, "entry_ordinal": 0}
                for i, k in enumerate(keys[1:])
            ),
        ),
    ]
    insert = ledger.lifedata.insert

    def fail_edges(table, batch):
        if table == "provenance":
            assert estate.execute("SELECT count(*) FROM people_sync_records").fetchone()[0] == 1
            raise RuntimeError("synthetic edge failure")
        insert(table, batch)

    monkeypatch.setattr(ledger.lifedata, "insert", fail_edges)
    with pytest.raises(RuntimeError, match="synthetic edge failure"):
        ledger.upsert(rows)
    monkeypatch.setattr(ledger.lifedata, "insert", insert)
    ledger.upsert(rows)
    edges = list(estate.execute("SELECT * FROM provenance"))
    assert {e["from_ref"] for e in edges} == set(keys)
    assert all(e["field"] is None and e["detail"] is None for e in edges)
    ledger.upsert(rows)
    assert len(list(estate.execute("SELECT * FROM provenance"))) == 4


@pytest.mark.parametrize(
    "status,deleted", [("matched", None), ("ignored", None), ("matched", "old")]
)
@pytest.mark.parametrize("held", [False, True])
def test_decisions_tombstones_and_held_observations(estate, status, deleted, held):
    estate.execute(
        """INSERT INTO people_sync_records (id,name,status,person_id,deleted_at)
        VALUES ('spotify:example','Prior Name',?,?,?)""",
        (status, "person-1", deleted),
    )
    before = dict(estate.execute("SELECT * FROM people_sync_records").fetchone())
    ledger.upsert(
        [
            record("profiles/spotify/captures/first.json"),
            record("profiles/spotify/captures/last.json", hold_existing=held),
        ]
    )
    after = dict(estate.execute("SELECT * FROM people_sync_records").fetchone())
    edges = list(estate.execute("SELECT * FROM provenance"))
    if held or deleted:
        assert before == after
        assert edges == []
    else:
        assert after["status"] == status and after["person_id"] == "person-1"
        assert len(edges) == 2


def test_profile_edges_distinguish_tables_and_preserve_tombstones(estate):
    key = "profiles/spotify/captures/first.json"
    ledger.upsert([record(key)])
    p = Profile(record_id="spotify:example", platform="spotify", display_name="Example Person")
    upsert_profile(p, raw_r2_key=key)
    first = [dict(e) for e in estate.execute("SELECT * FROM provenance")]
    assert {e["to_kind"] for e in first} == {"people_sync_records", "people_sync_profiles"}
    assert len({e["id"] for e in first}) == 2
    estate.execute("UPDATE provenance SET deleted_at='old'")
    upsert_profile(p, raw_r2_key=key)
    assert all(e[0] == "old" for e in estate.execute("SELECT deleted_at FROM provenance"))
    estate.execute("UPDATE people_sync_profiles SET deleted_at='old'")
    before = dict(estate.execute("SELECT * FROM people_sync_profiles").fetchone())
    p.display_name = "Changed Name"
    upsert_profile(p, raw_r2_key="profiles/spotify/captures/next.json")
    assert dict(estate.execute("SELECT * FROM people_sync_profiles").fetchone()) == before
    assert len(list(estate.execute("SELECT * FROM provenance"))) == 2


def test_facebook_handle_write_and_retry_have_exact_evidence(estate, mocker, capsys):
    from people_sync import cli

    estate.execute("""INSERT INTO people_sync_records (id,source,name,status,person_id)
        VALUES ('facebook:example person','facebook','Example Person','matched','person-1')""")
    mocker.patch("people_sync.cli._require_file_token")
    mocker.patch("people_sync.scrape.cdp.Browser.connect", return_value=mocker.Mock())
    mocker.patch(
        "people_sync.scrape.facebook.list_friends",
        return_value=[
            {
                "name": "Example Person",
                "handle": "example.person",
                "capture_refs": [
                    {
                        "capture_key": "profiles/facebook/captures/list.json",
                        "scope": "friends",
                        "ordinal": 0,
                        "entry_ordinal": 0,
                    }
                ],
            }
        ],
    )
    insert = ledger.lifedata.insert

    def fail(table, rows):
        if table == "provenance":
            raise RuntimeError("synthetic edge failure")
        insert(table, rows)

    mocker.patch.object(ledger.lifedata, "insert", side_effect=fail)
    with pytest.raises(RuntimeError, match="synthetic edge failure"):
        cli.main(["list", "facebook", "--endpoint", "127.0.0.1:1"])
    mocker.patch.object(ledger.lifedata, "insert", side_effect=insert)
    cli.main(["list", "facebook", "--endpoint", "127.0.0.1:1"])
    edge = dict(estate.execute("SELECT * FROM provenance").fetchone())
    assert edge["from_ref"] == "profiles/facebook/captures/list.json"
    assert edge["to_ref"] == "facebook:example person" and edge["detail"] is None
    row = dict(estate.execute("SELECT * FROM people_sync_records").fetchone())
    assert row["handle"] == "example.person" and row["status"] == "matched"
    assert json.loads(capsys.readouterr().out)["assigned"] == 0


def test_profile_retry_repairs_missing_edge_with_assertion_override(estate, monkeypatch):
    p = Profile(record_id="spotify:example", platform="spotify", display_name="Example Person")
    key = "profiles/spotify/captures/observation-1.json"
    insert = ledger.lifedata.insert

    def fail(table, rows):
        if table == "provenance":
            assert (
                estate.execute("SELECT raw_r2_key FROM people_sync_profiles").fetchone()[0] == key
            )
            raise RuntimeError("synthetic edge failure")
        insert(table, rows)

    monkeypatch.setattr(ledger.lifedata, "insert", fail)
    with pytest.raises(RuntimeError, match="synthetic edge failure"):
        upsert_profile(p, raw_r2_key=key)
    monkeypatch.setattr(ledger.lifedata, "insert", insert)
    monkeypatch.setenv("PEOPLE_SYNC_ASSERTED_BY", "script:synthetic-import")
    upsert_profile(p, raw_r2_key=key)
    upsert_profile(p, raw_r2_key="profiles/spotify/captures/observation-2.json")
    edges = list(estate.execute("SELECT * FROM provenance"))
    assert len(edges) == 2
    assert all(e["asserted_by"] == "script:synthetic-import" for e in edges)
    assert {e["from_ref"] for e in edges} == {key, "profiles/spotify/captures/observation-2.json"}


def test_replay_preserves_original_observation_without_importing(mocker):
    from people_sync import captures, replay

    original = captures.build_capture(
        "spotify",
        "profile",
        {"eval": {"name": "Example Person", "path": "/user/example"}, "captured": []},
        record_id="spotify:example",
        captured_at="2026-01-02T03:04:05.006Z",
    )
    # Any attempt to import a replay, or re-stamp it as a fresh visit, fails here.
    mocker.patch.object(ledger.lifedata, "sql", side_effect=AssertionError("replay wrote"))
    mocker.patch.object(ledger.lifedata, "insert", side_effect=AssertionError("replay wrote"))
    mocker.patch.object(ledger.lifedata, "now_iso", side_effect=AssertionError("fresh visit"))
    result = replay.replay_capture(original)
    assert result["status"] == "ok"
    assert result["capture_id"] == original["capture_id"]
    assert result["captured_at"] == "2026-01-02T03:04:05.006Z"
    assert result["input_sha256"] == original["payload_sha256"]
