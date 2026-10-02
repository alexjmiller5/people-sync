"""What changed on accounts already linked to a person: renames, new jobs and cities,
birthdays, new photos, unfollows. Read-only; life-data's `history` table already
records every cell edit that ingest and scrape make, so this only reads it back.
A first fill (empty -> value) is enrichment, promote's job, so it is not listed.
Acting on a change is a triage decision (reconcile, promote), never automatic."""

import json

from people_sync import lifedata

RECORD_COLS = ("name", "handle", "raw", "follows_me", "i_follow")
PROFILE_COLS = (
    "display_name",
    "bio",
    "location",
    "hometown",
    "education",
    "work",
    "birthday",
    "links",
    "is_private",
    "avatar_sha256",
    "profile_url",
)
# raw keys that are bookkeeping, not facts about the person: Partiful attendance
# accumulates on every event walk, and its mutual-row context is relative.
RAW_NOISE = {"events", "last_seen", "shared_events", "thumb"}
EMPTY = (None, "", [], {})


def _cols(cols) -> str:
    return ",".join(lifedata.sq(c) for c in cols)


def run(since: str | None = None) -> list[dict]:
    cutoff = f"AND h.created_at >= {lifedata.sq(since)} " if since else ""
    return diff(
        lifedata.sql(
            "SELECT h.tbl, h.row_id AS record_id, h.col, h.old, h.new, h.created_at AS at, "
            "r.person_id, p.name AS person "
            "FROM history h "
            "JOIN people_sync_records r ON r.id = h.row_id AND r.deleted_at IS NULL "
            "AND r.status = 'matched' "
            "JOIN people p ON p.id = r.person_id AND p.deleted_at IS NULL "
            f"WHERE ((h.tbl = 'people_sync_records' AND h.col IN ({_cols(RECORD_COLS)})) "
            f"OR (h.tbl = 'people_sync_profiles' AND h.col IN ({_cols(PROFILE_COLS)}))) "
            f"{cutoff}ORDER BY p.name, h.created_at"
        )
    )


def diff(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        base = {k: row[k] for k in ("person_id", "person", "record_id", "at")}
        if row["col"] == "raw":
            try:
                old, new = json.loads(row["old"] or "{}"), json.loads(row["new"] or "{}")
            except ValueError:
                continue
            if not isinstance(old, dict) or not isinstance(new, dict):
                continue
            for key in sorted((old.keys() | new.keys()) - RAW_NOISE):
                if old.get(key) not in EMPTY and old.get(key) != new.get(key):
                    out.append(
                        base | {"field": f"raw.{key}", "old": old.get(key), "new": new.get(key)}
                    )
        elif row["old"] not in EMPTY and row["old"] != row["new"]:
            prefix = "profile." if row["tbl"] == "people_sync_profiles" else ""
            out.append(base | {"field": prefix + row["col"], "old": row["old"], "new": row["new"]})
    return out
