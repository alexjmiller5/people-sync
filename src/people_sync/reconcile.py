"""Triage-session reconcile operations - the three moves a contacts review needs.

    link    attach a pending Google Contacts record to an existing person
    merge   fold one people row into another (the survivor)
    create  promote a Google Contacts record into a brand-new person

Every operation is LOSSLESS: nothing already in soma is ever overwritten.
A Google value that would clobber an existing one is appended to `notes`
instead (`google_last_name: ...`, `aka: ...`, `merged from ...`), a birthday
conflict is printed and skipped, and circles are only ever unioned. A wrong
call is therefore recoverable by reading the notes.

Dry run is the default: every write is printed, nothing executes until
--apply. All writes go through somadata (the `soma` CLI), soft deletes only.

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

from people_sync import somadata, match, notion_people
from people_sync.google_cleanup import user_groups

log = structlog.get_logger(__name__)

SOURCE = "google_contacts"

# Label/org strings become circles verbatim, with one deliberate exception:
# the Greek sigma does not survive round-trips through every client.
CIRCLE_ALIASES = {"ΣAE": "SAE"}

# Notion DBs whose one-way relation to People never shows on the People page.
# `merge` never writes to Notion: it reads the loser page's own relations, queries
# these DBs (plus People's own one-way self relations) and prints every anchor
# for a manual re-point. The user supplies them as PEOPLE_SYNC_NOTION_RELATIONS,
# a JSON object of {"<label>": ["<data_source_id>", "<relation property id>"]} -
# property IDs, not names, so a rename in Notion cannot silently turn a query
# into a no-op.
RELATIONS_ENV = "PEOPLE_SYNC_NOTION_RELATIONS"


def notion_relations() -> dict[str, tuple[str, str]]:
    raw = os.environ.get(RELATIONS_ENV)
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v)
        for v in parsed.values()
    ):
        raise ValueError(f"{RELATIONS_ENV} must map labels to [data_source_id, property_id]")
    return {k: (v[0], v[1]) for k, v in parsed.items()}


# {table: the columns that make two child rows the same fact}. A loser row whose
# key an ACTIVE survivor row already holds is soft-deleted instead of re-pointed.
MERGE_DEDUPE_KEYS = {
    "person_accounts": ("platform", "source_id"),
    "person_photos": ("sha256",),
    "person_locations": ("city", "country", "start", "end"),
    "person_employments": ("company", "title", "start", "end"),
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
            somadata.sql(query)

    def insert(self, table: str, rows: list[dict]) -> None:
        self._echo(f"INSERT INTO {table} {json.dumps(rows)}")
        if self.apply:
            somadata.insert(table, rows)


# --- shared helpers -----------------------------------------------------------


def _one(query: str) -> dict | None:
    rows = somadata.sql(query)
    return rows[0] if rows else None


def _empty(value) -> bool:
    return value is None or value == ""


def _lit(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return somadata.sq(value)


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
    """Google date -> soma birthday text.

    Unknown years use `--MM-DD`, including Apple's 1604 placeholder carried
    into Google Contacts. Retained source dates remain unchanged.
    """
    if not date.get("month") or not date.get("day"):
        return None
    month_day = f"{int(date['month']):02d}-{int(date['day']):02d}"
    if date.get("year") and int(date["year"]) != 1604:
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
        # system groups (myContacts) are Google's, not the user's - user_groups() omits them
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
    }


def account_row(person_id: str, record: dict, display_name: str | None) -> dict:
    """person_accounts row, id per match.py's <platform>:<person>:<handle-or-source_id>."""
    platform = record["source"]
    try:
        raw = json.loads(record["raw"]) if record.get("raw") else {}
    except json.JSONDecodeError:
        raw = {}
    return {
        "id": f"{platform}:{person_id}:{record.get('handle') or record['source_id']}",
        "person_id": person_id,
        "platform": platform,
        "handle": record.get("handle"),
        "url": match._url_from_raw(platform, raw),
        "source_id": record["source_id"],
        "display_name": display_name,
        "active": 1,
        "notes": None,
    }


