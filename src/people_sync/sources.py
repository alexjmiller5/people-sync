"""Privacy-filtered address-book observations, followed by pure interpretation.

Google list output is used only for resource enumeration. Apple reads presence
counts, never contact details. No source output or database path is logged.
"""

import glob
import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import structlog

from people_sync import captures
from people_sync.ledger import Record

log = structlog.get_logger(__name__)

_ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
)
_APPLE_QUERY = """
SELECT r.ZUNIQUEID AS id, r.ZFIRSTNAME AS first, r.ZLASTNAME AS last,
       r.ZMIDDLENAME AS middle, r.ZNICKNAME AS nick,
       r.ZORGANIZATION AS org, r.ZJOBTITLE AS title,
       r.ZBIRTHDAY AS birthday_epoch,
       CAST(strftime('%s', r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime') AS INTEGER)
       - CAST(strftime('%s', r.ZBIRTHDAY + 978307200, 'unixepoch') AS INTEGER)
           AS birthday_offset_seconds,
       (SELECT COUNT(*) FROM ZABCDPHONENUMBER WHERE ZOWNER = r.Z_PK) AS phone_count,
       (SELECT COUNT(*) FROM ZABCDEMAILADDRESS WHERE ZOWNER = r.Z_PK) AS email_count
FROM ZABCDRECORD r
"""

_META = {"primary": "bool", "sourcePrimary": "bool", "verified": "bool"}
_DATE = {"year": "year", "month": "month", "day": "day"}
_GOOGLE = {
    "resourceName": "google-id",
    "names": [
        {
            **dict.fromkeys(
                (
                    "displayName",
                    "displayNameLastFirst",
                    "givenName",
                    "familyName",
                    "middleName",
                    "honorificPrefix",
                    "honorificSuffix",
                    "phoneticFullName",
                    "phoneticGivenName",
                    "phoneticFamilyName",
                    "phoneticMiddleName",
                    "phoneticHonorificPrefix",
                    "phoneticHonorificSuffix",
                    "unstructuredName",
                ),
                "text",
            ),
            "metadata": _META,
        }
    ],
    "memberships": [
        {
            "metadata": _META,
            "contactGroupMembership": {
                "contactGroupResourceName": "group-id",
                "contactGroupId": "opaque-id",
            },
        }
    ],
    "organizations": [
        {
            **dict.fromkeys(
                (
                    "name",
                    "phoneticName",
                    "department",
                    "title",
                    "jobDescription",
                    "symbol",
                    "domain",
                    "location",
                    "type",
                    "formattedType",
                ),
                "text",
            ),
            "metadata": _META,
            "current": "bool",
            "startDate": _DATE,
            "endDate": _DATE,
        }
    ],
    "birthdays": [{"metadata": _META, "date": _DATE, "text": "date-text"}],
    "photos": [{"metadata": _META, "url": "url", "default": "bool"}],
}
_APPLE = {
    "id": "apple-id",
    **dict.fromkeys(("first", "last", "middle", "nick", "org", "title"), "text"),
    "birthday_epoch": "epoch",
    "birthday_offset_seconds": "offset",
    "phone_count": "count",
    "email_count": "count",
}
_FAILURES = {
    "acquisition-failed",
    "invalid-source",
    "missing resource",
    "pagination-cycle",
    "no-databases",
}
CONTACT_POLICY = (
    "contacts-allowlist-v1: only declared source identity, names, professional fields, "
    "groups, birthdays, photo URLs and presence counts; all other fields recursively "
    "excluded, including contact details, source metadata, tokens and database paths; "
    "unsafe declared values exclude the entry with invalid-source"
)


def _shape(value, schema, *, filtering=False):
    """One typed allowlist for acquisition and strict retained-input validation."""
    if isinstance(schema, dict):
        captures._require(isinstance(value, dict))
        if not filtering:
            captures._require(value.keys() <= schema.keys())
        return {
            k: _shape(v, schema[k], filtering=filtering) for k, v in value.items() if k in schema
        }
    if isinstance(schema, list):
        captures._require(isinstance(value, list))
        return [_shape(v, schema[0], filtering=filtering) for v in value]
    if value is None:
        return None
    if schema == "bool":
        captures._require(type(value) is bool)
    elif schema in {"year", "month", "day", "count", "offset"}:
        limits = {
            "year": (0, 9999),
            "month": (0, 12),
            "day": (0, 31),
            "count": (0, 1000000),
            "offset": (-86400, 86400),
        }
        low, high = limits[schema]
        captures._require(type(value) is int and low <= value <= high)
    elif schema == "epoch":
        captures._require(type(value) in (int, float) and math.isfinite(value))
        try:
            datetime(2001, 1, 1) + timedelta(seconds=value)
        except OverflowError:
            raise ValueError("invalid source timestamp") from None
    else:
        captures._require(isinstance(value, str))
        patterns = {
            "google-id": r"people/c[0-9A-Za-z_-]+",
            "group-id": r"contactGroups/[0-9A-Za-z_-]+",
            "opaque-id": r"[0-9A-Za-z_-]+",
            "apple-id": r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}:ABPerson",
        }
        if schema in patterns:
            captures._require(re.fullmatch(patterns[schema], value))
        elif schema == "date-text" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            datetime.strptime(value, "%Y-%m-%d")
        else:
            captures._check_export_value(value)
            if schema == "url":
                url = urlsplit(value)
                captures._require(url.scheme == "https" and url.hostname and not url.password)
    return value


