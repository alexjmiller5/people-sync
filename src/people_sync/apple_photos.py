"""Apple Photos people as ledger records: `people-sync ingest apple_photos`.

Photos clusters faces into people the user names. Each named, unmerged Photos person
becomes one `apple_photos:<person UUID>` record (name, display name, face count, and
the Contacts card Photos linked it to, when set); `match` and triage link it to a
person like any account. The library database is opened read-only and immutable, and
only that metadata leaves it: never an asset, a face, a location or a path. "Photos of
X" stays a query of the library through the linked UUID, so a deleted photo simply
drops out and nothing per photo goes stale. A Photos person merged into another one
leaves the next complete read and shows up in the ingest's `absent` report.
"""

import os
import re
import sqlite3
from pathlib import Path

from people_sync import captures, ledger, sources

SOURCE = "apple_photos"
FORMAT = "apple-photos-people-v1"
POLICY = (
    "apple-photos-people-v1: only person UUID, name, display name, face count and the "
    "linked Contacts card id of named unmerged Photos people; no assets, faces, "
    "locations, contact-matching data or library paths"
)
QUERY = """
SELECT ZPERSONUUID AS uuid, trim(ZFULLNAME) AS name, nullif(trim(ZDISPLAYNAME), '') AS display_name,
  coalesce(ZFACECOUNT, 0) AS face_count, nullif(ZPERSONURI, '') AS contact
FROM ZPERSON
WHERE ZFULLNAME IS NOT NULL AND trim(ZFULLNAME) <> '' AND ZMERGETARGETPERSON IS NULL
  AND ZPERSONUUID IS NOT NULL
ORDER BY Z_PK
"""
_ROW = {
    "uuid": "photos-uuid",
    "name": "text",
    "display_name": "text",
    "face_count": "count",
    "contact_ref": "contact-ref",
}
_UUID = re.compile(r"[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}")


def default_library() -> str:
    return os.environ.get("PEOPLE_SYNC_PHOTOS_LIBRARY") or os.path.expanduser(
        "~/Pictures/Photos Library.photoslibrary"
    )


def _row(raw: dict) -> dict:
    contact = raw["contact"]
    return {
        "uuid": raw["uuid"],
        "name": raw["name"],
        "display_name": raw["display_name"],
        "face_count": raw["face_count"],
        "contact_ref": f"apple_contacts:{contact}"
        if contact and re.fullmatch(r"[0-9A-Fa-f-]{36}:ABPerson", contact)
        else None,
    }


def collect(library: str) -> dict:
    """Named Photos people as an allowlisted payload; an unreadable library is incomplete."""
    db = Path(library) / "database" / "Photos.sqlite"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(QUERY)]
        finally:
            con.close()
    except sqlite3.Error:
        return {"format": FORMAT, "complete": False, "people": []}
    people, complete, seen = [], True, set()
    for raw in rows:
        try:
            row = _row(raw)
            validate_row(row)
            captures._require(row["uuid"] not in seen)
        except (ValueError, TypeError, KeyError):
            complete = False
            continue
        seen.add(row["uuid"])
        people.append(row)
    return {"format": FORMAT, "complete": complete, "people": people}


def validate_row(row: dict) -> None:
    captures._require(isinstance(row, dict) and set(row) == set(_ROW))
    captures._require(isinstance(row["uuid"], str) and _UUID.fullmatch(row["uuid"]))
    captures._require(isinstance(row["name"], str) and row["name"].strip())
    sources._shape(
        {k: v for k, v in row.items() if k != "uuid"},
        {k: v for k, v in _ROW.items() if k != "uuid"},
    )


def validate_payload(payload: dict) -> None:
    """Reject unknown fields, unsafe values and duplicate people."""
    r = captures._require
    r(isinstance(payload, dict) and set(payload) == {"format", "complete", "people"})
    r(payload["format"] == FORMAT and type(payload["complete"]) is bool)
    r(isinstance(payload["people"], list))
    uuids = [p.get("uuid") if isinstance(p, dict) else None for p in payload["people"]]
    r(len(uuids) == len(set(uuids)))
    for row in payload["people"]:
        validate_row(row)


def parse_records(payload: dict) -> list[ledger.Record]:
    """Pure: the same records from the same retained payload, online or offline."""
    validate_payload(payload)
    return [
        ledger.Record(
            SOURCE,
            p["uuid"],
            None,
            p["name"],
            {
                "display_name": p["display_name"],
                "face_count": p["face_count"],
                "contact_ref": p["contact_ref"],
            },
        )
        for p in payload["people"]
    ]


def capture(payload: dict) -> dict:
    return captures.build_capture(
        SOURCE,
        "contacts",
        payload,
        completeness="privacy-filtered" if payload["complete"] else "partial",
        exclusions=[POLICY],
    )


def fetch(library: str | None = None) -> tuple[list[ledger.Record], bool]:
    payload = collect(library or default_library())
    return sources.retained_records(capture(payload)), payload["complete"]