def _record(record_id: str) -> dict:
    record = _one(f"SELECT * FROM people_sync_records WHERE id = {somadata.sq(record_id)}")
    if not record:
        sys.exit(f"no contact record {record_id}")
    if record["status"] not in ("pending", "matched"):
        sys.exit(f"record {record_id} is {record['status']}, expected pending")
    if _one("SELECT name FROM sqlite_master WHERE type='table' AND name='public_accounts'"):
        if _one(
            "SELECT id FROM public_accounts "
            f"WHERE record_id = {somadata.sq(record_id)} AND deleted_at IS NULL"
        ):
            sys.exit(f"record {record_id} has a public account; resolve it explicitly first")
    return record


def _split_name(name: str) -> dict:
    parts = name.split()
    return {
        "first_name": parts[0] if parts else None,
        "middle_name": " ".join(parts[1:-1]) or None if len(parts) > 2 else None,
        "last_name": parts[-1] if len(parts) > 1 else None,
    }


def _spelling_variant(old: str, new: str) -> bool:
    """Same name, differently spelled: same first word, the rest within two edits."""
    a, b = old.casefold().split(), new.casefold().split()
    if not a or not b or a[0] != b[0] or len(a) != len(b) or len(a) < 2:
        return False
    rest_a, rest_b = "".join(a[1:]), "".join(b[1:])
    return rest_a != rest_b and _edit_distance(rest_a, rest_b) <= 2


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _rename(person: dict, new_name: str | None, updates: dict, notes):
    """Adopt a name losslessly. A corrected spelling keeps the old one as a
    `spelling:` note; a different name survives as nickname or an aka note."""
    if not new_name or new_name == person["name"]:
        return notes
    updates["name"] = new_name
    print(f"  name: {person['name']!r} -> {new_name!r}")
    old = person["name"]
    if old and _spelling_variant(old, new_name):
        notes = _append(notes, f"spelling: {old}")
        print(f"  notes: + spelling: {old} (corrected)")
        for field in ("first_name", "middle_name", "last_name"):
            if person.get(field) and person[field] not in new_name:
                updates[field] = _split_name(new_name)[field]
    elif old and _empty(person["nickname"]):
        updates["nickname"] = old
        print(f"  nickname: -> {old!r} (previous name preserved)")
    elif old:
        notes = _append(notes, f"aka: {old}")
        print(f"  notes: + aka: {old}")
    return notes


def _mark_matched(ops: Ops, record_id: str, person_id: str) -> None:
    ops.sql(
        f"UPDATE people_sync_records SET status = 'matched', person_id = {somadata.sq(person_id)} "
        f"WHERE id = {somadata.sq(record_id)}"
    )


def _link_account(ops: Ops, person_id: str, record: dict, display_name: str | None) -> None:
    existing = somadata.sql(
        f"SELECT id, source_id FROM person_accounts WHERE person_id = {somadata.sq(person_id)} "
        f"AND platform = {somadata.sq(record['source'])} AND active = 1 AND deleted_at IS NULL"
    )
    # A row with no source_id predates the backfill: it cannot be compared, and
    # inserting alongside it would give the person two google rows. Leave both alone.
    legacy = [row for row in existing if row["source_id"] is None]
    if legacy:
        log.warning(
            "manual-reconcile: legacy google account row without source_id",
            source=SOURCE,
            person_id=person_id,
            account_id=legacy[0]["id"],
        )
        print(f"  account NOT inserted: {legacy[0]['id']} has no source_id - reconcile by hand")
        return
    match = [row for row in existing if row["source_id"] == record["source_id"]]
    if match:
        print(f"  account already linked ({match[0]['id']})")
        return
    ops.insert("person_accounts", [account_row(person_id, record, display_name)])


# --- link ---------------------------------------------------------------------


