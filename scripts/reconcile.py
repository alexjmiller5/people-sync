"""Triage-session reconcile operations - the three moves a contacts review needs.

    link    attach a pending Google Contacts record to an existing person
    merge   fold one people row into another (the survivor)
    create  promote a Google Contacts record into a brand-new person

Every operation is LOSSLESS: nothing already in life-data is ever overwritten.
A Google value that would clobber an existing one is appended to `notes`
instead (`google_last_name: ...`, `aka: ...`, `merged from ...`), a birthday
conflict is printed and skipped, and circles are only ever unioned. A wrong
call is therefore recoverable by reading the notes.

Dry run is the default: every write is printed, nothing executes until
--apply. All writes go through lifedata (the `life` CLI), soft deletes only.

    uv run python scripts/reconcile.py link <person_id> <record_id> [--rename]
    uv run python scripts/reconcile.py merge <survivor_id> <loser_id>
    uv run python scripts/reconcile.py create <record_id>
"""

import argparse
import json
import os
import sys

import httpx
import structlog

from contact_sync import lifedata, notion_people
from google_cleanup import user_groups

log = structlog.get_logger(__name__)

SOURCE = "google_contacts"

# Label/org strings become circles verbatim, with one exception Alex decided on:
# the Greek sigma does not survive round-trips through every client.
CIRCLE_ALIASES = {"ΣAE": "SAE"}

# Notion DBs holding a relation to a People page. `merge` never writes to Notion,
# so it reports the loser's pages for a manual re-point instead.
# {db: (data_source_id, relation property)}
NOTION_PEOPLE_RELATIONS = {
    "Gifts": ("0c39fffe-c8c2-43a5-af03-0a378c682c1c", "Recipient(s)"),
    "Quotes": ("18f03953-a8af-802f-8950-000b03428f8e", "Person"),
    "Trips": ("19603953-a8af-80af-8803-000be09834a6", "Travel Companions"),
    "Calendar": ("24c03953-a8af-8036-8b1b-000bb8d77b03", "Attendees"),
}

# people columns the merge scalar fold must not touch: sync bookkeeping, plus the
# three with their own union/OR/concatenation rules.
MERGE_SKIP = {
    "id",
    "created_at",
    "updated_at",
    "deleted_at",
    "circles",
    "notes",
    "notify_birthday",
}


class Ops:
    """Write sink: prints every statement, and executes it only under --apply."""

    def __init__(self, apply: bool):
        self.apply = apply

    def _echo(self, what: str) -> None:
        print(("APPLY   " if self.apply else "DRY-RUN ") + what)

    def sql(self, query: str) -> None:
        self._echo(query)
        if self.apply:
            lifedata.sql(query)

    def insert(self, table: str, rows: list[dict]) -> None:
        self._echo(f"INSERT INTO {table} {json.dumps(rows)}")
        if self.apply:
            lifedata.insert(table, rows)


# --- shared helpers -----------------------------------------------------------


def _one(query: str) -> dict | None:
    rows = lifedata.sql(query)
    return rows[0] if rows else None


def _empty(value) -> bool:
    return value is None or value == ""