def validate_contacts(source: str, payload: dict) -> None:
    """Reject unknown fields and unsafe values, even with a matching payload hash."""
    captures._require(source in {"google_contacts", "apple_contacts"})
    google = source == "google_contacts"
    kind = "pages" if google else "databases"
    captures._require(set(payload) == {"format", "complete", kind})
    captures._require(payload["format"] == ("google" if google else "apple") + "-contacts-v1")
    captures._require(type(payload["complete"]) is bool and isinstance(payload[kind], list))
    captures._require(bool(payload[kind]))
    complete = True
    for ordinal, page in enumerate(payload[kind]):
        keys = {"ordinal", "status", "entries"} | ({"has_next"} if google else set())
        captures._require(page["status"] in {"complete", "failed"})
        if page["status"] == "failed":
            keys.add("reason")
            captures._require(page["reason"] in _FAILURES and page["entries"] == [])
            complete = False
        captures._require(
            set(page) == keys and type(page["ordinal"]) is int and page["ordinal"] == ordinal
        )
        captures._require(isinstance(page["entries"], list))
        if google:
            captures._require(
                type(page["has_next"]) is bool
                or (page["has_next"] is None and page["status"] == "failed")
            )
        for index, entry in enumerate(page["entries"]):
            captures._require(type(entry["ordinal"]) is int and entry["ordinal"] == index)
            captures._require(entry["status"] in {"ok", "failed"})
            allowed = {"ordinal", "status"}
            if google and "resource" in entry:
                _shape(entry["resource"], "google-id")
                captures._require(entry["resource"] is not None)
                allowed.add("resource")
            if entry["status"] == "failed":
                allowed.add("reason")
                captures._require(entry["reason"] in _FAILURES)
                complete = False
            else:
                key = "person" if google else "row"
                allowed.add(key)
                _shape(entry[key], _GOOGLE if google else _APPLE)
                if google:
                    captures._require(
                        "resource" in entry
                        and entry[key].get("resourceName", entry["resource"]) == entry["resource"]
                    )
                else:
                    row = entry[key]
                    if row.get("birthday_epoch") is not None:
                        captures._require(row.get("birthday_offset_seconds") is not None)
            captures._require(set(entry) == allowed)
    if google:
        complete = complete and payload[kind][-1]["has_next"] is False
    captures._require(payload["complete"] == complete)


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("source command failed")
    return proc.stdout


def _db_paths() -> list[str]:
    return sorted(glob.glob(_ADDRESSBOOK_GLOB))


def _primary_or_first(items: list[dict], key: str) -> str | None:
    for item in items:
        if (item.get("metadata") or {}).get("primary"):
            return item.get(key)
    return items[0].get(key) if items else None


def collect_google() -> dict:
    pages, token, seen = [], None, set()
    complete = True
    while True:
        page = {"ordinal": len(pages), "status": "complete", "has_next": None, "entries": []}
        pages.append(page)
        cmd = ["gog", "contacts", "list", "-j", "--max", "1000", "--no-input", "--gmail-no-send"]
        if token:
            cmd += ["--page", token]
        try:
            data = json.loads(_run(cmd))
            captures._require(isinstance(data, dict) and isinstance(data.get("contacts"), list))
            token = data.get("nextPageToken")
            captures._require(token is None or isinstance(token, str))
            page["has_next"] = bool(token)
        except (RuntimeError, ValueError, TypeError, OSError):
            page.update(status="failed", reason="acquisition-failed")
            complete = False
            break
        for index, item in enumerate(data["contacts"]):
            entry = {"ordinal": index, "status": "failed", "reason": "missing resource"}
            page["entries"].append(entry)
            try:
                resource = item.get("resource") if isinstance(item, dict) else None
                if not resource:
                    log.warning(
                        "skipping malformed entry",
                        source="google_contacts",
                        index=index,
                        reason="missing resource",
                    )
                    complete = False
                    continue
                _shape(resource, "google-id")
                entry.update(resource=resource, reason="acquisition-failed")
                person = json.loads(
                    _run(
                        ["gog", "contacts", "raw", resource, "-j", "--no-input", "--gmail-no-send"]
                    )
                )
                entry["reason"] = "invalid-source"
                person = _shape(person, _GOOGLE, filtering=True)
                captures._require(person.get("resourceName", resource) == resource)
                entry.update(status="ok", person=person)
                entry.pop("reason")
            except (RuntimeError, ValueError, TypeError, OSError, OverflowError):
                complete = False
        if not token:
            break
        if token in seen:
            pages.append(
                {
                    "ordinal": len(pages),
                    "status": "failed",
                    "reason": "pagination-cycle",
                    "has_next": None,
                    "entries": [],
                }
            )
            complete = False
            break
        seen.add(token)
    return {"format": "google-contacts-v1", "complete": complete, "pages": pages}


