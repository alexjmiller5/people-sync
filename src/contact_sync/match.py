"""Conservative auto-linker: connects pending ledger records to existing people.

A wrong auto-link silently corrupts the user's contact graph, while a missed
one just lands in triage - so every rule here errs toward leaving a record
pending. No fuzzy-matching: exact normalized equality and letters-equality
only.
"""

import json
import re
import unicodedata
from collections import defaultdict

import structlog

from contact_sync import lifedata

log = structlog.get_logger(__name__)

_NON_LETTER_SPACE = re.compile(r"[^a-z\s]")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _NON_LETTER_SPACE.sub("", s)
    return " ".join(s.split())


def letters(s: str) -> str:
    return normalize(s).replace(" ", "")


def _person_variants(person: dict) -> set[str]:
    variants = set()
    if person.get("name"):
        variants.add(person["name"])
    if person.get("first_name") and person.get("last_name"):
        variants.add(f"{person['first_name']} {person['last_name']}")
    if person.get("nickname") and person.get("last_name"):
        variants.add(f"{person['nickname']} {person['last_name']}")
    return variants


def _url_from_raw(source: str, raw: dict) -> str | None:
    """Only sources whose export actually carries a profile URL return one."""
    if source == "linkedin":
        return raw.get("URL") or None
    if source == "instagram":
        for key in ("followers", "following"):
            entry = raw.get(key) or {}
            items = entry.get("string_list_data") or []
            if items and items[0].get("href"):
                return items[0]["href"]
        return None
    return None


def run_match() -> dict:
    people = lifedata.sql(
        "SELECT id, name, first_name, last_name, nickname FROM people WHERE deleted_at IS NULL"
    )
    pending = lifedata.sql(
        "SELECT id, source, source_id, handle, name, raw FROM contact_records "
        "WHERE status = 'pending'"
    )

    variant_to_people: dict[str, set[str]] = defaultdict(set)
    letters_to_people: dict[str, set[str]] = defaultdict(set)
    people_by_id = {p["id"]: p for p in people}
    for p in people:
        for variant in _person_variants(p):
            norm = normalize(variant)
            if norm:
                variant_to_people[norm].add(p["id"])
            let = letters(variant)
            if let:
                letters_to_people[let].add(p["id"])

    # (person_id, source) -> list of pending records that uniquely resolved to it.
    # Scoped per source on purpose: two records from ONE source resolving to one person
    # is ambiguous (which account is really theirs?), but the same person turning up in
    # google AND apple contacts is confirmation, not ambiguity.
    person_matches: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in pending:
        if r["source"] == "instagram":
            key = letters(r["handle"]) if r.get("handle") else ""
            candidates = letters_to_people.get(key, set()) if key else set()
        else:
            key = normalize(r["name"]) if r.get("name") else ""
            candidates = variant_to_people.get(key, set()) if key else set()
        if len(candidates) == 1:
            person_matches[(next(iter(candidates)), r["source"])].append(r)

    auto = 0
    suggested = 0
    account_rows = []
    for (person_id, _source), records in person_matches.items():
        if len(records) != 1:
            # more than one pending record from the SAME source resolves to this
            # person - which one is right is ambiguous, so touch none of them
            continue
        record = records[0]
        person = people_by_id[person_id]
        word_count = len(normalize(person.get("name") or "").split())
        if word_count < 2:
            lifedata.sql(
                f"UPDATE contact_records SET suggested_person_id = {lifedata.sq(person_id)} "
                f"WHERE id = {lifedata.sq(record['id'])}"
            )
            suggested += 1
            continue

        lifedata.sql(
            "UPDATE contact_records SET status = 'matched', "
            f"person_id = {lifedata.sq(person_id)} WHERE id = {lifedata.sq(record['id'])}"
        )
        auto += 1

        try:
            raw = json.loads(record["raw"]) if record.get("raw") else {}
        except json.JSONDecodeError:
            log.warning("unreadable raw json", source=record["source"], reason="json decode")
            raw = {}
        source = record["source"]
        account_rows.append(
            {
                "id": f"{source}:{person_id}:{record.get('handle') or record['source_id']}",
                "person_id": person_id,
                "platform": source,
                "handle": record.get("handle"),
                "url": _url_from_raw(source, raw),
                "source_id": record["source_id"],
                "display_name": record.get("name"),
                "active": 1,
                "notes": None,
            }
        )

    if account_rows:
        lifedata.insert("person_accounts", account_rows)

    return {"auto": auto, "suggested": suggested, "left_pending": len(pending) - auto - suggested}