def link(person_id: str, record_id: str, rename: bool, ops: Ops, name: str | None = None) -> None:
    person = _one(
        f"SELECT * FROM people WHERE id = {somadata.sq(person_id)} AND deleted_at IS NULL"
    )
    if not person:
        sys.exit(f"no live person {person_id}")
    record = _record(record_id)
    if record["status"] == "matched":
        print(f"{record_id} is already matched to {record['person_id']} - nothing to do")
        return
    updates: dict = {}
    notes = person["notes"]
    print(f"link {record_id} -> {person_id}")

    if record["source"] != SOURCE:
        # Any other source: the account row and, if asked, a lossless rename.
        # Scraped facts reach the person through `promote`, not here.
        notes = _rename(person, name, updates, notes)
        if name:
            for field, value in _split_name(name).items():
                if value and _empty(person[field]):
                    updates[field] = value
        if notes != person["notes"]:
            updates["notes"] = notes
        if updates:
            ops.sql(f"UPDATE people SET {_set(updates)} WHERE id = {somadata.sq(person_id)}")
        _link_account(ops, person_id, record, name or record.get("name"))
        _mark_matched(ops, record_id, person_id)
        return

    google = parse_record(record, user_groups())
    notes = _rename(person, name or (google["display_name"] if rename else None), updates, notes)

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
        ops.sql(f"UPDATE people SET {_set(updates)} WHERE id = {somadata.sq(person_id)}")
    else:
        print("  no people fields to change")
    _link_account(ops, person_id, record, google["display_name"])
    _mark_matched(ops, record_id, person_id)


# --- merge --------------------------------------------------------------------