def collect_apple() -> dict:
    databases, complete = [], True
    try:
        paths = _db_paths()
    except OSError:
        paths = []
    for ordinal, path in enumerate(paths):
        database = {"ordinal": ordinal, "status": "complete", "entries": []}
        databases.append(database)
        try:
            out = _run(["sqlite3", "-json", f"file:{path}?mode=ro", _APPLE_QUERY]).strip()
            rows = json.loads(out) if out else []
            captures._require(isinstance(rows, list))
        except (RuntimeError, ValueError, TypeError, OSError):
            database.update(status="failed", reason="acquisition-failed")
            complete = False
            continue
        for index, row in enumerate(rows):
            entry = {"ordinal": index, "status": "failed", "reason": "invalid-source"}
            database["entries"].append(entry)
            try:
                safe = _shape(row, _APPLE, filtering=True)
                if safe.get("birthday_epoch") is not None:
                    captures._require(safe.get("birthday_offset_seconds") is not None)
                entry.update(status="ok", row=safe)
                entry.pop("reason")
            except (ValueError, TypeError, OverflowError):
                complete = False
    if not databases:
        databases.append(
            {"ordinal": 0, "status": "failed", "reason": "no-databases", "entries": []}
        )
        complete = False
    return {"format": "apple-contacts-v1", "complete": complete, "databases": databases}


def parse_google(payload: dict) -> list[Record]:
    validate_contacts("google_contacts", payload)
    records = []
    for page in payload["pages"]:
        for entry in page["entries"]:
            if entry["status"] != "ok":
                continue
            person = entry["person"]
            names = person.get("names") or []
            records.append(
                Record(
                    source="google_contacts",
                    source_id=entry["resource"],
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


def parse_apple(payload: dict) -> list[Record]:
    validate_contacts("apple_contacts", payload)
    records = []
    for database in payload["databases"]:
        for entry in database["entries"]:
            if entry["status"] != "ok" or not entry["row"].get("id"):
                continue
            row = entry["row"]
            birthday = None
            if row.get("birthday_epoch") is not None:
                local = datetime(2001, 1, 1) + timedelta(
                    seconds=row["birthday_epoch"] + row["birthday_offset_seconds"]
                )
                birthday = (
                    local.date().isoformat() if local.year >= 1900 else local.strftime("--%m-%d")
                )
            name = " ".join(p for p in (row.get("first"), row.get("last")) if p).strip()
            records.append(
                Record(
                    source="apple_contacts",
                    source_id=row["id"],
                    handle=None,
                    name=name or row.get("nick"),
                    raw={
                        **{
                            k: row.get(k)
                            for k in ("first", "last", "middle", "nick", "org", "title")
                        },
                        "birthday": birthday,
                        "has_phone": bool(row.get("phone_count")),
                        "has_email": bool(row.get("email_count")),
                    },
                )
            )
    return records


def contacts_capture(source: str, payload: dict) -> dict:
    return captures.build_capture(
        source + "_contacts",
        "contacts",
        payload,
        completeness="privacy-filtered" if payload["complete"] else "partial",
        exclusions=[CONTACT_POLICY],
    )


def retained_records(capture: dict) -> list[Record]:
    from people_sync import replay

    key = captures.retain(capture)
    result = replay.replay_capture(capture)
    if result["status"] != "ok":
        raise ValueError("retained input could not be replayed")
    return [Record(**(row | {"capture_key": key})) for row in result["records"]]


def fetch_google() -> list[Record]:
    return retained_records(contacts_capture("google", collect_google()))


def fetch_apple() -> list[Record]:
    return retained_records(contacts_capture("apple", collect_apple()))
