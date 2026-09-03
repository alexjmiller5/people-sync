import json

from contact_sync.ledger import Record, upsert


def rec(sid="alice123"):
    return Record(
        source="instagram",
        source_id=sid,
        handle=sid,
        name=None,
        raw={"value": sid},
        follows_me=1,
        i_follow=None,
    )


def test_new_record_inserted_pending(mocker):
    mocker.patch("contact_sync.lifedata.sql", return_value=[])  # nothing exists
    ins = mocker.patch("contact_sync.lifedata.insert")
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-02T00:00:00.000Z")
    out = upsert([rec()])
    row = ins.call_args.args[1][0]
    assert row["id"] == "instagram:alice123"
    assert row["status"] == "pending"
    assert row["first_seen"] == row["last_seen"] == "2026-09-02T00:00:00.000Z"
    assert json.loads(row["raw"]) == {"value": "alice123"}
    assert out == {"new": 1, "updated": 0}


def test_existing_record_updates_not_status(mocker):
    sql = mocker.patch("contact_sync.lifedata.sql", return_value=[{"id": "instagram:alice123"}])
    ins = mocker.patch("contact_sync.lifedata.insert")
    upsert([rec()])
    ins.assert_not_called()
    update = sql.call_args.args[0]
    assert "last_seen" in update and "status" not in update and "first_seen" not in update


def test_double_upsert_idempotent_counts(mocker):
    mocker.patch("contact_sync.lifedata.insert")
    mocker.patch(
        "contact_sync.lifedata.sql",
        side_effect=[[], [{"id": "instagram:alice123"}], []],
    )
    assert upsert([rec()]) == {"new": 1, "updated": 0}
    assert upsert([rec()]) == {"new": 0, "updated": 1}


def test_duplicate_ids_within_batch_collapse(mocker):
    """Sources without stable ids (facebook derives one from the name) can emit the
    same row_id twice; a batch must insert it once, not violate the UNIQUE constraint."""
    mocker.patch("contact_sync.lifedata.sql", return_value=[])
    ins = mocker.patch("contact_sync.lifedata.insert")
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-02T00:00:00.000Z")
    out = upsert([rec(), rec(), rec("bob456")])
    rows = ins.call_args.args[1]
    assert [r["id"] for r in rows] == ["instagram:alice123", "instagram:bob456"]
    assert out == {"new": 2, "updated": 0}
