"""people_sync_records resolution ledger - idempotent upserts."""

import hashlib
import json
import os
from dataclasses import dataclass

import structlog

from people_sync import lifedata

log = structlog.get_logger(__name__)


@dataclass
class Record:
    source: str
    source_id: str
    handle: str | None
    name: str | None
    raw: dict
    follows_me: int | None = None
    i_follow: int | None = None
    capture_key: str | None = None
    capture_refs: tuple[dict, ...] = ()
    hold_existing: bool = False  # In-memory only: permitted export fields were excluded.

    @property
    def row_id(self) -> str:
        return f"{self.source}:{self.source_id}"


def _int_sql(v: int | None) -> str:
    return "NULL" if v is None else str(v)


def capture_edge_id(key: str, table: str, row_id: str, rel: str, field: str | None) -> str:
    identity = json.dumps([key, table, row_id, rel, field], ensure_ascii=False)
    return "takeout:" + hashlib.sha256(identity.encode()).hexdigest()


def imported_from(table: str, row_id: str, capture_key=None, capture_refs=()) -> None:
    """Repair missing whole-row observation edges; never resurrect deleted edges."""
    keys = {ref["capture_key"] for ref in capture_refs}
    if capture_key:
        keys.add(capture_key)
    rows = [
        {
            "id": capture_edge_id(key, table, row_id, "imported_from", None),
            "from_kind": "takeout",
            "from_ref": key,
            "to_kind": table,
            "to_ref": row_id,
            "rel": "imported_from",
            "field": None,
            "detail": None,
            "asserted_by": os.environ.get("PEOPLE_SYNC_ASSERTED_BY") or "script:people-sync ingest",
        }
        for key in sorted(keys)
    ]
    if not rows:
        return
    ids = ",".join(lifedata.sq(row["id"]) for row in rows)
    existing = {r["id"] for r in lifedata.sql(f"SELECT id FROM provenance WHERE id IN ({ids})")}
    missing = [row for row in rows if row["id"] not in existing]
    if missing:
        lifedata.insert("provenance", missing)


def upsert(records: list[Record]) -> dict:
    if not records:
        return {"new": 0, "updated": 0}
    # Sources without stable ids (facebook derives one from the display name) can emit
    # the same row_id twice; collapse to the last occurrence so the batch insert stays
    # valid. CONSEQUENCE: two DIFFERENT people who share a display name collapse into
    # ONE ledger record, so one of them silently never appears in the triage queue.
    # That is a real data loss, not just a dedupe - hence the warning below.
    deduped = list({r.row_id: r for r in records}.values())
    if len(deduped) < len(records):
        log.warning(
            "collapsed duplicate row ids",
            source=records[0].source,
            dropped=len(records) - len(deduped),
            reason="source has no stable id; row_id derived from the display name",
        )
    observations = records
    records = deduped
    ids = ",".join(lifedata.sq(r.row_id) for r in records)
    existing = {
        row["id"]: row
        for row in lifedata.sql(
            f"SELECT id, deleted_at FROM people_sync_records WHERE id IN ({ids})"
        )
    }
    now = lifedata.now_iso()
    new_rows = []
    updated = 0
    held = []
    for r in records:
        if r.row_id in existing:
            if r.hold_existing or existing[r.row_id].get("deleted_at"):
                held.append(
                    {
                        "record_id": r.row_id,
                        "capture_key": r.capture_key,
                        "reason": "permitted-field-exclusions"
                        if r.hold_existing
                        else "deleted-record",
                    }
                )
                continue
            lifedata.sql(
                "UPDATE people_sync_records SET "
                f"handle = {lifedata.sq(r.handle)}, name = {lifedata.sq(r.name)}, "
                f"raw = {lifedata.sq(json.dumps(r.raw))}, "
                f"follows_me = {_int_sql(r.follows_me)}, i_follow = {_int_sql(r.i_follow)}, "
                f"last_seen = {lifedata.sq(now)} "
                f"WHERE id = {lifedata.sq(r.row_id)}"
            )
            updated += 1
        else:
            new_rows.append(
                {
                    "id": r.row_id,
                    "source": r.source,
                    "source_id": r.source_id,
                    "handle": r.handle,
                    "name": r.name,
                    "raw": json.dumps(r.raw),
                    "follows_me": r.follows_me,
                    "i_follow": r.i_follow,
                    "status": "pending",
                    "person_id": None,
                    "suggested_person_id": None,
                    "first_seen": now,
                    "last_seen": now,
                }
            )
    if new_rows:
        lifedata.insert("people_sync_records", new_rows)
    held_ids = {row["record_id"] for row in held}
    for r in observations:
        if r.row_id not in held_ids and not (r.hold_existing and r.row_id in existing):
            imported_from("people_sync_records", r.row_id, r.capture_key, r.capture_refs)
    return {"new": len(new_rows), "updated": updated} | ({"held": held} if held else {})
