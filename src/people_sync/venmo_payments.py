"""Venmo payment counterparties. Every person-to-person payment in soma's
`txns_venmo` points at its counterparty's ledger record (`venmo:<user id>`)
through one `provenance` edge: `imported_from` on the payment that first
brought the counterparty in (which also creates the pending record), then
`mentions` on every later one. A counterparty is a record, not a person: one
the user does not know stays `pending` or `ignored`, and a known one reaches
the person through `person_id`. Idempotent: existing records are never
rewritten (the friends API's raw is richer) and existing edges are skipped.

The account owner's side of a payment is the one Venmo's feed labels "you";
a payment without exactly one such side is skipped, never guessed at."""

import json
import os
import re

from people_sync import somadata
from people_sync.ledger import CHUNK, Record

PAYMENTS = (
    "SELECT id, date, raw FROM txns_venmo WHERE deleted_at IS NULL "
    "AND json_extract(raw, '$.type') = 'payment' ORDER BY date, id"
)
SELF_NAME = "you"


def _asserted_by() -> str:
    return os.environ.get("PEOPLE_SYNC_ASSERTED_BY") or "script:people-sync ingest venmo-payments"


def _counterparty(raw: str):
    """(counterparty side, direction) or None."""
    try:
        title = json.loads(raw)["title"]
        sender, receiver = title["sender"], title["receiver"]
    except (ValueError, KeyError, TypeError):
        return None
    mine = [side.get("displayName") == SELF_NAME for side in (sender, receiver)]
    if mine.count(True) != 1:
        return None
    other, direction = (receiver, "sent") if mine[0] else (sender, "received")
    if not re.fullmatch(r"\d+", str(other.get("id") or "")):
        return None
    return other, direction


def plan(txns: list[dict], records: set[str], edges: set[str]):
    """Pure: (new ledger records, missing edges, skipped payment count)."""
    new: dict[str, Record] = {}
    out, skipped = [], 0
    for txn in sorted(txns, key=lambda t: (t["date"], t["id"])):
        found = _counterparty(txn["raw"])
        if found is None:
            skipped += 1
            continue
        other, direction = found
        record_id = f"venmo:{other['id']}"
        created = record_id not in records and record_id not in new
        if created:
            new[record_id] = Record(
                source="venmo",
                source_id=str(other["id"]),
                handle=other.get("username"),
                name=other.get("displayName"),
                raw={k: other.get(k) for k in ("displayName", "id", "username")},
            )
        edge_id = f"txn:venmo:{txn['id']}:people_sync_records:{record_id}"
        if edge_id in edges:
            continue
        side = "receiver" if direction == "sent" else "sender"
        detail = {"cue": f"Payment counterparty is the {side}", "confidence": "high"}
        detail["direction"] = direction
        if created:
            detail["created_row"] = 1
        out.append(
            {
                "id": edge_id,
                "from_kind": "txn",
                "from_ref": f"venmo:{txn['id']}",
                "to_kind": "people_sync_records",
                "to_ref": record_id,
                "rel": "imported_from" if created else "mentions",
                "field": None,
                "detail": json.dumps(detail),
                "asserted_by": _asserted_by(),
            }
        )
    return list(new.values()), out, skipped


def run() -> dict:
    from people_sync import ledger

    txns = somadata.sql(PAYMENTS)
    records = {
        r["id"]
        for r in somadata.sql(
            "SELECT id FROM people_sync_records WHERE source = 'venmo' AND deleted_at IS NULL"
        )
    }
    edges = {
        e["id"]
        for e in somadata.sql(
            "SELECT id FROM provenance WHERE from_kind = 'txn' AND to_kind = 'people_sync_records'"
        )
    }
    new, missing, skipped = plan(txns, records, edges)
    if new:
        ledger.upsert(new)
    for start in range(0, len(missing), CHUNK):
        somadata.insert("provenance", missing[start : start + CHUNK])
    return {
        "payments": len(txns),
        "new_records": len(new),
        "linked": len(missing),
        "skipped": skipped,
    }
