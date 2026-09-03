"""Google Contacts and Apple Contacts ingests - turn address book entries into Records.

Real commands settled on (structure only - no personal values):

Google (gogcli), two-step because `list` only returns a curated display subset:
    gog contacts list -j --max 1000 --no-input [--page <token>]
        -> {"contacts": [{"resource": "people/c...", "name", "phone", "birthday"}, ...],
            "nextPageToken": "..."}
        Used only to enumerate resourceNames across pages - no labels/org/photo here.
    gog contacts raw <resourceName> -j --no-input
        -> full People API Person JSON: names, memberships, organizations, birthdays,
           photos, resourceName. One call per contact - this is where labels/org/photo
           actually come from.

Apple (sqlite3), per the apple-contacts skill's bulk-read query, adapted to expose
phone/email PRESENCE only - the query never SELECTs the values:
    sqlite3 -json "file:<AddressBook-v22.abcddb path>?mode=ro" "
      SELECT r.ZUNIQUEID AS id, r.ZFIRSTNAME AS first, r.ZLASTNAME AS last,
             r.ZMIDDLENAME AS middle, r.ZNICKNAME AS nick,
             r.ZORGANIZATION AS org, r.ZJOBTITLE AS title,
             CASE WHEN strftime('%Y', r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime') < '1900'
                  THEN strftime('%m-%d', r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime')
                  ELSE date(r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime') END AS birthday,
             (SELECT COUNT(*) FROM ZABCDPHONENUMBER WHERE ZOWNER = r.Z_PK) AS phone_count,
             (SELECT COUNT(*) FROM ZABCDEMAILADDRESS WHERE ZOWNER = r.Z_PK) AS email_count
      FROM ZABCDRECORD r
      WHERE r.ZFIRSTNAME IS NOT NULL OR r.ZLASTNAME IS NOT NULL OR r.ZORGANIZATION IS NOT NULL"
    run once per source DB under
    ~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb

Neither source's raw dict ever carries an actual phone number or email address -
those stay in Google/Apple and get resolved on demand later via source_id.
"""

import glob
import json
import os
import subprocess

import structlog

from contact_sync.ledger import Record

log = structlog.get_logger(__name__)

_ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
)

_APPLE_QUERY = """
SELECT r.ZUNIQUEID AS id, r.ZFIRSTNAME AS first, r.ZLASTNAME AS last,
       r.ZMIDDLENAME AS middle, r.ZNICKNAME AS nick,
       r.ZORGANIZATION AS org, r.ZJOBTITLE AS title,
       CASE WHEN strftime('%Y', r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime') < '1900'
            THEN strftime('%m-%d', r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime')
            ELSE date(r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime') END AS birthday,
       (SELECT COUNT(*) FROM ZABCDPHONENUMBER WHERE ZOWNER = r.Z_PK) AS phone_count,
       (SELECT COUNT(*) FROM ZABCDEMAILADDRESS WHERE ZOWNER = r.Z_PK) AS email_count
FROM ZABCDRECORD r
WHERE r.ZFIRSTNAME IS NOT NULL OR r.ZLASTNAME IS NOT NULL OR r.ZORGANIZATION IS NOT NULL
"""


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _db_paths() -> list[str]:
    return sorted(glob.glob(_ADDRESSBOOK_GLOB))


def _primary_or_first(items: list[dict], key: str) -> str | None:
    for item in items:
        if item.get("metadata", {}).get("primary"):
            return item.get(key)
    return items[0].get(key) if items else None


def fetch_google() -> list[Record]:
    resource_names: list[str] = []
    page_token = None
    while True:
        cmd = ["gog", "contacts", "list", "-j", "--max", "1000", "--no-input"]
        if page_token:
            cmd += ["--page", page_token]
        data = json.loads(_run(cmd))
        resource_names.extend(c["resource"] for c in data.get("contacts", []) if c.get("resource"))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    records = []
    for i, resource_name in enumerate(resource_names):
        try:
            person = json.loads(_run(["gog", "contacts", "raw", resource_name, "-j", "--no-input"]))
        except (RuntimeError, json.JSONDecodeError):
            log.warning(
                "skipping malformed entry",
                source="google_contacts",
                index=i,
                reason="raw fetch failed",
            )
            continue
        names = person.get("names") or []
        records.append(
            Record(
                source="google_contacts",
                source_id=person.get("resourceName", resource_name),
                handle=None,
                name=_primary_or_first(names, "displayName"),
                raw={
                    "names": names,
                    "labels": person.get("memberships") or [],
                    "org": person.get("organizations") or [],
                    "birthday": person.get("birthdays") or [],
                    "photo_url": _primary_or_first(person.get("photos") or [], "url"),
                },
            )
        )
    return records


def fetch_apple() -> list[Record]:
    records = []
    for db_path in _db_paths():
        try:
            out = _run(["sqlite3", "-json", f"file:{db_path}?mode=ro", _APPLE_QUERY]).strip()
            rows = json.loads(out) if out else []
        except (RuntimeError, json.JSONDecodeError):
            log.warning(
                "skipping unreadable database",
                source="apple_contacts",
                index=-1,
                reason="query failed",
            )
            continue
        for i, row in enumerate(rows):
            contact_id = row.get("id")
            if not contact_id:
                log.warning(
                    "skipping malformed entry",
                    source="apple_contacts",
                    index=i,
                    reason="missing id",
                )
                continue
            name = " ".join(p for p in (row.get("first"), row.get("last")) if p).strip()
            records.append(
                Record(
                    source="apple_contacts",
                    source_id=contact_id,
                    handle=None,
                    name=name or row.get("nick"),
                    raw={
                        "first": row.get("first"),
                        "last": row.get("last"),
                        "middle": row.get("middle"),
                        "nick": row.get("nick"),
                        "org": row.get("org"),
                        "title": row.get("title"),
                        "birthday": row.get("birthday"),
                        "has_phone": bool(row.get("phone_count")),
                        "has_email": bool(row.get("email_count")),
                    },
                )
            )
    return records
