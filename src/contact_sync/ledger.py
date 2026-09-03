"""contact_records resolution ledger - idempotent upserts."""

import json
from dataclasses import dataclass

from contact_sync import lifedata


@dataclass
class Record:
    source: str
    source_id: str
    handle: str | None
    name: str | None
    raw: dict
    follows_me: int | None = None
    i_follow: int | None = None

    @property
    def row_id(self) -> str:
        return f"{self.source}:{self.source_id}"


def _int_sql(v: int | None) -> str:
    return "NULL" if v is None else str(v)


def upsert(records: list[Record]) -> dict:
    if not records:
        return {"new": 0, "updated": 0}
    ids = ",".join(lifedata.sq(r.row_id) for r in records)
    existing = {
        row["id"] for row in lifedata.sql(f"SELECT id FROM contact_records WHERE id IN ({ids})")
    }
    now = lifedata.now_iso()
    new_rows = []
    updated = 0
    for r in records:
        if r.row_id in existing:
            lifedata.sql(
                "UPDATE contact_records SET "
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
        lifedata.insert("contact_records", new_rows)
    return {"new": len(new_rows), "updated": updated}