def _lit(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return lifedata.sq(value)


def _set(updates: dict) -> str:
    return ", ".join(f"{col} = {_lit(val)}" for col, val in updates.items())


def _append(existing: str | None, line: str) -> str:
    return f"{existing}\n{line}" if existing else line


def _primary_entry(items: list[dict]) -> dict:
    for item in items:
        if (item.get("metadata") or {}).get("primary"):
            return item
    return items[0] if items else {}


def _birthday(date: dict) -> str | None:
    """Google date -> life-data birthday text.

    life-data stores birthdays as YYYY-MM-DD; no partial birthday exists in the
    table yet, so a year-less Google birthday takes the ISO 8601 `--MM-DD` form.
    """
    if not date.get("month") or not date.get("day"):
        return None
    month_day = f"{int(date['month']):02d}-{int(date['day']):02d}"
    if date.get("year"):
        return f"{int(date['year']):04d}-{month_day}"
    return f"--{month_day}"


def circle(value: str) -> str:
    return CIRCLE_ALIASES.get(value, value)


def union_circles(existing: list[str], incoming: list[str]) -> list[str]:
    """Existing first, deduped, order preserved. A circle is never removed."""
    out: list[str] = []
    for value in [*existing, *incoming]:
        if value and value not in out:
            out.append(value)
    return out


def parse_record(record: dict, groups: dict[str, str]) -> dict:
    """A google_contacts ledger record -> the person fields it can contribute."""
    raw = json.loads(record.get("raw") or "{}")
    name = _primary_entry(raw.get("names") or [])
    circles = [
        circle(groups[resource_name])
        for membership in raw.get("labels") or []
        for resource_name in [
            (membership.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        ]
        # system groups (myContacts) are Google's, not Alex's - user_groups() omits them
        if resource_name in groups
    ]
    circles += [circle(org["name"]) for org in raw.get("org") or [] if org.get("name")]
    return {
        "display_name": name.get("displayName") or record.get("name"),
        "first_name": name.get("givenName"),
        "middle_name": name.get("middleName"),
        "last_name": name.get("familyName"),
        "birthday": _birthday(_primary_entry(raw.get("birthday") or []).get("date") or {}),
        "circles": union_circles([], circles),
        "photo_url": raw.get("photo_url"),
    }


def account_row(person_id: str, record: dict, display_name: str | None) -> dict:
    """person_accounts row, id per match.py's <platform>:<person>:<handle-or-source_id>."""
    return {
        "id": f"{SOURCE}:{person_id}:{record.get('handle') or record['source_id']}",
        "person_id": person_id,
        "platform": SOURCE,
        "handle": record.get("handle"),
        "url": None,
        "source_id": record["source_id"],
        "display_name": display_name,
        "active": 1,
        "notes": None,
    }


def _google_record(record_id: str) -> dict:
    record = _one(f"SELECT * FROM contact_records WHERE id = {lifedata.sq(record_id)}")
    if not record:
        sys.exit(f"no contact record {record_id}")
    if record["source"] != SOURCE:
        sys.exit(f"record {record_id} is a {record['source']} record, expected {SOURCE}")
    return record


def _mark_matched(ops: Ops, record_id: str, person_id: str) -> None:
    ops.sql(
        f"UPDATE contact_records SET status = 'matched', person_id = {lifedata.sq(person_id)} "
        f"WHERE id = {lifedata.sq(record_id)}"
    )


def _link_account(ops: Ops, person_id: str, record: dict, display_name: str | None) -> None:
    existing = lifedata.sql(
        f"SELECT id FROM person_accounts WHERE person_id = {lifedata.sq(person_id)} "
        f"AND platform = {lifedata.sq(SOURCE)} "
        f"AND source_id = {lifedata.sq(record['source_id'])} "
        "AND active = 1 AND deleted_at IS NULL"
    )
    if existing:
        print(f"  account already linked ({existing[0]['id']})")
        return
    ops.insert("person_accounts", [account_row(person_id, record, display_name)])


# --- link ---------------------------------------------------------------------


def link(person_id: str, record_id: str, rename: bool, ops: Ops) -> None:
    person = _one(
        f"SELECT * FROM people WHERE id = {lifedata.sq(person_id)} AND deleted_at IS NULL"
    )
    if not person:
        sys.exit(f"no live person {person_id}")
    record = _google_record(record_id)
    if record["status"] == "matched":
        print(f"{record_id} is already matched to {record['person_id']} - nothing to do")
        return
    if record["status"] != "pending":
        sys.exit(f"record {record_id} is {record['status']}, expected pending")

    google = parse_record(record, user_groups())
    updates: dict = {}
    notes = person["notes"]
    print(f"link {record_id} -> {person_id}")

    new_name = google["display_name"]
    if rename and new_name and new_name != person["name"]:
        updates["name"] = new_name
        print(f"  name: {person['name']!r} -> {new_name!r}")
        old = person["name"]
        if old and _empty(person["nickname"]):
            updates["nickname"] = old
            print(f"  nickname: -> {old!r} (previous name preserved)")
        elif old:
            notes = _append(notes, f"aka: {old}")
            print(f"  notes: + aka: {old}")

    for field in ("first_name", "middle_name", "last_name"):
        value = google[field]
        if not value:
            continue
        if _empty(person[field]):
            updates[field] = value
            print(f"  {field}: -> {value!r}")
        elif person[field] != value:
            notes = _append(notes, f"google_{field}: {value}")
            print(f"  notes: + google_{field}: {value} (kept {person[field]!r})")

    if google["birthday"]:
        if _empty(person["birthday"]):
            updates["birthday"] = google["birthday"]
            print(f"  birthday: -> {google['birthday']}")
        elif person["birthday"] != google["birthday"]:
            print(
                f"  CONFLICT birthday: person has {person['birthday']}, "
                f"google has {google['birthday']} - keeping the person's"
            )

    existing_circles = json.loads(person["circles"] or "[]")
    merged = union_circles(existing_circles, google["circles"])
    if merged != existing_circles:
        updates["circles"] = json.dumps(merged)
        print(f"  circles: + {[c for c in merged if c not in existing_circles]}")

    if notes != person["notes"]:
        updates["notes"] = notes
    if updates:
        ops.sql(f"UPDATE people SET {_set(updates)} WHERE id = {lifedata.sq(person_id)}")
    else:
        print("  no people fields to change")
    _link_account(ops, person_id, record, google["display_name"])
    _mark_matched(ops, record_id, person_id)


# --- merge --------------------------------------------------------------------


def _notion_hits(db: str, data_source_id: str, prop: str, page_id: str, token: str) -> list[str]:
    resp = httpx.post(
        f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2026-03-11",
            "Content-Type": "application/json",
        },
        json={"filter": {"property": prop, "relation": {"contains": page_id}}, "page_size": 100},
        timeout=30,
    )
    resp.raise_for_status()
    return [page.get("url", page.get("id", "")) for page in resp.json().get("results", [])]


def _dashed(row_id: str) -> str:
    """life-data person id -> Notion page id (the row id with its dashes restored)."""
    raw = row_id.replace("-", "")
    if len(raw) != 32:
        return row_id
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def report_notion_relations(loser_id: str) -> None:
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        log.warning(
            "skipping notion relation check",
            source="notion",
            index=-1,
            reason="NOTION_API_TOKEN is not set",
        )
        return
    page_id = _dashed(loser_id)
    for db, (data_source_id, prop) in NOTION_PEOPLE_RELATIONS.items():
        for url in _notion_hits(db, data_source_id, prop, page_id, token):
            print(
                f"NOTION RELATION on {db}: {url} still points at the loser page - re-point manually"
            )


def merge(survivor_id: str, loser_id: str, ops: Ops) -> None:
    if survivor_id == loser_id:
        sys.exit("survivor and loser are the same person")
    rows = lifedata.sql(
        f"SELECT * FROM people WHERE id IN ({lifedata.sq(survivor_id)}, {lifedata.sq(loser_id)}) "
        "AND deleted_at IS NULL"
    )
    by_id = {row["id"]: row for row in rows}
    missing = [pid for pid in (survivor_id, loser_id) if pid not in by_id]
    if missing:
        sys.exit(f"no live person: {', '.join(missing)}")
    survivor, loser = by_id[survivor_id], by_id[loser_id]

    # Notion first: it is read-only reporting, and a failure here must not land
    # halfway through the life-data writes.
    report_notion_relations(loser_id)

    print(f"merge {loser_id} -> {survivor_id}")
    print(f"  before survivor: {json.dumps(survivor)}")
    print(f"  before loser:    {json.dumps(loser)}")

    updates: dict = {}
    notes = survivor["notes"]
    if loser["notes"]:
        notes = f"{notes}\n---\n{loser['notes']}" if notes else loser["notes"]
    for col, loser_value in loser.items():
        if col in MERGE_SKIP or _empty(loser_value):
            continue
        if _empty(survivor[col]):
            updates[col] = loser_value
        elif str(survivor[col]) != str(loser_value):
            notes = _append(notes, f"merged from {loser['name']} ({loser_id}): {col}={loser_value}")

    merged_circles = union_circles(
        json.loads(survivor["circles"] or "[]"), json.loads(loser["circles"] or "[]")
    )
    if merged_circles:
        updates["circles"] = json.dumps(merged_circles)
    if survivor["notify_birthday"] or loser["notify_birthday"]:
        updates["notify_birthday"] = 1
    if notes != survivor["notes"]:
        updates["notes"] = notes
    if updates:
        print(f"  after survivor:  {json.dumps(updates)}")
        ops.sql(f"UPDATE people SET {_set(updates)} WHERE id = {lifedata.sq(survivor_id)}")

    _merge_accounts(survivor_id, loser_id, ops)
    for table in ("person_photos", "person_locations", "person_employments"):
        ops.sql(
            f"UPDATE {table} SET person_id = {lifedata.sq(survivor_id)} "
            f"WHERE person_id = {lifedata.sq(loser_id)} AND deleted_at IS NULL"
        )
    for col in ("person_id", "suggested_person_id"):
        ops.sql(
            f"UPDATE contact_records SET {col} = {lifedata.sq(survivor_id)} "
            f"WHERE {col} = {lifedata.sq(loser_id)} AND deleted_at IS NULL"
        )
    _merge_relations(survivor_id, loser_id, ops)
    ops.sql(
        f"UPDATE people SET deleted_at = {lifedata.sq(lifedata.now_iso())} "
        f"WHERE id = {lifedata.sq(loser_id)}"
    )


def _soft_delete(ops: Ops, table: str, row_id: str, why: str) -> None:
    print(f"  {table} {row_id}: {why}")
    ops.sql(
        f"UPDATE {table} SET deleted_at = {lifedata.sq(lifedata.now_iso())} "
        f"WHERE id = {lifedata.sq(row_id)}"
    )


def _merge_accounts(survivor_id: str, loser_id: str, ops: Ops) -> None:
    rows = lifedata.sql(
        "SELECT id, person_id, platform, source_id FROM person_accounts "
        f"WHERE person_id IN ({lifedata.sq(survivor_id)}, {lifedata.sq(loser_id)}) "
        "AND deleted_at IS NULL"
    )
    held = {(row["platform"], row["source_id"]) for row in rows if row["person_id"] == survivor_id}
    for row in rows:
        if row["person_id"] != loser_id:
            continue
        key = (row["platform"], row["source_id"])
        if key in held:
            _soft_delete(ops, "person_accounts", row["id"], "duplicate of a survivor account")
            continue
        held.add(key)
        ops.sql(
            f"UPDATE person_accounts SET person_id = {lifedata.sq(survivor_id)} "
            f"WHERE id = {lifedata.sq(row['id'])}"
        )


def _merge_relations(survivor_id: str, loser_id: str, ops: Ops) -> None:
    ids = f"{lifedata.sq(survivor_id)}, {lifedata.sq(loser_id)}"
    rows = lifedata.sql(
        "SELECT id, person_id, related_id, relation_type FROM person_relations "
        f"WHERE (person_id IN ({ids}) OR related_id IN ({ids})) AND deleted_at IS NULL"
    )
    touches_loser = [r for r in rows if loser_id in (r["person_id"], r["related_id"])]
    held = {
        (r["person_id"], r["related_id"], r["relation_type"])
        for r in rows
        if r not in touches_loser
    }
    for row in touches_loser:
        person_id = survivor_id if row["person_id"] == loser_id else row["person_id"]
        related_id = survivor_id if row["related_id"] == loser_id else row["related_id"]
        triple = (person_id, related_id, row["relation_type"])
        if person_id == related_id:
            _soft_delete(ops, "person_relations", row["id"], "would be self-referential")
            continue
        if triple in held:
            _soft_delete(ops, "person_relations", row["id"], "duplicate of a survivor relation")
            continue
        held.add(triple)
        ops.sql(
            f"UPDATE person_relations SET person_id = {lifedata.sq(person_id)}, "
            f"related_id = {lifedata.sq(related_id)} WHERE id = {lifedata.sq(row['id'])}"
        )


# --- create -------------------------------------------------------------------


def create(record_id: str, ops: Ops) -> None:
    record = _google_record(record_id)
    if record["status"] == "matched":
        print(f"{record_id} is already matched to {record['person_id']} - nothing to do")
        return
    google = parse_record(record, user_groups())
    name = google["display_name"]
    if not name:
        sys.exit(f"record {record_id} has no display name to create a person from")

    row = {
        "name": name,
        "first_name": google["first_name"],
        "middle_name": google["middle_name"],
        "last_name": google["last_name"],
        "birthday": google["birthday"],
        "circles": json.dumps(google["circles"]) if google["circles"] else None,
    }
    if not ops.apply:
        print(f"DRY-RUN would create a Notion People stub for {name!r}, then insert people row:")
        print("DRY-RUN " + json.dumps(row))
        ops.insert("person_accounts", [account_row("<new-person-id>", record, name)])
        _mark_matched(ops, record_id, "<new-person-id>")
        return

    # The Notion page id IS the row id (dash-stripped) - the stub comes first.
    page_id = notion_people.create_stub(name)
    person_id = page_id.replace("-", "")
    try:
        ops.insert("people", [{"id": person_id, **row}])
    except Exception:
        print(
            f"orphaned notion page {page_id}: created but life-data insert failed; "
            "re-run with this id or delete the page",
            file=sys.stderr,
        )
        raise
    _link_account(ops, person_id, record, name)
    _mark_matched(ops, record_id, person_id)
    print(person_id)


# --- cli ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # the flags live on every subcommand (not the top level) so they can be typed
    # after the arguments, where a triage session naturally reaches for them
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
    flags.add_argument("--apply", action="store_true", help="actually write to life-data")
    sub = parser.add_subparsers(dest="command", required=True)

    link_p = sub.add_parser(
        "link", parents=[flags], help="attach a google contact record to an existing person"
    )
    link_p.add_argument("person_id")
    link_p.add_argument("record_id")
    link_p.add_argument("--rename", action="store_true", help="adopt the google display name")

    merge_p = sub.add_parser(
        "merge", parents=[flags], help="fold the loser person into the survivor"
    )
    merge_p.add_argument("survivor_id")
    merge_p.add_argument("loser_id")

    create_p = sub.add_parser(
        "create", parents=[flags], help="promote a google contact record to a new person"
    )
    create_p.add_argument("record_id")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    ops = Ops(args.apply and not args.dry_run)
    if args.command == "link":
        link(args.person_id, args.record_id, args.rename, ops)
    elif args.command == "merge":
        merge(args.survivor_id, args.loser_id, ops)
    else:
        create(args.record_id, ops)


if __name__ == "__main__":
    main()