def _notion(token: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    """One read-only Notion call: GET, or POST for a data source query."""
    url = f"https://api.notion.com/v1/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    if body is None:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
    else:
        resp = httpx.post(url, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _all_results(fetch) -> list[dict]:
    """Every result of a cursor-paged Notion list; `fetch(cursor_params)` gets one page."""
    results, cursor = [], None
    while True:
        page = fetch({"start_cursor": cursor} if cursor else {})
        results += page.get("results", [])
        if not page.get("has_more"):
            return results
        cursor = page["next_cursor"]


def _notion_hits(data_source_id: str, prop: str, page_id: str, token: str) -> list[dict]:
    """Pages of one data source whose relation `prop` contains the page."""
    query = {"filter": {"property": prop, "relation": {"contains": page_id}}, "page_size": 100}
    return _all_results(
        lambda cursor: _notion(token, f"data_sources/{data_source_id}/query", {**query, **cursor})
    )


def _own_relations(page_id: str, token: str) -> dict[str, list[str]]:
    """The page's own non-empty relation properties: Father, Mother and the People
    side of every two-way relation. A page object lists at most 25 entries per
    relation (`has_more`), so a longer one is re-read from its property endpoint."""
    out = {}
    for name, prop in _notion(token, f"pages/{page_id}").get("properties", {}).items():
        if prop.get("type") != "relation":
            continue
        ids = [r["id"] for r in prop.get("relation", [])]
        if prop.get("has_more"):
            ids = [
                item["relation"]["id"]
                for item in _all_results(
                    lambda cursor: _notion(
                        token, f"pages/{page_id}/properties/{prop['id']}", params=cursor
                    )
                )
            ]
        if ids:
            out[name] = ids
    return out


def _one_way_self_relations(data_source_id: str, token: str) -> dict[str, tuple[str, str]]:
    """People-to-People relations without a two-way twin (Partner): the target
    page does not show them, so they are queried like a configured database."""
    schema = _notion(token, f"data_sources/{data_source_id}")
    return {
        f"People {name}": (data_source_id, prop["id"])
        for name, prop in schema.get("properties", {}).items()
        if prop.get("type") == "relation"
        and prop["relation"].get("type") == "single_property"
        and prop["relation"].get("data_source_id", "").replace("-", "")
        == data_source_id.replace("-", "")
    }


def _dashed(row_id: str) -> str:
    """soma person id -> Notion page id (the row id with its dashes restored)."""
    raw = row_id.replace("-", "")
    if len(raw) != 32:
        return row_id
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def report_notion_relations(loser_id: str) -> None:
    """Print every Notion relation still anchored on the loser page, then one line
    saying whether that list is complete: a check that did not run, or failed
    partway, must never read as "no relations"."""
    people_ds = os.environ.get(notion_people.DATA_SOURCE_ENV)
    configured = notion_relations()
    if not people_ds and not configured:
        print("NOTION RELATIONS NOT CHECKED: Notion is not configured")
        return
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        print("NOTION RELATIONS INCOMPLETE: NOTION_API_TOKEN is not set - nothing was checked")
        return
    page_id = _dashed(loser_id)
    gaps, seen = [], set()
    if not configured:
        gaps.append(f"{RELATIONS_ENV} is not set, one-way relations from other databases")

    def show(related_id: str, line: str) -> None:
        if related_id.replace("-", "") not in seen:
            seen.add(related_id.replace("-", ""))
            print(line)

    try:
        for name, ids in _own_relations(page_id, token).items():
            for related in ids:
                show(
                    related,
                    f"NOTION RELATION on the loser page: {name} -> {related} - re-point manually",
                )
    except httpx.HTTPError as e:
        gaps.append(f"the loser page ({type(e).__name__})")
    incoming = dict(configured)
    if people_ds:
        try:
            incoming.update(_one_way_self_relations(people_ds, token))
        except httpx.HTTPError as e:
            gaps.append(f"the People schema ({type(e).__name__})")
    for db, (data_source_id, prop) in incoming.items():
        # one DB failing must not block the merge or the other DBs
        try:
            hits = _notion_hits(data_source_id, prop, page_id, token)
        except httpx.HTTPError as e:
            gaps.append(f"{db} ({type(e).__name__})")
            continue
        for hit in hits:
            url = hit.get("url", hit.get("id", ""))
            show(
                hit.get("id", url),
                f"NOTION RELATION on {db}: {url} still points at the loser page - re-point manually",
            )
    if gaps:
        print(
            f"NOTION RELATIONS INCOMPLETE: {len(seen)} found, not checked: {'; '.join(gaps)}"
            " - check the loser page by hand"
        )
    else:
        print(
            f"NOTION RELATIONS CHECKED: {len(seen)} to re-point "
            f"(the loser page and {len(incoming)} one-way relations)"
        )


def merge(survivor_id: str, loser_id: str, ops: Ops) -> None:
    if survivor_id == loser_id:
        sys.exit("survivor and loser are the same person")
    rows = somadata.sql(
        f"SELECT * FROM people WHERE id IN ({somadata.sq(survivor_id)}, {somadata.sq(loser_id)}) "
        "AND deleted_at IS NULL"
    )
    by_id = {row["id"]: row for row in rows}
    missing = [pid for pid in (survivor_id, loser_id) if pid not in by_id]
    if missing:
        sys.exit(f"no live person: {', '.join(missing)}")
    survivor, loser = by_id[survivor_id], by_id[loser_id]

    # Notion first: it is read-only reporting, and a failure here must not land
    # halfway through the soma writes.
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
        ops.sql(f"UPDATE people SET {_set(updates)} WHERE id = {somadata.sq(survivor_id)}")

    for table, keys in MERGE_DEDUPE_KEYS.items():
        _merge_child_rows(table, keys, survivor_id, loser_id, ops)
    for col in ("person_id", "suggested_person_id"):
        ops.sql(
            f"UPDATE people_sync_records SET {col} = {somadata.sq(survivor_id)} "
            f"WHERE {col} = {somadata.sq(loser_id)} AND deleted_at IS NULL"
        )
    _merge_relations(survivor_id, loser_id, ops)
    _merge_foreign_refs(survivor_id, loser_id, ops)
    ops.sql(
        f"UPDATE people SET deleted_at = {somadata.sq(somadata.now_iso())} "
        f"WHERE id = {somadata.sq(loser_id)}"
    )


def _merge_foreign_refs(survivor_id: str, loser_id: str, ops: Ops) -> None:
    """Re-point every other cataloged column that references people (quotes, gift
    recipients, split counterparties, ...). The estate names them in its catalog,
    so nothing here knows a table by name; an estate without a catalog has none."""
    own = {*MERGE_DEDUPE_KEYS, "people_sync_records", "person_relations", "people"}
    try:
        columns = somadata.sql(
            "SELECT tbl, col, type FROM catalog_properties WHERE ref_table = 'people' "
            "AND type IN ('ref', 'multi_ref') AND deleted_at IS NULL"
        )
    except Exception:
        return
    for column in columns:
        table, col = column["tbl"], column["col"]
        if table in own:
            continue
        if column["type"] == "ref":
            ops.sql(
                f"UPDATE {table} SET {col} = {somadata.sq(survivor_id)} "
                f"WHERE {col} = {somadata.sq(loser_id)} AND deleted_at IS NULL"
            )
            continue
        rows = somadata.sql(
            f"SELECT id, {col} FROM {table} WHERE {col} LIKE {somadata.sq('%' + loser_id + '%')} "
            "AND deleted_at IS NULL"
        )
        for row in rows:
            try:
                ids = json.loads(row[col] or "[]")
            except json.JSONDecodeError:
                continue
            merged = union_circles([], [survivor_id if i == loser_id else i for i in ids])
            ops.sql(
                f"UPDATE {table} SET {col} = {somadata.sq(json.dumps(merged))} "
                f"WHERE id = {somadata.sq(row['id'])}"
            )


def _soft_delete(ops: Ops, table: str, row_id: str, why: str) -> None:
    print(f"  {table} {row_id}: {why}")
    ops.sql(
        f"UPDATE {table} SET deleted_at = {somadata.sq(somadata.now_iso())} "
        f"WHERE id = {somadata.sq(row_id)}"
    )


def _merge_child_rows(
    table: str, keys: tuple[str, ...], survivor_id: str, loser_id: str, ops: Ops
) -> None:
    """Re-point the loser's rows, soft-deleting the ones the survivor already holds."""
    rows = somadata.sql(
        f"SELECT * FROM {table} "
        f"WHERE person_id IN ({somadata.sq(survivor_id)}, {somadata.sq(loser_id)}) "
        "AND deleted_at IS NULL"
    )
    # active-only baseline: a retired survivor row (a renamed handle) is history,
    # not a reason to drop the loser's live row
    held = {
        tuple(row[key] for key in keys)
        for row in rows
        if row["person_id"] == survivor_id and row.get("active", 1)
    }
    for row in rows:
        if row["person_id"] != loser_id:
            continue
        row_key = tuple(row[key] for key in keys)
        if row_key in held:
            _soft_delete(ops, table, row["id"], "duplicate of a survivor row")
            continue
        held.add(row_key)
        ops.sql(
            f"UPDATE {table} SET person_id = {somadata.sq(survivor_id)} "
            f"WHERE id = {somadata.sq(row['id'])}"
        )


def _merge_relations(survivor_id: str, loser_id: str, ops: Ops) -> None:
    ids = f"{somadata.sq(survivor_id)}, {somadata.sq(loser_id)}"
    rows = somadata.sql(
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
            f"UPDATE person_relations SET person_id = {somadata.sq(person_id)}, "
            f"related_id = {somadata.sq(related_id)} WHERE id = {somadata.sq(row['id'])}"
        )


# --- create -------------------------------------------------------------------


def create(record_id: str, ops: Ops, name: str | None = None, circles: list[str] = ()) -> None:
    """`circles`: added to whatever the record contributes (a Google label, an org),
    e.g. the "Through <person>" label for someone known via a friend."""
    record = _record(record_id)
    if record["status"] == "matched":
        print(f"{record_id} is already matched to {record['person_id']} - nothing to do")
        return
    if record["source"] == SOURCE:
        google = parse_record(record, user_groups())
        name = name or google["display_name"]
        row = {
            "name": name,
            "first_name": google["first_name"],
            "middle_name": google["middle_name"],
            "last_name": google["last_name"],
            "birthday": google["birthday"],
            "circles": json.dumps(google["circles"]) if google["circles"] else None,
        }
        if name and name != google["display_name"]:
            row.update(_split_name(name))
    else:
        row = {"name": name, **_split_name(name or "")}
    if circles:
        merged = union_circles(json.loads(row.get("circles") or "[]"), list(circles))
        row["circles"] = json.dumps(merged)
    if not name:
        sys.exit(f"record {record_id} has no display name to create a person from; pass --name")
    if not ops.apply:
        print(f"DRY-RUN would create a Notion People stub for {name!r}, then insert people row:")
        print("DRY-RUN " + json.dumps(row))
        ops.insert("person_accounts", [account_row("<new-person-id>", record, name)])
        _mark_matched(ops, record_id, "<new-person-id>")
        return

    # With Notion configured the page id IS the row id (dash-stripped), so the
    # stub comes first; otherwise the id is minted locally.
    person_id, page_id = notion_people.new_person_id(name)
    try:
        ops.insert("people", [{"id": person_id, **row}])
    except Exception:
        if page_id:
            print(
                f"orphaned notion page {page_id}: created but soma insert failed; "
                "re-run with this id or delete the page",
                file=sys.stderr,
            )
        raise
    _link_account(ops, person_id, record, name)
    _mark_matched(ops, record_id, person_id)
    print(person_id)


def ignore(record_ids: list[str], ops: Ops) -> None:
    """Mark records as not someone the user knows. They leave the triage queue and
    show up in `people-sync unfollow`. A matched record is refused: unlinking an
    account from a person is a different decision."""
    for record_id in record_ids:
        record = _one(f"SELECT * FROM people_sync_records WHERE id = {somadata.sq(record_id)}")
        if not record:
            sys.exit(f"no contact record {record_id}")
        if record["status"] == "ignored":
            print(f"{record_id} is already ignored")
            continue
        if record["status"] == "public":
            print(f"{record_id} is public - not ignored")
            continue
        if record["status"] == "matched":
            print(f"{record_id} is matched to {record['person_id']} - not ignored")
            continue
        ops.sql(
            f"UPDATE people_sync_records SET status = 'ignored' WHERE id = {somadata.sq(record_id)}"
        )


# --- cli ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # the flags live on every subcommand (not the top level) so they can be typed
    # after the arguments, where a triage session naturally reaches for them
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
    flags.add_argument("--apply", action="store_true", help="actually write to soma")
    sub = parser.add_subparsers(dest="command", required=True)

    link_p = sub.add_parser(
        "link", parents=[flags], help="attach a pending record to an existing person"
    )
    link_p.add_argument("person_id")
    link_p.add_argument("record_id")
    link_p.add_argument("--rename", action="store_true", help="adopt the google display name")
    link_p.add_argument("--name", help="adopt this full name (the old one is kept as nickname/aka)")

    merge_p = sub.add_parser(
        "merge", parents=[flags], help="fold the loser person into the survivor"
    )
    merge_p.add_argument("survivor_id")
    merge_p.add_argument("loser_id")

    create_p = sub.add_parser(
        "create", parents=[flags], help="promote a pending record to a new person"
    )
    create_p.add_argument("record_id")
    create_p.add_argument("--name", help="full name (required for non-Google records)")
    create_p.add_argument(
        "--circle",
        action="append",
        default=[],
        help="add a circle (repeatable), e.g. 'Through Ada'",
    )
    ignore_p = sub.add_parser(
        "ignore", parents=[flags], help="mark pending records as not someone the user knows"
    )
    ignore_p.add_argument("record_ids", nargs="+")
    public_p = sub.add_parser(
        "public",
        parents=[flags],
        help="keep a reviewed social account outside personal relationships",
    )
    public_p.add_argument("record_id")
    owner = public_p.add_mutually_exclusive_group(required=True)
    owner.add_argument("--organization", metavar="ID")
    owner.add_argument("--figure", metavar="ID")
    owner.add_argument("--festival", metavar="ID")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    ops = Ops(args.apply and not args.dry_run)
    if args.command == "public":
        from people_sync import public_accounts

        kind = next(k for k in public_accounts.OWNERS if getattr(args, k))
        public_accounts.link(args.record_id, kind, getattr(args, kind), ops)
    elif args.command == "link":
        link(args.person_id, args.record_id, args.rename, ops, args.name)
    elif args.command == "merge":
        merge(args.survivor_id, args.loser_id, ops)
    elif args.command == "ignore":
        ignore(args.record_ids, ops)
    else:
        create(args.record_id, ops, args.name, args.circle)


if __name__ == "__main__":
    main()
