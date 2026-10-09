import json

from people_sync import cli, venmo_payments


def side(uid, name):
    return {
        "id": uid,
        "displayName": name,
        "username": f"user{uid}",
        "type": "user",
    }


def pay(txn_id, date, sender, receiver):
    return {
        "id": txn_id,
        "date": date,
        "raw": json.dumps(
            {"type": "payment", "title": {"sender": side(*sender), "receiver": side(*receiver)}}
        ),
    }


ME = ("1", "you")


def test_a_new_counterparty_is_created_from_its_first_payment_and_mentioned_after():
    txns = [
        pay("t2", "2026-02-01", ("7", "Example Person"), ME),
        pay("t1", "2026-01-01", ME, ("7", "Example Person")),
    ]
    records, edges, skipped = venmo_payments.plan(txns, set(), set())
    assert [r.row_id for r in records] == ["venmo:7"]
    assert records[0].name == "Example Person" and records[0].handle == "user7"
    assert records[0].raw == {"displayName": "Example Person", "id": "7", "username": "user7"}
    by_txn = {e["from_ref"]: e for e in edges}
    assert by_txn["venmo:t1"]["rel"] == "imported_from"
    assert json.loads(by_txn["venmo:t1"]["detail"]) == {
        "cue": "Payment counterparty is the receiver",
        "confidence": "high",
        "direction": "sent",
        "created_row": 1,
    }
    assert by_txn["venmo:t2"]["rel"] == "mentions"
    assert json.loads(by_txn["venmo:t2"]["detail"])["direction"] == "received"
    assert by_txn["venmo:t1"]["id"] == "txn:venmo:t1:people_sync_records:venmo:7"
    assert skipped == 0


def test_a_known_counterparty_only_gains_mentions_and_reruns_add_nothing():
    txns = [pay("t1", "2026-01-01", ME, ("7", "Example Person"))]
    records, edges, _ = venmo_payments.plan(txns, {"venmo:7"}, set())
    assert records == [] and [e["rel"] for e in edges] == ["mentions"]
    again = venmo_payments.plan(txns, {"venmo:7"}, {edges[0]["id"]})
    assert again[:2] == ([], [])


def test_a_payment_without_exactly_one_self_side_is_skipped():
    txns = [
        pay("t1", "2026-01-01", ("7", "A"), ("8", "B")),
        pay("t2", "2026-01-01", ME, ("not-a-number", "B")),
        pay("t3", "2026-01-01", ME, ("9", "you")),
    ]
    assert venmo_payments.plan(txns, set(), set()) == ([], [], 3)


def test_run_reads_payments_then_writes_records_and_missing_edges(mocker):
    sql = mocker.patch(
        "people_sync.somadata.sql",
        side_effect=lambda q: (
            [pay("t1", "2026-01-01", ME, ("7", "Example Person"))] if "FROM txns_venmo" in q else []
        ),
    )
    upsert = mocker.patch("people_sync.ledger.upsert", return_value={"new": 1, "updated": 0})
    insert = mocker.patch("people_sync.somadata.insert")

    out = venmo_payments.run()

    assert "json_extract(raw, '$.type') = 'payment'" in sql.call_args_list[0].args[0]
    assert [r.row_id for r in upsert.call_args.args[0]] == ["venmo:7"]
    assert insert.call_args.args[0] == "provenance" and len(insert.call_args.args[1]) == 1
    assert out == {"payments": 1, "new_records": 1, "linked": 1, "skipped": 0}


def test_cli_ingest_venmo_payments(mocker, capsys):
    run = mocker.patch("people_sync.venmo_payments.run", return_value={"linked": 0})
    cli.main(["ingest", "venmo-payments"])
    run.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == {"linked": 0}
