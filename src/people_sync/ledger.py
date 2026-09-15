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


CHUNK = 200  # rows per statement: keeps one `life sql` argument well under the OS limit


def batch_update(table: str, key_col: str, rows: dict[str, dict]) -> None:
    """One UPDATE per chunk of rows (each `life sql` write costs seconds, so per-row
    statements dominate a large ingest). `rows` maps key -> {column: sql-literal}."""
    keys = list(rows)
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start : start + CHUNK]
        columns = list(rows[chunk[0]])
        sets = []
        for col in columns:
            whens = " ".join(f"WHEN {lifedata.sq(k)} THEN {rows[k][col]}" for k in chunk)
            sets.append(f"{col} = CASE {key_col} {whens} END")
        lifedata.sql(
            f"UPDATE {table} SET {', '.join(sets)} "
            f"WHERE {key_col} IN ({','.join(lifedata.sq(k) for k in chunk)})"
        )


def imported_from(table: str, row_id: str, capture_key=None, capture_refs=()) -> None:
    imported_from_many(table, [(row_id, capture_key, capture_refs)])


def imported_from_many(table: str, items) -> None:
    """Repair missing whole-row observation edges for every (row_id, capture_key,
    capture_refs) in one round trip; never resurrect deleted edges."""
    rows = {}
    for row_id, capture_key, capture_refs in items:
        keys = {ref["capture_key"] for ref in capture_refs}
        if capture_key:
            keys.add(capture_key)
        for key in sorted(keys):
            edge_id = capture_edge_id(key, table, row_id, "imported_from", None)
            rows[edge_id] = {
                "id": edge_id,
                "from_kind": "takeout",
                "from_ref": key,
                "to_kind": table,
                "to_ref": row_id,
                "rel": "imported_from",
                "field": None,
                "detail": None,
                "asserted_by": os.environ.get("PEOPLE_SYNC_ASSERTED_BY")
                or "script:people-sync ingest",
            }
    if not rows:
        return
    existing = set()
    ids = list(rows)
    for start in range(0, len(ids), CHUNK):
        chunk = ",".join(lifedata.sq(i) for i in ids[start : start + CHUNK])
        existing |= {
            r["id"] for r in lifedata.sql(f"SELECT id FROM provenance WHERE id IN ({chunk})")
        }
    missing = [row for edge_id, row in rows.items() if edge_id not in existing]
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
    updates: dict[str, dict] = {}
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
            updates[r.row_id] = {
                "handle": lifedata.sq(r.handle),
                "name": lifedata.sq(r.name),
                "raw": lifedata.sq(json.dumps(r.raw)),
                "follows_me": _int_sql(r.follows_me),
                "i_follow": _int_sql(r.i_follow),
                "last_seen": lifedata.sq(now),
            }
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
    if updates:
        batch_update("people_sync_records", "id", updates)
    if new_rows:
        lifedata.insert("people_sync_records", new_rows)
    held_ids = {row["record_id"] for row in held}
    imported_from_many(
        "people_sync_records",
        [
            (r.row_id, r.capture_key, r.capture_refs)
            for r in observations
            if r.row_id not in held_ids and not (r.hold_existing and r.row_id in existing)
        ],
    )
    return {"new": len(new_rows), "updated": len(updates)} | ({"held": held} if held else {})
