import json

from people_sync.ledger import Record, upsert


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
    mocker.patch("people_sync.lifedata.sql", return_value=[])  # nothing exists
    ins = mocker.patch("people_sync.lifedata.insert")
    mocker.patch("people_sync.lifedata.now_iso", return_value="2026-09-02T00:00:00.000Z")
    out = upsert([rec()])
    row = ins.call_args.args[1][0]
    assert row["id"] == "instagram:alice123"
    assert row["status"] == "pending"
    assert row["first_seen"] == row["last_seen"] == "2026-09-02T00:00:00.000Z"
    assert json.loads(row["raw"]) == {"value": "alice123"}
    assert out == {"new": 1, "updated": 0}


def test_existing_record_updates_not_status(mocker):
    sql = mocker.patch("people_sync.lifedata.sql", return_value=[{"id": "instagram:alice123"}])
    ins = mocker.patch("people_sync.lifedata.insert")
    upsert([rec()])
    ins.assert_not_called()
    update = sql.call_args.args[0]
    assert "last_seen" in update and "status" not in update and "first_seen" not in update


def test_double_upsert_idempotent_counts(mocker):
    mocker.patch("people_sync.lifedata.insert")
    mocker.patch(
        "people_sync.lifedata.sql",
        side_effect=[[], [{"id": "instagram:alice123"}], []],
    )
    assert upsert([rec()]) == {"new": 1, "updated": 0}
    assert upsert([rec()]) == {"new": 0, "updated": 1}


def test_duplicate_ids_within_batch_collapse(mocker):
    """Sources without stable ids (facebook derives one from the name) can emit the
    same row_id twice; a batch must insert it once, not violate the UNIQUE constraint.
    The LAST occurrence wins, and the collapse is logged because it silently drops a
    person from the queue."""
    mocker.patch("people_sync.lifedata.sql", return_value=[])
    ins = mocker.patch("people_sync.lifedata.insert")
    mocker.patch("people_sync.lifedata.now_iso", return_value="2026-09-02T00:00:00.000Z")
    log = mocker.patch("people_sync.ledger.log")

    first, second = rec(), rec()
    first.name = "First Seen"
    second.name = "Second Seen"
    out = upsert([first, second, rec("bob456")])

    rows = ins.call_args.args[1]
    assert [r["id"] for r in rows] == ["instagram:alice123", "instagram:bob456"]
    # last occurrence wins
    assert rows[0]["name"] == "Second Seen"
    assert out == {"new": 2, "updated": 0}

    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "collapsed duplicate row ids"
    assert log.warning.call_args.kwargs["dropped"] == 1
    assert log.warning.call_args.kwargs["source"] == "instagram"


def test_no_warning_when_batch_has_no_duplicates(mocker):
    mocker.patch("people_sync.lifedata.sql", return_value=[])
    mocker.patch("people_sync.lifedata.insert")
    mocker.patch("people_sync.lifedata.now_iso", return_value="2026-09-02T00:00:00.000Z")
    log = mocker.patch("people_sync.ledger.log")
    upsert([rec(), rec("bob456")])
    log.warning.assert_not_called()


def _fake_estate(live_rows):
    """lifedata.sql stand-in: answers the existing-ids and live-rows reads, records writes."""
    writes = []

    def sql(query):
        if query.startswith("SELECT id, deleted_at"):
            return [{"id": r["id"]} for r in live_rows]
        if query.startswith("SELECT id, status, person_id, follows_me, i_follow"):
            return live_rows
        writes.append(query)
        return []

    return sql, writes


def test_complete_inventory_zeroes_follow_flags_of_absent_records(mocker):
    """An unfollow on both sides drops an account from the export entirely, so only
    absence can say the connection is gone; without it the unfollow list never clears."""
    live = [
        {
            "id": "instagram:alice123",
            "status": "matched",
            "person_id": "p0",
            "follows_me": 1,
            "i_follow": 1,
        },
        {
            "id": "instagram:gone1",
            "status": "matched",
            "person_id": "p1",
            "follows_me": 1,
            "i_follow": 1,
        },
        {
            "id": "instagram:gone2",
            "status": "ignored",
            "person_id": None,
            "follows_me": 0,
            "i_follow": 1,
        },
        {
            "id": "instagram:quiet",
            "status": "pending",
            "person_id": None,
            "follows_me": 0,
            "i_follow": 0,
        },
        {
            "id": "instagram:half",
            "status": "pending",
            "person_id": None,
            "follows_me": 1,
            "i_follow": None,
        },
    ]
    sql, writes = _fake_estate(live)
    mocker.patch("people_sync.lifedata.sql", side_effect=sql)
    mocker.patch("people_sync.lifedata.insert")

    out = upsert([rec()], complete=True)

    zeroing = [w for w in writes if "instagram:gone1" in w]
    assert len(zeroing) == 1
    assert "follows_me = CASE" in zeroing[0] and "i_follow = CASE" in zeroing[0]
    assert "'instagram:gone2'" in zeroing[0]
    assert "'instagram:quiet'" not in zeroing[0] and "'instagram:alice123'" not in zeroing[0]
    assert "last_seen" not in zeroing[0]
    assert "WHEN 'instagram:half' THEN 0" in zeroing[0]
    assert "WHEN 'instagram:half' THEN NULL" in zeroing[0]  # unknown i_follow stays unknown
    assert out["absent"] == {
        "count": 4,
        "matched": [{"record_id": "instagram:gone1", "person_id": "p1"}],
    }


def test_absent_record_keeps_unknown_flags_unknown(mocker):
    """Address-book and friend sources carry no follow flags; absence reports them and
    writes nothing, rather than inventing a 0."""
    live = [
        {
            "id": "instagram:alice123",
            "status": "pending",
            "person_id": None,
            "follows_me": 1,
            "i_follow": None,
        },
        {
            "id": "instagram:gone",
            "status": "matched",
            "person_id": "p1",
            "follows_me": None,
            "i_follow": None,
        },
    ]
    sql, writes = _fake_estate(live)
    mocker.patch("people_sync.lifedata.sql", side_effect=sql)
    mocker.patch("people_sync.lifedata.insert")

    out = upsert([rec()], complete=True)

    assert not [w for w in writes if "instagram:gone" in w]
    assert out["absent"]["count"] == 1


def test_partial_ingest_never_reads_absence(mocker):
    sql, writes = _fake_estate(
        [
            {
                "id": "instagram:alice123",
                "status": "pending",
                "person_id": None,
                "follows_me": 1,
                "i_follow": None,
            }
        ]
    )
    spy = mocker.patch("people_sync.lifedata.sql", side_effect=sql)
    mocker.patch("people_sync.lifedata.insert")

    out = upsert([rec()])

    assert "absent" not in out
    assert not any("SELECT id, status" in c.args[0] for c in spy.call_args_list)
