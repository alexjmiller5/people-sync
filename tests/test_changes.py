import json

from people_sync import changes, cli


def h(tbl, col, old, new, record_id="instagram:alice", at="2026-10-01T00:00:00.000Z"):
    return {
        "tbl": tbl,
        "record_id": record_id,
        "col": col,
        "old": old,
        "new": new,
        "at": at,
        "person_id": "p1",
        "person": "Alice Example",
    }


def test_profile_and_record_columns_become_named_fields():
    out = changes.diff(
        [
            h("people_sync_profiles", "location", "Boston, MA", "New York, NY"),
            h("people_sync_records", "follows_me", "1", "0"),
        ]
    )
    assert [(c["field"], c["old"], c["new"]) for c in out] == [
        ("profile.location", "Boston, MA", "New York, NY"),
        ("follows_me", "1", "0"),
    ]
    assert out[0]["person"] == "Alice Example" and out[0]["record_id"] == "instagram:alice"


def test_raw_is_diffed_per_key_and_bookkeeping_keys_are_dropped():
    old = {"names": [{"givenName": "Alice"}], "events": [], "birthday": "--05-03"}
    new = {"names": [{"givenName": "Alicia"}], "events": [{"id": "e1"}], "birthday": "--05-04"}
    out = changes.diff([h("people_sync_records", "raw", json.dumps(old), json.dumps(new))])
    assert {c["field"]: (c["old"], c["new"]) for c in out} == {
        "raw.names": ([{"givenName": "Alice"}], [{"givenName": "Alicia"}]),
        "raw.birthday": ("--05-03", "--05-04"),
    }


def test_a_first_fill_is_enrichment_not_a_change():
    """null -> value is promote's job (fill an empty field); this report is for a
    value that was known and is now different."""
    assert changes.diff([h("people_sync_records", "handle", None, "alice.example")]) == []
    assert changes.diff([h("people_sync_profiles", "work", "", "Example Co")]) == []
    raw = changes.diff(
        [h("people_sync_records", "raw", json.dumps({"refs": None}), json.dumps({"refs": ["x"]}))]
    )
    assert raw == []


def test_unchanged_and_unparseable_raw_emit_nothing():
    same = json.dumps({"a": 1})
    assert changes.diff([h("people_sync_records", "raw", same, same)]) == []
    assert changes.diff([h("people_sync_records", "raw", "not json", same)]) == []


def test_query_reads_only_matched_people_since_the_cutoff(mocker):
    sql = mocker.patch("people_sync.somadata.sql", return_value=[])
    changes.run("2026-09-01")
    query = sql.call_args.args[0]
    assert "r.status = 'matched'" in query
    assert "h.created_at >= '2026-09-01'" in query
    assert "'scraped_at'" not in query and "'raw_r2_key'" not in query


def test_cli_prints_changes(mocker, capsys):
    run = mocker.patch("people_sync.changes.run", return_value=[{"field": "name"}])
    cli.main(["changes", "--since", "2026-09-01"])
    run.assert_called_once_with("2026-09-01")
    assert json.loads(capsys.readouterr().out) == [{"field": "name"}]
